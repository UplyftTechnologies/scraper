"""Stream scraped products into Supabase as the run produces them.

A collection used to hold every product in memory and write it all at the end:
one Excel workbook, one CSV, one database sync. That end-of-run export is the
most expensive moment in the whole run - it peaked around 500 MB for a
ten-thousand product catalogue - and it is also the most fragile, because a
crash or a restart before it happens throws away everything the run collected.

This writes each batch to the database at the same points the run already
commits to its checkpoint, so memory stays flat, a run that dies keeps what it
had scraped, and the database fills in real time instead of in one lump.

Nothing here may end a run. The checkpoint remains the source of truth for
resuming, so a failed flush is counted and logged, and the export at the end
still reconciles whatever the stream missed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from pricing_scraper.models import Product

LOGGER = logging.getLogger("pricing_scraper.db_sink")

# Rows per request. Large enough that a full catalogue costs ~100 requests
# rather than thousands, small enough to stay well inside the statement
# timeout when a row carries a long description and a review list.
DEFAULT_BATCH = 100


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass(slots=True)
class SinkResult:
    """What one streaming session managed to write."""

    enabled: bool = False
    products_written: int = 0
    price_points_written: int = 0
    batches: int = 0
    failures: int = 0
    error: str = ""
    run_id: str = ""

    def summary(self) -> str:
        if not self.enabled:
            return "database streaming disabled"
        return (
            f"{self.products_written:,} product rows and "
            f"{self.price_points_written:,} price points in "
            f"{self.batches:,} batch(es), {self.failures:,} failed"
        )


@dataclass(slots=True)
class DatabaseSink:
    """Buffer products and upsert them in batches while the run works."""

    store: Any
    site: str
    run_id: str = ""
    batch_size: int = DEFAULT_BATCH
    logger: logging.Logger = LOGGER
    _buffer: list[Product] = field(default_factory=list)
    result: SinkResult = field(default_factory=SinkResult)

    def __post_init__(self) -> None:
        self.result.enabled = True
        self.result.run_id = self.run_id

    def add(self, products: Iterable[Product]) -> None:
        """Queue products, flushing whenever a full batch has accumulated.

        A product reached twice in one run - once from the listing, again with
        its detail - is queued twice on purpose: the second row is the richer
        one and the upsert lets it replace the first.
        """
        for product in products:
            if not product.product_id:
                continue
            self._buffer.append(product)
        while len(self._buffer) >= self.batch_size:
            self._flush(self._buffer[: self.batch_size])
            del self._buffer[: self.batch_size]

    def flush(self) -> None:
        """Write whatever is queued, leaving the buffer empty."""
        if self._buffer:
            self._flush(self._buffer)
            self._buffer.clear()

    @staticmethod
    def _latest_per_product(products: Sequence[Product]) -> list[Product]:
        """Keep one row per product, the last one queued.

        A product is reached twice in a run: once from the listing, again with
        its detail. Postgres refuses an upsert that touches the same key twice
        in a single statement - "ON CONFLICT DO UPDATE command cannot affect
        row a second time" - and rejects the whole batch, not just the
        duplicate. Keeping the last occurrence both avoids that and is the row
        wanted anyway, because the detail is the richer of the two.
        """
        latest: dict[tuple[str, str], Product] = {}
        for product in products:
            latest[(product.site.casefold(), product.product_id)] = product
        return list(latest.values())

    def _rows(self, products: Sequence[Product]) -> tuple[list[dict], list[dict]]:
        observed = _now()
        rows: list[dict[str, Any]] = []
        history: list[dict[str, Any]] = []
        for product in self._latest_per_product(products):
            row = product.to_dict()
            row["scraped_at"] = product.scraped_at or observed
            row["last_checked_at"] = observed
            row["last_seen_at"] = observed
            row["missing_run_count"] = 0
            row["is_active"] = True
            if self.run_id:
                row["last_seen_run_id"] = self.run_id
            # first_seen_at is deliberately absent. PostgREST rejects a bulk
            # upsert whose objects do not all carry the same keys, so setting
            # it on new products only made every mixed batch fail with
            # PGRST102. The column defaults to now() on insert and is left
            # untouched on update, which is the behaviour that was wanted.
            rows.append(row)
            history.append(
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
                    "scraped_at": row["scraped_at"],
                }
            )
        return rows, history

    def _flush(self, products: Sequence[Product]) -> None:
        if not products:
            return
        rows, history = self._rows(products)
        self.result.batches += 1
        try:
            self.result.products_written += self.store._upsert(
                self.store.products_table, rows, "site,product_id"
            )
            self.result.price_points_written += self.store._upsert(
                self.store.price_history_table,
                history,
                "site,product_id,scraped_at",
            )
        except Exception as exc:  # noqa: BLE001 - a lost batch must not end the run
            self.result.failures += 1
            self.result.error = f"{type(exc).__name__}: {exc}"
            self.logger.warning(
                "db_sink_batch_failed site=%s rows=%s error=%s",
                self.site,
                len(rows),
                exc,
            )

    def _finish_run(self, *, complete_sweep: bool) -> None:
        """Close the run record this sink opened.

        Without this every run stays marked ``running`` for ever, which makes
        the run history useless and makes a health check report healthy runs as
        stuck - the watchdog cannot tell an abandoned run from one nobody
        closed.
        """
        if not self.run_id:
            return
        status = (
            "success"
            if complete_sweep and not self.result.failures
            else "partial"
        )
        try:
            self.store.finish_run(
                self.run_id,
                status=status,
                products_seen=self.result.products_written,
                message=self.result.summary(),
            )
        except Exception as exc:  # noqa: BLE001 - bookkeeping must not fail a run
            self.logger.warning(
                "db_sink_finish_failed site=%s error=%s", self.site, exc
            )

    def close(self, *, complete_sweep: bool = False, inactive_threshold: int = 3) -> SinkResult:
        """Flush the tail and, after a complete sweep, age missing products.

        The aging call is only safe when the run actually saw the whole
        catalogue. A partial or stopped run has no opinion about what is
        missing, and letting it age products would deactivate perfectly live
        ones just because the run ended early.
        """
        self.flush()
        self._finish_run(complete_sweep=complete_sweep)
        if complete_sweep and self.run_id and not self.result.failures:
            try:
                self.store.finalize_missing(
                    site=self.site,
                    run_id=self.run_id,
                    inactive_threshold=inactive_threshold,
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(
                    "db_sink_finalize_failed site=%s error=%s", self.site, exc
                )
        self.logger.info("db_sink site=%s %s", self.site, self.result.summary())
        return self.result


def open_sink(
    site: str,
    *,
    enabled: bool,
    run_id: str = "",
    batch_size: int = DEFAULT_BATCH,
) -> DatabaseSink | None:
    """Create a sink when the database is configured, else None.

    A missing or misconfigured database is not an error here: the run still
    collects, checkpoints, and exports exactly as it did before streaming
    existed.
    """
    if not enabled:
        return None
    try:
        from pricing_scraper.database import SupabaseCatalogStore

        store, _required = SupabaseCatalogStore.from_environment()
    except Exception as exc:  # noqa: BLE001 - never block a run
        LOGGER.info("Database streaming unavailable (%s).", exc)
        return None
    if store is None:
        return None
    return DatabaseSink(
        store=store, site=site, run_id=run_id, batch_size=batch_size
    )
