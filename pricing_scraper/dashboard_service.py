"""Application service used by the Streamlit dashboard."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from pricing_scraper.checkpoint import CheckpointStore, DetailCheckpointStore
from pricing_scraper.clients.base import ConfigurationError, RequestFailed
from pricing_scraper.clients.nykaa import NykaaClient
from pricing_scraper.clients.tira import TiraClient
from pricing_scraper.exporter import (
    ExportResult,
    deduplicate,
    export_products,
    merge_with_existing_sites,
)
from pricing_scraper.models import Product

ProgressCallback = Callable[[str, int, int, str], None]

AMAZON_UNAVAILABLE_MESSAGE = (
    "Amazon collection needs the Playwright browser dependencies, which the "
    "hosted deployment does not install. Run Amazon locally with "
    "`pip install -r requirements.txt` and `playwright install chromium`."
)


def amazon_dependencies_available() -> bool:
    """Report whether the Playwright-backed Amazon client can be imported."""
    try:
        return importlib.util.find_spec("playwright") is not None
    except (ImportError, ValueError):
        return False


def _load_amazon_client() -> type:
    """Import the Amazon client only when an Amazon run actually starts."""
    try:
        from pricing_scraper.clients.amazon import AmazonClient
    except ImportError as exc:
        raise ConfigurationError(AMAZON_UNAVAILABLE_MESSAGE) from exc
    return AmazonClient


def _database_sync_enabled(config: dict[str, Any]) -> bool:
    database = config.get("database")
    return bool(
        isinstance(database, dict)
        and database.get("enabled", False)
    )


def _parent_id(product: Product) -> str:
    if product.parent_product_id:
        return product.parent_product_id
    match = re.search(r"/p/(\d+)", product.product_url)
    return match.group(1) if match else product.product_id


def _permanent_detail_not_found(exc: Exception) -> bool:
    """Return whether a retailer permanently rejected a product detail ID."""
    return (
        isinstance(exc, RequestFailed)
        and exc.status_code in {404, 410}
    )


def _export_status_callback(
    progress_callback: ProgressCallback | None,
) -> Callable[[str], None] | None:
    if progress_callback is None:
        return None

    def report(message: str) -> None:
        progress_callback("finalize", 0, 0, message)

    return report


@dataclass(frozen=True, slots=True)
class CollectionResult:
    """Products, diagnostics, and output paths from one dashboard run."""

    products: list[Product]
    export: ExportResult
    failures: int
    blocks: int
    requests: int
    completed: bool
    next_page: int | None
    stop_reasons: tuple[str, ...]
    resumed_products: int
    checkpoint_paths: tuple[Path, ...]
    listing_products: int
    detail_parents: int


def _full_catalog_partitions(
    client: NykaaClient,
    selected: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Expand the all-skincare scope into smaller API result windows."""
    del client
    expanded: list[dict[str, Any]] = []
    for category in selected:
        partitions = category.get("partitions")
        if not category.get("covers_all") or not isinstance(partitions, list):
            item = dict(category)
            item["checkpoint_key"] = str(category["id"])
            expanded.append(item)
            continue
        for partition in partitions:
            if not isinstance(partition, dict):
                continue
            key = str(partition.get("key") or "").strip()
            query = partition.get("query")
            if not key or not isinstance(query, dict):
                continue
            item = dict(category)
            item["name"] = (
                f"{category['name']} — "
                f"{partition.get('name') or key}"
            )
            item["query"] = dict(query)
            item["checkpoint_key"] = f"{category['id']}_{key}"
            item["partitions"] = []
            expanded.append(item)
    return expanded or selected


def _scope_id(categories: Iterable[dict[str, Any]]) -> str:
    identifiers = [str(item["id"]) for item in categories]
    return "full_catalog" if "8377" in identifiers else "_".join(identifiers)


def _output_paths(
    run_config: dict[str, Any],
    output_path: Path | None,
) -> tuple[Path, Path | None]:
    output_config = run_config["output"]
    excel_path = output_path or Path(
        str(output_config.get("excel_path") or "data/pricing.xlsx")
    )
    configured_csv = str(
        output_config.get("combined_csv_path") or ""
    ).strip()
    csv_path = (
        excel_path.with_name(f"{excel_path.stem}_combined.csv")
        if output_path
        else Path(configured_csv) if configured_csv else None
    )
    return excel_path, csv_path


def collect_nykaa(
    config: dict[str, Any],
    categories: Iterable[str],
    page_limit: int,
    output_path: Path | None = None,
    *,
    resume: bool = True,
    enrich_details: bool | None = None,
    progress_callback: ProgressCallback | None = None,
) -> CollectionResult:
    """Collect Nykaa listings and optionally enrich every parent/SKU detail."""
    run_config = copy.deepcopy(config)
    run_config["nykaa"]["page_limit"] = max(1, int(page_limit))
    detail_config = run_config["nykaa"].get("details", {})
    detail_config = detail_config if isinstance(detail_config, dict) else {}
    details_enabled = (
        bool(detail_config.get("enabled", True))
        if enrich_details is None
        else bool(enrich_details)
    )

    with NykaaClient(
        site_config=run_config["nykaa"],
        request_config=run_config["request"],
        brands=run_config.get("brands", ()),
    ) as client:
        requested = client.select_categories(list(categories))
        selected = _full_catalog_partitions(client, requested)
        checkpoint_dir = Path(
            str(
                run_config["nykaa"].get("checkpoint_dir")
                or "data/checkpoints"
            )
        )
        listing_stores = {
            str(category.get("checkpoint_key") or category["id"]): CheckpointStore(
                checkpoint_dir,
                site="nykaa",
                category_id=str(
                    category.get("checkpoint_key") or category["id"]
                ),
                start_page=client.start_page,
            )
            for category in selected
        }
        detail_store = DetailCheckpointStore(
            checkpoint_dir,
            site="nykaa",
            category_id=_scope_id(selected),
        )
        listing_states = {
            category_id: store.load_state()
            for category_id, store in listing_stores.items()
        }
        listings_complete_before = all(
            state.completed for state in listing_states.values()
        )
        details_complete_before = detail_store.load_state().completed
        new_run = (
            not resume
            or (
                listings_complete_before
                and (
                    not details_enabled
                    or details_complete_before
                )
            )
        )
        if new_run:
            for store in listing_stores.values():
                store.reset()
            detail_store.reset()

        stop_reasons: list[str] = []
        next_page: int | None = None
        listing_products: list[Product] = []
        previous_by_checkpoint = {
            checkpoint_key: store.load_products()
            for checkpoint_key, store in listing_stores.items()
        }
        live_listing_ids = {
            product.product_id
            for products in previous_by_checkpoint.values()
            for product in products
        }
        resumed_products = sum(
            len(products) for products in previous_by_checkpoint.values()
        )
        listing_completed = True

        if progress_callback is not None:
            progress_callback(
                "listing_products",
                len(live_listing_ids),
                0,
                (
                    f"{len(live_listing_ids):,} unique listing products "
                    "available from checkpoints"
                ),
            )

        for category_index, category in enumerate(selected, start=1):
            checkpoint_key = str(
                category.get("checkpoint_key") or category["id"]
            )
            store = listing_stores[checkpoint_key]
            state = store.load_state()
            previous = previous_by_checkpoint[checkpoint_key]
            if progress_callback is not None:
                progress_callback(
                    "listing",
                    category_index - 1,
                    len(selected),
                    (
                        f"Listing partition {category_index}/{len(selected)}: "
                        f"{category['name']}"
                    ),
                )

            if not state.completed:
                def save_listing_page(
                    page: int,
                    page_products: Iterable[Product],
                    *,
                    current_store: CheckpointStore = store,
                    category_name: str = str(category["name"]),
                ) -> None:
                    saved_products = list(page_products)
                    current_store.append_page(page, saved_products)
                    live_listing_ids.update(
                        product.product_id for product in saved_products
                    )
                    if progress_callback is not None:
                        progress_callback(
                            "listing_products",
                            len(live_listing_ids),
                            0,
                            (
                                f"{category_name}: page {page:,} saved · "
                                f"{len(live_listing_ids):,} unique products "
                                "discovered"
                            ),
                        )

                run = client.scrape_category_resumable(
                    category,
                    start_page=state.next_page,
                    seen_product_ids=(
                        product.product_id for product in previous
                    ),
                    on_page=save_listing_page,
                )
                category_products = store.load_products()
                if run.completed:
                    store.mark_complete(
                        empty_page=run.next_page,
                        products=category_products,
                    )
                else:
                    listing_completed = False
                    next_page = (
                        run.next_page
                        if next_page is None
                        else min(next_page, run.next_page)
                    )
                    stop_reasons.append(
                        f"{category['name']} listing: {run.stop_reason}"
                    )
            category_products = store.load_products()
            listing_products.extend(category_products)

        listing_products = deduplicate(listing_products)
        if not listing_products:
            raise ValueError(
                "Nykaa returned no products. Check the selected categories "
                "and private cURL session."
            )

        if progress_callback is not None:
            progress_callback(
                "listing_products",
                len(listing_products),
                0,
                (
                    f"{len(listing_products):,} unique listing products "
                    "discovered"
                ),
            )
            progress_callback(
                "listing",
                len(selected),
                len(selected),
                (
                    f"Discovered {len(listing_products):,} unique listing "
                    "SKU rows."
                ),
            )

        final_products = listing_products
        details_completed = not details_enabled
        detail_parent_count = 0

        # Enrich every parent discovered so far, even when listing discovery
        # stopped at its safety cap. The detail checkpoint makes this safe:
        # later runs skip completed parents and enrich only newly discovered
        # ones.
        if details_enabled:
            representatives: dict[str, Product] = {}
            for product in listing_products:
                parent_id = _parent_id(product)
                representatives.setdefault(parent_id, product)
            detail_parent_count = len(representatives)
            processed_before = detail_store.load_processed_ids()
            enriched_before = detail_store.load_products()
            live_sku_ids = {
                product.product_id for product in enriched_before
            }
            processed_live = set(processed_before)
            resumed_products += len(enriched_before)
            pending = [
                (parent_id, product)
                for parent_id, product in representatives.items()
                if parent_id not in processed_before
            ]
            detail_failures_before = client.detail_failures

            if progress_callback is not None:
                progress_callback(
                    "details",
                    len(processed_live),
                    detail_parent_count,
                    (
                        f"Product details: {len(processed_live):,}/"
                        f"{detail_parent_count:,} parents completed"
                    ),
                )
                progress_callback(
                    "sku_rows",
                    len(live_sku_ids),
                    0,
                    f"{len(live_sku_ids):,} enriched SKU rows ready",
                )

            for parent_id, product in pending:
                try:
                    detail_products = client.fetch_product_details(product)
                    detail_store.append_parent(parent_id, detail_products)
                    processed_live.add(parent_id)
                    live_sku_ids.update(
                        item.product_id for item in detail_products
                    )
                except Exception as exc:
                    if _permanent_detail_not_found(exc):
                        # Nykaa listing checkpoints can retain discontinued
                        # products after the detail API removes them. Preserve
                        # the last listing observation and tombstone the parent
                        # as processed so one stale record cannot block export.
                        detail_store.append_parent(parent_id, [product])
                        processed_live.add(parent_id)
                        live_sku_ids.add(product.product_id)
                        client.logger.warning(
                            "nykaa_detail_unavailable parent_id=%s "
                            "status=%s listing_fallback=saved",
                            parent_id,
                            getattr(exc, "status_code", None),
                        )
                    else:
                        client.detail_failures += 1
                        client.logger.exception(
                            "nykaa_detail parent_id=%s failed=%s",
                            parent_id,
                            exc,
                        )
                if progress_callback is not None:
                    progress_callback(
                        "details",
                        len(processed_live),
                        detail_parent_count,
                        (
                            f"Product details: {len(processed_live):,}/"
                            f"{detail_parent_count:,} parents completed"
                        ),
                    )
                    progress_callback(
                        "sku_rows",
                        len(live_sku_ids),
                        0,
                        f"{len(live_sku_ids):,} enriched SKU rows ready",
                    )

            processed = detail_store.load_processed_ids()
            unprocessed = set(representatives) - processed
            details_completed = listing_completed and not unprocessed
            if details_completed:
                detail_store.mark_complete()
            elif unprocessed:
                stop_reasons.append(
                    f"Product details: {len(unprocessed)} parent(s) pending"
                )
            elif not listing_completed:
                stop_reasons.append(
                    "Product details are current for all products discovered "
                    "so far; catalogue discovery is still incomplete"
                )
            if client.detail_failures > detail_failures_before:
                stop_reasons.append(
                    "Some product-detail requests exhausted their retries"
                )

            enriched = detail_store.load_products()
            fallback = [
                product
                for parent_id, product in representatives.items()
                if parent_id in unprocessed
            ]
            final_products = deduplicate([*enriched, *fallback])
            if progress_callback is not None:
                progress_callback(
                    "details",
                    len(processed),
                    detail_parent_count,
                    (
                        f"Enriched {len(processed):,}/"
                        f"{detail_parent_count:,} parents"
                    ),
                )

        completed = listing_completed and details_completed
        excel_path, csv_path = _output_paths(run_config, output_path)
        combined_products = merge_with_existing_sites(
            final_products,
            csv_path,
            replacing_site="nykaa",
        )
        export = export_products(
            combined_products,
            excel_path,
            csv_path,
            sync_database=_database_sync_enabled(run_config),
            status_callback=_export_status_callback(progress_callback),
        )
        return CollectionResult(
            products=final_products,
            export=export,
            failures=(
                client.failures
                + client.page_failures
                + client.product_failures
                + client.detail_failures
            ),
            blocks=client.blocks_encountered,
            requests=client.requests_made,
            completed=completed,
            next_page=next_page,
            stop_reasons=tuple(dict.fromkeys(stop_reasons)),
            resumed_products=resumed_products,
            checkpoint_paths=tuple(
                [
                    store.state_path
                    for store in listing_stores.values()
                ]
                + [detail_store.state_path]
            ),
            listing_products=len(listing_products),
            detail_parents=detail_parent_count,
        )


def _tira_scope_id(categories: Iterable[dict[str, Any]]) -> str:
    identifiers = [str(item["id"]) for item in categories]
    return "full_catalog" if "skin" in identifiers else "_".join(identifiers)


def collect_tira(
    config: dict[str, Any],
    categories: Iterable[str],
    page_limit: int,
    output_path: Path | None = None,
    *,
    resume: bool = True,
    enrich_details: bool | None = None,
    progress_callback: ProgressCallback | None = None,
) -> CollectionResult:
    """Collect Tira catalogue rows and enrich each variant's current price."""
    run_config = copy.deepcopy(config)
    run_config["tira"]["page_limit"] = max(1, int(page_limit))
    detail_config = run_config["tira"].get("details", {})
    detail_config = detail_config if isinstance(detail_config, dict) else {}
    details_enabled = (
        bool(detail_config.get("enabled", True))
        if enrich_details is None
        else bool(enrich_details)
    )

    with TiraClient(
        site_config=run_config["tira"],
        request_config=run_config["request"],
        brands=run_config.get("brands", ()),
    ) as client:
        selected = client.select_categories(list(categories))
        checkpoint_dir = Path(
            str(
                run_config["tira"].get("checkpoint_dir")
                or "data/checkpoints"
            )
        )
        listing_stores = {
            str(category["id"]): CheckpointStore(
                checkpoint_dir,
                site="tira",
                category_id=str(category["id"]),
                start_page=client.start_page,
            )
            for category in selected
        }
        detail_store = DetailCheckpointStore(
            checkpoint_dir,
            site="tira",
            category_id=_tira_scope_id(selected),
        )
        listing_states = {
            category_id: store.load_state()
            for category_id, store in listing_stores.items()
        }
        new_run = (
            not resume
            or (
                all(state.completed for state in listing_states.values())
                and (
                    not details_enabled
                    or detail_store.load_state().completed
                )
            )
        )
        if new_run:
            for store in listing_stores.values():
                store.reset()
            detail_store.reset()

        previous_by_collection = {
            collection_id: store.load_products()
            for collection_id, store in listing_stores.items()
        }
        resumed_products = sum(
            len(products) for products in previous_by_collection.values()
        )
        live_listing_ids = {
            product.product_id
            for products in previous_by_collection.values()
            for product in products
        }
        listing_products: list[Product] = []
        listing_completed = True
        next_page: int | None = None
        stop_reasons: list[str] = []

        if progress_callback is not None:
            progress_callback(
                "listing_products",
                len(live_listing_ids),
                0,
                (
                    f"{len(live_listing_ids):,} Tira SKU rows available "
                    "from checkpoints"
                ),
            )

        for category_index, category in enumerate(selected, start=1):
            collection_id = str(category["id"])
            store = listing_stores[collection_id]
            state = store.load_state()
            previous = previous_by_collection[collection_id]
            if progress_callback is not None:
                progress_callback(
                    "listing",
                    category_index - 1,
                    len(selected),
                    (
                        f"Collection {category_index}/{len(selected)}: "
                        f"{category['name']}"
                    ),
                )

            if not state.completed:
                def save_listing_page(
                    page: int,
                    page_products: Iterable[Product],
                    *,
                    current_store: CheckpointStore = store,
                    category_name: str = str(category["name"]),
                ) -> None:
                    saved_products = list(page_products)
                    current_store.append_page(page, saved_products)
                    live_listing_ids.update(
                        product.product_id for product in saved_products
                    )
                    if progress_callback is not None:
                        progress_callback(
                            "listing_products",
                            len(live_listing_ids),
                            0,
                            (
                                f"{category_name}: page {page:,} saved - "
                                f"{len(live_listing_ids):,} SKU rows "
                                "discovered"
                            ),
                        )

                run = client.scrape_category_resumable(
                    category,
                    start_page=state.next_page,
                    seen_product_ids=(
                        product.product_id for product in previous
                    ),
                    on_page=save_listing_page,
                )
                collection_products = store.load_products()
                if run.completed:
                    store.mark_complete(
                        empty_page=run.next_page,
                        products=collection_products,
                    )
                else:
                    listing_completed = False
                    next_page = (
                        run.next_page
                        if next_page is None
                        else min(next_page, run.next_page)
                    )
                    stop_reasons.append(
                        f"{category['name']} listing: {run.stop_reason}"
                    )
            listing_products.extend(store.load_products())

        listing_products = deduplicate(listing_products)
        if not listing_products:
            raise ValueError(
                "Tira returned no products. Check the selected collections "
                "and Tira configuration."
            )
        if progress_callback is not None:
            progress_callback(
                "listing_products",
                len(listing_products),
                0,
                f"{len(listing_products):,} unique Tira SKU rows discovered",
            )
            progress_callback(
                "listing",
                len(selected),
                len(selected),
                f"Discovered {len(listing_products):,} Tira SKU rows.",
            )

        final_products = listing_products
        detail_count = 0
        details_completed = not details_enabled
        if details_enabled:
            candidates = {
                product.product_id: product
                for product in listing_products
                if (
                    product.mrp is None
                    or product.selling_price is None
                    or not product.sku
                )
            }
            detail_count = len(candidates)
            processed_before = detail_store.load_processed_ids()
            enriched_before = detail_store.load_products()
            resumed_products += len(enriched_before)
            processed_live = set(processed_before)
            priced_ids = {
                product.product_id
                for product in listing_products
                if product.product_id not in candidates
            }
            priced_ids.update(
                product.product_id for product in enriched_before
            )

            if progress_callback is not None:
                progress_callback(
                    "details",
                    len(processed_live),
                    detail_count,
                    (
                        f"Variant prices: {len(processed_live):,}/"
                        f"{detail_count:,} additional variants completed"
                    ),
                )
                progress_callback(
                    "sku_rows",
                    len(priced_ids),
                    len(listing_products),
                    f"{len(priced_ids):,} SKU rows have current prices",
                )

            for product_id, product in candidates.items():
                if product_id in processed_before:
                    continue
                try:
                    enriched = client.fetch_variant_price(product)
                    detail_store.append_parent(product_id, [enriched])
                    processed_live.add(product_id)
                    priced_ids.add(product_id)
                except Exception as exc:
                    client.detail_failures += 1
                    client.logger.exception(
                        "tira_detail product_id=%s failed=%s",
                        product_id,
                        exc,
                    )
                if progress_callback is not None:
                    progress_callback(
                        "details",
                        len(processed_live),
                        detail_count,
                        (
                            f"Variant prices: {len(processed_live):,}/"
                            f"{detail_count:,} additional variants completed"
                        ),
                    )
                    progress_callback(
                        "sku_rows",
                        len(priced_ids),
                        len(listing_products),
                        f"{len(priced_ids):,} SKU rows have current prices",
                    )

            processed = detail_store.load_processed_ids()
            unprocessed = set(candidates) - processed
            details_completed = listing_completed and not unprocessed
            if details_completed:
                detail_store.mark_complete()
            elif unprocessed:
                stop_reasons.append(
                    f"Variant prices: {len(unprocessed)} SKU(s) pending"
                )
            elif not listing_completed:
                stop_reasons.append(
                    "Variant prices are current for all SKUs discovered so "
                    "far; catalogue discovery is still incomplete"
                )
            final_products = deduplicate(
                [*listing_products, *detail_store.load_products()]
            )

        completed = listing_completed and details_completed
        excel_path, csv_path = _output_paths(run_config, output_path)
        combined_products = merge_with_existing_sites(
            final_products,
            csv_path,
            replacing_site="tira",
        )
        export = export_products(
            combined_products,
            excel_path,
            csv_path,
            sync_database=_database_sync_enabled(run_config),
            status_callback=_export_status_callback(progress_callback),
        )
        return CollectionResult(
            products=final_products,
            export=export,
            failures=(
                client.failures
                + client.page_failures
                + client.product_failures
                + client.detail_failures
            ),
            blocks=client.blocks_encountered,
            requests=client.requests_made,
            completed=completed,
            next_page=next_page,
            stop_reasons=tuple(dict.fromkeys(stop_reasons)),
            resumed_products=resumed_products,
            checkpoint_paths=tuple(
                [
                    store.state_path
                    for store in listing_stores.values()
                ]
                + [detail_store.state_path]
            ),
            listing_products=len(listing_products),
            detail_parents=detail_count,
        )


def collect_amazon(
    config: dict[str, Any],
    categories: Iterable[str],
    page_limit: int,
    output_path: Path | None = None,
    *,
    resume: bool = True,
    enrich_details: bool | None = None,
    progress_callback: ProgressCallback | None = None,
) -> CollectionResult:
    """Collect Amazon search results and public product-page details."""
    del enrich_details
    amazon_client_class = _load_amazon_client()
    run_config = copy.deepcopy(config)
    run_config["amazon"]["search_page_limit"] = max(1, int(page_limit))
    requested_categories = list(categories)
    scope_source = (
        "|".join(sorted(name.casefold() for name in requested_categories))
        if requested_categories
        else "all-configured-categories"
    )
    scope_digest = hashlib.sha1(
        scope_source.encode("utf-8")
    ).hexdigest()[:12]
    checkpoint_dir = Path(
        str(
            run_config["amazon"].get("checkpoint_dir")
            or "data/checkpoints"
        )
    )
    detail_store = DetailCheckpointStore(
        checkpoint_dir,
        site="amazon",
        category_id=f"catalog_{scope_digest}",
    )
    if not resume or detail_store.load_state().completed:
        detail_store.reset()
    processed_before = detail_store.load_processed_ids()
    resumed = detail_store.load_products()

    with amazon_client_class(
        site_config=run_config["amazon"],
        request_config=run_config["request"],
        brands=run_config.get("brands", ()),
    ) as client:
        selected = client.select_categories(requested_categories)

        def save_product(product: Product) -> None:
            detail_store.append_parent(product.product_id, [product])

        run = client.scrape(
            selected,
            processed_asins=processed_before,
            on_product=save_product,
            progress_callback=progress_callback,
        )
        final_products = deduplicate(
            [*resumed, *detail_store.load_products()]
        )
        if not final_products:
            if client.blocks_encountered:
                raise ValueError(
                    "Amazon blocked the search pages "
                    f"{client.blocks_encountered} time(s). Check logs for "
                    "amazon_captcha screenshots and retry later or run Chrome "
                    "in visible mode."
                )
            raise ValueError(
                "Amazon search pages contained no discoverable ASINs. "
                "Diagnostic HTML was saved under logs/failures/."
            )
        stop_reasons: list[str] = []
        if run.failed_asins:
            stop_reasons.append(
                f"Amazon product pages: {len(run.failed_asins)} ASIN(s) pending"
            )
        if run.completed:
            detail_store.mark_complete()

        excel_path, csv_path = _output_paths(run_config, output_path)
        combined_products = merge_with_existing_sites(
            final_products,
            csv_path,
            replacing_site="amazon",
        )
        export = export_products(
            combined_products,
            excel_path,
            csv_path,
            sync_database=_database_sync_enabled(run_config),
            status_callback=_export_status_callback(progress_callback),
        )
        return CollectionResult(
            products=final_products,
            export=export,
            failures=(
                client.failures
                + client.page_failures
                + client.product_failures
                + client.detail_failures
            ),
            blocks=client.blocks_encountered,
            requests=client.requests_made,
            completed=run.completed,
            next_page=None,
            stop_reasons=tuple(stop_reasons),
            resumed_products=len(resumed),
            checkpoint_paths=(detail_store.state_path,),
            listing_products=run.discovered_asins,
            detail_parents=len(detail_store.load_processed_ids()),
        )
