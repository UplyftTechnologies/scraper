"""Propagate barcodes between platforms for products that are the same item.

A barcode belongs to the product, not to the shop. When one platform publishes
it and another does not, the missing one can be filled in - but only when the
two rows are genuinely the same product.

Matching requires the brand, the pack size, the product form, the ingredient
strength and single-versus-kit to agree, and the titles to share real words.
Two platforms publishing different barcodes for a matched pair means the match
is wrong, so those are reported and skipped rather than guessed at.

Reads and writes the Supabase catalogue. Defaults to a dry run.

Usage:
    python build_gtin_supervisor.py                  # report, write nothing
    python build_gtin_supervisor.py --apply          # write the barcodes
    python build_gtin_supervisor.py --threshold 0.95 # stricter matching
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

from pricing_scraper.config import (
    apply_environment_overrides,
    default_config_path,
    load_config,
)
from pricing_scraper.supervisor import DEFAULT_THRESHOLD, SITES, reconcile_gtins

LOGGER = logging.getLogger("supervisor")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fill missing barcodes from the platform that has them."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the barcodes. Without this the run only reports.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=(
            f"Minimum match confidence (default: {DEFAULT_THRESHOLD}). Measured "
            "100%% correct at 0.90 and 90%% at 0.70, so lower with care."
        ),
    )
    parser.add_argument(
        "--sites",
        default=",".join(SITES),
        help=f"Platforms to reconcile (default: {','.join(SITES)}).",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Write the full plan to a CSV for review.",
    )
    parser.add_argument(
        "--all-brands",
        action="store_true",
        help="Ignore SCRAPE_BRANDS and reconcile every brand in the database.",
    )
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    config = load_config(args.config or default_config_path())
    apply_environment_overrides(config)

    sites = tuple(
        part.strip().casefold() for part in args.sites.split(",") if part.strip()
    )
    result = reconcile_gtins(
        threshold=args.threshold,
        sites=sites,
        brands=() if args.all_brands else (config.get("brands") or ()),
        dry_run=not args.apply,
    )

    print(f"\n{result.summary()}")
    for item in result.filled[:20]:
        print(
            f"  {item.gtin}  ->  {item.site:7} {item.brand} | "
            f"{item.product_name[:44]}  (from {item.donor_site}, "
            f"conf {item.confidence})"
        )
    if len(result.filled) > 20:
        print(f"  ...and {len(result.filled) - 20} more")

    if result.conflicts:
        print("\nConflicts - the match is wrong, nothing written for these:")
        for clash in result.conflicts[:10]:
            print(
                f"  {clash.brand} | {clash.product_name[:40]}: "
                f"{clash.left_site}={clash.left_gtin} vs "
                f"{clash.right_site}={clash.right_gtin} (conf {clash.confidence})"
            )
        if len(result.conflicts) > 10:
            print(f"  ...and {len(result.conflicts) - 10} more")

    if args.report:
        output = args.report.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "gtin",
                    "site",
                    "product_id",
                    "brand",
                    "product_name",
                    "size",
                    "donor_site",
                    "donor_product_id",
                    "confidence",
                ]
            )
            for item in result.filled:
                writer.writerow(
                    [
                        item.gtin,
                        item.site,
                        item.product_id,
                        item.brand,
                        item.product_name,
                        item.size,
                        item.donor_site,
                        item.donor_product_id,
                        item.confidence,
                    ]
                )
        print(f"\nPlan written to {output}")

    if result.dry_run:
        print("\nDry run: nothing was written. Re-run with --apply.")
    else:
        print(f"\nWrote {result.written:,} barcode(s), {result.failures:,} failed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
