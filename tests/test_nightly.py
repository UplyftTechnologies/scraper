import logging
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, Mock, patch

from pricing_scraper import nightly, watchdog
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



def quiet_logger() -> logging.Logger:
    """A logger that writes nowhere, so a failing leg does not spam the run."""
    logger = logging.getLogger("nightly-test")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return logger


class RevisionReportingTests(unittest.TestCase):
    """A hosted run must say which build it is, not leave it to be inferred.

    A run triggered while a build is still in flight uses the previous image,
    so a fix that is already pushed can appear to have failed. Working that
    out from line numbers in a traceback is slow; the log should just say it.
    """

    def test_the_render_commit_is_reported_short_with_its_branch(self):
        with patch.dict(
            os.environ,
            {
                "RENDER_GIT_COMMIT": "5e3139cf10a55180816f2ac2bafa4fb072dd05b1",
                "RENDER_GIT_BRANCH": "main",
            },
            clear=False,
        ):
            self.assertEqual(nightly._running_revision(), "5e3139c on main")

    def test_a_commit_without_a_branch_still_reports(self):
        with patch.dict(
            os.environ,
            {"RENDER_GIT_COMMIT": "abcdef1234567890"},
            clear=False,
        ):
            with patch.dict(os.environ, {"RENDER_GIT_BRANCH": ""}, clear=False):
                self.assertEqual(nightly._running_revision(), "abcdef1")

    def test_an_unlabelled_build_says_so_rather_than_guessing(self):
        environment = {
            key: ""
            for key in ("RENDER_GIT_COMMIT", "GIT_COMMIT", "RENDER_GIT_BRANCH")
        }
        with patch.dict(os.environ, environment, clear=False):
            self.assertIn("unknown", nightly._running_revision())


class StorefrontLegTests(unittest.TestCase):
    """The storefronts run in the night without the incremental machinery.

    They discover their own catalogue and hand back finished products, so the
    nightly streams them into the database and closes the run, rather than
    calling run_incremental_site - which accepts only nykaa and tira.
    """

    def _config(self):
        return {
            "request": {
                "timeout_seconds": 5,
                "delay_min_seconds": 0,
                "delay_max_seconds": 0,
                "logs_dir": "logs",
            },
            "brands": ["Innisfree"],
            "broadway": {},
        }

    def test_a_storefront_is_collected_and_streamed(self):
        from pricing_scraper.models import Product

        collected = [
            Product(
                site="broadway",
                product_id="1",
                brand="Innisfree",
                product_name="Green Tea",
                gtin="8800294993574",
            )
        ]
        store = Mock()
        store.start_run.return_value = "run-1"
        store._upsert.return_value = 1

        client = MagicMock()
        client.__enter__.return_value = client
        client.collect.side_effect = lambda on_product=None, **_k: (
            [on_product(item) for item in collected] and collected
        )
        with patch(
            "pricing_scraper.clients.broadway.BroadwayClient", return_value=client
        ):
            step = nightly._run_storefront(
                "broadway", self._config(), store, quiet_logger()
            )

        self.assertTrue(step.ok)
        self.assertIn("1 collected", step.detail)
        self.assertIn("with a barcode", step.detail)
        store.start_run.assert_called_once()

    def test_a_storefront_failure_does_not_raise(self):
        """One retailer must never stop the night."""
        store = Mock()
        store.start_run.return_value = "run-2"
        client = MagicMock()
        client.__enter__.return_value = client
        client.collect.side_effect = RuntimeError("sitemap unreachable")
        with patch(
            "pricing_scraper.clients.broadway.BroadwayClient", return_value=client
        ):
            step = nightly._run_storefront(
                "broadway", self._config(), store, quiet_logger()
            )

        self.assertFalse(step.ok)
        self.assertIn("RuntimeError", step.detail)

    def test_run_site_routes_storefronts_away_from_the_incremental_path(self):
        with patch.object(nightly, "_run_storefront") as storefront:
            with patch.object(nightly, "run_incremental_site") as incremental:
                for site in nightly.STOREFRONT_SITES:
                    nightly._run_site(site, {}, Mock(), quiet_logger())
                incremental.assert_not_called()
        self.assertEqual(storefront.call_count, len(nightly.STOREFRONT_SITES))


class WatchdogCoverageTests(unittest.TestCase):
    def test_the_watchdog_watches_every_site_the_night_refreshes(self):
        """A site refreshed but unwatched can go stale without anyone noticing."""
        from pricing_scraper import watchdog as watchdog_module

        parser_default = watchdog_module.HOSTED_SITES
        self.assertEqual(tuple(parser_default), tuple(nightly.HOSTED_SITES))

    def test_amazon_stays_out_of_the_hosted_night(self):
        """The hosted image installs no browser."""
        self.assertNotIn("amazon", nightly.HOSTED_SITES)


class StepProgressTests(unittest.TestCase):
    """A hosted job has no terminal, so progress is an occasional line."""

    def lines(self, calls, *, interval=30.0):
        printed = []
        report = nightly.StepProgress("nykaa", interval_seconds=interval)
        with patch("builtins.print", lambda *a, **k: printed.append(a[0])):
            for call in calls:
                report(*call)
        return printed

    def test_a_total_gives_a_percentage_and_an_estimate(self):
        [line] = self.lines([("details", 250, 1000, "")])
        self.assertIn("250/1,000", line)
        self.assertIn("(25%)", line)
        self.assertIn("left", line)

    def test_progress_without_a_total_still_reports_the_count(self):
        [line] = self.lines([("products", 42, 0, "")])
        self.assertIn("42", line)
        self.assertNotIn("%", line)

    def test_lines_are_rate_limited_so_the_log_stays_readable(self):
        """One line per product would be the flood this replaces."""
        calls = [("details", n, 5000, "") for n in range(1, 400)]
        printed = self.lines(calls, interval=30.0)
        self.assertEqual(len(printed), 1, f"expected one line, got {len(printed)}")

    def test_a_new_stage_always_prints(self):
        printed = self.lines(
            [("listing", 1, 0, ""), ("details", 1, 0, "")], interval=999
        )
        self.assertEqual(len(printed), 2)

    def test_the_final_update_always_prints(self):
        printed = self.lines(
            [("details", 1, 10, ""), ("details", 10, 10, "")], interval=999
        )
        self.assertEqual(len(printed), 2)
        self.assertIn("(100%)", printed[-1])

    def test_the_site_is_named_so_legs_can_be_told_apart(self):
        [line] = self.lines([("listing", 5, 0, "")])
        self.assertIn("[nykaa]", line)


class ProgressWiringTests(unittest.TestCase):
    def test_the_incremental_leg_is_given_a_reporter(self):
        store = Mock()
        with patch.object(nightly, "run_incremental_site") as incremental:
            incremental.return_value = Mock(
                status="success", products_seen=1, products_new=0, products_changed=0
            )
            nightly._run_site("nykaa", {}, store, quiet_logger())
        reporter = incremental.call_args.kwargs.get("progress")
        self.assertIsInstance(reporter, nightly.StepProgress)

    def test_the_enrichers_accept_a_reporter(self):
        """Progress has to reach the detail loop; that is where the time goes."""
        import inspect

        from pricing_scraper import automation

        for name in ("_enrich_nykaa", "_enrich_tira"):
            signature = inspect.signature(getattr(automation, name))
            self.assertIn("progress", signature.parameters, name)
