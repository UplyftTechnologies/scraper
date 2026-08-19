"""Optional Supabase persistence for normalized catalogue and price history."""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

import requests

from pricing_scraper.config import environment_values
from pricing_scraper.models import Product

TABLE_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Statuses worth repeating an idempotent request for. A 4xx other than these
# describes the request itself, which a retry cannot change.
RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


class DatabaseConfigurationError(ValueError):
    """Raised when database environment settings are incomplete or invalid."""


class DatabaseSyncError(RuntimeError):
    """Raised when configured database synchronization fails."""


@dataclass(frozen=True, slots=True)
class DatabaseSyncResult:
    """Summary of one optional database synchronization."""

    enabled: bool
    products_written: int = 0
    price_points_written: int = 0
    error: str = ""


def _environment() -> dict[str, str]:
    return environment_values()


def _boolean(value: Any, default: bool) -> bool:
    text = str(value or "").strip().casefold()
    if not text:
        return default
    return text in {"1", "true", "yes", "on"}


def _table_name(value: Any, default: str) -> str:
    name = str(value or default).strip()
    if not TABLE_NAME_PATTERN.fullmatch(name):
        raise DatabaseConfigurationError(
            f"Invalid Supabase table name: {name!r}"
        )
    return name


class SupabaseCatalogStore:
    """Upsert current products and append timestamped price observations."""

    def __init__(
        self,
        *,
        url: str,
        service_role_key: str,
        schema: str = "public",
        products_table: str = "retailer_products",
        price_history_table: str = "retailer_price_history",
        runs_table: str = "retailer_scrape_runs",
        batch_size: int = 100,
        timeout_seconds: float = 45,
        max_attempts: int = 4,
        session: requests.Session | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        logger: logging.Logger | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.service_role_key = service_role_key
        self.schema = _table_name(schema, "public")
        self.products_table = _table_name(
            products_table,
            "retailer_products",
        )
        self.price_history_table = _table_name(
            price_history_table,
            "retailer_price_history",
        )
        self.runs_table = _table_name(runs_table, "retailer_scrape_runs")
        self.batch_size = max(1, min(1_000, int(batch_size)))
        self.timeout_seconds = max(5.0, float(timeout_seconds))
        self.max_attempts = max(1, min(10, int(max_attempts)))
        self.session = session or requests.Session()
        self.sleeper = sleeper
        self.logger = logger or logging.getLogger(
            "pricing_scraper.database"
        )

    @classmethod
    def from_environment(
        cls,
    ) -> tuple[SupabaseCatalogStore | None, bool]:
        """Create a store when both Supabase credentials are configured."""
        values = _environment()
        url = values.get("SUPABASE_URL", "").strip()
        key = (
            values.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
            or values.get("SUPABASE_SECRET_KEY", "").strip()
            or values.get("SUPABASE_KEY", "").strip()
        )
        required = _boolean(
            values.get("DATABASE_SYNC_REQUIRED"),
            True,
        )
        if not url and not key:
            return None, required
        if not url or not key:
            raise DatabaseConfigurationError(
                "Set SUPABASE_URL and either SUPABASE_SECRET_KEY or "
                "SUPABASE_SERVICE_ROLE_KEY in .env."
            )
        return (
            cls(
                url=url,
                service_role_key=key,
                schema=values.get("SUPABASE_SCHEMA", "public"),
                products_table=values.get(
                    "SUPABASE_PRODUCTS_TABLE",
                    "retailer_products",
                ),
                price_history_table=values.get(
                    "SUPABASE_PRICE_HISTORY_TABLE",
                    "retailer_price_history",
                ),
                runs_table=values.get(
                    "SUPABASE_RUNS_TABLE",
                    "retailer_scrape_runs",
                ),
                batch_size=int(
                    values.get("DATABASE_BATCH_SIZE", "100")
                ),
                timeout_seconds=float(
                    values.get("DATABASE_TIMEOUT_SECONDS", "45")
                ),
                max_attempts=int(values.get("DATABASE_MAX_ATTEMPTS", "4")),
            ),
            required,
        )

    @property
    def headers(self) -> dict[str, str]:
        return {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
            "Content-Type": "application/json",
            "Accept-Profile": self.schema,
            "Content-Profile": self.schema,
            "Prefer": "resolution=merge-duplicates,return=minimal",
        }

    def _send(
        self,
        method: str,
        url: str,
        *,
        retry_statuses: frozenset[int] = RETRYABLE_STATUSES,
        **kwargs: Any,
    ) -> requests.Response:
        """Send one PostgREST request, retrying transient failures.

        Only callers whose request is idempotent may retry. An upsert resolves
        duplicates and a patch writes absolute values, so repeating either is
        harmless; ``finalize_retailer_scrape_run`` increments a counter and is
        therefore sent exactly once.

        A dropped connection or a write timeout part-way through a large upload
        is the normal failure here, and it used to end a collection that had
        already spent hours on rate-limited requests.
        """
        attempts = max(1, self.max_attempts)
        send = getattr(self.session, method.casefold())
        last_error = ""
        for attempt in range(attempts):
            try:
                response = send(url, timeout=self.timeout_seconds, **kwargs)
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt + 1 >= attempts:
                    raise DatabaseSyncError(
                        f"Supabase request to {url} failed after "
                        f"{attempts} attempt(s): {last_error}"
                    ) from exc
            else:
                if (
                    response.status_code not in retry_statuses
                    or attempt + 1 >= attempts
                ):
                    return response
                last_error = f"HTTP {response.status_code}"
            delay = min(30.0, 2.0 * (2**attempt))
            self.logger.warning(
                "database_request_retry method=%s attempt=%s/%s error=%s "
                "retry_in=%.1fs",
                method,
                attempt + 1,
                attempts,
                last_error,
                delay,
            )
            self.sleeper(delay)
        raise DatabaseSyncError(  # pragma: no cover - the loop always returns
            f"Supabase request to {url} failed: {last_error}"
        )

    @staticmethod
    def _by_shape(
        rows: list[Mapping[str, Any]]
    ) -> list[list[Mapping[str, Any]]]:
        """Group rows so every request carries objects of one shape.

        PostgREST rejects a bulk upsert whose objects differ in their keys -
        PGRST102, "All object keys must match" - and it rejects the whole
        request, not the odd row out. Callers legitimately vary the keys: a
        column like first_seen_at belongs only on a product being inserted, and
        last_detail_scraped_at only on one whose detail was re-read.

        Padding the gaps with null would be wrong, because on an upsert a null
        overwrites the stored value. Splitting by shape keeps each row's
        meaning exactly as the caller intended: a key that is absent stays
        absent, and the column keeps whatever the database already holds.
        """
        groups: dict[frozenset[str], list[Mapping[str, Any]]] = {}
        for row in rows:
            groups.setdefault(frozenset(row), []).append(row)
        return list(groups.values())

    def _upsert(
        self,
        table: str,
        rows: list[Mapping[str, Any]],
        conflict_columns: str,
    ) -> int:
        written = 0
        endpoint = f"{self.url}/rest/v1/{table}"
        batches = [
            group[start : start + self.batch_size]
            for group in self._by_shape(rows)
            for start in range(0, len(group), self.batch_size)
        ]
        for batch in batches:
            response = self._send(
                "POST",
                endpoint,
                params={"on_conflict": conflict_columns},
                headers=self.headers,
                json=batch,
            )
            if response.status_code not in {200, 201, 204}:
                detail = response.text.strip()[:1_500]
                raise DatabaseSyncError(
                    f"Supabase upsert to {table!r} returned "
                    f"HTTP {response.status_code}: {detail}"
                )
            written += len(batch)
        return written

    def _patch(
        self,
        table: str,
        *,
        params: Mapping[str, str],
        values: Mapping[str, Any],
    ) -> None:
        """Patch rows selected through PostgREST query filters."""
        headers = dict(self.headers)
        headers["Prefer"] = "return=minimal"
        response = self._send(
            "PATCH",
            f"{self.url}/rest/v1/{table}",
            params=dict(params),
            headers=headers,
            json=dict(values),
        )
        if response.status_code not in {200, 204}:
            raise DatabaseSyncError(
                f"Supabase patch to {table!r} returned HTTP "
                f"{response.status_code}: {response.text[:1_500]}"
            )

    def fetch_site_products(
        self,
        site: str,
        *,
        page_size: int = 500,
        columns: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Read the current database snapshot for one retailer.

        Pages are keyed on the primary key rather than an offset. An offset
        window over an unordered query lets Postgres return the same row twice
        or skip one between requests, which silently truncates the snapshot;
        a run comparing against a short snapshot would treat existing products
        as new. Keyset paging also keeps each request cheap enough to stay
        inside the statement timeout as the table grows.

        ``columns`` restricts the projection for callers that do not need the
        review and description payloads.
        """
        selection = "*"
        if columns:
            wanted = list(dict.fromkeys(["id", *columns]))
            selection = ",".join(wanted)

        rows: list[dict[str, Any]] = []
        size = max(1, min(1_000, int(page_size)))
        last_id = 0
        endpoint = f"{self.url}/rest/v1/{self.products_table}"
        headers = dict(self.headers)
        headers["Prefer"] = "count=none"
        while True:
            response = self._send(
                "GET",
                endpoint,
                # A slow page is handled below by asking for fewer rows, which
                # is more useful than repeating the same oversized request.
                retry_statuses=frozenset(),
                params={
                    "site": f"eq.{site}",
                    "select": selection,
                    "order": "id.asc",
                    "limit": str(size),
                    "id": f"gt.{last_id}",
                },
                headers=headers,
            )
            if response.status_code in {500, 503, 504, 408} and size > 25:
                # A statement timeout on a wide row set: ask for less at once
                # rather than abandoning the snapshot.
                size = max(25, size // 4)
                self.logger.warning(
                    "database_read_retry site=%s page_size=%s status=%s",
                    site,
                    size,
                    response.status_code,
                )
                continue
            if response.status_code not in {200, 206}:
                raise DatabaseSyncError(
                    "Supabase catalogue read returned HTTP "
                    f"{response.status_code}: {response.text[:1_500]}"
                )
            payload = response.json()
            if not isinstance(payload, list):
                raise DatabaseSyncError("Supabase catalogue response is not a list.")
            page = [item for item in payload if isinstance(item, dict)]
            rows.extend(page)
            if len(payload) < size:
                break
            identifier = page[-1].get("id") if page else None
            if identifier is None:
                raise DatabaseSyncError(
                    "Supabase catalogue rows have no id to page on."
                )
            last_id = int(identifier)
        return rows

    def start_run(self, site: str, *, metadata: Mapping[str, Any]) -> str:
        """Create a durable run record before network collection starts."""
        now = datetime.now(timezone.utc)
        stale_before = (now - timedelta(hours=12)).isoformat()
        self._patch(
            self.runs_table,
            params={
                "site": f"eq.{site}",
                "status": "eq.running",
                "started_at": f"lt.{stale_before}",
            },
            values={
                "status": "failed",
                "finished_at": now.isoformat(),
                "message": "Previous job ended without a completion update.",
            },
        )
        run_id = str(uuid.uuid4())
        self._upsert(
            self.runs_table,
            [
                {
                    "id": run_id,
                    "site": site,
                    "status": "running",
                    "started_at": now.isoformat(),
                    "metadata": dict(metadata),
                }
            ],
            "id",
        )
        return run_id

    def finish_run(self, run_id: str, **summary: Any) -> None:
        """Finish a run with counters and its final success state."""
        values = {
            key: value
            for key, value in summary.items()
            if key
            in {
                "status",
                "products_seen",
                "products_new",
                "products_changed",
                "products_unchanged",
                "details_refreshed",
                "failures",
                "blocks",
                "requests",
                "message",
                "metadata",
            }
        }
        values["finished_at"] = datetime.now(timezone.utc).isoformat()
        self._patch(
            self.runs_table,
            params={"id": f"eq.{run_id}"},
            values=values,
        )

    def incremental_sync(
        self,
        *,
        site: str,
        run_id: str,
        rows: list[Mapping[str, Any]],
        price_rows: list[Mapping[str, Any]],
        seen_product_ids: Iterable[str],
        complete_catalogue: bool,
        missing_runs_before_inactive: int,
    ) -> DatabaseSyncResult:
        """Persist changed rows, touch unchanged rows, and age missing rows."""
        products_written = self._upsert(
            self.products_table,
            rows,
            "site,product_id",
        ) if rows else 0
        price_points_written = self._upsert(
            self.price_history_table,
            price_rows,
            "site,product_id,scraped_at",
        ) if price_rows else 0

        now = datetime.now(timezone.utc).isoformat()
        ids = list(dict.fromkeys(str(value) for value in seen_product_ids if value))
        for start in range(0, len(ids), 75):
            batch = ids[start : start + 75]
            encoded = ",".join(f'"{value.replace(chr(34), "")}"' for value in batch)
            self._patch(
                self.products_table,
                params={
                    "site": f"eq.{site}",
                    "product_id": f"in.({encoded})",
                },
                values={
                    "last_checked_at": now,
                    "last_seen_at": now,
                    "last_seen_run_id": run_id,
                    "missing_run_count": 0,
                    "is_active": True,
                },
            )

        if complete_catalogue:
            headers = dict(self.headers)
            headers["Prefer"] = "return=minimal"
            response = self.session.post(
                f"{self.url}/rest/v1/rpc/finalize_retailer_scrape_run",
                headers=headers,
                json={
                    "p_site": site,
                    "p_run_id": run_id,
                    "p_inactive_threshold": max(
                        1, int(missing_runs_before_inactive)
                    ),
                },
                timeout=self.timeout_seconds,
            )
            if response.status_code not in {200, 204}:
                raise DatabaseSyncError(
                    "Supabase missing-product finalization returned HTTP "
                    f"{response.status_code}: {response.text[:1_500]}"
                )
        return DatabaseSyncResult(
            enabled=True,
            products_written=products_written,
            price_points_written=price_points_written,
        )

    def finalize_missing(
        self,
        *,
        site: str,
        run_id: str,
        inactive_threshold: int = 3,
    ) -> None:
        """Age products a complete sweep did not see towards inactive.

        Sent exactly once and never retried: the function increments a counter,
        so repeating it would age products towards inactive twice for a single
        sweep.
        """
        headers = dict(self.headers)
        headers["Prefer"] = "return=minimal"
        response = self.session.post(
            f"{self.url}/rest/v1/rpc/finalize_retailer_scrape_run",
            headers=headers,
            json={
                "p_site": site,
                "p_run_id": run_id,
                "p_inactive_threshold": max(1, int(inactive_threshold)),
            },
            timeout=self.timeout_seconds,
        )
        if response.status_code not in {200, 204}:
            raise DatabaseSyncError(
                "Supabase missing-product finalization returned HTTP "
                f"{response.status_code}: {response.text[:1_500]}"
            )

    def sync(self, products: Iterable[Product]) -> DatabaseSyncResult:
        """Write the latest product state and its price observations."""
        normalized = list(products)
        fallback_timestamp = datetime.now(timezone.utc).isoformat(
            timespec="microseconds"
        )
        current_rows: list[dict[str, Any]] = []
        for product in normalized:
            row = product.to_dict()
            row["scraped_at"] = product.scraped_at or fallback_timestamp
            current_rows.append(row)
        history_rows = [
            {
                "site": product.site,
                "product_id": product.product_id,
                "parent_product_id": product.parent_product_id,
                "sku": product.sku,
                "variant": product.variant,
                "mrp": product.mrp,
                "selling_price": product.selling_price,
                "discount_pct": product.discount_pct,
                "in_stock": product.in_stock,
                "scraped_at": product.scraped_at or fallback_timestamp,
            }
            for product in normalized
        ]
        products_written = self._upsert(
            self.products_table,
            current_rows,
            "site,product_id",
        )
        price_points_written = self._upsert(
            self.price_history_table,
            history_rows,
            "site,product_id,scraped_at",
        )
        self.logger.info(
            "database_sync products=%s price_points=%s",
            products_written,
            price_points_written,
        )
        return DatabaseSyncResult(
            enabled=True,
            products_written=products_written,
            price_points_written=price_points_written,
        )


def sync_products_to_database(
    products: Iterable[Product],
) -> DatabaseSyncResult:
    """Sync products when .env credentials exist; otherwise remain disabled."""
    store, required = SupabaseCatalogStore.from_environment()
    if store is None:
        return DatabaseSyncResult(enabled=False)
    try:
        return store.sync(products)
    except Exception as exc:
        if required:
            raise
        logging.getLogger("pricing_scraper.database").exception(
            "database_sync_failed"
        )
        return DatabaseSyncResult(
            enabled=True,
            error=str(exc),
        )
