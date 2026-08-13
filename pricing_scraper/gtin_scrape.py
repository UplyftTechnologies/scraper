"""Collect only barcodes, for products that do not have one yet.

A normal run re-requests a product's whole detail payload - description,
gallery, ingredients, reviews - to fill any field that is missing. When the
only gap is the barcode, that is far more work than the question deserves.

This mode asks each retailer for the cheapest thing that yields a GTIN:

- **Tira** publishes the barcode in its listing JSON, so a listing sweep is
  enough and no product is opened individually.
- **Nykaa** returns it only from the product-detail endpoint, so this costs one
  request per parent - but only for parents still missing a barcode.
- **Amazon** puts it in the product-information table of the product page. The
  attributes of an earlier run are already stored, so those are read first for
  free and pages are opened only for what remains.

The found barcodes are merged into the stored catalogue and re-exported, so no
other field is touched.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from pricing_scraper.dashboard_service import (
    ProgressCallback,
    _database_sync_enabled,
    _load_amazon_client,
    _output_paths,
    _parent_id,
    _sleeper_kwargs,
)
from pricing_scraper.exporter import ExportResult, export_products, load_products_csv
from pricing_scraper.models import Product, brand_key, normalize_gtin

LOGGER = logging.getLogger("pricing_scraper.gtin")

SITES = ("nykaa", "tira", "amazon")


@dataclass(slots=True)
class GtinResult:
    """What one barcode-only run achieved."""

    site: str
    stored_products: int = 0
    already_had: int = 0
    targeted: int = 0
    found: int = 0
    requests: int = 0
    failures: int = 0
    borrowed: int = 0
    skipped_pages: int = 0
    filtered_out: int = 0
    export: ExportResult | None = None
    found_by: dict[str, str] = field(default_factory=dict)

    @property
    def coverage(self) -> float:
        if not self.stored_products:
            return 0.0
        return (self.already_had + self.found) / self.stored_products * 100

    def summary(self) -> str:
        extra = []
        if self.borrowed:
            extra.append(f"{self.borrowed:,} matched from another platform")
        if self.skipped_pages:
            extra.append(f"{self.skipped_pages:,} page(s) skipped as already read")
        if self.filtered_out:
            extra.append(
                f"{self.filtered_out:,} product(s) outside SCRAPE_BRANDS skipped"
            )
        tail = f"; {', '.join(extra)}" if extra else ""
        return (
            f"{self.site}: {self.found:,} new barcode(s) for {self.targeted:,} "
            f"product(s) without one; coverage "
            f"{self.already_had + self.found:,}/{self.stored_products:,} "
            f"({self.coverage:.1f}%), {self.requests:,} request(s), "
            f"{self.failures:,} failure(s){tail}"
        )


def _report(
    progress_callback: ProgressCallback | None,
    current: int,
    total: int,
    message: str,
) -> None:
    if progress_callback is not None:
        progress_callback("gtin", current, total, message)


def _stored_products(csv_path: Path | None) -> list[Product]:
    if csv_path is None:
        return []
    return load_products_csv(Path(csv_path))


def _amazon_from_attributes(products: Sequence[Product]) -> dict[str, str]:
    """Recover Amazon barcodes from attributes an earlier run already stored.

    Amazon hides the EAN in the model or part number rather than a barcode row,
    so a catalogue scraped before that was read still contains the answer and
    no page has to be opened again.
    """
    from pricing_scraper.clients.amazon import _attribute_gtin

    found: dict[str, str] = {}
    for product in products:
        attributes = product.product_attributes
        if not isinstance(attributes, dict) or not attributes:
            continue
        gtin = _attribute_gtin(attributes)
        if gtin:
            found[product.product_id] = gtin
    return found


def _collect_nykaa_gtins(
    config: dict[str, Any],
    targets: Sequence[Product],
    *,
    limit: int,
    progress_callback: ProgressCallback | None,
    sleeper: Callable[[float], None] | None,
) -> tuple[dict[str, str], int, int]:
    """One detail request per parent still missing a barcode."""
    from pricing_scraper.clients.nykaa import NykaaClient

    parents: dict[str, Product] = {}
    for product in targets:
        parents.setdefault(_parent_id(product), product)
    wanted = list(parents.items())[:limit] if limit else list(parents.items())

    found: dict[str, str] = {}
    failures = 0
    with NykaaClient(
        site_config=config["nykaa"],
        request_config=config["request"],
        brands=config.get("brands", ()),
        **_sleeper_kwargs(sleeper),
    ) as client:
        for index, (parent_id, product) in enumerate(wanted, start=1):
            try:
                for row in client.fetch_product_details(product):
                    gtin = normalize_gtin(row.gtin)
                    if gtin:
                        found[row.product_id] = gtin
            except Exception as exc:  # noqa: BLE001 - one product must not stop the run
                failures += 1
                LOGGER.warning("gtin_detail_failed site=nykaa parent=%s: %s", parent_id, exc)
            _report(
                progress_callback,
                index,
                len(wanted),
                f"Nykaa barcodes: {index:,}/{len(wanted):,} parents, "
                f"{len(found):,} found",
            )
        requests = client.requests_made
    return found, requests, failures


def _collect_tira_gtins(
    config: dict[str, Any],
    *,
    progress_callback: ProgressCallback | None,
    sleeper: Callable[[float], None] | None,
) -> tuple[dict[str, str], int, int]:
    """Tira publishes the barcode in its listing, so sweep listings only."""
    from pricing_scraper.clients.tira import TiraClient

    found: dict[str, str] = {}
    failures = 0
    with TiraClient(
        site_config=config["tira"],
        request_config=config["request"],
        brands=config.get("brands", ()),
        **_sleeper_kwargs(sleeper),
    ) as client:
        categories = client.select_categories([])
        for index, category in enumerate(categories, start=1):
            try:
                run = client.scrape_category_resumable(
                    category,
                    start_page=client.start_page,
                )
            except Exception as exc:  # noqa: BLE001 - one collection must not stop the run
                failures += 1
                LOGGER.warning(
                    "gtin_listing_failed site=tira collection=%s: %s",
                    category.get("id"),
                    exc,
                )
                continue
            for product in run.products:
                gtin = normalize_gtin(product.gtin)
                if gtin:
                    found[product.product_id] = gtin
            _report(
                progress_callback,
                index,
                len(categories),
                f"Tira barcodes: collection {index:,}/{len(categories):,}, "
                f"{len(found):,} found",
            )
        requests = client.requests_made
    return found, requests, failures


def _collect_amazon_gtins(
    config: dict[str, Any],
    targets: Sequence[Product],
    *,
    limit: int,
    open_pages: bool,
    progress_callback: ProgressCallback | None,
    sleeper: Callable[[float], None] | None,
) -> tuple[dict[str, str], int, int]:
    """Open a product page per ASIN, only for those still missing a barcode."""
    if not open_pages or not targets:
        return {}, 0, 0
    amazon_client_class = _load_amazon_client()
    wanted = list(targets)[:limit] if limit else list(targets)

    found: dict[str, str] = {}
    failures = 0
    with amazon_client_class(
        site_config=config["amazon"],
        request_config=config["request"],
        brands=config.get("brands", ()),
        **_sleeper_kwargs(sleeper),
    ) as client:
        for index, product in enumerate(wanted, start=1):
            try:
                fetched, _variants = client.fetch_product(
                    product.product_url or product.product_id,
                    product.categories,
                )
            except Exception as exc:  # noqa: BLE001 - one page must not stop the run
                failures += 1
                LOGGER.warning(
                    "gtin_page_failed site=amazon asin=%s: %s",
                    product.product_id,
                    exc,
                )
                continue
            gtin = normalize_gtin(fetched.gtin)
            if gtin:
                found[product.product_id] = gtin
            _report(
                progress_callback,
                index,
                len(wanted),
                f"Amazon barcodes: {index:,}/{len(wanted):,} pages, "
                f"{len(found):,} found",
            )
        requests = client.requests_made
    return found, requests, failures


def collect_gtins(
    config: dict[str, Any],
    site: str,
    output_path: Path | None = None,
    *,
    only_missing: bool = True,
    limit: int = 0,
    open_amazon_pages: bool = True,
    recheck_pages: bool = False,
    cross_fill: bool = True,
    cross_fill_threshold: float = 0.90,
    write: bool = True,
    sync_database: bool = True,
    progress_callback: ProgressCallback | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> GtinResult:
    """Fill in missing barcodes for one retailer and re-export the catalogue.

    Only the ``gtin`` column is changed. Every other field keeps the value the
    last full collection stored, so this can be run against a catalogue at any
    time without losing content or overwriting fresher prices.

    The barcodes are pushed to Supabase along with the Excel and CSV, exactly
    as a normal collection does, subject to ``database.enabled`` in the config.
    Pass ``sync_database=False`` for a local-only sweep.
    """
    normalized_site = str(site or "").casefold().strip()
    if normalized_site not in SITES:
        raise ValueError(f"GTIN collection supports {', '.join(SITES)}, not {site!r}.")

    run_config = copy.deepcopy(config)
    excel_path, csv_path = _output_paths(run_config, output_path)
    catalogue = _stored_products(csv_path)
    site_products = [
        product
        for product in catalogue
        if product.site.casefold() == normalized_site
    ]
    # The saved catalogue can predate the current SCRAPE_BRANDS list, so it
    # holds brands that are no longer wanted. A barcode sweep reaches products
    # by ID rather than through a listing, which is where the clients normally
    # apply the filter, so it has to be applied here or the run spends requests
    # on brands the collection would have skipped.
    allowed = {
        brand_key(brand) for brand in (run_config.get("brands") or ()) if brand
    }
    if allowed:
        before = len(site_products)
        site_products = [
            product
            for product in site_products
            if brand_key(product.brand) in allowed
        ]
        result_filtered = before - len(site_products)
        if result_filtered:
            LOGGER.info(
                "brand_filter site=%s kept=%s skipped=%s",
                normalized_site,
                len(site_products),
                result_filtered,
            )

    result = GtinResult(site=normalized_site, stored_products=len(site_products))
    result.filtered_out = (
        len([p for p in catalogue if p.site.casefold() == normalized_site])
        - len(site_products)
    )
    if not site_products:
        raise ValueError(
            f"No stored {normalized_site} products to work from"
            + (" for the configured brands." if allowed else ".")
            + " Run a normal collection first so there is a catalogue to fill in."
        )

    result.already_had = sum(1 for item in site_products if item.gtin)
    targets = [
        product
        for product in site_products
        if not (only_missing and product.gtin)
    ]
    result.targeted = len(targets)
    _report(
        progress_callback,
        0,
        len(targets),
        f"{len(targets):,} {normalized_site} product(s) without a barcode",
    )
    if not targets:
        LOGGER.info("Every stored %s product already has a barcode.", normalized_site)
        return result

    found: dict[str, str] = {}
    if normalized_site == "amazon":
        # Free first: the answer is often already in the stored attributes.
        offline = _amazon_from_attributes(targets)
        found.update(offline)
        for product_id in offline:
            result.found_by[product_id] = "stored attributes"
        # Only open a page when there is nothing stored to read. Amazon's
        # barcode lives in the product-information table, which is exactly what
        # the stored attributes already are: re-opening a page whose attributes
        # we hold returns the same answer. Measured on a full sweep, 522 page
        # opens produced zero new barcodes.
        remaining = [
            item
            for item in targets
            if item.product_id not in found
            and (recheck_pages or not item.product_attributes)
        ]
        result.skipped_pages = (
            len(targets) - len(offline) - len(remaining)
        )
        LOGGER.info(
            "Recovered %s Amazon barcode(s) offline; %s page(s) to open, "
            "%s skipped as already read",
            len(offline),
            len(remaining),
            result.skipped_pages,
        )
        online, requests, failures = _collect_amazon_gtins(
            run_config,
            remaining,
            limit=limit,
            open_pages=open_amazon_pages,
            progress_callback=progress_callback,
            sleeper=sleeper,
        )
        found.update(online)
        for product_id in online:
            result.found_by[product_id] = "product page"
        result.requests, result.failures = requests, failures
    elif normalized_site == "nykaa":
        found, result.requests, result.failures = _collect_nykaa_gtins(
            run_config,
            targets,
            limit=limit,
            progress_callback=progress_callback,
            sleeper=sleeper,
        )
        for product_id in found:
            result.found_by[product_id] = "product detail"
    else:
        found, result.requests, result.failures = _collect_tira_gtins(
            run_config,
            progress_callback=progress_callback,
            sleeper=sleeper,
        )
        for product_id in found:
            result.found_by[product_id] = "listing"

    applied = _apply(catalogue, found, site=normalized_site, only_missing=only_missing)
    result.found = applied

    if cross_fill:
        borrowed = _cross_fill(
            catalogue,
            site=normalized_site,
            threshold=cross_fill_threshold,
            allowed_brands=allowed,
        )
        for product_id, (gtin, donor) in borrowed.items():
            result.found_by[product_id] = f"matched {donor}"
        result.borrowed = len(borrowed)
        result.found += len(borrowed)
        applied += len(borrowed)
    LOGGER.info("%s", result.summary())

    if write and applied:
        result.export = export_products(
            catalogue,
            excel_path,
            csv_path,
            sync_database=sync_database and _database_sync_enabled(run_config),
            status_callback=(
                (lambda message: _report(progress_callback, 0, 0, message))
                if progress_callback is not None
                else None
            ),
        )
    return result


def _cross_fill(
    catalogue: Sequence[Product],
    *,
    site: str,
    threshold: float,
    allowed_brands: set[str] | None = None,
) -> dict[str, tuple[str, str]]:
    """Give a product the barcode of the same product on another platform.

    Amazon India publishes no barcode of its own for most beauty products, so
    the only remaining source is a platform that does. A match is the same
    physical product, so its barcode applies.

    The threshold is deliberately high. Measured against pairs where two
    platforms each published a barcode, matches at 0.90 agreed every time
    (25/25), while 0.70 disagreed in 3 of 29 - those looser pairs are different
    pack sizes of the same product, and copying a barcode across them would
    state something false about the SKU.
    """
    from pricing_scraper.comparison import match_products

    report = match_products(list(catalogue), threshold=threshold)
    borrowed: dict[str, tuple[str, str]] = {}
    for match in report.matches:
        target = match.members.get(site)
        if target is None or target.product.gtin:
            continue
        if allowed_brands and brand_key(target.product.brand) not in allowed_brands:
            continue
        for donor_site, item in sorted(match.members.items()):
            if donor_site == site or not item.gtin:
                continue
            target.product.gtin = item.gtin
            borrowed[target.product.product_id] = (item.gtin, donor_site)
            break
    if borrowed:
        LOGGER.info(
            "cross_filled site=%s count=%s threshold=%s",
            site,
            len(borrowed),
            threshold,
        )
    return borrowed


def _apply(
    catalogue: Iterable[Product],
    found: dict[str, str],
    *,
    site: str,
    only_missing: bool,
) -> int:
    """Write the barcodes onto the stored rows, leaving every other field."""
    applied = 0
    for product in catalogue:
        if product.site.casefold() != site:
            continue
        gtin = found.get(product.product_id)
        if not gtin or gtin == product.gtin:
            continue
        if only_missing and product.gtin:
            continue
        product.gtin = gtin
        applied += 1
    return applied
