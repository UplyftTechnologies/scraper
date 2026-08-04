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
