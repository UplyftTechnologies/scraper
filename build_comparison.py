"""Build a cross-platform price comparison sheet.

Retailer rows come from Supabase when it is configured, otherwise from the
combined CSV export. The own catalogue (Roopsee) is read from a CSV or Excel
file you drop in; its headers are matched by name, so a Shopify-style export
works without editing.

Output is data/comparison.xlsx with three sheets - every match, the ones worth
reviewing, and products seen on a single platform - plus the same match rows as
data/comparison.csv.

Usage:
    python build_comparison.py --own data/roopsee_catalogue.csv
    python build_comparison.py --source csv --threshold 0.78
    python build_comparison.py --own catalogue.xlsx --brand "Minimalist,COSRX"
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from pricing_scraper.comparison import (
    ROOPSEE_SITE,
    ComparisonInputError,
    load_own_catalogue,
    load_retailer_products,
    match_products,
    write_comparison,
)
from pricing_scraper.config import (
    apply_environment_overrides,
    default_config_path,
    load_config,
    parse_brand_filter,
)
from pricing_scraper.models import brand_key

LOGGER = logging.getLogger("comparison")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Match products across platforms and export a comparison."
    )
    parser.add_argument(
        "--own",
        type=Path,
        default=None,
        help="CSV/Excel export of your own catalogue (Roopsee).",
    )
    parser.add_argument(
        "--own-site",
        default=ROOPSEE_SITE,
        help=f"Column prefix for the own catalogue (default: {ROOPSEE_SITE}).",
    )
    parser.add_argument(
        "--source",
        choices=("auto", "db", "csv"),
        default="auto",
        help="Where retailer rows come from (default: database, else the CSV).",
    )
    parser.add_argument(
        "--retailer-csv",
        type=Path,
        default=None,
        help="Combined CSV export to read when not using the database.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/comparison.xlsx"),
        help="Excel output path (default: data/comparison.xlsx).",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.70,
        help="Minimum match confidence, 0-1 (default: 0.70).",
    )
    parser.add_argument(
        "--review-below",
        type=float,
        default=0.80,
        help="Matches under this confidence also go to the review sheet.",
    )
    parser.add_argument(
        "--brand",
        default="",
        help="Restrict the comparison to these brands (comma-separated).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="YAML config used for the default CSV path.",
    )
    parser.add_argument("--verbose", action="store_true", help="Debug logging.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    config = load_config(args.config or default_config_path())
    apply_environment_overrides(config)
    retailer_csv = args.retailer_csv or Path(
        str(config["output"].get("combined_csv_path") or "data/pricing_combined.csv")
    )

    try:
        products = load_retailer_products(
            csv_path=retailer_csv,
            use_database=args.source in {"auto", "db"},
        )
    except ComparisonInputError as exc:
        LOGGER.error("%s", exc)
        return 1

    own_site = str(args.own_site).casefold().strip() or ROOPSEE_SITE
    if args.own is not None:
        try:
            own = load_own_catalogue(args.own, site=own_site)
        except ComparisonInputError as exc:
            LOGGER.error("%s", exc)
            return 1
        LOGGER.info("%s: %s rows from %s", own_site, len(own), args.own)
        products = [*own, *products]
    else:
        LOGGER.warning(
            "No --own catalogue given: comparing the scraped retailers only."
        )

    brands = parse_brand_filter(args.brand)
    if brands:
        wanted = {brand_key(brand) for brand in brands}
        products = [
            product for product in products if brand_key(product.brand) in wanted
        ]
        LOGGER.info("Brand filter kept %s rows", len(products))
    if not products:
        LOGGER.error("Nothing to compare after loading and filtering.")
        return 1

    report = match_products(products, threshold=args.threshold)
    LOGGER.info(
        "Rows per platform: %s",
        ", ".join(f"{site}={count}" for site, count in report.items_by_site.items()),
    )

    result = write_comparison(
        report,
        args.output,
        own_site=own_site,
        review_below=args.review_below,
    )
    print(f"\nMatched products: {result.matches_written}")
    for combination, count in sorted(
        result.matches_by_platforms.items(), key=lambda item: -item[1]
    ):
        print(f"  {combination}: {count}")
    print(f"Needs review: {result.review_rows}")
    print(f"Single-platform products: {result.unmatched_rows}")
    print(f"Excel: {result.excel_path}")
    print(f"CSV: {result.csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
