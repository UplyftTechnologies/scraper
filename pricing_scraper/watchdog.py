"""Report whether the nightly scraping is actually healthy.

A scheduled job that silently stops is worse than one that fails loudly:
nothing alerts, the dashboard keeps showing yesterday's numbers, and the
catalogue quietly rots. This reads the run records and the catalogue itself and
answers three questions a person would ask.

    Is a run stuck?      started long ago, never finished
    Did last night run?  or did the schedule simply not fire
    Is the data fresh?   or is every product older than it should be

Exit code 0 means healthy, 1 means something needs attention, so a cron entry
or an uptime check can act on it without parsing the text.

    python -m pricing_scraper.watchdog
    python -m pricing_scraper.watchdog --max-age-hours 30
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from pricing_scraper.config import (
    apply_environment_overrides,
    default_config_path,
    load_config,
)
from pricing_scraper.database import SupabaseCatalogStore

LOGGER = logging.getLogger("pricing_scraper.watchdog")

# A run still marked running after this long is not running any more. The
# longest observed legitimate sweep is a few hours, so this leaves generous room
# before calling one stuck.
STUCK_AFTER_HOURS = 8
# How stale the newest product may be before the schedule is presumed missed.
MAX_DATA_AGE_HOURS = 30


@dataclass(slots=True)
class Finding:
    """One thing worth telling someone about."""

    level: str          # "ok" | "warn" | "alert"
    subject: str
    detail: str


@dataclass(slots=True)
class HealthReport:
    findings: list[Finding] = field(default_factory=list)

    @property
    def alerts(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "alert"]

    def add(self, level: str, subject: str, detail: str) -> None:
        self.findings.append(Finding(level, subject, detail))

    def render(self) -> str:
        mark = {"ok": "ok   ", "warn": "warn ", "alert": "ALERT"}
        lines = ["", "=" * 62, "SCRAPER HEALTH", "=" * 62]
        for finding in self.findings:
            lines.append(
                f"  [{mark[finding.level]}] {finding.subject:<22} {finding.detail}"
            )
        lines.append("-" * 62)
        lines.append(
            "  HEALTHY" if not self.alerts else f"  {len(self.alerts)} ALERT(S)"
        )
        lines.append("=" * 62)
        return "\n".join(lines)


def _parse(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def check(
    store: Any,
    *,
    sites: Sequence[str],
    stuck_after_hours: int = STUCK_AFTER_HOURS,
    max_age_hours: int = MAX_DATA_AGE_HOURS,
    now: datetime | None = None,
) -> HealthReport:
    """Inspect run records and catalogue freshness for each retailer."""
    moment = now or datetime.now(timezone.utc)
    report = HealthReport()

    response = store.session.get(
        f"{store.url}/rest/v1/{store.runs_table}",
        params={
            "select": "site,status,started_at,finished_at,products_seen,message",
            "order": "started_at.desc",
            "limit": "40",
        },
        headers=dict(store.headers),
        timeout=store.timeout_seconds,
    )
    runs = response.json() if response.status_code == 200 else []

    for site in sites:
        site_runs = [r for r in runs if r.get("site") == site]
        if not site_runs:
            report.add("alert", f"{site} runs", "no run has ever been recorded")
            continue

        latest = site_runs[0]
        started = _parse(latest.get("started_at"))
        age = (moment - started).total_seconds() / 3600 if started else None
        status = str(latest.get("status") or "?")

        if status == "running" and age is not None and age > stuck_after_hours:
            report.add(
                "alert",
                f"{site} run",
                f"STUCK: started {age:.0f}h ago and never finished",
            )
        elif status == "failed":
            report.add(
                "alert",
                f"{site} run",
                f"last run failed: {str(latest.get('message'))[:70]}",
            )
        elif age is not None and age > max_age_hours:
            report.add(
                "alert",
                f"{site} schedule",
                f"no run started in {age:.0f}h - did the schedule fire?",
            )
        else:
            report.add(
                "ok",
                f"{site} run",
                f"{status}, started {age:.1f}h ago, "
                f"{latest.get('products_seen') or 0:,} seen",
            )

    # Freshness of the catalogue itself, which is what actually matters.
    for site in sites:
        r = store.session.get(
            f"{store.url}/rest/v1/{store.products_table}",
            params={
                "select": "scraped_at",
                "site": f"eq.{site}",
                "order": "scraped_at.desc",
                "limit": "1",
            },
            headers=dict(store.headers),
            timeout=store.timeout_seconds,
        )
        rows = r.json() if r.status_code == 200 else []
        newest = _parse(rows[0].get("scraped_at")) if rows else None
        if newest is None:
            report.add("alert", f"{site} data", "no products in the database")
            continue
        hours = (moment - newest).total_seconds() / 3600
        level = "alert" if hours > max_age_hours else "ok"
        report.add(level, f"{site} data", f"newest product {hours:.0f}h old")

    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report whether the nightly scraping is healthy."
    )
    parser.add_argument("--sites", default="nykaa,tira")
    parser.add_argument("--stuck-after-hours", type=int, default=STUCK_AFTER_HOURS)
    parser.add_argument("--max-age-hours", type=int, default=MAX_DATA_AGE_HOURS)
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    config = load_config(args.config or default_config_path())
    apply_environment_overrides(config)
    store, _ = SupabaseCatalogStore.from_environment()
    if store is None:
        print("error: the watchdog reads Supabase; set SUPABASE_URL and a key.")
        return 2

    report = check(
        store,
        sites=[s.strip() for s in args.sites.split(",") if s.strip()],
        stuck_after_hours=args.stuck_after_hours,
        max_age_hours=args.max_age_hours,
    )
    print(report.render())
    return 1 if report.alerts else 0


if __name__ == "__main__":
    raise SystemExit(main())
