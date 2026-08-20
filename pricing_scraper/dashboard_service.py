"""Application service used by the Streamlit dashboard."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

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
from pricing_scraper.db_sink import open_sink
from pricing_scraper.models import Product
from pricing_scraper.refresh import (
    RefreshPlan,
    RefreshPolicy,
    build_plan,
    decide,
    load_known_products,
    plan_for_site,
)

LOGGER = logging.getLogger("pricing_scraper.dashboard_service")

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


def hosted_deployment() -> bool:
    """Whether this process is the hosted container rather than a local run."""
    return os.getenv("HOSTED_DASHBOARD", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _write_excel_here() -> bool:
    """Build the workbook locally, skip it on the hosted container.

    The workbook holds every cell in memory and peaks near 500 MB for a large
    catalogue, which a small instance cannot afford. It is also written to a
    temporary disk there, so it would not survive the next restart. Hosted runs
    keep the CSV and rely on the database.
    """
    return not hosted_deployment()


def _open_run_sink(
    site: str,
    run_config: dict[str, Any],
) -> tuple[Any, str]:
    """Open a streaming database sink and register the run, when configured."""
    if not _database_sync_enabled(run_config):
        return None, ""
    run_id = ""
    try:
        from pricing_scraper.database import SupabaseCatalogStore

        store, _required = SupabaseCatalogStore.from_environment()
        if store is not None:
            run_id = store.start_run(
                site, metadata={"mode": "dashboard", "streaming": True}
            )
    except Exception as exc:  # noqa: BLE001 - bookkeeping must not block a run
        LOGGER.info("Could not register the run in the database (%s).", exc)
    sink = open_sink(site, enabled=True, run_id=run_id)
    return sink, run_id


def _close_sink(sink: Any, *, complete_sweep: bool) -> Any:
    """Flush the tail of the stream and age missing products when appropriate."""
    if sink is None:
        return None
    return sink.close(complete_sweep=complete_sweep)


def _needs_final_sync(
    run_config: dict[str, Any], sink: Any, exporting: int = 0
) -> bool:
    """Whether the export still has to push the whole catalogue.

    Streaming covers a run that actually scrapes: every product goes up in
    batches as it is collected. Two cases still need the full sync.

    A failed batch, where the sync becomes the reconciliation pass. And a run
    that reused most of its work from checkpoints - it streams almost nothing,
    because nothing was newly scraped, so skipping the sync would leave the
    export in the files and never in the database.
    """
    if not _database_sync_enabled(run_config):
        return False
    if sink is None:
        return True
    if sink.result.failures:
        return True
    return sink.result.products_written < exporting


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


def _sleeper_kwargs(
    sleeper: Callable[[float], None] | None,
) -> dict[str, Callable[[float], None]]:
    """Pass a sleeper to a client only when the caller supplied one.

    The background worker supplies one that raises when the dashboard asks the
    run to stop, so a request waiting out a backoff does not have to finish
    before the stop takes effect. Direct callers keep ``time.sleep``.
    """
    return {"sleeper": sleeper} if sleeper is not None else {}


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
    listing_products: int
    detail_parents: int


def _full_catalog_partitions(
    selected: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Expand the all-skincare scope into smaller API result windows."""
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


@dataclass(frozen=True, slots=True)
class _ReuseResult:
    """What a refresh check let this run skip."""

    parent_ids: set[str]
    product_ids: set[str]
    plan: RefreshPlan | None


def _reuse_current_details(
    *,
    site: str,
    representatives: Mapping[str, Product],
    listing_products: Sequence[Product],
    detail_store: DetailCheckpointStore,
    processed_before: set[str],
    policy: RefreshPolicy,
    csv_path: Path | None,
    use_database: bool,
    parent_of: Callable[[Product], str],
) -> _ReuseResult:
    """Commit stored detail for products that do not need requesting again.

    A parent is only reused when every SKU beneath it is current: one changed
    size means the whole detail response is worth fetching, because that is the
    unit the retailer returns.

    The stored rows are appended to the detail checkpoint exactly as a real
    fetch would, so completion, export, and resume logic need no special case.
    """
    empty = _ReuseResult(set(), set(), None)
    if not policy.enabled or not representatives:
        return empty

    plan = plan_for_site(
        site,
        list(listing_products),
        policy=policy,
        csv_path=csv_path,
        use_database=use_database,
    )
    if not plan.known:
        return _ReuseResult(set(), set(), plan)

    stored = plan.stored_products()
    by_parent: dict[str, list[Product]] = {}
    for product in stored.values():
        by_parent.setdefault(parent_of(product), []).append(product)

    # A parent needs work when any of its discovered SKUs does.
    needs_parent: set[str] = set()
    for product in listing_products:
        if plan.needs(product.product_id):
            needs_parent.add(parent_of(product))

    parent_ids: set[str] = set()
    product_ids: set[str] = set()
    for parent_id in representatives:
        if parent_id in processed_before or parent_id in needs_parent:
            continue
        rows = by_parent.get(parent_id)
        if not rows:
            # Nothing stored to reuse, so it still has to be requested.
            continue
        detail_store.append_parent(parent_id, rows)
        parent_ids.add(parent_id)
        product_ids.update(row.product_id for row in rows)
    if parent_ids:
        LOGGER.info(
            "refresh_reused site=%s parents=%s rows=%s source=%s",
            site,
            len(parent_ids),
            len(product_ids),
            plan.source,
        )
    return _ReuseResult(parent_ids, product_ids, plan)


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
    refresh_only_stale: bool | None = None,
    sample_limit: int = 0,
    progress_callback: ProgressCallback | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> CollectionResult:
    """Collect Nykaa listings and optionally enrich every parent/SKU detail."""
    run_config = copy.deepcopy(config)
    refresh_policy = RefreshPolicy.from_config(run_config, enabled=refresh_only_stale)
    run_config["nykaa"]["page_limit"] = max(1, int(page_limit))
    sink, _run_id = _open_run_sink("nykaa", run_config)
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
        **_sleeper_kwargs(sleeper),
    ) as client:
        requested = client.select_categories(list(categories))
        selected = _full_catalog_partitions(requested)
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
                    if sink is not None:
                        sink.add(saved_products)
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
        if sample_limit:
            # A smoke test wants proof the pipeline works, not the catalogue.
            listing_products = listing_products[:sample_limit]
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

            # Reuse the stored detail for parents that are complete and
            # current, committing it to the checkpoint as though it had just
            # been fetched. Everything downstream then behaves identically to a
            # full run, minus the requests.
            reused = _reuse_current_details(
                site="nykaa",
                representatives=representatives,
                listing_products=listing_products,
                detail_store=detail_store,
                processed_before=processed_before,
                policy=refresh_policy,
                csv_path=_output_paths(run_config, output_path)[1],
                use_database=_database_sync_enabled(run_config),
                parent_of=_parent_id,
            )
            processed_live |= reused.parent_ids
            live_sku_ids |= reused.product_ids
            pending = [
                (parent_id, product)
                for parent_id, product in representatives.items()
                if parent_id not in processed_before
                and parent_id not in reused.parent_ids
            ]
            if reused.plan is not None and progress_callback is not None:
                progress_callback(
                    "details",
                    len(processed_live),
                    detail_parent_count,
                    f"Refresh check: {reused.plan.summary()}",
                )
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
                    if sink is not None:
                        sink.add(detail_products)
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
            if sample_limit:
                # The detail checkpoint replays everything an earlier run
                # stored, so truncating the listing alone is not enough.
                final_products = final_products[:sample_limit]
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
        _close_sink(sink, complete_sweep=completed)
        export = export_products(
            combined_products,
            excel_path,
            csv_path,
            sync_database=_needs_final_sync(
                run_config, sink, len(combined_products)
            ),
            write_excel=_write_excel_here(),
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
    refresh_only_stale: bool | None = None,
    sample_limit: int = 0,
    progress_callback: ProgressCallback | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> CollectionResult:
    """Collect Tira catalogue rows and enrich each variant's current price."""
    run_config = copy.deepcopy(config)
    refresh_policy = RefreshPolicy.from_config(run_config, enabled=refresh_only_stale)
    run_config["tira"]["page_limit"] = max(1, int(page_limit))
    sink, _run_id = _open_run_sink("tira", run_config)
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
        **_sleeper_kwargs(sleeper),
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
                    if sink is not None:
                        sink.add(saved_products)
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
        if sample_limit:
            # A smoke test wants proof the pipeline works, not the catalogue.
            listing_products = listing_products[:sample_limit]
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
            processed_before = detail_store.load_processed_ids()
            enriched_before = detail_store.load_products()
            processed_live = set(processed_before)

            # Reuse the stored price for variants nothing has changed about.
            # Tira only asks for variants whose listing row has no price, so
            # this mostly helps a re-run that already resolved them once.
            reused = _reuse_current_details(
                site="tira",
                representatives=dict(candidates),
                listing_products=listing_products,
                detail_store=detail_store,
                processed_before=processed_before,
                policy=refresh_policy,
                csv_path=_output_paths(run_config, output_path)[1],
                use_database=_database_sync_enabled(run_config),
                parent_of=lambda product: product.product_id,
            )
            processed_live |= reused.parent_ids
            for product_id in reused.parent_ids:
                candidates.pop(product_id, None)
            detail_count = len(candidates)

            priced_ids = {
                product.product_id
                for product in listing_products
                if product.product_id not in candidates
            }
            priced_ids.update(
                product.product_id for product in enriched_before
            )
            priced_ids |= reused.product_ids

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
                    if sink is not None:
                        sink.add([enriched])
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
            if sample_limit:
                final_products = final_products[:sample_limit]

        completed = listing_completed and details_completed
        excel_path, csv_path = _output_paths(run_config, output_path)
        combined_products = merge_with_existing_sites(
            final_products,
            csv_path,
            replacing_site="tira",
        )
        _close_sink(sink, complete_sweep=completed)
        export = export_products(
            combined_products,
            excel_path,
            csv_path,
            sync_database=_needs_final_sync(
                run_config, sink, len(combined_products)
            ),
            write_excel=_write_excel_here(),
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
            listing_products=len(listing_products),
            detail_parents=detail_count,
        )


def _current_amazon_products(
    policy: RefreshPolicy,
    csv_path: Path | None,
    *,
    use_database: bool,
) -> dict[str, Product]:
    """Stored Amazon products that are complete and inside the refresh window."""
    if not policy.enabled:
        return {}
    known, source = load_known_products(
        "amazon", csv_path=csv_path, use_database=use_database
    )
    if not known:
        return {}
    plan = build_plan(
        [],
        known,
        policy=policy,
        known_source=source,
    )
    stored = plan.stored_products()
    return {
        product_id: product
        for product_id, product in stored.items()
        if not decide(product, known[product_id], policy=policy).needed
    }


def collect_amazon(
    config: dict[str, Any],
    categories: Iterable[str],
    page_limit: int,
    output_path: Path | None = None,
    *,
    resume: bool = True,
    enrich_details: bool | None = None,
    refresh_only_stale: bool | None = None,
    sample_limit: int = 0,
    progress_callback: ProgressCallback | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> CollectionResult:
    """Collect Amazon search results and public product-page details."""
    del enrich_details
    amazon_client_class = _load_amazon_client()
    run_config = copy.deepcopy(config)
    refresh_policy = RefreshPolicy.from_config(run_config, enabled=refresh_only_stale)
    run_config["amazon"]["search_page_limit"] = max(1, int(page_limit))
    if sample_limit:
        # A smoke test opens a handful of pages, not the catalogue. Brand
        # search alone would be 207 searches before a single product page.
        run_config["amazon"]["max_products_per_category"] = sample_limit
        run_config["amazon"]["brand_search"] = {"enabled": False}
    sink, _run_id = _open_run_sink("amazon", run_config)
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

    # Amazon costs a full page load per product, so the refresh check saves the
    # most here. There is no cheap listing to compare against, so an ASIN is
    # skipped purely on its stored record being complete and recent; a price
    # change is caught by the next refresh window rather than the same day.
    current = _current_amazon_products(
        refresh_policy,
        _output_paths(run_config, output_path)[1],
        use_database=_database_sync_enabled(run_config),
    )
    skip_asins = set(processed_before)
    for product_id, product in current.items():
        if product_id in processed_before:
            continue
        detail_store.append_parent(product_id, [product])
        skip_asins.add(product_id)
    if current:
        LOGGER.info("refresh_reused site=amazon products=%s", len(current))

    with amazon_client_class(
        site_config=run_config["amazon"],
        request_config=run_config["request"],
        brands=run_config.get("brands", ()),
        **_sleeper_kwargs(sleeper),
    ) as client:
        selected = client.select_categories(requested_categories)

        def save_product(product: Product) -> None:
            detail_store.append_parent(product.product_id, [product])
            if sink is not None:
                sink.add([product])

        run = client.scrape(
            selected,
            processed_asins=skip_asins,
            on_product=save_product,
            progress_callback=progress_callback,
            max_products=sample_limit,
        )
        final_products = deduplicate(
            [*resumed, *detail_store.load_products()]
        )
        if sample_limit:
            # Amazon has no listing pass to truncate: its products come from
            # the checkpoint, which replays every ASIN an earlier run stored.
            # Capping discovery alone let a sample return 793 products.
            final_products = final_products[:sample_limit]
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
        _close_sink(sink, complete_sweep=run.completed)
        export = export_products(
            combined_products,
            excel_path,
            csv_path,
            sync_database=_needs_final_sync(
                run_config, sink, len(combined_products)
            ),
            write_excel=_write_excel_here(),
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
            listing_products=run.discovered_asins,
            detail_parents=len(detail_store.load_processed_ids()),
        )


# The three plain-HTTP storefronts differ only in which client reads them:
# each discovers its own catalogue in one request and returns finished
# products, so they need none of the category and checkpoint machinery the
# older retailers do.
STOREFRONT_CLIENTS: dict[str, str] = {
    "purplle": "PurplleClient",
    "kindlife": "KindlifeClient",
    "broadway": "BroadwayClient",
}


def collect_storefront(
    site: str,
    config: dict[str, Any],
    *,
    output_path: str | Path | None = None,
    sample_limit: int = 0,
    progress_callback: ProgressCallback | None = None,
) -> CollectionResult:
    """Collect one storefront, then export and synchronize exactly as the
    established retailers do.

    Products stream into the database sink as they are read, so a run that
    fails part way keeps what it collected, and the export at the end
    reconciles the whole catalogue.
    """
    import importlib

    if site not in STOREFRONT_CLIENTS:
        raise ValueError(f"{site!r} is not a storefront client.")
    module = importlib.import_module(f"pricing_scraper.clients.{site}")
    client_class = getattr(module, STOREFRONT_CLIENTS[site])

    run_config = copy.deepcopy(config)
    sink, _run_id = _open_run_sink(site, run_config)
    seen = 0

    def on_product(product: Product) -> None:
        nonlocal seen
        seen += 1
        if sink is not None:
            sink.add([product])
        if progress_callback is not None:
            # The reporter takes positional arguments. Passing a mapping raised
            # a TypeError inside the per-product handler, which counted every
            # product as a failure and skipped the sample limit with it.
            progress_callback("products", seen, 0, f"{seen:,} products")

    client = client_class(
        run_config.get(site) or {},
        run_config["request"],
        brands=run_config.get("brands") or [],
    )
    completed = True
    try:
        with client:
            products = client.collect(
                on_product=on_product, max_products=sample_limit
            )
    except Exception:
        # The sink already holds whatever was read, and the run is reported
        # incomplete so nothing ages products it never got to.
        completed = False
        _close_sink(sink, complete_sweep=False)
        raise

    excel_path, csv_path = _output_paths(run_config, output_path)
    combined = merge_with_existing_sites(products, csv_path, replacing_site=site)
    # A sample is a smoke test, not a sweep, so it must never be treated as a
    # complete view of the catalogue: doing so would age every product it
    # skipped towards inactive.
    complete_sweep = completed and not sample_limit
    _close_sink(sink, complete_sweep=complete_sweep)
    export = export_products(
        combined,
        excel_path,
        csv_path,
        sync_database=_needs_final_sync(run_config, sink, len(combined)),
        write_excel=_write_excel_here(),
        status_callback=_export_status_callback(progress_callback),
    )
    return CollectionResult(
        products=products,
        export=export,
        failures=client.failures + getattr(client, "product_failures", 0),
        blocks=client.blocks_encountered,
        requests=client.requests_made,
        completed=complete_sweep,
        next_page=None,
        stop_reasons=(),
        listing_products=len(products),
        detail_parents=0,
    )
