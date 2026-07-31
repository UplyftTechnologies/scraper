"""Streamlit dashboard for Nykaa, Tira, and Amazon catalogue collection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from pricing_scraper.config import default_config_path, load_config
from pricing_scraper.dashboard_service import (
    collect_amazon,
    collect_nykaa,
    collect_tira,
)
from pricing_scraper.exporter import load_products_csv

st.set_page_config(
    page_title="Beauty pricing dashboard",
    page_icon=":material/monitoring:",
    layout="wide",
)


@st.cache_data(show_spinner=False, max_entries=4)
def load_saved_csv(path: str, modified_ns: int) -> pd.DataFrame:
    """Load a generated CSV; mtime invalidates the cache after a new export."""
    del modified_ns
    return pd.read_csv(path)


def saved_dataframe(config: dict[str, Any]) -> pd.DataFrame:
    """Load the most recent combined CSV when one exists."""
    csv_path = Path(
        str(
            config["output"].get("combined_csv_path")
            or "data/pricing_combined.csv"
        )
    )
    if not csv_path.exists():
        return pd.DataFrame()
    return load_saved_csv(str(csv_path.resolve()), csv_path.stat().st_mtime_ns)


def products_dataframe(products: list[dict[str, Any]]) -> pd.DataFrame:
    """Convert session product dictionaries into a display dataframe."""
    if not products:
        return pd.DataFrame()
    frame = pd.DataFrame(products)
    display_columns = [
        "site",
        "parent_product_id",
        "product_id",
        "sku",
        "categories",
        "source_categories",
        "brand",
        "product_name",
        "variant",
        "mrp",
        "selling_price",
        "discount_pct",
        "rating",
        "rating_count",
        "review_count",
        "in_stock",
        "image_url",
        "image_urls",
        "description",
        "ingredients",
        "how_to_use",
        "key_features",
        "special_features",
        "product_attributes",
        "rating_breakdown",
        "top_reviews",
        "product_url",
        "scraped_at",
    ]
    return frame[[column for column in display_columns if column in frame.columns]]


def json_collection(value: Any) -> list[Any] | dict[str, Any]:
    """Decode JSON collections loaded from CSV without breaking the dashboard."""
    if isinstance(value, (list, dict)):
        return value
    if not isinstance(value, str) or not value.strip().startswith(("[", "{")):
        return []
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return []
    return decoded if isinstance(decoded, (list, dict)) else []


def normalize_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize numeric and stock columns loaded from either memory or CSV."""
    result = frame.copy()
    for column in (
        "mrp",
        "selling_price",
        "discount_pct",
        "rating",
        "rating_count",
        "review_count",
    ):
        if column in result:
            result[column] = pd.to_numeric(result[column], errors="coerce")
    if "in_stock" in result:
        result["in_stock"] = (
            result["in_stock"]
            .astype(str)
            .str.casefold()
            .map({"true": True, "false": False, "1": True, "0": False})
        )
    for column in (
        "categories",
        "source_categories",
        "image_urls",
        "key_features",
        "special_features",
        "rating_breakdown",
        "top_reviews",
        "product_attributes",
    ):
        if column in result:
            result[column] = result[column].map(json_collection)
    return result


st.session_state.setdefault("products", [])
st.session_state.setdefault("last_run", {})
st.session_state.setdefault("dashboard_error", "")

st.title("Beauty pricing dashboard")
st.caption(
    "Collect Nykaa, Tira, and Amazon India public catalogue pricing, compare "
    "the normalized results, and download Excel or CSV."
)

config_path = default_config_path()
try:
    config = load_config(config_path)
except Exception as exc:
    st.error(f"Configuration could not be loaded: {exc}")
    st.stop()

with st.sidebar:
    st.header("Collection settings")
    st.caption(f"Config: `{config_path}`")
    retailer = st.selectbox(
        "Retailer",
        options=("Nykaa", "Tira", "Amazon"),
        help=(
            "Each run refreshes one retailer while preserving the other "
            "site's rows."
        ),
    )
    site_key = retailer.casefold()
    category_records = [
        item
        for item in config[site_key].get("categories", [])
        if (
            isinstance(item, dict)
            and item.get("name")
            and item.get("enabled", True) is not False
        )
    ]
    category_names = [str(item["name"]) for item in category_records]
    configured_page_limit = int(config[site_key].get("page_limit", 2))
    all_category = ""
    default_categories = category_names
    with st.form(f"{site_key}_collection_form"):
        selected_categories = st.multiselect(
            "Categories",
            options=category_names,
            default=default_categories,
            help=(
                "Only the approved comparison taxonomy is listed. Products "
                "found in multiple selections retain every category label."
                if site_key == "nykaa"
                else
                "Tira categories map to its direct public collections."
                if site_key == "tira"
                else
                "Amazon uses one beauty search per selected category and then "
                "opens the resulting public product pages."
            ),
        )
        page_limit = st.number_input(
            "Page safety cap",
            min_value=1,
            max_value=1000,
            value=max(1, configured_page_limit),
            step=1,
            help=(
                "Applied to each listing partition. The scraper stops earlier "
                "using Nykaa's total/offset metadata."
                if site_key == "nykaa"
                else
                "Applied to each collection. Tira stops when the JSON response "
                "reports that no next page exists."
                if site_key == "tira"
                else
                "Maximum Amazon search-result pages per selected category."
            ),
        )
        resume_run = st.checkbox(
            "Resume an interrupted run",
            value=True,
            help=(
                "Successful pages are stored under data/checkpoints. Disable "
                "this only when you intentionally want to start over."
            ),
        )
        enrich_details = st.checkbox(
            "Include full details and every SKU size",
            value=True,
            help=(
                "Makes one additional JSON API request per parent product to "
                "collect SKU-level prices, galleries, description, "
                "ingredients, usage, ratings and highlighted reviews."
                if site_key == "nykaa"
                else
                "Listing JSON supplies content and variant names. Additional "
                "JSON requests fill current prices and SKUs for non-default "
                "variants."
                if site_key == "tira"
                else
                "Amazon always opens product pages to collect prices, images, "
                "sizes, ingredients, directions and product information."
            ),
            disabled=site_key == "amazon",
        )
        submitted = st.form_submit_button(
            "Collect latest prices",
            type="primary",
            icon=":material/refresh:",
            width="stretch",
        )
    if site_key == "nykaa":
        st.caption(
            "Private Nykaa cURL headers remain on this computer and are never "
            "displayed in the dashboard."
        )
    elif site_key == "tira":
        st.caption(
            "Tira uses public JSON services used by its storefront; rendered "
            "product HTML is not scraped."
        )
    else:
        st.caption(
            "Amazon uses Playwright with a fresh browser context on retries. "
            "CAPTCHA pages are screenshotted to logs and remain pending."
        )
    st.caption(
        "Large catalogue runs can take several hours due to rate limits. "
        "Listing pages and detail/variant requests are checkpointed."
    )

if submitted:
    if not selected_categories:
        st.session_state.dashboard_error = "Select at least one category."
    elif (
        all_category
        and all_category in selected_categories
        and len(selected_categories) > 1
    ):
        st.session_state.dashboard_error = (
            f"Select {all_category} by itself, or select individual child "
            f"categories without {all_category}."
        )
    else:
        st.session_state.dashboard_error = ""
        st.session_state.last_run = {}
        with st.status(
            f"Collecting {retailer} prices...",
            expanded=True,
        ) as status:
            try:
                with st.container(horizontal=True):
                    listing_count = st.empty()
                    parent_count = st.empty()
                    sku_count = st.empty()
                listing_count.metric(
                    "Products discovered",
                    "0",
                    border=True,
                )
                parent_count.metric(
                    (
                        "Detail parents"
                        if site_key == "nykaa"
                        else "Variant prices"
                        if site_key == "tira"
                        else "Product pages"
                    ),
                    "0",
                    border=True,
                )
                sku_count.metric(
                    (
                        "Enriched SKU rows"
                        if site_key == "nykaa"
                        else "Priced SKU rows"
                        if site_key == "tira"
                        else "Amazon ASIN rows"
                    ),
                    "0",
                    border=True,
                )
                detail_progress = st.progress(
                    0,
                    text="Discovering product catalogue",
                )
                live_progress = {
                    "percent": 0,
                    "listing_products": 0,
                    "detail_parents": 0,
                    "sku_rows": 0,
                }

                def report_progress(
                    stage: str,
                    current: int,
                    total: int,
                    message: str,
                ) -> None:
                    if stage == "listing_products":
                        live_progress["listing_products"] = current
                        listing_count.metric(
                            "Products discovered",
                            f"{current:,}",
                            border=True,
                        )
                    elif stage == "details":
                        live_progress["detail_parents"] = current
                        parent_count.metric(
                            (
                                "Detail parents"
                                if site_key == "nykaa"
                                else "Variant prices"
                                if site_key == "tira"
                                else "Product pages"
                            ),
                            (
                                f"{current:,} / {total:,}"
                                if total
                                else f"{current:,}"
                            ),
                            border=True,
                        )
                    elif stage == "sku_rows":
                        live_progress["sku_rows"] = current
                        sku_count.metric(
                            (
                                "Enriched SKU rows"
                                if site_key == "nykaa"
                                else "Priced SKU rows"
                                if site_key == "tira"
                                else "Amazon ASIN rows"
                            ),
                            f"{current:,}",
                            border=True,
                        )

                    if total and stage in {"listing", "details"}:
                        live_progress["percent"] = min(
                            100,
                            int((current / total) * 100),
                        )
                    detail_progress.progress(
                        live_progress["percent"],
                        text=message,
                    )

                st.write(
                    f"Fetching {len(selected_categories)} category selection(s), "
                    f"up to {int(page_limit)} page(s) each."
                )
                collector = {
                    "nykaa": collect_nykaa,
                    "tira": collect_tira,
                    "amazon": collect_amazon,
                }[site_key]
                result = collector(
                    config,
                    selected_categories,
                    int(page_limit),
                    resume=resume_run,
                    enrich_details=enrich_details,
                    progress_callback=report_progress,
                )
                listing_count.metric(
                    "Products discovered",
                    f"{result.listing_products:,}",
                    border=True,
                )
                parent_count.metric(
                    (
                        "Detail parents"
                        if site_key == "nykaa"
                        else "Variant prices"
                        if site_key == "tira"
                        else "Product pages"
                    ),
                    f"{result.detail_parents:,}",
                    border=True,
                )
                sku_count.metric(
                    "Export rows ready",
                    f"{len(result.products):,}",
                    border=True,
                )
                detail_progress.progress(
                    100,
                    text=(
                        "Catalogue and product details processed"
                        if enrich_details
                        else "Catalogue processed"
                    ),
                )
                st.write(
                    f"Discovered {result.listing_products:,} listing products, "
                    f"processed {result.detail_parents:,} parent products, and "
                    f"exported {len(result.products):,} separate SKU rows."
                )
                if result.resumed_products:
                    st.write(
                        f"Resumed with {result.resumed_products:,} products "
                        "from the previous checkpoint."
                    )
                if result.export.database_enabled:
                    st.success(
                        "Database synchronized: "
                        f"{result.export.database_products_written:,} current "
                        "product rows and "
                        f"{result.export.database_price_points_written:,} "
                        "price-history points.",
                        icon=":material/database:",
                    )
                    if result.export.database_error:
                        st.warning(result.export.database_error)
                else:
                    st.info(
                        "Database storage is disabled. Add Supabase "
                        "credentials to `.env` to enable it.",
                        icon=":material/database_off:",
                    )
                st.session_state.products = [
                    product.to_dict()
                    for product in load_products_csv(result.export.csv_path)
                ]
                st.session_state.last_run = {
                    "site": site_key,
                    "failures": result.failures,
                    "blocks": result.blocks,
                    "requests": result.requests,
                    "excel_path": str(result.export.excel_path),
                    "csv_path": str(result.export.csv_path),
                    "completed": result.completed,
                    "next_page": result.next_page,
                    "stop_reasons": list(result.stop_reasons),
                    "products_written": result.export.products_written,
                    "listing_products": result.listing_products,
                    "detail_parents": result.detail_parents,
                }
                if result.completed:
                    status.update(
                        label="Scraping complete",
                        state="complete",
                        expanded=False,
                    )
                    st.toast(
                        "Scraping complete",
                        icon=":material/check_circle:",
                    )
                else:
                    status.update(
                        label="Scraping paused — checkpoint saved",
                        state="error",
                        expanded=True,
                    )
            except Exception as exc:
                st.session_state.dashboard_error = str(exc)
                status.update(
                    label="Collection failed",
                    state="error",
                    expanded=True,
                )

if st.session_state.dashboard_error:
    st.error(st.session_state.dashboard_error)

frame = products_dataframe(st.session_state.products)
if frame.empty:
    frame = saved_dataframe(config)
frame = normalize_frame(frame)

if frame.empty:
    st.info(
        "No saved catalogue is available yet. Choose a category and select "
        "**Collect latest prices**."
    )
    st.stop()

last_run = st.session_state.last_run
if last_run.get("completed"):
    st.success(
        "Scraping complete — "
        f"{last_run.get('products_written', len(frame)):,} SKU rows were "
        "exported to Excel and CSV, including the structured images sheet.",
        icon=":material/check_circle:",
    )
elif last_run:
    reasons = ", ".join(last_run.get("stop_reasons", ())) or "interrupted"
    resume_page = last_run.get("next_page")
    page_message = (
        f" The next run will resume at page {resume_page}."
        if resume_page
        else ""
    )
    st.warning(
        f"Scraping is incomplete ({reasons}).{page_message}",
        icon=":material/pause_circle:",
    )

with st.container(horizontal=True):
    st.metric("Products", f"{len(frame):,}", border=True)
    st.metric("Brands", f"{frame['brand'].nunique():,}", border=True)
    average_discount = frame["discount_pct"].dropna().mean()
    st.metric(
        "Average discount",
        f"{average_discount:.1f}%" if pd.notna(average_discount) else "—",
        border=True,
    )
    stock_rate = frame["in_stock"].dropna().mean() * 100
    st.metric(
        "In stock",
        f"{stock_rate:.1f}%" if pd.notna(stock_rate) else "—",
        border=True,
    )

if last_run:
    st.caption(
        f"Last run: {last_run.get('requests', 0)} requests · "
        f"{last_run.get('failures', 0)} failures · "
        f"{last_run.get('blocks', 0)} blocks"
    )

brand_options = sorted(frame["brand"].dropna().astype(str).unique())
category_options = sorted(
    {
        category
        for values in frame.get("categories", [])
        for category in (values if isinstance(values, list) else [])
    }
)
selected_categories_filter = st.multiselect(
    "Filter categories",
    options=category_options,
    default=category_options,
    key="dashboard_category_filter",
)
selected_brands = st.multiselect(
    "Filter brands",
    options=brand_options,
    default=brand_options,
    key="dashboard_brand_filter",
)
filtered = frame.copy()
if selected_categories_filter and "categories" in filtered:
    filtered = filtered[
        filtered["categories"].map(
            lambda values: bool(
                set(values if isinstance(values, list) else ())
                & set(selected_categories_filter)
            )
        )
    ]
elif category_options:
    filtered = filtered.iloc[0:0]
filtered = (
    filtered[filtered["brand"].isin(selected_brands)].copy()
    if selected_brands
    else filtered.iloc[0:0].copy()
)

chart_column, download_column = st.columns([2, 1])
with chart_column:
    with st.container(border=True):
        st.subheader("Products by brand")
        brand_counts = (
            filtered.groupby("brand", as_index=False)
            .size()
            .rename(columns={"size": "Products"})
            .sort_values("Products", ascending=False)
        )
        st.bar_chart(
            brand_counts,
            x="brand",
            y="Products",
            x_label="Brand",
            y_label="Products",
        )

with download_column:
    with st.container(border=True):
        st.subheader("Download catalogue")
        excel_path = Path(
            str(last_run.get("excel_path") or config["output"]["excel_path"])
        )
        csv_path = Path(
            str(
                last_run.get("csv_path")
                or config["output"]["combined_csv_path"]
            )
        )
        if excel_path.exists():
            st.download_button(
                "Download Excel",
                data=excel_path.read_bytes(),
                file_name=excel_path.name,
                mime=(
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet"
                ),
                icon=":material/download:",
                on_click="ignore",
                width="stretch",
            )
        if csv_path.exists():
            st.download_button(
                "Download CSV",
                data=csv_path.read_bytes(),
                file_name=csv_path.name,
                mime="text/csv",
                icon=":material/download:",
                on_click="ignore",
                width="stretch",
            )

with st.container(border=True):
    st.subheader("Product catalogue")
    st.dataframe(
        filtered,
        hide_index=True,
        height=560,
        column_config={
            "site": st.column_config.TextColumn("Site"),
            "parent_product_id": st.column_config.TextColumn("Parent ID"),
            "product_id": st.column_config.TextColumn("Product ID"),
            "sku": st.column_config.TextColumn("SKU"),
            "categories": st.column_config.ListColumn(
                "Comparison categories"
            ),
            "source_categories": st.column_config.ListColumn(
                "Retailer categories"
            ),
            "brand": st.column_config.TextColumn("Brand", pinned=True),
            "product_name": st.column_config.TextColumn(
                "Product", pinned=True, width="large"
            ),
            "variant": st.column_config.TextColumn("Variant"),
            "mrp": st.column_config.NumberColumn("MRP", format="₹ %.2f"),
            "selling_price": st.column_config.NumberColumn(
                "Selling price", format="₹ %.2f"
            ),
            "discount_pct": st.column_config.NumberColumn(
                "Discount", format="%.1f%%"
            ),
            "rating": st.column_config.ProgressColumn(
                "Rating", min_value=0, max_value=5, format="%.1f"
            ),
            "rating_count": st.column_config.NumberColumn(
                "Ratings", format="localized"
            ),
            "review_count": st.column_config.NumberColumn(
                "Reviews", format="localized"
            ),
            "in_stock": st.column_config.CheckboxColumn("In stock"),
            "image_url": st.column_config.ImageColumn("Primary image"),
            "image_urls": st.column_config.ListColumn("All images"),
            "description": st.column_config.TextColumn(
                "Description", width="large"
            ),
            "ingredients": st.column_config.TextColumn(
                "Ingredients", width="large"
            ),
            "how_to_use": st.column_config.TextColumn(
                "How to use", width="large"
            ),
            "key_features": st.column_config.ListColumn("Key features"),
            "special_features": st.column_config.ListColumn(
                "Special features"
            ),
            "product_attributes": st.column_config.JsonColumn(
                "Product information"
            ),
            "rating_breakdown": st.column_config.JsonColumn(
                "Rating breakdown"
            ),
            "top_reviews": st.column_config.JsonColumn(
                "Highlighted reviews"
            ),
            "product_url": st.column_config.LinkColumn(
                "Product page", display_text="Open"
            ),
            "scraped_at": st.column_config.TextColumn("Scraped at"),
        },
    )
