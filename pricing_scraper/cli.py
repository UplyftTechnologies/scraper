"""Command-line interface for the supported pricing scraper."""

from __future__ import annotations

import argparse
import json
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
