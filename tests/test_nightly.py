import unittest
from datetime import datetime, timedelta, timezone

from pricing_scraper import watchdog
from pricing_scraper.nightly import NightlyReport, StepResult

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def ago(hours):
    return (NOW - timedelta(hours=hours)).isoformat()


class FakeStore:
    products_table = "retailer_products"
    runs_table = "retailer_scrape_runs"
    url = "https://x.test"
    timeout_seconds = 5
    headers = {}

    def __init__(self, runs, newest):
        self.runs = runs
        self.newest = newest
        self.session = self

    def get(self, url, *, params, headers, timeout):
        del headers, timeout
        payload = (
            self.runs
            if self.runs_table in url
            else (
                [{"scraped_at": self.newest[params["site"].removeprefix("eq.")]}]
                if self.newest.get(params["site"].removeprefix("eq."))
                else []
            )
        )
        return type("R", (), {"status_code": 200, "json": lambda _s=None: payload})()


class NightlyReportTests(unittest.TestCase):
    def test_a_failed_step_is_named_in_the_report(self):
        report = NightlyReport(
            steps=[
                StepResult("nykaa", True, 120.0, "5,000 seen"),
                StepResult("tira", False, 5.0, "ConnectionError"),
            ]
        )
        rendered = report.render()
        self.assertIn("FAILED: tira", rendered)
        self.assertEqual(len(report.failed), 1)

    def test_an_all_ok_night_names_nothing(self):
        report = NightlyReport(steps=[StepResult("nykaa", True, 60.0, "ok")])
        self.assertNotIn("FAILED", report.render())


class WatchdogTests(unittest.TestCase):
    """A schedule that silently stops is worse than one that fails loudly."""

    def report(self, runs, newest):
        return watchdog.check(
            FakeStore(runs, newest), sites=["nykaa"], now=NOW
        )

    def test_a_run_still_running_after_hours_is_stuck(self):
        report = self.report(
            [{"site": "nykaa", "status": "running", "started_at": ago(19)}],
            {"nykaa": ago(1)},
        )
        self.assertTrue(any("STUCK" in f.detail for f in report.alerts))

    def test_a_run_that_finished_normally_is_healthy(self):
        report = self.report(
            [{"site": "nykaa", "status": "success", "started_at": ago(2),
              "products_seen": 5000}],
            {"nykaa": ago(2)},
        )
        self.assertEqual(report.alerts, [])

    def test_a_failed_run_alerts(self):
        report = self.report(
            [{"site": "nykaa", "status": "failed", "started_at": ago(1),
              "message": "curl session expired"}],
            {"nykaa": ago(1)},
        )
        self.assertTrue(any("failed" in f.detail for f in report.alerts))

    def test_a_schedule_that_never_fired_alerts(self):
        """No run in over a day means the cron did not run at all."""
        report = self.report(
            [{"site": "nykaa", "status": "success", "started_at": ago(40)}],
            {"nykaa": ago(2)},
        )
        self.assertTrue(any("schedule" in f.subject for f in report.alerts))

    def test_stale_data_alerts_even_when_runs_look_fine(self):
        report = self.report(
            [{"site": "nykaa", "status": "success", "started_at": ago(1)}],
            {"nykaa": ago(72)},
        )
        self.assertTrue(any("data" in f.subject for f in report.alerts))

    def test_a_site_that_never_ran_alerts(self):
        report = self.report([], {"nykaa": ago(1)})
        self.assertTrue(
            any("has ever been recorded" in f.detail for f in report.alerts)
        )

    def test_an_empty_catalogue_alerts(self):
        report = self.report(
            [{"site": "nykaa", "status": "success", "started_at": ago(1)}], {}
        )
        self.assertTrue(any("no products" in f.detail for f in report.alerts))


if __name__ == "__main__":
    unittest.main()
