"""Product view tab: read-only storefront for inspecting scraped products."""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from pricing_scraper.catalog_reader import (
    CatalogSnapshot,
    checkpoint_signature,
    csv_signature,
    load_checkpoint_file,
    load_exported_products,
    load_scrape_runs,
    load_supabase_products,
    products_to_csv_bytes,
)
from pricing_scraper.exporter import deduplicate
from pricing_scraper.models import Product

ROOT = Path(__file__).resolve().parent.parent
CHECKPOINT_DIR = ROOT / "data" / "checkpoints"
COMBINED_CSV = ROOT / "data" / "pricing_combined.csv"
STYLESHEET = ROOT / "product_viewer_styles.css"
PLACEHOLDER_IMAGE = (
    "data:image/svg+xml;utf8,"
    "<svg xmlns='http://www.w3.org/2000/svg' width='640' height='640'>"
    "<rect width='100%25' height='100%25' fill='%23f5f2f4'/>"
    "<text x='50%25' y='50%25' dominant-baseline='middle' "
    "text-anchor='middle' fill='%23948b92' font-family='Arial' "
    "font-size='28'>No image available</text></svg>"
)

st.html(STYLESHEET)


@st.cache_data(ttl=60, max_entries=64, show_spinner=False)
def _cached_checkpoint_file(
    path: str,
    size: int,
    modified_ns: int,
) -> tuple[list[Product], int]:
    del size, modified_ns
    return load_checkpoint_file(Path(path))


def _cached_checkpoints(directory: Path) -> CatalogSnapshot:
    signatures = checkpoint_signature(directory)
    products: list[Product] = []
    invalid_rows = 0
    for path, size, modified_ns in signatures:
        file_products, file_invalid = _cached_checkpoint_file(
            path,
            size,
            modified_ns,
        )
        products.extend(file_products)
        invalid_rows += file_invalid
    return CatalogSnapshot(
        products=deduplicate(products),
        source="Live checkpoints",
        files_read=len(signatures),
        invalid_rows=invalid_rows,
    )


@st.cache_data(max_entries=4, show_spinner=False)
def _cached_csv(
    path: str,
    signature: tuple[str, int, int] | None,
) -> CatalogSnapshot:
    del signature
    return load_exported_products(Path(path))


@st.cache_data(ttl=10, max_entries=2, show_spinner=False)
def _cached_database() -> CatalogSnapshot:
    return load_supabase_products()


@st.cache_data(ttl=30, max_entries=2, show_spinner=False)
def _cached_scrape_runs() -> list[dict[str, Any]]:
    return load_scrape_runs(limit=20)


def _render_automation_status() -> None:
    """Show the latest independent cron result for each automated retailer."""
    st.subheader("Nightly automation")
    try:
        runs = _cached_scrape_runs()
    except Exception as exc:
        st.caption(f"Run history unavailable: {exc}")
        return
    latest: dict[str, dict[str, Any]] = {}
    for run in runs:
        site = str(run.get("site") or "").casefold()
        if site in {"nykaa", "tira"} and site not in latest:
            latest[site] = run
    for site in ("nykaa", "tira"):
        run = latest.get(site)
        if run is None:
            st.markdown(f"**{site.title()}** · no run yet")
            continue
        status = str(run.get("status") or "unknown")
        badge = {
            "success": ":green-badge[success]",
            "running": ":blue-badge[running]",
            "partial": ":orange-badge[partial]",
            "failed": ":red-badge[failed]",
        }.get(status, f":gray-badge[{status}]")
        finished = str(run.get("finished_at") or run.get("started_at") or "")
        st.markdown(f"**{site.title()}** {badge}")
        st.caption(
            f"{finished[:19].replace('T', ' ')} UTC · "
            f"seen {int(run.get('products_seen') or 0):,} · "
            f"new {int(run.get('products_new') or 0):,} · "
            f"changed {int(run.get('products_changed') or 0):,}"
        )
        message = str(run.get("message") or "").strip()
        if message and message != "Scraping complete":
            st.caption(message)


def _load_snapshot(source: str) -> CatalogSnapshot:
    if source == "Live checkpoints":
        return _cached_checkpoints(CHECKPOINT_DIR)
    if source == "Supabase database":
        return _cached_database()
    return _cached_csv(str(COMBINED_CSV), csv_signature(COMBINED_CSV))


def _product_key(product: Product) -> str:
    """Return a stable identifier for selecting one product observation."""
    return "|".join((product.site, product.product_id, product.sku))


def _open_product(product_key: str) -> None:
    st.session_state.selected_product_key = product_key
    st.session_state.viewer_screen = "Product"


def _open_catalog() -> None:
    st.session_state.viewer_screen = "Catalogue"
    st.session_state.selected_product_key = ""


def _clear_filters() -> None:
    for key in (
        "catalogue_query",
        "catalogue_sites",
        "catalogue_categories",
        "catalogue_brands",
        "catalogue_stock",
        "shop_page",
        "table_page",
    ):
        st.session_state.pop(key, None)


def _money(value: float | None) -> str:
    return f"₹{value:,.0f}" if value is not None else "Price unavailable"


def _stock_text(value: bool | None) -> str:
    if value is True:
        return "In stock"
    if value is False:
        return "Out of stock"
    return "Stock not reported"


def _stock_badge(value: bool | None) -> str:
    if value is True:
        return ":green-badge[In stock]"
    if value is False:
        return ":red-badge[Out of stock]"
    return ":gray-badge[Stock unknown]"


def _image_urls(product: Product) -> list[str]:
    return list(
        dict.fromkeys(
            image
            for image in [*product.image_urls, product.image_url]
            if image
        )
    )


def _matches_search(product: Product, query: str) -> bool:
    if not query:
        return True
    haystack = " ".join(
        [
            product.site,
            product.parent_product_id,
            product.product_id,
            product.sku,
            product.brand,
            product.product_name,
            product.variant,
            " ".join(product.categories),
            " ".join(product.source_categories),
        ]
    ).casefold()
    return query.casefold() in haystack


def _variant_family(product: Product, products: list[Product]) -> list[Product]:
    """Return other SKU sizes belonging to the selected product."""
    if product.parent_product_id:
        family = [
            candidate
            for candidate in products
            if candidate.site == product.site
            and candidate.parent_product_id == product.parent_product_id
        ]
    else:
        name = product.product_name.strip().casefold()
        family = [
            candidate
            for candidate in products
            if candidate.site == product.site
            and candidate.product_name.strip().casefold() == name
        ]
    return sorted(
        deduplicate(family or [product]),
        key=lambda item: (
            item.variant.casefold(),
            item.selling_price if item.selling_price is not None else math.inf,
        ),
    )


def _format_variant(product: Product) -> str:
    label = product.variant or product.sku or product.product_id
    return f"{label} · {_money(product.selling_price)}"


def _product_table(products: list[Product]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "site": product.site.title(),
                "product_id": product.product_id,
                "sku": product.sku,
                "brand": product.brand,
                "product_name": product.product_name,
                "variant": product.variant,
                "mrp": product.mrp,
                "selling_price": product.selling_price,
                "discount_pct": product.discount_pct,
                "rating": product.rating,
                "rating_count": product.rating_count,
                "in_stock": product.in_stock,
                "categories": ", ".join(product.categories),
                "scraped_at": product.scraped_at,
                "product_url": product.product_url,
            }
            for product in products
        ]
    )


def _safe_value(value: Any) -> str:
    if value is None or value == "":
        return "Not available"
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(item) for item in value)
    if isinstance(value, dict):
        return ", ".join(f"{key}: {item}" for key, item in value.items())
    return str(value)


def _render_store_header() -> None:
    with st.container(
        key="store_header",
        horizontal=True,
        vertical_alignment="center",
        horizontal_alignment="distribute",
    ):
        st.title("GlowCompare")
        st.caption(
            "Beauty and skincare catalogue · Nykaa · Tira · Amazon India"
        )
        st.badge(
            "Read-only live catalogue",
            icon=":material/visibility:",
            color="red",
        )


def _render_product_card(product: Product, position: int) -> None:
    card_key = f"product_card_{position}_{abs(hash(_product_key(product)))}"
    with st.container(
        key=card_key,
        border=True,
        height=465,
        vertical_alignment="distribute",
    ):
        images = _image_urls(product)
        st.image(
            images[0] if images else PLACEHOLDER_IMAGE,
            width="stretch",
        )
        st.caption(
            " · ".join(
                value
                for value in (
                    product.site.title(),
                    product.brand,
                    product.variant,
                )
                if value
            )
        )
        title = product.product_name or product.product_id
        st.markdown(f"**{title[:92]}{'…' if len(title) > 92 else ''}**")
        price_bits = [f"### {_money(product.selling_price)}"]
        if product.mrp is not None and product.mrp != product.selling_price:
            price_bits.append(f"~~{_money(product.mrp)}~~")
        if product.discount_pct:
            price_bits.append(f":green[{product.discount_pct:.0f}% off]")
        st.markdown(" &nbsp; ".join(price_bits))
        rating_text = (
            f"★ {product.rating:.1f}"
            if product.rating is not None
            else "No rating"
        )
        if product.rating_count:
            rating_text += f" ({product.rating_count:,})"
        st.caption(f"{rating_text} · {_stock_text(product.in_stock)}")
        st.button(
            "View product",
            key=f"open_{card_key}",
            icon=":material/arrow_forward:",
            width="stretch",
            on_click=_open_product,
            args=(_product_key(product),),
        )


def _render_shop_grid(products: list[Product]) -> None:
    page_size = 20
    total_pages = max(1, math.ceil(len(products) / page_size))
    if st.session_state.get("shop_page", 1) > total_pages:
        st.session_state.shop_page = 1
    page_slot = st.container()
    with st.container(horizontal_alignment="right"):
        page = st.pagination(
            total_pages,
            key="shop_page",
            max_visible_pages=7,
        )
    start = (page - 1) * page_size
    page_products = products[start : start + page_size]
    page_slot.caption(
        f"Showing {start + 1:,}–{start + len(page_products):,} of "
        f"{len(products):,} products"
    )
    with page_slot:
        for row_start in range(0, len(page_products), 4):
            columns = st.columns(4)
            row = page_products[row_start : row_start + 4]
            for index, (column, product) in enumerate(zip(columns, row)):
                with column:
                    _render_product_card(product, start + row_start + index)


def _render_data_table(products: list[Product]) -> None:
    page_size = 250
    total_pages = max(1, math.ceil(len(products) / page_size))
    if st.session_state.get("table_page", 1) > total_pages:
        st.session_state.table_page = 1
    table_slot = st.container()
    with st.container(horizontal_alignment="right"):
        page = st.pagination(total_pages, key="table_page")
    start = (page - 1) * page_size
    page_products = products[start : start + page_size]
    table_slot.caption(
        f"Showing {start + 1:,}–{start + len(page_products):,} of "
        f"{len(products):,} products. Select a row to open its product page."
    )
    event = table_slot.dataframe(
        _product_table(page_products),
        hide_index=True,
        width="stretch",
        height=620,
        key="product_catalogue_table",
        on_select="rerun",
        selection_mode="single-row",
        column_order=(
            "site",
            "brand",
            "product_name",
            "variant",
            "selling_price",
            "mrp",
            "discount_pct",
            "rating",
            "rating_count",
            "in_stock",
            "categories",
            "product_id",
            "sku",
            "scraped_at",
            "product_url",
        ),
        column_config={
            "site": st.column_config.TextColumn("Retailer"),
            "brand": st.column_config.TextColumn("Brand"),
            "product_name": st.column_config.TextColumn(
                "Product",
                pinned=True,
                width="large",
            ),
            "variant": st.column_config.TextColumn("Size / variant"),
            "selling_price": st.column_config.NumberColumn(
                "Selling price",
                format="₹%.2f",
            ),
            "mrp": st.column_config.NumberColumn("MRP", format="₹%.2f"),
            "discount_pct": st.column_config.NumberColumn(
                "Discount",
                format="%.2f%%",
            ),
            "rating": st.column_config.NumberColumn("Rating", format="%.2f"),
            "rating_count": st.column_config.NumberColumn(
                "Ratings",
                format="localized",
            ),
            "in_stock": st.column_config.CheckboxColumn("In stock"),
            "categories": st.column_config.TextColumn("Categories"),
            "product_id": st.column_config.TextColumn("Product ID"),
            "sku": st.column_config.TextColumn("SKU / ASIN"),
            "scraped_at": st.column_config.TextColumn("Scraped at"),
            "product_url": st.column_config.LinkColumn(
                "Retailer page",
                display_text="Open",
            ),
        },
    )
    if event.selection.rows:
        _open_product(_product_key(page_products[event.selection.rows[0]]))
        st.rerun()


def _render_rating_breakdown(product: Product) -> None:
    if not product.rating_breakdown:
        st.caption("No rating breakdown was published for this SKU.")
        return
    rows = []
    for item in product.rating_breakdown:
        if isinstance(item, dict):
            rows.append(
                {
                    "Rating": item.get("rating")
                    or item.get("star")
                    or item.get("stars")
                    or item.get("label"),
                    "Reviews": item.get("count")
                    or item.get("rating_count")
                    or item.get("value"),
                }
            )
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


def _render_reviews(product: Product) -> None:
    if not product.top_reviews:
        st.caption("No highlighted reviews were available for this SKU.")
        return
    for index, review in enumerate(product.top_reviews[:20]):
        if not isinstance(review, dict):
            continue
        with st.container(key=f"review_card_{index}", border=True):
            heading = (
                review.get("title")
                or review.get("review_title")
                or f"Customer review {index + 1}"
            )
            rating = review.get("rating") or review.get("score")
            st.markdown(
                f"**{heading}**"
                + (f" · :orange[★ {rating}]" if rating is not None else "")
            )
            st.write(
                review.get("description")
                or review.get("review")
                or review.get("text")
                or _safe_value(review)
            )
            reviewer = (
                review.get("name")
                or review.get("user_name")
                or review.get("author")
            )
            if reviewer:
                st.caption(str(reviewer))


def _render_product_information(product: Product) -> None:
    overview, ingredients, details, reviews = st.tabs(
        [
            "Description",
            "Ingredients & usage",
            "Product details",
            "Ratings & reviews",
        ],
        on_change="rerun",
    )
    if overview.open:
        with overview:
            if product.description_html:
                st.html(product.description_html)
            elif product.description:
                st.write(product.description)
            else:
                st.caption("No product description was published.")
            if product.key_features:
                st.subheader("Key benefits")
                for feature in product.key_features:
                    st.markdown(f"- {feature}")
            if product.special_features:
                st.subheader("Special features")
                st.write(", ".join(product.special_features))
    if ingredients.open:
        with ingredients:
            st.subheader("Ingredients")
            st.write(product.ingredients or "Not available")
            st.subheader("How to use")
            st.write(product.how_to_use or "Not available")
    if details.open:
        with details:
            categories = product.categories or product.source_categories
            rows = [
                {"Detail": "Retailer", "Value": product.site.title()},
                {"Detail": "Brand", "Value": product.brand},
                {"Detail": "Size / variant", "Value": product.variant},
                {"Detail": "SKU / ASIN", "Value": product.sku},
                {"Detail": "Product ID", "Value": product.product_id},
                {"Detail": "Categories", "Value": ", ".join(categories)},
            ]
            rows.extend(
                {
                    "Detail": str(key).replace("_", " ").title(),
                    "Value": _safe_value(value),
                }
                for key, value in product.product_attributes.items()
            )
            st.dataframe(
                pd.DataFrame(rows),
                hide_index=True,
                width="stretch",
                column_config={
                    "Detail": st.column_config.TextColumn(
                        "Detail",
                        width="medium",
                    ),
                    "Value": st.column_config.TextColumn(
                        "Value",
                        width="large",
                    ),
                },
            )
            st.caption(f"Last scraped: {product.scraped_at or 'Not available'}")
    if reviews.open:
        with reviews:
            summary, breakdown = st.columns([1, 2])
            with summary:
                st.metric(
                    "Overall rating",
                    (
                        f"{product.rating:.1f}/5"
                        if product.rating is not None
                        else "Not available"
                    ),
                    (
                        f"{product.rating_count:,} ratings"
                        if product.rating_count is not None
                        else None
                    ),
                    border=True,
                )
                st.metric(
                    "Written reviews",
                    (
                        f"{product.review_count:,}"
                        if product.review_count is not None
                        else "Not available"
                    ),
                    border=True,
                )
            with breakdown:
                _render_rating_breakdown(product)
            st.subheader("Highlighted reviews")
            _render_reviews(product)


def _render_product_page(product: Product, products: list[Product]) -> None:
    st.button(
        "Back to catalogue",
        icon=":material/arrow_back:",
        on_click=_open_catalog,
    )
    categories = product.categories or product.source_categories
    breadcrumb = " / ".join(
        value
        for value in (
            "Home",
            product.site.title(),
            categories[0] if categories else "",
            product.brand,
        )
        if value
    )
    st.caption(breadcrumb)

    gallery, purchase = st.columns([1.05, 1], gap="large")
    images = _image_urls(product)
    with gallery:
        with st.container(key="product_hero", border=True):
            st.image(
                images[0] if images else PLACEHOLDER_IMAGE,
                width="stretch",
            )
            if len(images) > 1:
                st.caption(f"{len(images)} product images")
                st.image(images[1:9], width=105)

    with purchase:
        st.caption(
            " · ".join(
                value for value in (product.brand, product.site.title()) if value
            )
        )
        st.title(product.product_name or product.product_id)
        if product.variant:
            st.caption(product.variant)
        rating_text = (
            f":orange[★ {product.rating:.1f}/5]"
            if product.rating is not None
            else "No rating available"
        )
        if product.rating_count is not None:
            rating_text += f" · {product.rating_count:,} ratings"
        if product.review_count is not None:
            rating_text += f" · {product.review_count:,} reviews"
        st.markdown(rating_text)

        with st.container(key="price_panel"):
            price_line = f"## {_money(product.selling_price)}"
            if product.mrp is not None and product.mrp != product.selling_price:
                price_line += f" &nbsp; ~~{_money(product.mrp)}~~"
            if product.discount_pct:
                price_line += f" &nbsp; :green[{product.discount_pct:.0f}% off]"
            st.markdown(price_line)
            st.caption("Inclusive of all taxes where reported by the retailer")

        family = _variant_family(product, products)
        if len(family) > 1:
            current_index = next(
                (
                    index
                    for index, candidate in enumerate(family)
                    if _product_key(candidate) == _product_key(product)
                ),
                0,
            )
            selected_variant = st.selectbox(
                "Select size / variant",
                family,
                index=current_index,
                format_func=_format_variant,
                key=f"variant_{abs(hash(_product_key(product)))}",
            )
            if _product_key(selected_variant) != _product_key(product):
                _open_product(_product_key(selected_variant))
                st.rerun()
        elif product.variant:
            st.markdown(f"**Selected size:** {product.variant}")

        st.markdown(_stock_badge(product.in_stock))
        with st.container(key="buy_panel"):
            st.markdown("**Buy from the retailer**")
            st.caption(
                "Price and stock shown are from the latest successful scrape. "
                "Confirm them on the retailer page before purchasing."
            )
            if product.product_url:
                st.link_button(
                    f"View on {product.site.title()}",
                    product.product_url,
                    icon=":material/open_in_new:",
                    width="stretch",
                )
            else:
                st.button(
                    "Retailer link unavailable",
                    disabled=True,
                    width="stretch",
                )

        with st.expander(
            "Product identifiers",
            icon=":material/qr_code_2:",
        ):
            st.write(f"Product ID: {product.product_id or 'Not available'}")
            st.write(f"SKU / ASIN: {product.sku or 'Not available'}")
            if product.parent_product_id:
                st.write(f"Parent ID: {product.parent_product_id}")

    st.space("medium")
    with st.container(key="product_information", border=True):
        _render_product_information(product)


def _render_catalogue(
    products: list[Product],
    snapshot: CatalogSnapshot,
) -> None:
    sites = sorted({product.site.title() for product in products})
    categories = sorted(
        {
            category
            for product in products
            for category in product.categories
            if category
        }
    )
    brands = sorted(
        {product.brand for product in products if product.brand},
        key=str.casefold,
    )

    with st.sidebar:
        st.subheader("Shop filters")
        query = st.text_input(
            "Search products",
            placeholder="Product, brand, SKU or ID",
            key="catalogue_query",
        )
        selected_sites = st.multiselect(
            "Retailers",
            sites,
            default=sites,
            key="catalogue_sites",
        )
        selected_categories = st.multiselect(
            "Categories",
            categories,
            key="catalogue_categories",
        )
        selected_brands = st.multiselect(
            "Brands",
            brands,
            key="catalogue_brands",
        )
        stock_filter = st.selectbox(
            "Availability",
            ("All", "In stock", "Out of stock", "Unknown"),
            key="catalogue_stock",
        )
        st.button(
            "Clear filters",
            icon=":material/filter_alt_off:",
            on_click=_clear_filters,
            width="stretch",
        )

    selected_category_set = set(selected_categories)
    filtered = [
        product
        for product in products
        if product.site.title() in selected_sites
        and (
            not selected_category_set
            or bool(set(product.categories) & selected_category_set)
        )
        and (not selected_brands or product.brand in selected_brands)
        and (
            stock_filter == "All"
            or stock_filter == "In stock"
            and product.in_stock is True
            or stock_filter == "Out of stock"
            and product.in_stock is False
            or stock_filter == "Unknown"
            and product.in_stock is None
        )
        and _matches_search(product, query)
    ]

    latest = max(
        (product.scraped_at for product in products if product.scraped_at),
        default="—",
    )
    st.subheader("Beauty and skincare products")
    with st.container(horizontal=True):
        st.metric("Products", f"{len(filtered):,}", border=True)
        st.metric(
            "Retailers",
            f"{len({product.site for product in filtered}):,}",
            border=True,
        )
        st.metric(
            "Brands",
            f"{len({product.brand for product in filtered if product.brand}):,}",
            border=True,
        )
        st.metric("Latest scrape", latest[:19], border=True)

    if snapshot.files_read:
        st.caption(
            f"Live read-only snapshot from {snapshot.files_read:,} checkpoint "
            f"files · malformed rows skipped: {snapshot.invalid_rows:,}"
        )
    if not filtered:
        st.info("No products match these filters.")
        return

    with st.container(
        horizontal=True,
        vertical_alignment="center",
        horizontal_alignment="distribute",
    ):
        view = st.segmented_control(
            "View",
            ("Shop", "Data table"),
            default="Shop",
            key="catalogue_view",
        )
        st.download_button(
            "Download filtered CSV",
            data=products_to_csv_bytes(filtered),
            file_name="filtered_products.csv",
            mime="text/csv",
            icon=":material/download:",
        )

    if view == "Data table":
        _render_data_table(filtered)
    else:
        _render_shop_grid(filtered)


def _render_source(source: str) -> None:
    try:
        snapshot = _load_snapshot(source)
    except Exception as exc:
        st.error(f"Could not load {source}: {exc}")
        return
    products = snapshot.products
    if not products:
        st.warning(
            "No products are available from this source yet. If scraping is "
            "running, wait for its first checkpoint write and refresh."
        )
        return

    selected_key = st.session_state.get("selected_product_key", "")
    screen = st.session_state.get("viewer_screen", "Catalogue")
    selected = next(
        (
            product
            for product in products
            if _product_key(product) == selected_key
        ),
        None,
    )
    if screen == "Product" and selected is not None:
        _render_product_page(selected, products)
    else:
        if screen == "Product":
            _open_catalog()
        _render_catalogue(products, snapshot)


st.session_state.setdefault("viewer_screen", "Catalogue")
st.session_state.setdefault("selected_product_key", "")

# _render_store_header()
# st.caption(
#     "Browse the live scraped catalogue without changing checkpoints, database "
#     "records, Excel files, or the active scraping process."
# )

with st.sidebar:
    st.header("Catalogue settings")
    hosted = os.getenv("HOSTED_DASHBOARD", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }
    source = st.selectbox(
        "Data source",
        ("Live checkpoints", "Supabase database", "Latest exported CSV"),
        index=1 if hosted else 0,
        help=(
            "Live checkpoints show products before the scraper reaches its "
            "final database and export stage."
        ),
    )
    auto_refresh = st.toggle(
        "Auto-refresh live data",
        value=source == "Live checkpoints",
        disabled=source != "Live checkpoints",
        help="Reloads the read-only checkpoint snapshot every 15 seconds.",
    )
    if st.button(
        "Refresh now",
        icon=":material/refresh:",
        width="stretch",
    ):
        _cached_checkpoint_file.clear()
        _cached_csv.clear()
        _cached_database.clear()
        _cached_scrape_runs.clear()
    if hosted or source == "Supabase database":
        _render_automation_status()
    st.caption("Viewer only · no writes · scraper-safe")

if auto_refresh:

    @st.fragment(run_every="15s")
    def _live_catalogue() -> None:
        _render_source(source)

    _live_catalogue()
else:
    _render_source(source)
