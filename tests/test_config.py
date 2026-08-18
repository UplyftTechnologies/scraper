import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pricing_scraper import config as config_module
from pricing_scraper.config import (
    apply_environment_overrides,
    load_config,
    parse_brand_filter,
)


class ConfigTests(unittest.TestCase):
    def test_private_config_deep_merges_and_resolves_curl_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "base.yaml").write_text(
                """
request:
  max_retries: 4
nykaa:
  page_limit: 50
  curl_command: placeholder
output:
  excel_path: data/pricing.xlsx
""".strip(),
                encoding="utf-8",
            )
            (root / "local.yaml").write_text(
                """
extends: base.yaml
nykaa:
  page_limit: 2
  curl_file: private/request.txt
""".strip(),
                encoding="utf-8",
            )
            config = load_config(root / "local.yaml")
            self.assertEqual(config["request"]["max_retries"], 4)
            self.assertEqual(config["nykaa"]["page_limit"], 2)
            self.assertEqual(
                config["nykaa"]["curl_file"],
                str((root / "private" / "request.txt").resolve()),
            )


class BrandFilterEnvironmentTests(unittest.TestCase):
    def base_config(self) -> dict:
        return {"nykaa": {}, "tira": {}, "brands": ["Configured Brand"]}

    def test_parses_and_deduplicates_a_brand_list(self):
        self.assertEqual(
            parse_brand_filter(" COSRX , Laneige ,,\nAnua , cosrx "),
            ["COSRX", "Laneige", "Anua"],
        )
        self.assertEqual(parse_brand_filter(""), [])
        self.assertEqual(parse_brand_filter(None), [])

    def test_env_file_brands_replace_the_configured_list(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text("SCRAPE_BRANDS=COSRX,Laneige\n", encoding="utf-8")
            config = self.base_config()
            with patch.object(config_module, "ENV_FILE", env_path), patch.dict(
                os.environ, {}, clear=True
            ):
                apply_environment_overrides(config)
            self.assertEqual(config["brands"], ["COSRX", "Laneige"])

    def test_real_environment_wins_over_the_env_file(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text("SCRAPE_BRANDS=COSRX\n", encoding="utf-8")
            config = self.base_config()
            with patch.object(config_module, "ENV_FILE", env_path), patch.dict(
                os.environ, {"SCRAPE_BRANDS": "Anua"}, clear=True
            ):
                apply_environment_overrides(config)
            self.assertEqual(config["brands"], ["Anua"])

    def test_blank_variable_keeps_the_configured_list(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text("SCRAPE_BRANDS=\n", encoding="utf-8")
            config = self.base_config()
            with patch.object(config_module, "ENV_FILE", env_path), patch.dict(
                os.environ, {}, clear=True
            ):
                apply_environment_overrides(config)
            self.assertEqual(config["brands"], ["Configured Brand"])


if __name__ == "__main__":
    unittest.main()


class ProgressReporterTests(unittest.TestCase):
    """A run takes hours; the operator needs to see where it is."""

    def reporter(self, **kwargs):
        from pricing_scraper.cli import ProgressReporter

        return ProgressReporter("nykaa", interval_seconds=0, **kwargs)

    def test_it_reports_position_percentage_and_elapsed(self):
        from io import StringIO
        from contextlib import redirect_stdout

        out = StringIO()
        with redirect_stdout(out):
            self.reporter(position="1/3")("details", 25, 100, "working")
        line = out.getvalue()
        self.assertIn("1/3 nykaa", line)
        self.assertIn("25/100", line)
        self.assertIn("(25%)", line)
        self.assertIn("elapsed", line)

    def test_an_estimate_needs_a_baseline_first(self):
        """One sample says nothing about the rate, so no estimate is offered."""
        from io import StringIO
        from contextlib import redirect_stdout

        reporter = self.reporter()
        out = StringIO()
        with redirect_stdout(out):
            reporter("details", 10, 100, "first")
        self.assertNotIn("left", out.getvalue())

        out = StringIO()
        reporter._stage_started -= 60
        with redirect_stdout(out):
            reporter("details", 60, 100, "later")
        self.assertIn("left", out.getvalue())

    def test_a_new_stage_is_timed_from_scratch(self):
        """Stages move at different speeds, so one cannot estimate the next."""
        reporter = self.reporter()
        reporter("listing", 5, 17, "a")
        first_started = reporter._stage_started
        reporter("details", 0, 5000, "b")
        self.assertNotEqual(reporter._stage_started, first_started)
        self.assertEqual(reporter._stage_first, 0)

    def test_a_stage_with_no_total_still_reports(self):
        from io import StringIO
        from contextlib import redirect_stdout

        out = StringIO()
        with redirect_stdout(out):
            self.reporter()("sku_rows", 1512, 0, "rows ready")
        line = out.getvalue()
        self.assertIn("1,512", line)
        self.assertNotIn("%", line)

    def test_durations_read_the_way_a_person_would_say_them(self):
        from pricing_scraper.cli import _duration

        self.assertEqual(_duration(12), "12s")
        self.assertEqual(_duration(95), "1m")
        self.assertEqual(_duration(2500), "41m")
        self.assertEqual(_duration(11400), "3h 10m")


class SampleModeTests(unittest.TestCase):
    """A tiny run must not be able to replace the real catalogue."""

    def args(self, **overrides):
        import argparse

        values = {"sample": 2, "output": None}
        values.update(overrides)
        return argparse.Namespace(**values)

    def base_config(self):
        return {
            "nykaa": {"page_limit": 700},
            "tira": {"page_limit": 200},
            "amazon": {"search_page_limit": 2},
            "database": {"enabled": True},
            "output": {
                "excel_path": "data/pricing.xlsx",
                "combined_csv_path": "data/pricing_combined.csv",
            },
        }

    def test_the_database_is_switched_off(self):
        """Exporting two products would otherwise shrink the site to two rows."""
        from pricing_scraper.cli import _apply_sample_mode

        config = self.base_config()
        _apply_sample_mode(self.args(), config)
        self.assertFalse(config["database"]["enabled"])

    def test_output_is_redirected_away_from_the_real_files(self):
        from pathlib import Path

        from pricing_scraper.cli import _apply_sample_mode

        args = self.args()
        _apply_sample_mode(args, self.base_config())
        self.assertIn("sample", str(args.output))
        self.assertNotEqual(Path(args.output), Path("data/pricing.xlsx"))

    def test_an_explicit_output_is_respected(self):
        from pathlib import Path

        from pricing_scraper.cli import _apply_sample_mode

        args = self.args(output=Path("somewhere/mine.xlsx"))
        _apply_sample_mode(args, self.base_config())
        self.assertEqual(args.output, Path("somewhere/mine.xlsx"))

    def test_page_limits_drop_to_one(self):
        from pricing_scraper.cli import _apply_sample_mode

        config = self.base_config()
        _apply_sample_mode(self.args(), config)
        self.assertEqual(config["nykaa"]["page_limit"], 1)
        self.assertEqual(config["tira"]["page_limit"], 1)
        self.assertEqual(config["amazon"]["search_page_limit"], 1)
