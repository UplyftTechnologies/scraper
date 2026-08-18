"""Command-line interface for the supported pricing scraper."""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path
from typing import Any, Sequence

from pricing_scraper.clients.base import (
    ConfigurationError,
    set_console_log_level,
)
from pricing_scraper.config import (
    apply_environment_overrides,
    default_config_path,
    load_config,
)
from pricing_scraper.dashboard_service import (
    collect_amazon,
    collect_nykaa,
    collect_tira,
)
from pricing_scraper.exporter import ExportResult


def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Collect normalized beauty/skincare pricing data."
    )
    parser.add_argument(
        "--site",
        default="nykaa",
        choices=("nykaa", "tira", "amazon", "all"),
        help="Retailer to scrape.",
    )
    parser.add_argument(
        "--category",
        action="append",
        help="Configured category name; repeat to select multiple categories.",
    )
    parser.add_argument(
        "--all-categories",
        action="store_true",
        help="Scrape every category in the selected site's configuration.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Excel output path; defaults to output.excel_path in config.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=default_config_path(),
        help="YAML configuration path (prefers config.local.yaml when present).",
    )
    parser.add_argument(
        "--preview-limit",
        type=int,
        default=3,
        help="Number of normalized records to print (default: 3; use 0 to hide).",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help=(
            "Re-request every product. By default a run skips products whose "
            "stored record is complete, unchanged, and inside the refresh "
            "window."
        ),
    )
    parser.add_argument(
        "--gtin-only",
        action="store_true",
        help=(
            "Collect only missing barcodes for the selected site, using the "
            "cheapest request each retailer offers, and leave every other "
            "field untouched."
        ),
    )
    parser.add_argument(
        "--gtin-limit",
        type=int,
        default=0,
        help="Cap how many products a --gtin-only run requests (0 = no cap).",
    )
    parser.add_argument(
        "--refresh-all-gtins",
        action="store_true",
        help="With --gtin-only, also re-read barcodes that are already stored.",
    )
    parser.add_argument(
        "--gtin-no-sync-db",
        action="store_true",
        help=(
            "With --gtin-only, write the Excel and CSV but skip Supabase. By "
            "default a barcode sweep syncs like any other run."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help=(
            "Print every request to the console. Off by default: two lines per "
            "request buries the progress. The full log is always in "
            "logs/scraper.log either way."
        ),
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Smoke test: collect only N products per site. Writes to "
            "data/sample/ and never touches the database, so a tiny run "
            "cannot replace the real catalogue."
        ),
    )
    # Filled in by main() so each site can report "1/3", "2/3", "3/3".
    parser.set_defaults(position="")
    return parser


def _output_paths(
    args: argparse.Namespace, config: dict[str, Any]
) -> tuple[Path, Path | None]:
    output_config = config["output"]
    excel_path = args.output or Path(
        str(output_config.get("excel_path") or "data/pricing.xlsx")
    )
    if args.output:
        csv_path = excel_path.with_name(f"{excel_path.stem}_combined.csv")
    else:
        configured_csv = str(output_config.get("combined_csv_path") or "").strip()
        csv_path = Path(configured_csv) if configured_csv else None
    return excel_path, csv_path


def _apply_sample_mode(
    args: argparse.Namespace, config: dict[str, Any]
) -> None:
    """Shrink and isolate a run so it can be used as a smoke test.

    A small run is dangerous by default. Exporting replaces a site's whole
    snapshot, so collecting two products would cut that retailer down to two
    rows in the CSV and in the database, and a sweep reported complete would
    then age every product it did not see. A sample therefore writes to its own
    files and leaves the database alone entirely.
    """
    sample = max(1, int(args.sample))
    for site in ("nykaa", "tira"):
        if isinstance(config.get(site), dict):
            config[site]["page_limit"] = 1
    if isinstance(config.get("amazon"), dict):
        config["amazon"]["search_page_limit"] = 1
    # Never write to the real catalogue or the real database from a test.
    config["database"] = {"enabled": False}
    if args.output is None:
        args.output = Path("data") / "sample" / "sample.xlsx"
    print(
        f"SAMPLE MODE: {sample} product(s) per site -> {args.output}\n"
        "  database writes disabled; the real catalogue is untouched.",
        flush=True,
    )


def _print_summary(
    *,
    result: ExportResult,
    failures: int,
    blocks: int,
    requests: int,
    database_configured: bool = False,
) -> None:
    site_counts = ", ".join(
        f"{site}={count}" for site, count in sorted(result.products_by_site.items())
    )
    print("\nScrape complete")
    print(f"Products: {result.products_written} ({site_counts})")
    print(f"Failures: {failures}")
    print(f"Blocks: {blocks}")
    print(f"Requests: {requests}")
    print(f"Excel: {result.excel_path}")
    print(f"CSV: {result.csv_path}")
    if result.database_enabled:
        print(
            "Database: "
            f"{result.database_products_written} current rows, "
            f"{result.database_price_points_written} price-history points"
        )
        if result.database_error:
            print(f"Database warning: {result.database_error}")
    elif database_configured:
        # Streaming already wrote every product as it was scraped, so the
        # export had nothing left to send. Saying "disabled" here sent people
        # looking for missing credentials that were never missing.
        print("Database: written during the run (streamed, no final sync needed)")
    else:
        print("Database: disabled (add Supabase credentials to .env)")


def _duration(seconds: float) -> str:
    """Render a rough duration the way a person would say it."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


class ProgressReporter:
    """Print periodic progress with elapsed time and an estimate of what is left.

    A full run takes hours behind the request rate limit, and the per-request
    client log says a great deal about individual requests while saying nothing
    about how far through the work is. This prints one line every interval
    instead: which site, which stage, how far, and how long it is likely to
    take.

    The estimate is measured per stage rather than across the whole run,
    because the stages move at completely different speeds - a listing page
    returns dozens of products for one request, while a detail request returns
    one product.
    """

    def __init__(
        self,
        label: str,
        *,
        position: str = "",
        interval_seconds: float = 15.0,
    ) -> None:
        self.label = label
        self.position = position
        self.interval = interval_seconds
        self.started = time.monotonic()
        self._last_print = 0.0
        self._stage = ""
        self._stage_started = self.started
        self._stage_first = 0

    def _eta(self, current: int, total: int) -> str:
        done = current - self._stage_first
        if total <= 0 or done <= 0:
            return ""
        spent = time.monotonic() - self._stage_started
        remaining = (total - current) * (spent / done)
        return f" · ~{_duration(remaining)} left"

    def __call__(self, stage: str, current: int, total: int, message: str) -> None:
        now = time.monotonic()
        changed = stage != self._stage
        if changed:
            # A new stage moves at its own pace, so time it from scratch.
            self._stage = stage
            self._stage_started = now
            self._stage_first = current
        final = bool(total) and current >= total
        # A stage change is always worth a line: it is the clearest signal that
        # the run moved on, and suppressing it can leave the terminal silent
        # for minutes at a time.
        if not changed and not final and now - self._last_print < self.interval:
            return
        self._last_print = now
        where = f"{self.position} " if self.position else ""
        progress = f" {current:,}/{total:,}" if total else f" {current:,}"
        # A stage can report more done than its total: the counters include
        # work replayed from a checkpoint while the total counts only what is
        # newly outstanding. Clamp rather than print "47200%".
        percent = (
            f" ({min(100.0, current / total * 100):.0f}%)" if total else ""
        )
        print(
            f"  [{where}{self.label}] {stage}{progress}{percent} · "
            f"{_duration(now - self.started)} elapsed{self._eta(current, total)}"
            f" · {message}",
            flush=True,
        )

    def done(self) -> str:
        return _duration(time.monotonic() - self.started)


def _progress_printer(interval_seconds: float = 10.0) -> Any:
    """Progress reporter for the barcode-only sweep."""
    return ProgressReporter("gtin", interval_seconds=interval_seconds)


def run_gtin_only(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Fill in missing barcodes for one site without a full collection."""
    from pricing_scraper.gtin_scrape import collect_gtins

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sites = ("nykaa", "tira", "amazon") if args.site == "all" else (args.site,)
    for site in sites:
        print(f"\nCollecting {site} barcodes...")
        result = collect_gtins(
            config,
            site,
            output_path=args.output,
            only_missing=not args.refresh_all_gtins,
            limit=max(0, int(args.gtin_limit)),
            sync_database=not args.gtin_no_sync_db,
            progress_callback=_progress_printer(),
        )
        print(f"\n{result.summary()}")
        sources: dict[str, int] = {}
        for origin in result.found_by.values():
            sources[origin] = sources.get(origin, 0) + 1
        for origin, count in sorted(sources.items(), key=lambda item: -item[1]):
            print(f"  from {origin}: {count}")
        if result.export is not None:
            print(f"  Excel: {result.export.excel_path}")
            print(f"  CSV: {result.export.csv_path}")
        else:
            print("  Nothing changed, so the export was left alone.")
    return 0


def run_nykaa(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Scrape Nykaa with resumable page and detail checkpoints.

    Uses the same collector the dashboard does. The older direct call had no
    checkpoints, no refresh check and no progress reporting, and it exported
    only its own products - which replaced the other retailers' rows in the
    combined CSV instead of preserving them.
    """
    reporter = ProgressReporter("nykaa", position=args.position)
    result = collect_nykaa(
        config,
        args.category or [],
        int(config["nykaa"].get("page_limit", 700)),
        output_path=args.output,
        resume=True,
        enrich_details=True,
        refresh_only_stale=not args.full,
        progress_callback=reporter,
        sample_limit=args.sample,
    )
    preview_limit = max(0, args.preview_limit)
    if preview_limit:
        print(
            json.dumps(
                [item.to_dict() for item in result.products[:preview_limit]],
                ensure_ascii=False,
                indent=2,
            )
        )
    print(f"\nNykaa finished in {reporter.done()}")
    _print_summary(
        result=result.export,
        failures=result.failures,
        blocks=result.blocks,
        requests=result.requests,
        database_configured=bool(
            isinstance(config.get("database"), dict)
            and config["database"].get("enabled", False)
        ),
    )
    if not result.completed:
        reasons = ", ".join(result.stop_reasons) or "page limit"
        print(f"Checkpoint saved; run again to resume ({reasons}).")
    return 0


def run_tira(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Scrape Tira with resumable page and variant checkpoints."""
    categories = args.category or []
    reporter = ProgressReporter("tira", position=args.position)
    result = collect_tira(
        config,
        categories,
        int(config["tira"].get("page_limit", 200)),
        output_path=args.output,
        resume=True,
        enrich_details=True,
        refresh_only_stale=not args.full,
        progress_callback=reporter,
        sample_limit=args.sample,
    )
    preview_limit = max(0, args.preview_limit)
    if preview_limit:
        print(
            json.dumps(
                [
                    item.to_dict()
                    for item in result.products[:preview_limit]
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
    print(f"\nTira finished in {reporter.done()}")
    _print_summary(
        result=result.export,
        failures=result.failures,
        blocks=result.blocks,
        requests=result.requests,
        database_configured=bool(
            isinstance(config.get("database"), dict)
            and config["database"].get("enabled", False)
        ),
    )
    if not result.completed:
        reasons = ", ".join(result.stop_reasons) or "page limit"
        print(f"Checkpoint saved; run again to resume ({reasons}).")
    return 0


def run_amazon(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Scrape configured Amazon categories and public product pages."""
    reporter = ProgressReporter("amazon", position=args.position)
    result = collect_amazon(
        config,
        args.category or [],
        int(config["amazon"].get("search_page_limit", 2)),
        output_path=args.output,
        resume=True,
        refresh_only_stale=not args.full,
        progress_callback=reporter,
        sample_limit=args.sample,
    )
    preview_limit = max(0, args.preview_limit)
    if preview_limit:
        print(
            json.dumps(
                [
                    item.to_dict()
                    for item in result.products[:preview_limit]
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
    print(f"\nAmazon finished in {reporter.done()}")
    _print_summary(
        result=result.export,
        failures=result.failures,
        blocks=result.blocks,
        requests=result.requests,
        database_configured=bool(
            isinstance(config.get("database"), dict)
            and config["database"].get("enabled", False)
        ),
    )
    if not result.completed:
        reasons = ", ".join(result.stop_reasons) or "CAPTCHA or page failure"
        print(f"Checkpoint saved; run again to resume ({reasons}).")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse CLI arguments and run the selected scraper."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        set_console_log_level(logging.INFO if args.verbose else logging.WARNING)
        config = load_config(args.config)
        apply_environment_overrides(config)
        if args.sample:
            _apply_sample_mode(args, config)
        if args.gtin_only:
            return run_gtin_only(args, config)
        if args.site == "nykaa":
            return run_nykaa(args, config)
        if args.site == "tira":
            return run_tira(args, config)
        if args.site == "amazon":
            return run_amazon(args, config)
        started = time.monotonic()
        runners = (("nykaa", run_nykaa), ("tira", run_tira), ("amazon", run_amazon))
        for index, (site, runner) in enumerate(runners, start=1):
            args.position = f"{index}/{len(runners)}"
            print(f"\n=== {site} ({args.position}) ===", flush=True)
            status = runner(args, config)
            if status:
                return status
        print(
            f"\nAll three finished in {_duration(time.monotonic() - started)}"
        )
        return 0
    except (ConfigurationError, OSError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
