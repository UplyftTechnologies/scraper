"""Propagate barcodes between platforms for products that are the same item.

A product sold on all three platforms often carries a barcode on only one of
them: Nykaa publishes one per SKU, Tira for about two thirds of its catalogue,
and Amazon India publishes none at all. The barcode is a property of the
product, not of the shop, so once any platform states it the others can be
filled in.

This reads the catalogue back out of Supabase, matches products across
platforms, and writes the barcode onto the rows that are missing one. It
changes exactly one column and never touches a product that already has a
barcode of its own.

Matching is the same conservative rule the comparison sheet uses: brand, pack
size, product form, active-ingredient strength and single-versus-kit must all
agree, and the titles must share real words. The default threshold is 0.90
because that is where the rule was measured to be right: checked against pairs
where two platforms had each published their own barcode, 0.90 agreed 25 times
out of 25, while 0.70 disagreed in 3 of 29 - those looser pairs being different
pack sizes of the same product.

Two platforms publishing *different* barcodes for a matched pair is not a
barcode problem, it is proof the match is wrong. Those are reported as
conflicts and nothing is written for them.

This is deliberately rule-based rather than a language model. Every decision
here has to be explainable and repeatable - a wrong barcode silently
misidentifies a product - and the rules can be measured against ground truth,
which a model's judgement cannot be.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from pricing_scraper.comparison import MATCH_COLUMNS, _product_from_row, match_products
from pricing_scraper.models import Product, brand_key

LOGGER = logging.getLogger("pricing_scraper.supervisor")

SITES = ("nykaa", "tira", "amazon")
# Where a barcode is trusted from, best first. Nykaa publishes one per SKU;
# Amazon's is inferred from the model-number row, so it is the last resort.
DONOR_ORDER = ("nykaa", "tira", "amazon", "manual")
DEFAULT_THRESHOLD = 0.90


@dataclass(frozen=True, slots=True)
class Propagation:
    """One barcode copied from the platform that published it."""

    site: str
    product_id: str
    brand: str
    product_name: str
    gtin: str
    donor_site: str
    donor_product_id: str
    confidence: float
    size: str


@dataclass(frozen=True, slots=True)
class Conflict:
    """A matched pair whose platforms disagree about the barcode."""

    brand: str
    product_name: str
    left_site: str
    left_gtin: str
    right_site: str
    right_gtin: str
    confidence: float


@dataclass(slots=True)
class SupervisorResult:
    """What one reconciliation pass found and wrote."""

    considered: int = 0
    had_gtin: int = 0
    matches: int = 0
    filled: list[Propagation] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    written: int = 0
    failures: int = 0
    dry_run: bool = True
    by_site: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        action = "would fill" if self.dry_run else "filled"
        spread = ", ".join(
            f"{site}+{count}" for site, count in sorted(self.by_site.items())
        )
        return (
            f"{self.considered:,} products, {self.had_gtin:,} already had a "
            f"barcode; {self.matches:,} cross-platform matches; {action} "
            f"{len(self.filled):,} ({spread or 'none'}); "
            f"{len(self.conflicts):,} conflict(s)"
        )


def load_catalogue(
    store: Any,
    *,
    sites: Sequence[str] = SITES,
    allowed_brands: set[str] | None = None,
) -> list[Product]:
    """Read the stored catalogue for each platform out of the database."""
    products: list[Product] = []
    for site in sites:
        rows = store.fetch_site_products(site, columns=MATCH_COLUMNS)
        for row in rows:
            try:
                product = _product_from_row(row)
            except Exception:  # noqa: BLE001 - a bad row is simply skipped
                continue
            if allowed_brands and brand_key(product.brand) not in allowed_brands:
                continue
            products.append(product)
        LOGGER.info("supervisor_loaded site=%s rows=%s", site, len(rows))
    return products


def plan_propagation(
    products: Sequence[Product],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    donor_order: Sequence[str] = DONOR_ORDER,
) -> SupervisorResult:
    """Decide which products should inherit a barcode, without writing."""
    result = SupervisorResult(considered=len(products))
    result.had_gtin = sum(1 for product in products if product.gtin)

    report = match_products(list(products), threshold=threshold)
    result.matches = len(report.matches)

    for match in report.matches:
        with_barcode = {
            site: item for site, item in match.members.items() if item.gtin
        }
        if not with_barcode:
            continue

        distinct = {item.gtin for item in with_barcode.values()}
        if len(distinct) > 1:
            # Two shops publishing different barcodes for what the rule called
            # one product means the rule was wrong here. Never guess between
            # them; report the pair so the match can be reviewed.
            sites = sorted(with_barcode)
            left, right = sites[0], sites[1]
            result.conflicts.append(
                Conflict(
                    brand=match.anchor.product.brand,
                    product_name=match.anchor.name,
                    left_site=left,
                    left_gtin=with_barcode[left].gtin,
                    right_site=right,
                    right_gtin=with_barcode[right].gtin,
                    confidence=round(match.confidence, 3),
                )
            )
            continue

        donor_site = next(
            (site for site in donor_order if site in with_barcode),
            sorted(with_barcode)[0],
        )
        donor = with_barcode[donor_site]
        for site, item in sorted(match.members.items()):
            if item.gtin:
                continue
            result.filled.append(
                Propagation(
                    site=site,
                    product_id=item.product.product_id,
                    brand=item.product.brand,
                    product_name=item.name,
                    gtin=donor.gtin,
                    donor_site=donor_site,
                    donor_product_id=donor.product.product_id,
                    confidence=round(match.confidence, 3),
                    size=item.size.label() if item.size else "",
                )
            )
            result.by_site[site] = result.by_site.get(site, 0) + 1
    return result


def apply_propagation(
    store: Any,
    filled: Iterable[Propagation],
) -> tuple[int, int]:
    """Write each inherited barcode, touching only the ``gtin`` column."""
    written = 0
    failures = 0
    for item in filled:
        try:
            store._patch(
                store.products_table,
                params={
                    "site": f"eq.{item.site}",
                    "product_id": f"eq.{item.product_id}",
                },
                values={"gtin": item.gtin},
            )
            written += 1
        except Exception as exc:  # noqa: BLE001 - one row must not stop the pass
            failures += 1
            LOGGER.warning(
                "supervisor_write_failed site=%s product_id=%s error=%s",
                item.site,
                item.product_id,
                exc,
            )
    return written, failures


def reconcile_gtins(
    *,
    threshold: float = DEFAULT_THRESHOLD,
    sites: Sequence[str] = SITES,
    brands: Iterable[str] = (),
    dry_run: bool = True,
    store: Any = None,
) -> SupervisorResult:
    """Fill missing barcodes from the platform that published them.

    Defaults to a dry run: the plan is worked out and reported, and nothing is
    written until ``dry_run=False``.
    """
    if store is None:
        from pricing_scraper.database import (
            DatabaseConfigurationError,
            SupabaseCatalogStore,
        )

        store, _required = SupabaseCatalogStore.from_environment()
        if store is None:
            raise DatabaseConfigurationError(
                "The supervisor reads the catalogue from Supabase. Set "
                "SUPABASE_URL and a server-side key in .env."
            )

    allowed = {brand_key(brand) for brand in brands if brand_key(brand)}
    products = load_catalogue(store, sites=sites, allowed_brands=allowed or None)
    result = plan_propagation(products, threshold=threshold)
    result.dry_run = dry_run

    if not dry_run and result.filled:
        result.written, result.failures = apply_propagation(store, result.filled)
    LOGGER.info("supervisor %s", result.summary())
    return result
