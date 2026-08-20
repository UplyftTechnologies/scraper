"""One nightly job: refresh every retailer, then reconcile barcodes.

Render runs a cron entry as a single command, and the work has a strict order -
barcodes can only be propagated between platforms once every platform has been
refreshed. This runs the retailers in sequence and then the supervisor, and
reports on all of it as one unit.

A retailer that fails does not stop the others: partial data is worth having,
and the report says exactly what was missed. The barcode step is skipped when
no retailer succeeded, because propagating between stale catalogues would
spread yesterday's mistakes rather than fix anything.

    python -m pricing_scraper.nightly
    python -m pricing_scraper.nightly --sites nykaa --skip-supervisor
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from pricing_scraper.automation import run_incremental_site
from pricing_scraper.clients.base import (
    ConfigurationError,
    build_logger,
    set_console_log_level,
)
from pricing_scraper.config import (
    apply_environment_overrides,
    default_config_path,
    load_config,
)
from pricing_scraper.database import (
    DatabaseConfigurationError,
    SupabaseCatalogStore,
)

# Amazon is deliberately absent. The hosted image installs no browser, so an
# Amazon leg here would fail every night; it is run locally instead. The three
# storefronts need no browser, so they do run here.
HOSTED_SITES = ("nykaa", "tira", "purplle", "kindlife", "broadway")

# Storefronts discover their own catalogue in a request or two and hand back
# finished products, so they do not go through the incremental machinery the
# older retailers need.
STOREFRONT_SITES = ("purplle", "kindlife", "broadway")


class StepProgress:
    """Report how far a leg of the night has got, into a log stream.

    A hosted job has no terminal to redraw, so this is not a bar: it prints a
    line at a fixed interval and nothing in between. The per-request logging
    the clients emit is far too fine-grained to follow - thousands of lines
    that never say how many are left - and it is quietened while the night
    runs so these lines are visible at all.
    """

    __slots__ = ("site", "interval", "_started", "_last", "_stage")

    def __init__(self, site: str, *, interval_seconds: float = 30.0) -> None:
        self.site = site
        self.interval = max(1.0, float(interval_seconds))
        self._started = time.monotonic()
        self._last = 0.0
        self._stage = ""

    @staticmethod
    def _clock(seconds: float) -> str:
        seconds = max(0, int(seconds))
        hours, rest = divmod(seconds, 3600)
        minutes, secs = divmod(rest, 60)
        if hours:
            return f"{hours}h{minutes:02d}m"
        if minutes:
            return f"{minutes}m{secs:02d}s"
        return f"{secs}s"

    def __call__(
        self, stage: str, done: int, total: int = 0, message: str = ""
    ) -> None:
        now = time.monotonic()
        # A stage change always prints: it is the clearest sign the leg moved
        # on, and suppressing it can leave the log silent for a long stretch.
        changed = stage != self._stage
        finished = bool(total) and done >= total
        if not changed and not finished and now - self._last < self.interval:
            return
        self._stage = stage
        self._last = now
        elapsed = now - self._started
        parts = [f"[{self.site}] {stage}", f"{done:,}"]
        if total:
            parts[-1] = f"{done:,}/{total:,} ({done / total:.0%})"
            if done and not finished:
                remaining = elapsed / done * (total - done)
                parts.append(f"~{self._clock(remaining)} left")
        parts.append(f"{self._clock(elapsed)} elapsed")
        if message:
            parts.append(message)
        print("  " + " · ".join(parts), flush=True)


@dataclass(slots=True)
class StepResult:
    """What one step of the night did."""

    name: str
    ok: bool
    seconds: float
    detail: str = ""


@dataclass(slots=True)
class NightlyReport:
    """Everything the night produced, for the log and the exit code."""

    steps: list[StepResult] = field(default_factory=list)

    @property
    def failed(self) -> list[StepResult]:
        return [step for step in self.steps if not step.ok]

    def render(self) -> str:
        lines = ["", "=" * 62, "NIGHTLY REPORT", "=" * 62]
        for step in self.steps:
            mark = "ok  " if step.ok else "FAIL"
            lines.append(
                f"  [{mark}] {step.name:<12} {step.seconds / 60:5.1f}m  {step.detail}"
            )
        total = sum(step.seconds for step in self.steps)
        lines.append("-" * 62)
        lines.append(
            f"  {len(self.steps) - len(self.failed)}/{len(self.steps)} steps ok "
            f"in {total / 60:.0f}m"
        )
        if self.failed:
            lines.append(
                "  FAILED: " + ", ".join(step.name for step in self.failed)
            )
        lines.append("=" * 62)
        return "\n".join(lines)


def _run_storefront(site, config, store, logger) -> StepResult:
    """Collect one storefront straight into the database.

    Nothing is exported here. The nightly job writes to Supabase, and the
    hosted container has no durable disk for a workbook to live on, so the
    products stream into the sink as they are read and the run record is
    closed at the end.
    """
    import importlib

    from pricing_scraper.dashboard_service import STOREFRONT_CLIENTS
    from pricing_scraper.db_sink import DatabaseSink

    started = time.monotonic()
    run_id = ""
    try:
        run_id = store.start_run(site, metadata={"mode": "nightly"})
    except Exception as exc:  # noqa: BLE001 - bookkeeping must not stop the night
        logger.warning("nightly_run_record_failed site=%s error=%s", site, exc)

    sink = DatabaseSink(store=store, site=site, run_id=run_id, logger=logger)
    report = StepProgress(site)
    module = importlib.import_module(f"pricing_scraper.clients.{site}")
    client_class = getattr(module, STOREFRONT_CLIENTS[site])
    try:
        client = client_class(
            config.get(site) or {},
            config["request"],
            brands=config.get("brands") or [],
            logger=logger,
        )
        seen = 0

        def on_product(item) -> None:
            nonlocal seen
            seen += 1
            sink.add([item])
            report("products", seen, 0, "")

        with client:
            products = client.collect(on_product=on_product)
    except Exception as exc:  # noqa: BLE001 - one storefront must not stop the night
        logger.exception("nightly_site_failed site=%s", site)
        sink.close(complete_sweep=False)
        return StepResult(
            site,
            False,
            time.monotonic() - started,
            f"{type(exc).__name__}: {exc}"[:160],
        )

    result = sink.close(complete_sweep=True)
    with_gtin = sum(1 for item in products if item.gtin)
    return StepResult(
        site,
        not result.failures,
        time.monotonic() - started,
        f"{len(products):,} collected, {with_gtin:,} with a barcode, "
        f"{result.products_written:,} written, {result.failures:,} failed batches",
    )


def _run_site(site, config, store, logger) -> StepResult:
    if site in STOREFRONT_SITES:
        return _run_storefront(site, config, store, logger)
    started = time.monotonic()
    try:
        summary = run_incremental_site(
            site=site,
            config=config,
            store=store,
            logger=logger,
            progress=StepProgress(site),
        )
    except Exception as exc:  # noqa: BLE001 - one retailer must not stop the night
        logger.exception("nightly_site_failed site=%s", site)
        return StepResult(site, False, time.monotonic() - started, f"{type(exc).__name__}: {exc}"[:160])
    return StepResult(
        site,
        summary.status in {"success", "partial"},
        time.monotonic() - started,
        f"{summary.products_seen:,} seen, {summary.products_new:,} new, "
        f"{summary.products_changed:,} changed, status={summary.status}",
    )


def _run_supervisor(config, store, logger, threshold: float) -> StepResult:
    from pricing_scraper.supervisor import reconcile_gtins

    started = time.monotonic()
    try:
        result = reconcile_gtins(
            threshold=threshold,
            brands=config.get("brands") or (),
            dry_run=False,
            store=store,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("nightly_supervisor_failed")
        return StepResult("supervisor", False, time.monotonic() - started, f"{type(exc).__name__}: {exc}"[:160])
    return StepResult(
        "supervisor",
        result.failures == 0,
        time.monotonic() - started,
        f"{result.written:,} barcodes written, {len(result.conflicts):,} conflicts",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Refresh every retailer, then propagate barcodes."
    )
    parser.add_argument(
        "--sites",
        default=",".join(HOSTED_SITES),
        help=f"Retailers to refresh, in order (default: {','.join(HOSTED_SITES)}).",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=1.0,
        help="Match confidence for barcode propagation (default: 1.0).",
    )
    parser.add_argument(
        "--skip-supervisor",
        action="store_true",
        help="Refresh the retailers and stop before propagating barcodes.",
    )
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument(
        "--verbose",
        action="store_true",
        help=(
            "Log every retailer request. Off by default: the per-request "
            "lines bury the progress reports."
        ),
    )
    return parser


def _running_revision() -> str:
    """Describe the build this process is actually running.

    A hosted job runs whatever image was last built, which is not necessarily
    the newest commit: a run triggered while a build is still in flight uses
    the previous one. That has already cost a full nightly sweep, where a
    fixed bug appeared to reoccur simply because the fix was not deployed
    yet. Stating the revision in the log makes that visible immediately
    instead of having to infer it from line numbers in a traceback.
    """
    commit = (
        os.environ.get("RENDER_GIT_COMMIT")
        or os.environ.get("GIT_COMMIT")
        or ""
    ).strip()
    branch = os.environ.get("RENDER_GIT_BRANCH", "").strip()
    if not commit:
        return "unknown (no RENDER_GIT_COMMIT in the environment)"
    return f"{commit[:7]}{f' on {branch}' if branch else ''}"


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logger = build_logger("nightly", Path("logs") / "nightly")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # The clients log a line per request. Over a full night that is tens of
    # thousands of lines saying nothing about how far along the run is, and
    # they hid the progress reports completely. The file logs still get them.
    set_console_log_level(logging.INFO if args.verbose else logging.WARNING)
    revision = _running_revision()
    logger.info("nightly_revision commit=%s", revision)
    print(f"running revision: {revision}")

    report = NightlyReport()
    try:
        config = load_config(args.config or default_config_path())
        apply_environment_overrides(config)
        store, _required = SupabaseCatalogStore.from_environment()
        if store is None:
            raise DatabaseConfigurationError(
                "The nightly job writes to Supabase. Set SUPABASE_URL and a "
                "server-side key."
            )
    except (ConfigurationError, DatabaseConfigurationError) as exc:
        logger.error("nightly_configuration_failed error=%s", exc)
        print(f"error: {exc}")
        return 2

    sites = [part.strip().casefold() for part in args.sites.split(",") if part.strip()]
    for site in sites:
        logger.info("nightly_step_started site=%s", site)
        report.steps.append(_run_site(site, config, store, logger))

    if args.skip_supervisor:
        pass
    elif not any(step.ok for step in report.steps):
        # Propagating between catalogues that were never refreshed spreads
        # yesterday's state rather than correcting anything.
        report.steps.append(
            StepResult("supervisor", False, 0.0, "skipped: no retailer refreshed")
        )
    else:
        report.steps.append(
            _run_supervisor(config, store, logger, args.threshold)
        )

    rendered = report.render()
    print(rendered)
    logger.info("nightly_finished%s", rendered.replace("\n", " | "))
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
