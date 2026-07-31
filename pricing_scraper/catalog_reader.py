"""Read-only catalogue snapshots for the standalone product viewer."""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

import requests

from pricing_scraper.database import (
    DatabaseConfigurationError,
    SupabaseCatalogStore,
)
from pricing_scraper.exporter import deduplicate, load_products_csv
from pricing_scraper.models import Product

LOGGER = logging.getLogger("pricing_scraper.catalog_reader")
PRODUCT_FIELDS = set(Product.__dataclass_fields__)


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    """Products and diagnostics from one read-only catalogue source."""

    products: list[Product]
    source: str
    files_read: int = 0
    invalid_rows: int = 0


def _product_from_mapping(payload: Mapping[str, Any]) -> Product:
    values = {
        key: value
        for key, value in payload.items()
        if key in PRODUCT_FIELDS
    }
    return Product(**values)


def checkpoint_signature(directory: Path) -> tuple[tuple[str, int, int], ...]:
    """Return a cache key that changes whenever checkpoint data changes."""
    root = directory.resolve()
    if not root.exists():
        return ()
    return tuple(
        (
            str(path.resolve()),
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in sorted(root.glob("*.products.jsonl"))
        if path.is_file()
    )


def load_checkpoint_products(directory: Path) -> CatalogSnapshot:
    """Load a consistent best-effort snapshot while scrapers append records."""
    root = directory.resolve()
    products: list[Product] = []
    invalid_rows = 0
    files_read = 0
    if not root.exists():
        return CatalogSnapshot([], "Live checkpoints")

    for path in sorted(root.glob("*.products.jsonl")):
        if not path.is_file():
            continue
        files_read += 1
        file_products, file_invalid = load_checkpoint_file(path)
        products.extend(file_products)
        invalid_rows += file_invalid
    return CatalogSnapshot(
        products=deduplicate(products),
        source="Live checkpoints",
        files_read=files_read,
        invalid_rows=invalid_rows,
    )


def load_checkpoint_file(path: Path) -> tuple[list[Product], int]:
    """Read one checkpoint file without locking or mutating it."""
    products: list[Product] = []
    invalid_rows = 0
    try:
        handle = path.resolve().open(
            "r",
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        LOGGER.warning(
            "viewer_checkpoint_open_failed path=%s error=%s",
            path,
            exc,
        )
        return products, invalid_rows
    with handle:
        for line_number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
                if not isinstance(payload, Mapping):
                    raise TypeError("row is not an object")
                products.append(_product_from_mapping(payload))
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                invalid_rows += 1
                LOGGER.warning(
                    "viewer_checkpoint_row_skipped path=%s line=%s "
                    "error=%s",
                    path,
                    line_number,
                    exc,
                )
    return products, invalid_rows


def csv_signature(path: Path) -> tuple[str, int, int] | None:
    """Return a cache key for an exported combined CSV."""
    resolved = path.resolve()
    if not resolved.exists():
        return None
    stat = resolved.stat()
    return str(resolved), stat.st_size, stat.st_mtime_ns


def load_exported_products(path: Path) -> CatalogSnapshot:
    """Read the latest normalized combined CSV export."""
    resolved = path.resolve()
    return CatalogSnapshot(
        products=deduplicate(load_products_csv(resolved)),
        source="Latest exported CSV",
        files_read=1 if resolved.exists() else 0,
    )


def load_supabase_products(
    *,
    page_size: int = 1_000,
    max_rows: int = 100_000,
    session: requests.Session | None = None,
) -> CatalogSnapshot:
    """Fetch every current catalogue row from Supabase using pagination."""
    store, _required = SupabaseCatalogStore.from_environment()
    if store is None:
        raise DatabaseConfigurationError(
            "Supabase credentials are not configured in .env."
        )
    client = session or requests.Session()
    batch_size = max(1, min(1_000, int(page_size)))
    row_limit = max(batch_size, int(max_rows))
    rows: list[Product] = []
    offset = 0
    select_fields = ",".join(sorted(PRODUCT_FIELDS))
    endpoint = f"{store.url}/rest/v1/{store.products_table}"
    while offset < row_limit:
        headers = dict(store.headers)
        headers["Prefer"] = "count=exact"
        headers["Range"] = f"{offset}-{offset + batch_size - 1}"
        response = client.get(
            endpoint,
            params={
                "select": select_fields,
                "order": "scraped_at.desc",
            },
            headers=headers,
            timeout=store.timeout_seconds,
        )
        if response.status_code not in {200, 206}:
            raise RuntimeError(
                "Supabase catalogue read returned "
                f"HTTP {response.status_code}: {response.text[:1_000]}"
            )
        payload = response.json()
        if not isinstance(payload, list):
            raise RuntimeError("Supabase catalogue response is not a list.")
        for item in payload:
            if isinstance(item, Mapping):
                rows.append(_product_from_mapping(item))
        if len(payload) < batch_size:
            break
        offset += batch_size
    return CatalogSnapshot(
        products=deduplicate(rows),
        source="Supabase database",
    )


def load_scrape_runs(
    *,
    limit: int = 20,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    """Read recent Nykaa/Tira nightly run summaries from Supabase."""
    store, _required = SupabaseCatalogStore.from_environment()
    if store is None:
        raise DatabaseConfigurationError(
            "Supabase credentials are not configured in .env."
        )
    headers = dict(store.headers)
    headers["Prefer"] = "count=none"
    response = (session or requests.Session()).get(
        f"{store.url}/rest/v1/{store.runs_table}",
        params={
            "select": (
                "id,site,status,started_at,finished_at,products_seen,"
                "products_new,products_changed,products_unchanged,"
                "details_refreshed,failures,blocks,requests,message"
            ),
            "order": "started_at.desc",
            "limit": str(max(1, min(100, int(limit)))),
        },
        headers=headers,
        timeout=store.timeout_seconds,
    )
    if response.status_code != 200:
        raise RuntimeError(
            "Supabase run-history read returned "
            f"HTTP {response.status_code}: {response.text[:1_000]}"
        )
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError("Supabase run-history response is not a list.")
    return [dict(row) for row in payload if isinstance(row, Mapping)]


def products_to_csv_bytes(products: Iterable[Product]) -> bytes:
    """Create an in-memory normalized CSV download for filtered products."""
    rows = [product.to_dict() for product in products]
    if not rows:
        return b""
    from io import StringIO

    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                key: (
                    json.dumps(value, ensure_ascii=False)
                    if isinstance(value, (list, dict))
                    else value
                )
                for key, value in row.items()
            }
        )
    return buffer.getvalue().encode("utf-8-sig")
