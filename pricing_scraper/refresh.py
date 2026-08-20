"""Decide which products a run actually has to request.

A collection normally spends almost all of its time on product-detail requests,
and most of those return what is already stored. This module compares the
listing a run just discovered against the catalogue already held - in Supabase
when it is configured, otherwise in the combined CSV export - and asks for
details only when there is a reason to.

A product is requested when it is new, when the listing shows it changed, when
its stored record is missing detail content, or when that record is older than
the configured refresh window. Everything else is left alone.

Nothing here may ever block a run: if the stored catalogue cannot be read, the
plan simply treats every product as needing a refresh, which is the behavior
the scraper had before this existed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from pricing_scraper.automation import source_fingerprint
from pricing_scraper.models import Product

LOGGER = logging.getLogger("pricing_scraper.refresh")

# Fields that a successful detail request always fills. A stored row missing
# these was never enriched, so it is worth requesting again.
#
# Deliberately narrow: `ingredients`, `how_to_use` and `gtin` are legitimately
# absent for many real products, so requiring them would mark those products
# incomplete on every run and re-request them forever. Use the GTIN-only mode
# to fill barcodes, and widen `refresh.required_fields` in config.yaml if you
# want a stricter definition.
DEFAULT_REQUIRED_FIELDS = ("description", "image_urls")

# Columns the decision needs; keeps the database read narrow.
KNOWN_COLUMNS = (
    "site",
    "product_id",
    "parent_product_id",
    "sku",
    "gtin",
    "brand",
    "product_name",
    "variant",
    "categories",
    "source_categories",
    "mrp",
    "selling_price",
    "discount_pct",
    "rating",
    "rating_count",
    "review_count",
    "in_stock",
    "product_url",
    "image_url",
    "image_urls",
    "description",
    "ingredients",
    "how_to_use",
    "scraped_at",
)

NEW = "new"
CHANGED = "changed"
INCOMPLETE = "incomplete"
STALE = "stale"
FRESH = "fresh"



# Fields that exist only when a site collects descriptive copy.
_CONTENT_FIELDS = frozenset(
    {
        "description",
        "description_html",
        "key_features",
        "ingredients",
        "how_to_use",
        "top_reviews",
        "special_features",
        "key_ingredients",
    }
)


def _site_collects_content(config: Mapping[str, Any]) -> bool:
    """Is any configured site still gathering descriptive copy?

    Only Amazon can be told to skip it today. The check is written over the
    whole config rather than one key so that a second site gaining the same
    switch does not silently reintroduce the forever-incomplete problem.
    """
    switches = [
        section.get("collect_content")
        for section in config.values()
        if isinstance(section, Mapping) and "collect_content" in section
    ]
    if not switches:
        return True
    return any(bool(value) for value in switches)


@dataclass(frozen=True, slots=True)
class RefreshPolicy:
    """When a stored product is considered worth requesting again."""

    enabled: bool = True
    refresh_days: int = 30
    required_fields: tuple[str, ...] = DEFAULT_REQUIRED_FIELDS

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any],
        *,
        enabled: bool | None = None,
    ) -> "RefreshPolicy":
        """Read the policy from the ``refresh`` section, falling back sanely.

        ``automation.detail_refresh_days`` is reused when ``refresh`` does not
        set its own window, so the dashboard and the nightly jobs age products
        out on the same schedule unless deliberately separated.
        """
        section = config.get("refresh")
        section = section if isinstance(section, Mapping) else {}
        automation = config.get("automation")
        automation = automation if isinstance(automation, Mapping) else {}
        days = section.get("refresh_days", automation.get("detail_refresh_days", 30))
        fields = section.get("required_fields")
        if isinstance(fields, (list, tuple)) and fields:
            required = tuple(str(name).strip() for name in fields if str(name).strip())
        else:
            required = DEFAULT_REQUIRED_FIELDS
        if not _site_collects_content(config):
            # A field the scrape no longer collects can never be filled, so
            # requiring it would mark every product incomplete and re-request
            # the whole catalogue on every run - the exact opposite of what
            # switching the copy off was meant to achieve.
            required = tuple(
                name for name in required if name not in _CONTENT_FIELDS
            )
        return cls(
            enabled=(
                bool(section.get("enabled", True)) if enabled is None else bool(enabled)
            ),
            refresh_days=max(0, int(days)),
            required_fields=required,
        )


@dataclass(frozen=True, slots=True)
class RefreshDecision:
    """Whether one product needs requesting, and why."""

    needed: bool
    reason: str


@dataclass(slots=True)
class RefreshPlan:
    """The per-product decisions for one run, with counts for reporting."""

    policy: RefreshPolicy
    decisions: dict[str, RefreshDecision] = field(default_factory=dict)
    known: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    known_rows: int = 0
    source: str = "none"

    def needs(self, product_id: str) -> bool:
        decision = self.decisions.get(product_id)
        return True if decision is None else decision.needed

    def stored_products(self) -> dict[str, Product]:
        """Rebuild the stored rows as products, for reuse without a request.

        A skipped product still has to reach the export, or refreshing one
        retailer would quietly strip the descriptions and galleries of every
        product it decided not to re-request.
        """
        from pricing_scraper.comparison import _product_from_row

        rebuilt: dict[str, Product] = {}
        for product_id, row in self.known.items():
            try:
                rebuilt[product_id] = _product_from_row(row)
            except Exception:  # noqa: BLE001 - a bad row is simply re-requested
                continue
        return rebuilt

    @property
    def counts(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for decision in self.decisions.values():
            totals[decision.reason] = totals.get(decision.reason, 0) + 1
        return totals

    @property
    def to_request(self) -> int:
        return sum(1 for item in self.decisions.values() if item.needed)

    @property
    def skipped(self) -> int:
        return sum(1 for item in self.decisions.values() if not item.needed)

    def summary(self) -> str:
        parts = [
            f"{reason}={count}"
            for reason, count in sorted(
                self.counts.items(), key=lambda item: -item[1]
            )
        ]
        return (
            f"{self.to_request:,} to request, {self.skipped:,} already current "
            f"({', '.join(parts) or 'no products'}; known={self.known_rows:,} "
            f"from {self.source})"
        )


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (
        parsed.astimezone(timezone.utc)
        if parsed.tzinfo
        else parsed.replace(tzinfo=timezone.utc)
    )


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict, set)):
        return not value
    return False


def load_known_products(
    site: str,
    *,
    csv_path: Path | None,
    use_database: bool = True,
) -> tuple[dict[str, dict[str, Any]], str]:
    """Return the stored rows for one site, keyed by product ID.

    Supabase is preferred because a hosted run has no local export to compare
    against. Any failure degrades to an empty result: a freshness check that
    cannot read the catalogue must cost a few extra requests, never the run.
    """
    if use_database:
        try:
            from pricing_scraper.database import SupabaseCatalogStore

            store, _ = SupabaseCatalogStore.from_environment()
        except Exception as exc:  # noqa: BLE001 - never block a run
            LOGGER.info("Database unavailable for the refresh check (%s).", exc)
            store = None
        if store is not None:
            try:
                rows = store.fetch_site_products(site, columns=KNOWN_COLUMNS)
                known = {
                    str(row.get("product_id")): dict(row)
                    for row in rows
                    if row.get("product_id")
                }
                if known:
                    return known, "database"
            except Exception as exc:  # noqa: BLE001 - never block a run
                LOGGER.warning(
                    "Database read failed for the refresh check (%s).", exc
                )

    if csv_path is not None:
        try:
            from pricing_scraper.exporter import load_products_csv

            products = load_products_csv(Path(csv_path))
        except Exception as exc:  # noqa: BLE001 - never block a run
            LOGGER.warning("Could not read %s for the refresh check (%s).", csv_path, exc)
            return {}, "none"
        known = {
            product.product_id: product.to_dict()
            for product in products
            if product.site.casefold() == site.casefold()
        }
        if known:
            return known, "csv"
    return {}, "none"


def decide(
    product: Product,
    stored: Mapping[str, Any] | None,
    *,
    policy: RefreshPolicy,
    now: datetime | None = None,
) -> RefreshDecision:
    """Decide whether one product needs a detail request."""
    if not policy.enabled:
        return RefreshDecision(True, NEW)
    if stored is None:
        return RefreshDecision(True, NEW)

    stored_fingerprint = str(stored.get("source_fingerprint") or "")
    if not stored_fingerprint:
        stored_fingerprint = source_fingerprint(stored)
    if stored_fingerprint != source_fingerprint(product):
        return RefreshDecision(True, CHANGED)

    for name in policy.required_fields:
        if _is_empty(stored.get(name)):
            return RefreshDecision(True, INCOMPLETE)

    if policy.refresh_days:
        scraped = _parse_timestamp(
            stored.get("last_detail_scraped_at") or stored.get("scraped_at")
        )
        moment = now or datetime.now(timezone.utc)
        if scraped is None or scraped < moment - timedelta(days=policy.refresh_days):
            return RefreshDecision(True, STALE)

    return RefreshDecision(False, FRESH)


def build_plan(
    products: Iterable[Product],
    known: Mapping[str, Mapping[str, Any]],
    *,
    policy: RefreshPolicy,
    now: datetime | None = None,
    known_source: str = "none",
) -> RefreshPlan:
    """Decide for every discovered product at once."""
    moment = now or datetime.now(timezone.utc)
    plan = RefreshPlan(
        policy=policy,
        known=dict(known),
        known_rows=len(known),
        source=known_source,
    )
    for product in products:
        plan.decisions[product.product_id] = decide(
            product,
            known.get(product.product_id),
            policy=policy,
            now=moment,
        )
    return plan


def plan_for_site(
    site: str,
    products: Sequence[Product],
    *,
    policy: RefreshPolicy,
    csv_path: Path | None,
    use_database: bool = True,
    now: datetime | None = None,
) -> RefreshPlan:
    """Load what is already stored for a site and plan the run against it."""
    if not policy.enabled:
        return RefreshPlan(
            policy=policy,
            decisions={
                product.product_id: RefreshDecision(True, NEW) for product in products
            },
            known_rows=0,
            source="disabled",
        )
    known, source = load_known_products(
        site,
        csv_path=csv_path,
        use_database=use_database,
    )
    return build_plan(
        products,
        known,
        policy=policy,
        now=now,
        known_source=source,
    )
