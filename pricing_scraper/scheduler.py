"""CLI entry point used by Render cron jobs and local task schedulers."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any, Sequence

from pricing_scraper.automation import run_incremental_site
from pricing_scraper.clients.base import ConfigurationError, build_logger
from pricing_scraper.config import default_config_path, load_config
from pricing_scraper.database import DatabaseConfigurationError, SupabaseCatalogStore


def _apply_environment_overrides(config: dict[str, Any]) -> None:
    nykaa_command = os.getenv("NYKAA_CURL_COMMAND", "").strip()
    nykaa_file = os.getenv("NYKAA_CURL_FILE", "").strip()
    if nykaa_command:
        config["nykaa"]["curl_command"] = nykaa_command
        config["nykaa"]["curl_file"] = ""
    elif nykaa_file:
        config["nykaa"]["curl_file"] = nykaa_file
    tira_id = os.getenv("TIRA_APPLICATION_ID", "").strip()
    tira_token = os.getenv("TIRA_APPLICATION_TOKEN", "").strip()
    if tira_id:
        config["tira"]["application_id"] = tira_id
    if tira_token:
        config["tira"]["application_token"] = tira_token


def build_parser() -> argparse.ArgumentParser:
    """Build the unattended-job argument parser."""
    parser = argparse.ArgumentParser(
        description="Run one database-backed incremental retailer scrape."
    )
    parser.add_argument("--site", required=True, choices=("nykaa", "tira"))
    parser.add_argument(
        "--config",
        type=Path,
        default=default_config_path(),
        help="YAML config path.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run Nykaa or Tira once and return a cron-friendly exit code."""
    args = build_parser().parse_args(argv)
    logger = build_logger(
        f"nightly.{args.site}",
        Path("logs") / "nightly" / args.site,
    )
    try:
        config = load_config(args.config)
        _apply_environment_overrides(config)
        store, _required = SupabaseCatalogStore.from_environment()
        if store is None:
            raise DatabaseConfigurationError(
                "Nightly jobs require SUPABASE_URL and a server-side secret key."
            )
        summary = run_incremental_site(
            site=args.site,
            config=config,
            store=store,
            logger=logger,
        )
    except (ConfigurationError, DatabaseConfigurationError, ValueError) as exc:
        logger.error("nightly_configuration_failed site=%s error=%s", args.site, exc)
        return 2
    except Exception:
        return 1
    print("Scraping complete" if summary.status == "success" else "Scraping partial")
    print(
        f"site={summary.site} run_id={summary.run_id} status={summary.status} "
        f"seen={summary.products_seen} new={summary.products_new} "
        f"changed={summary.products_changed} unchanged={summary.products_unchanged} "
        f"details={summary.details_refreshed} failures={summary.failures} "
        f"blocks={summary.blocks} requests={summary.requests}"
    )
    return 0 if summary.status == "success" else 3


if __name__ == "__main__":
    raise SystemExit(main())
