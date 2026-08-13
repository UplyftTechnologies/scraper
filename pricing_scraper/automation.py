"""Database-backed incremental collection for unattended nightly jobs."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from pricing_scraper.clients.base import RequestFailed
from pricing_scraper.clients.nykaa import NykaaClient
from pricing_scraper.clients.tira import TiraClient
from pricing_scraper.database import SupabaseCatalogStore
from pricing_scraper.exporter import deduplicate
from pricing_scraper.models import Product

SOURCE_FIELDS = (
    "parent_product_id",
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
    "product_url",
    "image_url",
)
RICH_FIELDS = (
    "gtin",
    "image_urls",
    "description",
    "description_html",
    "ingredients",
    "key_ingredients",
    "how_to_use",
    "key_features",
    "special_features",
    "product_attributes",
    "rating_breakdown",
    "top_reviews",
)
PRICE_FIELDS = ("mrp", "selling_price", "discount_pct", "in_stock")


@dataclass(slots=True)
class NightlySummary:
    """Counters and status produced by one incremental retailer run."""

    site: str
    run_id: str
    status: str = "running"
    products_seen: int = 0
    products_new: int = 0
    products_changed: int = 0
    products_unchanged: int = 0
    details_refreshed: int = 0
    failures: int = 0
    blocks: int = 0
    requests: int = 0
    complete_catalogue: bool = False
    message: str = ""


def _stable_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _stable_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple, set)):
        normalized = [_stable_value(item) for item in value]
        if all(isinstance(item, str) for item in normalized):
            return sorted(set(normalized), key=str.casefold)
        return normalized
    if isinstance(value, float):
        return round(value, 5)
    return value


def source_fingerprint(value: Product | Mapping[str, Any]) -> str:
    """Hash only listing fields that should trigger a database update."""
    payload = value.to_dict() if isinstance(value, Product) else dict(value)
    stable = {
        field: _stable_value(payload.get(field))
        for field in SOURCE_FIELDS
    }
    serialized = json.dumps(
        stable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def detail_fingerprint(value: Product | Mapping[str, Any]) -> str:
    """Hash rich product content separately from fast listing fields."""
    payload = value.to_dict() if isinstance(value, Product) else dict(value)
    stable = {field: _stable_value(payload.get(field)) for field in RICH_FIELDS}
    serialized = json.dumps(
        stable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _price_changed(product: Product, old: Mapping[str, Any] | None) -> bool:
    if old is None:
        return True
    return any(
        _stable_value(getattr(product, field))
        != _stable_value(old.get(field))
        for field in PRICE_FIELDS
    )


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _needs_detail(
    product: Product,
    old: Mapping[str, Any] | None,
    *,
    now: datetime,
    refresh_days: int,
    changed: bool,
) -> bool:
    if old is None or changed:
        return True
    if bool(old.get("detail_refresh_pending")):
        return True
    if bool(old.get("detail_unavailable")):
        return False
    last_detail = _parse_datetime(
        old.get("last_detail_scraped_at") or old.get("scraped_at")
    )
    if last_detail is None or last_detail < now - timedelta(days=refresh_days):
        return True
    return not any(
        old.get(field)
        for field in ("description", "ingredients", "how_to_use", "image_urls")
    )


def _preserve_rich_fields(
    product: Product,
    old: Mapping[str, Any] | None,
) -> dict[str, Any]:
    row = product.to_dict()
    if old is None:
        return row
    for field in RICH_FIELDS:
        if row.get(field) in (None, "", [], {}):
            row[field] = old.get(field, row.get(field))
    return row


def _history_row(product: Product, scraped_at: str) -> dict[str, Any]:
    return {
        "site": product.site,
        "product_id": product.product_id,
        "parent_product_id": product.parent_product_id,
        "sku": product.sku,
        "variant": product.variant,
        "mrp": product.mrp,
        "selling_price": product.selling_price,
        "discount_pct": product.discount_pct,
        "in_stock": product.in_stock,
        "scraped_at": scraped_at,
    }


def _collect_listing(
    client: NykaaClient | TiraClient,
) -> tuple[list[Product], bool, list[str]]:
    categories = client.select_categories([])
    products: list[Product] = []
    complete = True
    stop_reasons: list[str] = []
    for category in categories:
        result = client.scrape_category_resumable(
            category,
            start_page=client.start_page,
        )
        products.extend(result.products)
        if not result.completed:
            complete = False
            stop_reasons.append(
                f"{category.get('name', category.get('id', 'category'))}:"
                f"{result.stop_reason}"
            )
    return deduplicate(products), complete, stop_reasons


def _detail_candidates(
    products: Iterable[Product],
    old_by_id: Mapping[str, Mapping[str, Any]],
    *,
    now: datetime,
    refresh_days: int,
) -> tuple[list[Product], set[str], set[str]]:
    urgent: list[Product] = []
    routine: list[Product] = []
    new_ids: set[str] = set()
    changed_ids: set[str] = set()
    for product in products:
        old = old_by_id.get(product.product_id)
        old_fingerprint = str(old.get("source_fingerprint") or "") if old else ""
        if old and not old_fingerprint:
            old_fingerprint = source_fingerprint(old)
        changed = old is not None and old_fingerprint != source_fingerprint(product)
        if old is None:
            new_ids.add(product.product_id)
        elif changed:
            changed_ids.add(product.product_id)
        needs_detail = _needs_detail(
            product,
            old,
            now=now,
            refresh_days=refresh_days,
            changed=changed,
        )
        if needs_detail:
            (
                urgent
                if old is None or changed or bool(old.get("detail_refresh_pending"))
                else routine
            ).append(product)
    return [*urgent, *routine], new_ids, changed_ids


def _enrich_nykaa(
    client: NykaaClient,
    candidates: list[Product],
    *,
    limit: int,
    logger: logging.Logger,
) -> tuple[list[Product], set[str], set[str], int]:
    enriched: list[Product] = []
    refreshed_ids: set[str] = set()
    unavailable_ids: set[str] = set()
    failures = 0
    parents: dict[str, Product] = {}
    parent_members: dict[str, set[str]] = {}
    for product in candidates:
        parent = product.parent_product_id or product.product_id
        parents.setdefault(parent, product)
        parent_members.setdefault(parent, set()).add(product.product_id)
    for parent, product in list(parents.items())[:limit]:
        try:
            details = client.fetch_product_details(product)
        except RequestFailed as exc:
            failures += 1
            if exc.status_code in {404, 410}:
                unavailable_ids.update(parent_members.get(parent, ()))
                logger.warning(
                    "nightly_detail_skipped site=nykaa parent=%s status=%s",
                    parent,
                    exc.status_code,
                )
            else:
                logger.exception("nightly_detail_failed site=nykaa parent=%s", parent)
            continue
        except Exception:
            failures += 1
            logger.exception("nightly_detail_failed site=nykaa parent=%s", parent)
            continue
        enriched.extend(details)
        refreshed_ids.update(parent_members.get(parent, ()))
        refreshed_ids.update(item.product_id for item in details)
    return enriched, refreshed_ids, unavailable_ids, failures


def _enrich_tira(
    client: TiraClient,
    candidates: list[Product],
    *,
    limit: int,
    logger: logging.Logger,
) -> tuple[list[Product], set[str], set[str], int]:
    enriched: list[Product] = []
    refreshed_ids: set[str] = set()
    failures = 0
    for product in candidates[:limit]:
        try:
            detail = client.fetch_variant_price(product)
        except Exception:
            failures += 1
            logger.exception(
                "nightly_detail_failed site=tira product_id=%s",
                product.product_id,
            )
            continue
        enriched.append(detail)
        refreshed_ids.add(detail.product_id)
    return enriched, refreshed_ids, set(), failures


def run_incremental_site(
    *,
    site: str,
    config: Mapping[str, Any],
    store: SupabaseCatalogStore,
    logger: logging.Logger,
) -> NightlySummary:
    """Collect one retailer and persist only new or changed product states."""
    if site not in {"nykaa", "tira"}:
        raise ValueError("Nightly automation supports only nykaa and tira.")
    automation = config.get("automation")
    automation = automation if isinstance(automation, Mapping) else {}
    refresh_days = max(1, int(automation.get("detail_refresh_days", 30)))
    detail_limit = max(0, int(automation.get("max_detail_requests_per_run", 1_000)))
    inactive_threshold = max(1, int(automation.get("missing_runs_before_inactive", 3)))
    minimum_catalogue_ratio = max(
        0.0,
        min(1.0, float(automation.get("minimum_catalogue_ratio", 0.5))),
    )
    run_id = store.start_run(
        site,
        metadata={
            "mode": "incremental",
            "detail_refresh_days": refresh_days,
            "detail_request_limit": detail_limit,
        },
    )
    summary = NightlySummary(site=site, run_id=run_id)
    logger.info("nightly_run_started site=%s run_id=%s", site, run_id)
    try:
        old_rows = store.fetch_site_products(site)
        old_by_id = {
            str(row.get("product_id")): row
            for row in old_rows
            if row.get("product_id")
        }
        client_class = NykaaClient if site == "nykaa" else TiraClient
        with client_class(
            site_config=config[site],
            request_config=config["request"],
            brands=config.get("brands", ()),
        ) as client:
            listing, complete, stop_reasons = _collect_listing(client)
            if client.brand_filter:
                # A brand-filtered sweep never sees the rest of the catalogue,
                # so it must not age other brands out of the database.
                complete = False
                stop_reasons.append(
                    f"brand_filter:{len(client.brand_filter)} brand(s)"
                )
            if not listing:
                raise RuntimeError(
                    f"{site.title()} returned zero products; refusing to update "
                    "or age the saved catalogue."
                )
            active_old_count = sum(
                1 for row in old_rows if row.get("is_active") is not False
            )
            if (
                active_old_count
                and len(listing) < active_old_count * minimum_catalogue_ratio
            ):
                complete = False
                stop_reasons.append(
                    "catalogue_safety_gate:"
                    f"{len(listing)}/{active_old_count} active rows"
                )
            summary.complete_catalogue = complete
            summary.products_seen = len(listing)
            now = datetime.now(timezone.utc)
            candidates, new_ids, changed_ids = _detail_candidates(
                listing,
                old_by_id,
                now=now,
                refresh_days=refresh_days,
            )
            listing_fingerprints = {
                product.product_id: source_fingerprint(product)
                for product in listing
            }
            if site == "nykaa":
                details, refreshed_ids, unavailable_ids, detail_failures = _enrich_nykaa(
                    client,
                    candidates,
                    limit=detail_limit,
                    logger=logger,
                )
            else:
                details, refreshed_ids, unavailable_ids, detail_failures = _enrich_tira(
                    client,
                    candidates,
                    limit=detail_limit,
                    logger=logger,
                )
            detail_by_id = {product.product_id: product for product in details}
            final_products = [detail_by_id.get(item.product_id, item) for item in listing]
            listing_ids = {product.product_id for product in listing}
            extra_details = [
                item for item in details
                if item.product_id not in listing_ids
            ]
            final_products = deduplicate([*final_products, *extra_details])

            observed_at = now.isoformat(timespec="microseconds")
            candidate_ids = {product.product_id for product in candidates}
            rows_to_write: list[dict[str, Any]] = []
            price_rows: list[dict[str, Any]] = []
            content_changed_ids: set[str] = set()
            for product in final_products:
                old = old_by_id.get(product.product_id)
                fingerprint = listing_fingerprints.get(
                    product.product_id,
                    source_fingerprint(product),
                )
                old_fingerprint = str(old.get("source_fingerprint") or "") if old else ""
                if old and not old_fingerprint:
                    old_fingerprint = source_fingerprint(old)
                changed = old is None or old_fingerprint != fingerprint
                refreshed = product.product_id in refreshed_ids
                if not changed and not refreshed and product.product_id not in candidate_ids:
                    continue
                row = _preserve_rich_fields(product, old)
                rich_fingerprint = detail_fingerprint(row)
                old_rich_fingerprint = (
                    str(old.get("detail_fingerprint") or "") if old else ""
                )
                if old and not old_rich_fingerprint:
                    old_rich_fingerprint = detail_fingerprint(old)
                content_changed = refreshed and (
                    old is None or old_rich_fingerprint != rich_fingerprint
                )
                if content_changed and old is not None:
                    content_changed_ids.add(product.product_id)
                row.update(
                    {
                        "source_fingerprint": fingerprint,
                        "detail_fingerprint": rich_fingerprint,
                        "last_checked_at": observed_at,
                        "last_seen_at": observed_at,
                        "last_seen_run_id": run_id,
                        "last_changed_at": (
                            observed_at
                            if changed or content_changed
                            else old.get("last_changed_at")
                        ),
                        "missing_run_count": 0,
                        "is_active": True,
                        "detail_refresh_pending": (
                            product.product_id in candidate_ids
                            and not refreshed
                            and product.product_id not in unavailable_ids
                        ),
                        "detail_unavailable": (
                            product.product_id in unavailable_ids
                        ),
                    }
                )
                if old is None:
                    row["first_seen_at"] = observed_at
                if refreshed:
                    row["last_detail_scraped_at"] = observed_at
                rows_to_write.append(row)
                if _price_changed(product, old):
                    price_rows.append(_history_row(product, observed_at))

            seen_ids = [product.product_id for product in final_products]
            store.incremental_sync(
                site=site,
                run_id=run_id,
                rows=rows_to_write,
                price_rows=price_rows,
                seen_product_ids=seen_ids,
                complete_catalogue=complete,
                missing_runs_before_inactive=inactive_threshold,
            )
            summary.products_new = len(new_ids)
            all_changed_ids = changed_ids | content_changed_ids
            summary.products_changed = len(all_changed_ids)
            summary.products_unchanged = max(
                0, len(listing) - len(new_ids) - len(all_changed_ids)
            )
            summary.details_refreshed = len(refreshed_ids)
            summary.failures = (
                client.failures
                + client.page_failures
                + client.product_failures
                + detail_failures
            )
            summary.blocks = client.blocks_encountered
            summary.requests = client.requests_made
            summary.status = "success" if complete else "partial"
            deferred = max(0, len(candidates) - detail_limit)
            parts = []
            if stop_reasons:
                parts.append("; ".join(stop_reasons))
            if deferred:
                parts.append(f"{deferred} detail refreshes deferred")
            summary.message = "; ".join(parts) or "Scraping complete"
    except Exception as exc:
        summary.status = "failed"
        summary.message = str(exc)[:1_500]
        logger.exception("nightly_run_failed site=%s run_id=%s", site, run_id)
        try:
            store.finish_run(
                run_id,
                status=summary.status,
                failures=max(1, summary.failures),
                message=summary.message,
            )
        except Exception:
            logger.exception("nightly_run_finish_failed run_id=%s", run_id)
        raise

    store.finish_run(
        run_id,
        status=summary.status,
        products_seen=summary.products_seen,
        products_new=summary.products_new,
        products_changed=summary.products_changed,
        products_unchanged=summary.products_unchanged,
        details_refreshed=summary.details_refreshed,
        failures=summary.failures,
        blocks=summary.blocks,
        requests=summary.requests,
        message=summary.message,
        metadata={"complete_catalogue": summary.complete_catalogue},
    )
    logger.info(
        "nightly_run_finished site=%s run_id=%s status=%s seen=%s new=%s "
        "changed=%s unchanged=%s details=%s failures=%s blocks=%s requests=%s",
        site,
        run_id,
        summary.status,
        summary.products_seen,
        summary.products_new,
        summary.products_changed,
        summary.products_unchanged,
        summary.details_refreshed,
        summary.failures,
        summary.blocks,
        summary.requests,
    )
    return summary
