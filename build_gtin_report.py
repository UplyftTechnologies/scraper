"""Report the GTIN for products sold on more than one platform.

Amazon India never publishes a barcode on a beauty page, so an Amazon listing
can only be given one by matching it to a platform that does. This script
matches products across platforms, then fills each match's barcode from
whichever member carries one.

Nykaa publishes a GTIN per SKU but only returns it from its product-detail
endpoint, so a catalogue collected before that field was stored has none. Pass
``--refresh-nykaa`` to re-request the detail for the matched Nykaa products
only - a few dozen requests rather than a full re-scrape.

Usage:
    python build_gtin_report.py
    python build_gtin_report.py --sites amazon,nykaa --refresh-nykaa
    python build_gtin_report.py --sites amazon,nykaa,tira --output data/gtin.xlsx
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

from pricing_scraper.comparison import (
    ComparisonInputError,
    Match,
    load_retailer_products,
    match_products,
)
from pricing_scraper.config import (
    apply_environment_overrides,
    default_config_path,
    load_config,
)
from pricing_scraper.models import Product, normalize_gtin

LOGGER = logging.getLogger("gtin")

# Platforms that publish a usable barcode, best source first. Nykaa is
# preferred because it publishes one per SKU; Amazon is last because its
# barcode is inferred from the model-number row rather than a barcode field.
GTIN_SOURCES = ("nykaa", "tira", "amazon")

COLUMNS = (
    "gtin",
    "gtin_source",
    "brand",
    "product",
    "form",
    "size",
    "platforms",
    "confidence",
    "amazon_asin",
    "amazon_name",
    "amazon_price",
    "amazon_url",
    "nykaa_product_id",
    "nykaa_name",
    "nykaa_price",
    "nykaa_url",
    "tira_sku",
    "tira_name",
    "tira_price",
    "tira_url",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report GTINs for products matched across platforms."
    )
    parser.add_argument(
        "--sites",
        default="amazon,nykaa",
        help="Only report matches present on all of these (default: amazon,nykaa).",
    )
    parser.add_argument(
        "--refresh-nykaa",
        action="store_true",
        help="Re-request Nykaa product details to read each matched SKU's GTIN.",
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
        default=Path("data/gtin_matches.csv"),
        help="CSV output path (default: data/gtin_matches.csv).",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.70,
        help="Minimum match confidence, 0-1 (default: 0.70).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="YAML config used for the default CSV path and Nykaa session.",
    )
    parser.add_argument("--verbose", action="store_true", help="Debug logging.")
    return parser.parse_args(argv)


def required_sites(value: str) -> tuple[str, ...]:
    return tuple(
        part.strip().casefold()
        for part in str(value or "").split(",")
        if part.strip()
    )


def backfill_amazon_gtins(products: Sequence[Product]) -> int:
    """Recover Amazon barcodes from attributes that were already scraped.

    Amazon publishes no barcode row, so its GTIN is read from the model or part
    number. A catalogue collected before that was supported still holds the raw
    attributes, and the barcode can be recomputed from them without re-opening
    a single product page.
    """
    from pricing_scraper.clients.amazon import _attribute_gtin

    recovered = 0
    for product in products:
        if product.site.casefold() != "amazon" or product.gtin:
            continue
        attributes = product.product_attributes
        if not isinstance(attributes, dict) or not attributes:
            continue
        gtin = _attribute_gtin(attributes)
        if gtin:
            product.gtin = gtin
            recovered += 1
    if recovered:
        LOGGER.info("Recovered %s Amazon barcode(s) from stored attributes", recovered)
    return recovered


def matches_on(
    matches: Iterable[Match],
    wanted: Sequence[str],
) -> list[Match]:
    """Keep only the matches present on every requested platform."""
    return [
        match
        for match in matches
        if all(site in match.members for site in wanted)
    ]


def refresh_nykaa_gtins(
    matches: Sequence[Match],
    config: dict[str, Any],
) -> dict[str, str]:
    """Read the current GTIN for each matched Nykaa SKU.

    Only the matched products are requested, so this costs one detail call per
    parent rather than a full catalogue sweep. A parent that fails is skipped:
    a missing barcode is worth far less than the rest of the report.
    """
    from pricing_scraper.clients.nykaa import NykaaClient

    wanted: dict[str, Product] = {}
    for match in matches:
        item = match.members.get("nykaa")
        if item is not None:
            wanted[item.product.product_id] = item.product
    if not wanted:
        return {}

    found: dict[str, str] = {}
    LOGGER.info("Requesting Nykaa details for %s matched product(s)", len(wanted))
    with NykaaClient(
        site_config=config["nykaa"],
        request_config=config["request"],
        brands=config.get("brands", ()),
    ) as client:
        for index, (product_id, product) in enumerate(wanted.items(), start=1):
            try:
                rows = client.fetch_product_details(product)
            except Exception as exc:  # noqa: BLE001 - one product must not stop the report
                LOGGER.warning("nykaa_detail_failed product_id=%s: %s", product_id, exc)
                continue
            for row in rows:
                gtin = normalize_gtin(row.gtin)
                if gtin:
                    found[row.product_id] = gtin
            if index % 10 == 0 or index == len(wanted):
                LOGGER.info("  %s/%s parents requested", index, len(wanted))
    LOGGER.info("Nykaa returned %s barcode(s)", len(found))
    return found


def resolve_gtin(
    match: Match,
    nykaa_gtins: dict[str, str],
) -> tuple[str, str]:
    """Return the match's barcode and the platform it came from."""
    nykaa = match.members.get("nykaa")
    if nykaa is not None:
        refreshed = nykaa_gtins.get(nykaa.product.product_id, "")
        if refreshed:
            return refreshed, "nykaa"
    for site in GTIN_SOURCES:
        item = match.members.get(site)
        if item is not None and item.gtin:
            return item.gtin, site
    return "", ""


def report_rows(
    matches: Sequence[Match],
    nykaa_gtins: dict[str, str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for match in matches:
        gtin, source = resolve_gtin(match, nykaa_gtins)
        anchor = match.anchor
        row: dict[str, Any] = {
            "gtin": gtin,
            "gtin_source": source,
            "brand": anchor.product.brand,
            "product": anchor.name,
            "form": anchor.form,
            "size": anchor.size.label() if anchor.size else "",
            "platforms": ", ".join(match.sites),
            "confidence": round(match.confidence, 3),
        }
        for site, identifier in (
            ("amazon", "amazon_asin"),
            ("nykaa", "nykaa_product_id"),
            ("tira", "tira_sku"),
        ):
            item = match.members.get(site)
            product = item.product if item else None
            row[identifier] = (
                (product.sku or product.product_id) if product else ""
            )
            row[f"{site}_name"] = product.product_name if product else ""
            row[f"{site}_price"] = product.selling_price if product else None
            row[f"{site}_url"] = product.product_url if product else ""
        rows.append(row)
    # Rows that carry a barcode are the point of the report, so lead with them.
    rows.sort(
        key=lambda item: (
            not item["gtin"],
            str(item["brand"]).casefold(),
            str(item["product"]).casefold(),
        )
    )
    return rows


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

    wanted = required_sites(args.sites)
    if not wanted:
        LOGGER.error("--sites needs at least one platform name.")
        return 1

    # Do this before matching: a barcode on both sides is the strongest match
    # signal the comparison has, and it also rejects pairs that disagree.
    backfill_amazon_gtins(products)

    report = match_products(products, threshold=args.threshold)
    selected = matches_on(report.matches, wanted)
    LOGGER.info(
        "%s match(es) present on %s",
        len(selected),
        " and ".join(wanted),
    )
    if not selected:
        LOGGER.error("Nothing to report for %s.", ", ".join(wanted))
        return 1

    nykaa_gtins: dict[str, str] = {}
    if args.refresh_nykaa and "nykaa" in wanted:
        nykaa_gtins = refresh_nykaa_gtins(selected, config)

    rows = report_rows(selected, nykaa_gtins)
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    resolved = [row for row in rows if row["gtin"]]
    by_source: dict[str, int] = {}
    for row in resolved:
        by_source[row["gtin_source"]] = by_source.get(row["gtin_source"], 0) + 1

    print(f"\nProducts on {' + '.join(wanted)}: {len(rows)}")
    print(f"With a GTIN: {len(resolved)}  ({len(resolved) / len(rows) * 100:.0f}%)")
    for site, count in sorted(by_source.items(), key=lambda item: -item[1]):
        print(f"  from {site}: {count}")
    missing = len(rows) - len(resolved)
    if missing:
        print(f"Without a GTIN: {missing}")
        if not args.refresh_nykaa and "nykaa" in wanted:
            print("  Re-run with --refresh-nykaa to read them from Nykaa.")
    print(f"CSV: {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
