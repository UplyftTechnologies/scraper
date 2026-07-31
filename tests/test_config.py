import tempfile
import unittest
from pathlib import Path

from pricing_scraper.config import load_config


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


if __name__ == "__main__":
    unittest.main()
