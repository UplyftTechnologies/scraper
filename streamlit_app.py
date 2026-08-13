"""Beauty pricing dashboard: collect Nykaa, Tira, and Amazon catalogue pricing.

Run it with ``python main.py`` or ``streamlit run streamlit_app.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from app_auth import require_password
from pricing_scraper.config import (
    apply_environment_overrides,
    default_config_path,
    load_config,
)
from pricing_scraper.dashboard_service import (
    AMAZON_UNAVAILABLE_MESSAGE,
    amazon_dependencies_available,
)
from pricing_scraper.background import (
    ACTIVE_STATES,
    RunRequest,
    active_status,
    latest_status,
    read_log,
    read_status,
    request_stop,
    start_run,
)

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
        "gtin",
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
        "key_ingredients",
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
        "key_ingredients",
        "key_features",
        "special_features",
        "rating_breakdown",
        "top_reviews",
        "product_attributes",
    ):
        if column in result:
            result[column] = result[column].map(json_collection)
    return result


require_password()

st.session_state.setdefault("products", [])
st.session_state.setdefault("last_run", {})
st.session_state.setdefault("dashboard_error", "")
st.session_state.setdefault("run_id", "")
st.session_state.setdefault("manual_result", None)

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
apply_environment_overrides(config)

amazon_available = amazon_dependencies_available()

with st.sidebar:
    st.header("Collection settings")
    brand_filter = list(config.get("brands") or ())
    if brand_filter:
        # The full list runs to a couple of hundred names, which pushed every
        # control below it off the screen. Keep the count visible and the
        # names one click away.
        with st.expander(f"Brands: {len(brand_filter)} (SCRAPE_BRANDS)"):
            st.caption(", ".join(brand_filter))
    else:
        st.caption("Brands: all (set SCRAPE_BRANDS in .env to narrow)")
    retailer = st.selectbox(
        "Retailer",
        options=("Nykaa", "Tira", "Amazon") if amazon_available else ("Nykaa", "Tira"),
        help=(
            "Each run refreshes one retailer while preserving the other "
            "site's rows."
        ),
    )
    if not amazon_available:
        st.caption(AMAZON_UNAVAILABLE_MESSAGE)
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
    with st.form(f"{site_key}_collection_form"):
        selected_categories = st.multiselect(
            "Categories",
            options=category_names,
            default=category_names,
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
        skip_current = st.checkbox(
            "Skip products already up to date",
            value=True,
            help=(
                "Compares each product against the stored catalogue (Supabase "
                "when configured, otherwise the saved CSV) and requests it "
                "only when it is new, when the listing shows it changed, when "
                "its stored record is missing content, or when that record is "
                "older than the refresh window. Uncheck to re-request "
                "everything."
            ),
        )
        submitted = st.form_submit_button(
            "Collect latest prices",
            type="primary",
            icon=":material/refresh:",
            width="stretch",
        )
    with st.form(f"{site_key}_gtin_form"):
        st.caption("Barcodes only — fills empty `gtin`, changes nothing else.")
        gtin_all = st.checkbox(
            "Also re-read barcodes already stored",
            value=False,
            help="By default only products with an empty gtin are requested.",
        )
        gtin_limit = st.number_input(
            "Product cap (0 = no cap)",
            min_value=0,
            max_value=100000,
            value=0,
            step=100,
            help=(
                "Tira reads barcodes from its listing, so a cap has no effect "
                "there. Nykaa costs one request per parent and Amazon one page "
                "per product."
            ),
        )
        gtin_submitted = st.form_submit_button(
            "Collect GTINs only",
            icon=":material/barcode_scanner:",
            width="stretch",
        )
    st.divider()
    st.subheader("Insert products from a sheet")
    st.caption("Inserts only rows Supabase does not already have.")
    upload = st.file_uploader(
        "Product sheet",
        type=("csv", "xlsx", "xlsm"),
        help=(
            "Needs a brand and a product name; site, product_id, sku, gtin, "
            "mrp, selling_price, variant, categories and URLs are optional. "
            "Headers are matched by name, so a Shopify or marketplace export "
            "works unedited."
        ),
    )
    manual_site = st.text_input(
        "Site for rows that do not name one",
        value="manual",
        help=(
            "Kept separate from nykaa/tira/amazon on purpose: a retailer sweep "
            "replaces that retailer's rows and ages the ones it did not see, "
            "which would delete hand-added products."
        ),
    )
    check_column, insert_column = st.columns(2)
    with check_column:
        check_clicked = st.button(
            "Check only",
            icon=":material/search:",
            width="stretch",
            disabled=upload is None,
        )
    with insert_column:
        insert_clicked = st.button(
            "Insert",
            type="primary",
            icon=":material/database_upload:",
            width="stretch",
            disabled=upload is None,
        )
    if upload is not None and (check_clicked or insert_clicked):
        from pricing_scraper.manual import insert_manual_products

        try:
            outcome = insert_manual_products(
                upload.getvalue(),
                filename=upload.name,
                default_site=str(manual_site).casefold().strip() or "manual",
                dry_run=check_clicked,
            )
            st.session_state.manual_result = {
                "summary": outcome.summary(),
                "inserted": [
                    f"{item.site} · {item.brand} · {item.product_name}"
                    for item in outcome.inserted
                ],
                "skipped": [
                    f"{item.brand} · {item.product_name} ({reason})"
                    for item, reason in outcome.skipped_existing
                ],
                "rejected": [f"row {line}: {why}" for line, why in outcome.rejected],
                "retailer_sites": sorted(outcome.retailer_sites),
                "written": outcome.written,
                "checked_only": check_clicked,
            }
        except Exception as exc:
            st.session_state.manual_result = {"error": str(exc)}

    st.caption("Runs continue after you close this tab.")

def begin(request: RunRequest) -> None:
    """Start a worker and reattach the dashboard to it."""
    st.session_state.dashboard_error = ""
    try:
        started = start_run(request)
        st.session_state.run_id = started["run_id"]
        st.session_state.products = []
        st.session_state.last_run = {}
    except Exception as exc:
        st.session_state.dashboard_error = str(exc)
    st.rerun()


if submitted:
    if not selected_categories:
        st.session_state.dashboard_error = "Select at least one category."
    else:
        begin(
            RunRequest(
                site=site_key,
                categories=list(selected_categories),
                page_limit=int(page_limit),
                resume=bool(resume_run),
                enrich_details=bool(enrich_details),
                refresh_only_stale=bool(skip_current),
                config_path=str(config_path),
            )
        )

if gtin_submitted:
    begin(
        RunRequest(
            site=site_key,
            categories=list(selected_categories),
            page_limit=int(page_limit),
            resume=True,
            enrich_details=False,
            mode="gtin",
            gtin_all=bool(gtin_all),
            gtin_limit=int(gtin_limit),
            config_path=str(config_path),
        )
    )


def current_status() -> dict[str, Any] | None:
    """Find the run this dashboard should display."""
    return (
        active_status()
        or (
            read_status(st.session_state.run_id)
            if st.session_state.get("run_id")
            else None
        )
        or latest_status()
    )


def render_run(status: dict[str, Any]) -> None:
    """Draw one run's progress, counters, and log.

    The worker owns the run, so this is only a reader: closing the tab or
    reopening the dashboard later reattaches to the same status file.
    """
    state = str(status.get("state") or "")
    running = state in ACTIVE_STATES
    label = {
        "starting": f"Starting {status.get('site', '')} collection...",
        "running": f"Collecting {status.get('site', '')} prices...",
        "success": "Scraping complete",
        "incomplete": "Scraping paused - checkpoint saved",
        "stopped": "Run stopped",
        "failed": "Collection failed",
    }.get(state, state.title())

    with st.status(label, expanded=running or state in {"failed", "incomplete"}):
        with st.container(horizontal=True):
            st.metric(
                "Products discovered",
                f"{int(status.get('listing_products', 0)):,}",
                border=True,
            )
            st.metric(
                "Detail parents",
                f"{int(status.get('detail_parents', 0)):,}",
                border=True,
            )
            st.metric(
                "SKU rows",
                f"{int(status.get('sku_rows', 0)):,}",
                border=True,
            )
        st.progress(
            int(status.get("percent", 0)),
            text=str(status.get("message") or ""),
        )
        st.caption(
            f"Run `{status.get('run_id', '')}` started "
            f"{status.get('started_at', '')} (UTC). It keeps running on the "
            "server if you close this tab."
        )
        if running:
            requested_at = str(status.get("stop_requested_at") or "")
            if requested_at:
                # A stop lands at the next progress checkpoint, so say that
                # plainly instead of leaving the button looking unresponsive.
                st.caption(
                    f"Stop requested at {requested_at} (UTC). The worker stops "
                    "once the request in flight returns, and the checkpoint is "
                    "kept. If nothing changes, the run log shows what it is "
                    "waiting for."
                )
            if st.button(
                "Stop this run",
                icon=":material/stop_circle:",
                disabled=bool(requested_at),
            ):
                request_stop(str(status["run_id"]))
                st.rerun(scope="app")

        log_tail = read_log(str(status.get("run_id", "")), lines=300)
        with st.expander("Run log", expanded=bool(status.get("error"))):
            if log_tail:
                st.code(log_tail, language="log", height=320)
                st.download_button(
                    "Download this log",
                    log_tail,
                    file_name=f"{status.get('run_id', 'run')}.log",
                    icon=":material/download:",
                )
            else:
                st.caption("The worker has not written any output yet.")
        if status.get("error"):
            st.error(status["error"])
        if status.get("database_error"):
            st.warning(status["database_error"])
        if state == "success" and status.get("database_enabled"):
            st.success(
                "Database synchronized: "
                f"{int(status.get('database_products_written', 0)):,} product "
                "rows and "
                f"{int(status.get('database_price_points_written', 0)):,} "
                "price-history points.",
                icon=":material/database:",
            )


@st.fragment(run_every=3)
def live_run_panel() -> None:
    """Refresh only this box while the worker is busy.

    Rerunning the whole page every few seconds would reload the catalogue and
    never let the script settle.
    """
    status = current_status()
    if not status:
        return
    render_run(status)
    if str(status.get("state")) not in ACTIVE_STATES:
        # The run just finished: refresh the page once to pick up its rows.
        st.rerun(scope="app")


run_status = current_status()
if run_status:
    if str(run_status.get("state")) in ACTIVE_STATES:
        live_run_panel()
    else:
        render_run(run_status)
        st.session_state.last_run = {
            "site": run_status.get("site", ""),
            "completed": bool(run_status.get("completed")),
            "next_page": run_status.get("next_page"),
            "stop_reasons": list(run_status.get("stop_reasons", ())),
            "products_written": int(run_status.get("products_written", 0)),
            "excel_path": run_status.get("excel_path", ""),
            "csv_path": run_status.get("csv_path", ""),
            "failures": run_status.get("failures", 0),
            "blocks": run_status.get("blocks", 0),
            "requests": run_status.get("requests", 0),
            "listing_products": run_status.get("listing_products", 0),
            "detail_parents": run_status.get("detail_parents", 0),
        }

if st.session_state.dashboard_error:
    st.error(st.session_state.dashboard_error)

manual_result = st.session_state.get("manual_result")
if manual_result:
    with st.container(border=True):
        st.subheader("Sheet import")
        if manual_result.get("error"):
            st.error(manual_result["error"])
        else:
            summary = manual_result["summary"]
            if manual_result["checked_only"]:
                st.info(f"{summary} — nothing was written.", icon=":material/search:")
            elif manual_result["written"]:
                st.success(
                    f"{summary} — inserted into Supabase.",
                    icon=":material/database:",
                )
            else:
                st.info(
                    f"{summary} — nothing new, so nothing was inserted.",
                    icon=":material/info:",
                )
            if manual_result["retailer_sites"]:
                st.warning(
                    "Rows were filed under "
                    f"{', '.join(manual_result['retailer_sites'])}. The next "
                    "run of that retailer replaces its rows and ages the ones "
                    "it does not see, which will remove these. Leave the site "
                    "column blank to keep them separate.",
                    icon=":material/warning:",
                )
            verb = "Would insert" if manual_result["checked_only"] else "Inserted"
            for label, items in (
                (f"{verb} ({len(manual_result['inserted'])})", manual_result["inserted"]),
                (
                    f"Already in Supabase ({len(manual_result['skipped'])})",
                    manual_result["skipped"],
                ),
                (
                    f"Unusable rows ({len(manual_result['rejected'])})",
                    manual_result["rejected"],
                ),
            ):
                if items:
                    with st.expander(label):
                        st.write("\n".join(f"- {line}" for line in items[:200]))
                        if len(items) > 200:
                            st.caption(f"...and {len(items) - 200} more")
    if st.button("Dismiss", icon=":material/close:"):
        st.session_state.manual_result = None
        st.rerun()

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

# chart_column, download_column = st.columns([2, 1])
# with chart_column:
#     with st.container(border=True):
#         st.subheader("Products by brand")
#         brand_counts = (
#             filtered.groupby("brand", as_index=False)
#             .size()
#             .rename(columns={"size": "Products"})
#             .sort_values("Products", ascending=False)
#         )
#         st.bar_chart(
#             brand_counts,
#             x="brand",
#             y="Products",
#             x_label="Brand",
#             y_label="Products",
#         )

# The "Products by brand" chart above is commented out, which took the
# column layout with it, so the download panel stands on its own.
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
            "gtin": st.column_config.TextColumn("GTIN/EAN"),
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
            "key_ingredients": st.column_config.ListColumn("Key ingredients"),
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
