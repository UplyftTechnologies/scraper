import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pricing_scraper.clients.base import RequestFailed
from pricing_scraper.clients.nykaa import CategoryScrapeResult
from pricing_scraper.dashboard_service import (
    _full_catalog_partitions,
    collect_nykaa,
    collect_tira,
)
from pricing_scraper.models import Product


class DashboardServiceTests(unittest.TestCase):
    def test_full_catalog_expands_to_non_overlapping_price_partitions(self):
        selected = [
            {
                "id": "8377",
                "name": "All Skincare",
                "covers_all": True,
                "partitions": [
                    {
                        "key": "low",
                        "name": "Low",
                        "query": {"price_range_filter": "0-499"},
                    },
                    {
                        "key": "high",
                        "name": "High",
                        "query": {"price_range_filter": "500-*"},
                    },
                ],
            }
        ]

        partitions = _full_catalog_partitions(None, selected)  # type: ignore[arg-type]

        self.assertEqual(
            [item["checkpoint_key"] for item in partitions],
            ["8377_low", "8377_high"],
        )
        self.assertEqual(
            partitions[0]["query"],
            {"price_range_filter": "0-499"},
        )

    def test_enriches_discovered_products_when_listing_is_incomplete(self):
        class FakeNykaaClient:
            start_page = 1

            def __init__(self, *_args, **_kwargs):
                self.failures = 0
                self.page_failures = 0
                self.product_failures = 0
                self.detail_failures = 0
                self.blocks_encountered = 0
                self.requests_made = 2
                self.logger = logging.getLogger("fake-nykaa")

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def select_categories(self, _names):
                return [{"id": "10", "name": "Test"}]

            def scrape_category_resumable(
                self,
                _category,
                *,
                start_page,
                seen_product_ids,
                on_page,
            ):
                del start_page, seen_product_ids
                listing = Product(
                    site="nykaa",
                    product_id="sku-1",
                    parent_product_id="parent-1",
                    brand="Brand",
                    product_name="Cleanser",
                )
                on_page(1, [listing])
                return CategoryScrapeResult(
                    products=[listing],
                    next_page=2,
                    completed=False,
                    pages_scraped=1,
                    stop_reason="page_limit",
                )

            def fetch_product_details(self, product):
                return [
                    Product(
                        site="nykaa",
                        product_id=product.product_id,
                        parent_product_id=product.parent_product_id,
                        brand=product.brand,
                        product_name=product.product_name,
                        description="Gentle cleanser",
                        description_html="<p>Gentle cleanser</p>",
                        ingredients="Water",
                        how_to_use="Massage and rinse",
                    )
                ]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {
                "nykaa": {
                    "page_limit": 1,
                    "checkpoint_dir": str(root / "checkpoints"),
                    "details": {"enabled": True},
                },
                "request": {},
                "brands": [],
                "output": {
                    "excel_path": str(root / "pricing.xlsx"),
                    "combined_csv_path": str(root / "pricing.csv"),
                },
            }
            with patch(
                "pricing_scraper.dashboard_service.NykaaClient",
                FakeNykaaClient,
            ):
                result = collect_nykaa(
                    config,
                    ["Test"],
                    1,
                    enrich_details=True,
                )

        self.assertFalse(result.completed)
        self.assertEqual(
            result.products[0].description_html,
            "<p>Gentle cleanser</p>",
        )
        self.assertEqual(result.products[0].ingredients, "Water")
        self.assertEqual(result.products[0].how_to_use, "Massage and rinse")

    def test_nykaa_404_detail_is_checkpointed_with_listing_fallback(self):
        class FakeNykaaClient:
            start_page = 1

            def __init__(self, *_args, **_kwargs):
                self.failures = 0
                self.page_failures = 0
                self.product_failures = 0
                self.detail_failures = 0
                self.blocks_encountered = 0
                self.requests_made = 1
                self.logger = logging.getLogger("fake-nykaa-404")

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def select_categories(self, _names):
                return [{"id": "10", "name": "Test"}]

            def scrape_category_resumable(
                self,
                _category,
                *,
                start_page,
                seen_product_ids,
                on_page,
            ):
                del start_page, seen_product_ids
                listing = Product(
                    site="nykaa",
                    product_id="sku-missing",
                    parent_product_id="parent-missing",
                    brand="Archived Brand",
                    product_name="Archived Product",
                    selling_price=499,
                )
                on_page(1, [listing])
                return CategoryScrapeResult(
                    products=[listing],
                    next_page=2,
                    completed=True,
                    pages_scraped=1,
                    stop_reason="empty_page",
                )

            def fetch_product_details(self, _product):
                raise RequestFailed(
                    "HTTP 404",
                    status_code=404,
                    attempts=1,
                    response_text='{"message":"No result found"}',
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint_dir = root / "checkpoints"
            config = {
                "nykaa": {
                    "page_limit": 1,
                    "checkpoint_dir": str(checkpoint_dir),
                    "details": {"enabled": True},
                },
                "request": {},
                "brands": [],
                "output": {
                    "excel_path": str(root / "pricing.xlsx"),
                    "combined_csv_path": str(root / "pricing.csv"),
                },
            }
            with patch(
                "pricing_scraper.dashboard_service.NykaaClient",
                FakeNykaaClient,
            ):
                result = collect_nykaa(
                    config,
                    ["Test"],
                    1,
                    enrich_details=True,
                )

            processed = "\n".join(
                path.read_text(encoding="utf-8")
                for path in checkpoint_dir.glob("*.details.processed.txt")
            )

        self.assertTrue(result.completed)
        self.assertEqual(result.failures, 0)
        self.assertEqual(result.products[0].product_id, "sku-missing")
        self.assertEqual(result.products[0].selling_price, 499)
        self.assertIn("parent-missing", processed)

    def test_tira_enriches_variant_price_and_checkpoints_incomplete_listing(self):
        class FakeTiraClient:
            start_page = 1

            def __init__(self, *_args, **_kwargs):
                self.failures = 0
                self.page_failures = 0
                self.product_failures = 0
                self.detail_failures = 0
                self.blocks_encountered = 0
                self.requests_made = 2
                self.logger = logging.getLogger("fake-tira")

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def select_categories(self, _names):
                return [{"id": "skin", "name": "All Skin"}]

            def scrape_category_resumable(
                self,
                _category,
                *,
                start_page,
                seen_product_ids,
                on_page,
            ):
                del start_page, seen_product_ids
                listing = Product(
                    site="tira",
                    product_id="variant-1",
                    parent_product_id="parent-1",
                    brand="Brand",
                    product_name="Cleanser",
                    variant="100 ml",
                    description_html="<p>Gentle cleanser</p>",
                    ingredients="Water",
                    how_to_use="Massage and rinse",
                )
                on_page(1, [listing])
                return CategoryScrapeResult(
                    products=[listing],
                    next_page=2,
                    completed=False,
                    pages_scraped=1,
                    stop_reason="page_limit",
                )

            def fetch_variant_price(self, product):
                product.sku = "TIRA-SKU"
                product.mrp = 1000
                product.selling_price = 750
                product.discount_pct = 25
                product.in_stock = True
                product.scraped_at = "2026-07-28T12:00:00+00:00"
                return product

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {
                "nykaa": {},
                "tira": {
                    "page_limit": 1,
                    "checkpoint_dir": str(root / "checkpoints"),
                    "details": {"enabled": True},
                },
                "request": {},
                "brands": [],
                "output": {
                    "excel_path": str(root / "pricing.xlsx"),
                    "combined_csv_path": str(root / "pricing.csv"),
                },
            }
            with patch(
                "pricing_scraper.dashboard_service.TiraClient",
                FakeTiraClient,
            ):
                result = collect_tira(
                    config,
                    ["All Skin"],
                    1,
                    enrich_details=True,
                )

        self.assertFalse(result.completed)
        self.assertEqual(result.products[0].sku, "TIRA-SKU")
        self.assertEqual(result.products[0].selling_price, 750)
        self.assertEqual(result.products[0].ingredients, "Water")


if __name__ == "__main__":
    unittest.main()
