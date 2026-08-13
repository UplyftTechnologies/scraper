"""Command-line interface for the supported pricing scraper."""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path
from typing import Any, Sequence

from pricing_scraper.clients.base import ConfigurationError
from pricing_scraper.clients.nykaa import NykaaClient
from pricing_scraper.config import (
    apply_environment_overrides,
    default_config_path,
    load_config,
)
from pricing_scraper.dashboard_service import collect_amazon, collect_tira
from pricing_scraper.exporter import ExportResult, export_products


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


def _print_summary(
    *,
    result: ExportResult,
    failures: int,
    blocks: int,
    requests: int,
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
    else:
        print("Database: disabled (add Supabase credentials to .env)")


def _progress_printer(interval_seconds: float = 10.0) -> Any:
    """Print a progress line periodically instead of once per product.

    A barcode sweep can run for half an hour behind the request rate limit, and
    the per-request client log says nothing about how far through it is.
    """
    last = [0.0]

    def report(_stage: str, current: int, total: int, message: str) -> None:
        now = time.monotonic()
        final = bool(total) and current >= total
        if not final and current and now - last[0] < interval_seconds:
            return
        last[0] = now
        print(f"  {message}", flush=True)

    return report


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
    """Scrape Nykaa and export normalized records."""
    with NykaaClient(
        site_config=config["nykaa"],
        request_config=config["request"],
        brands=config.get("brands", ()),
    ) as client:
        requested_categories = (
            None if args.all_categories or not args.category else args.category
        )
        categories = client.select_categories(requested_categories)
        if not categories:
            raise ConfigurationError(
                "No valid Nykaa categories are configured or selected."
            )
        products = client.scrape(categories)
        preview_limit = max(0, args.preview_limit)
        if preview_limit:
            print(
                json.dumps(
                    [item.to_dict() for item in products[:preview_limit]],
                    ensure_ascii=False,
                    indent=2,
                )
            )
        if not products:
            failures = (
                client.failures + client.page_failures + client.product_failures
            )
            print(
                "\nNo products were collected. "
                f"failures={failures}, blocks={client.blocks_encountered}, "
                f"requests={client.requests_made}"
            )
            return 2

        excel_path, csv_path = _output_paths(args, config)
        database_config = config.get("database")
        result = export_products(
            products,
            excel_path,
            csv_path,
            sync_database=bool(
                isinstance(database_config, dict)
                and database_config.get("enabled", False)
            ),
        )
        failures = client.failures + client.page_failures + client.product_failures
        _print_summary(
            result=result,
            failures=failures,
            blocks=client.blocks_encountered,
            requests=client.requests_made,
        )
        return 0


def run_tira(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Scrape Tira with resumable page and variant checkpoints."""
    categories = args.category or []
    result = collect_tira(
        config,
        categories,
        int(config["tira"].get("page_limit", 200)),
        output_path=args.output,
        resume=True,
        enrich_details=True,
        refresh_only_stale=not args.full,
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
    _print_summary(
        result=result.export,
        failures=result.failures,
        blocks=result.blocks,
        requests=result.requests,
    )
    if not result.completed:
        reasons = ", ".join(result.stop_reasons) or "page limit"
        print(f"Checkpoint saved; run again to resume ({reasons}).")
    return 0


def run_amazon(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Scrape configured Amazon categories and public product pages."""
    result = collect_amazon(
        config,
        args.category or [],
        int(config["amazon"].get("search_page_limit", 2)),
        output_path=args.output,
        resume=True,
        refresh_only_stale=not args.full,
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
    _print_summary(
        result=result.export,
        failures=result.failures,
        blocks=result.blocks,
        requests=result.requests,
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
        config = load_config(args.config)
        apply_environment_overrides(config)
        if args.gtin_only:
            return run_gtin_only(args, config)
        if args.site == "nykaa":
            return run_nykaa(args, config)
        if args.site == "tira":
            return run_tira(args, config)
        if args.site == "amazon":
            return run_amazon(args, config)
        nykaa_status = run_nykaa(args, config)
        if nykaa_status:
            return nykaa_status
        tira_status = run_tira(args, config)
        if tira_status:
            return tira_status
        return run_amazon(args, config)
    except (ConfigurationError, OSError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
