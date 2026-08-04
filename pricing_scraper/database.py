"""Optional Supabase persistence for normalized catalogue and price history."""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

import requests

from pricing_scraper.config import environment_values
from pricing_scraper.models import Product

TABLE_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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
        session: requests.Session | None = None,
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
        self.session = session or requests.Session()
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

    def _upsert(
        self,
        table: str,
        rows: list[Mapping[str, Any]],
        conflict_columns: str,
    ) -> int:
        written = 0
        endpoint = f"{self.url}/rest/v1/{table}"
        for start in range(0, len(rows), self.batch_size):
            batch = rows[start : start + self.batch_size]
            response = self.session.post(
                endpoint,
                params={"on_conflict": conflict_columns},
                headers=self.headers,
                json=batch,
                timeout=self.timeout_seconds,
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
        response = self.session.patch(
            f"{self.url}/rest/v1/{table}",
            params=dict(params),
            headers=headers,
            json=dict(values),
            timeout=self.timeout_seconds,
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
        page_size: int = 1_000,
    ) -> list[dict[str, Any]]:
        """Read the current database snapshot for one retailer."""
        rows: list[dict[str, Any]] = []
        offset = 0
        size = max(1, min(1_000, int(page_size)))
        endpoint = f"{self.url}/rest/v1/{self.products_table}"
        while True:
            headers = dict(self.headers)
            headers["Prefer"] = "count=none"
            headers["Range"] = f"{offset}-{offset + size - 1}"
            response = self.session.get(
                endpoint,
                params={"site": f"eq.{site}", "select": "*"},
                headers=headers,
                timeout=self.timeout_seconds,
            )
            if response.status_code not in {200, 206}:
                raise DatabaseSyncError(
                    "Supabase catalogue read returned HTTP "
                    f"{response.status_code}: {response.text[:1_500]}"
                )
            payload = response.json()
            if not isinstance(payload, list):
                raise DatabaseSyncError("Supabase catalogue response is not a list.")
            rows.extend(item for item in payload if isinstance(item, dict))
            if len(payload) < size:
                break
            offset += size
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
