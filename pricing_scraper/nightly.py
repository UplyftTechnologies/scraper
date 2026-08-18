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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from pricing_scraper.automation import run_incremental_site
from pricing_scraper.clients.base import ConfigurationError, build_logger
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
# Amazon leg here would fail every night; it is run locally instead.
HOSTED_SITES = ("nykaa", "tira")


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


def _run_site(site, config, store, logger) -> StepResult:
    started = time.monotonic()
    try:
        summary = run_incremental_site(
            site=site, config=config, store=store, logger=logger
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logger = build_logger("nightly", Path("logs") / "nightly")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

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
