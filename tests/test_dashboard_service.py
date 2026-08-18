import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from pricing_scraper.clients.base import RequestFailed
from pricing_scraper.clients.nykaa import CategoryScrapeResult
from pricing_scraper.dashboard_service import (
    _full_catalog_partitions,
    _sleeper_kwargs,
    collect_nykaa,
    collect_tira,
)
from pricing_scraper.models import Product


class SleeperForwardingTests(unittest.TestCase):
    def test_a_supplied_sleeper_reaches_the_client(self):
        def sleeper(_seconds: float) -> None:
            return None

        self.assertEqual(_sleeper_kwargs(sleeper), {"sleeper": sleeper})

    def test_no_sleeper_leaves_the_client_default_alone(self):
        self.assertEqual(_sleeper_kwargs(None), {})

    def test_collect_nykaa_passes_the_sleeper_to_the_client(self):
        received: dict[str, object] = {}

        class FakeNykaaClient:
            start_page = 1

            def __init__(self, *_args, **kwargs):
                received.update(kwargs)
                self.page_failures = 0
                self.product_failures = 0
                self.detail_failures = 0
                self.blocks_encountered = 0
                self.requests_made = 0
                self.logger = logging.getLogger("fake-nykaa-sleeper")

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def select_categories(self, _names):
                return [{"id": "1", "name": "Test"}]

            def scrape_category_resumable(self, *_args, **_kwargs):
                return CategoryScrapeResult(
                    products=[],
                    next_page=1,
                    completed=True,
                    pages_scraped=0,
                    stop_reason="",
                )

        def sleeper(_seconds: float) -> None:
            return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {
                "nykaa": {
                    "page_limit": 1,
                    "checkpoint_dir": str(root / "checkpoints"),
                    "details": {"enabled": False},
                },
                "request": {},
                "brands": [],
                "output": {
                    "excel_path": str(root / "pricing.xlsx"),
                    "combined_csv_path": str(root / "pricing.csv"),
                },
            }
            with patch(
                "pricing_scraper.dashboard_service.NykaaClient", FakeNykaaClient
            ):
                with self.assertRaises(ValueError):
                    # No products come back, which is a separate failure; the
                    # client was still constructed with the sleeper.
                    collect_nykaa(
                        config,
                        ["Test"],
                        1,
                        enrich_details=False,
                        refresh_only_stale=False,
                        sleeper=sleeper,
                    )

        self.assertIs(received.get("sleeper"), sleeper)


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

        partitions = _full_catalog_partitions(selected)

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
                    refresh_only_stale=False,
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
                    refresh_only_stale=False,
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
                    refresh_only_stale=False,
                )

        self.assertFalse(result.completed)
        self.assertEqual(result.products[0].sku, "TIRA-SKU")
        self.assertEqual(result.products[0].selling_price, 750)
        self.assertEqual(result.products[0].ingredients, "Water")


if __name__ == "__main__":
    unittest.main()


class RefreshReuseTests(unittest.TestCase):
    """A skipped product must cost no request and still reach the export."""

    class FakeNykaaClient:
        start_page = 1
        detail_calls: list = []

        def __init__(self, *_args, **_kwargs):
            self.failures = 0
            self.page_failures = 0
            self.product_failures = 0
            self.detail_failures = 0
            self.blocks_encountered = 0
            self.requests_made = 1
            self.logger = logging.getLogger("fake-nykaa-refresh")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def select_categories(self, _names):
            return [{"id": "10", "name": "Test"}]

        def scrape_category_resumable(
            self, _category, *, start_page, seen_product_ids, on_page
        ):
            del start_page, seen_product_ids
            listing = Product(
                site="nykaa",
                product_id="sku-1",
                parent_product_id="parent-1",
                brand="Brand",
                product_name="Cleanser",
                selling_price=400.0,
            )
            on_page(1, [listing])
            return CategoryScrapeResult(
                products=[listing],
                next_page=2,
                completed=True,
                pages_scraped=1,
                stop_reason="",
            )

        def fetch_product_details(self, product):
            type(self).detail_calls.append(product.product_id)
            return [
                Product(
                    site="nykaa",
                    product_id=product.product_id,
                    parent_product_id=product.parent_product_id,
                    brand=product.brand,
                    product_name=product.product_name,
                    selling_price=product.selling_price,
                    description="freshly fetched",
                    image_urls=["https://example.test/new.jpg"],
                )
            ]

    def _config(self, root):
        return {
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
            "refresh": {"enabled": True, "refresh_days": 30},
        }

    def _seed(self, csv_path, *, scraped_at, description="stored description"):
        from pricing_scraper.exporter import export_products

        export_products(
            [
                Product(
                    site="nykaa",
                    product_id="sku-1",
                    parent_product_id="parent-1",
                    brand="Brand",
                    product_name="Cleanser",
                    selling_price=400.0,
                    description=description,
                    image_urls=["https://example.test/stored.jpg"],
                    scraped_at=scraped_at,
                )
            ],
            csv_path.with_suffix(".xlsx"),
            csv_path,
        )

    def _run(self, root):
        with patch(
            "pricing_scraper.dashboard_service.NykaaClient",
            self.FakeNykaaClient,
        ):
            return collect_nykaa(self._config(root), ["Test"], 1, enrich_details=True)

    def test_a_current_product_is_not_requested_again(self):
        RefreshReuseTests.FakeNykaaClient.detail_calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._seed(
                root / "pricing.csv",
                scraped_at=datetime.now(timezone.utc).isoformat(),
            )
            result = self._run(root)

        self.assertEqual(self.FakeNykaaClient.detail_calls, [])
        # The stored content must survive: refreshing a retailer cannot strip
        # the description off every product it decided not to re-request.
        stored = {item.product_id: item for item in result.products}
        self.assertEqual(stored["sku-1"].description, "stored description")

    def test_a_stale_product_is_requested(self):
        RefreshReuseTests.FakeNykaaClient.detail_calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
            self._seed(root / "pricing.csv", scraped_at=old)
            result = self._run(root)

        self.assertEqual(self.FakeNykaaClient.detail_calls, ["sku-1"])
        stored = {item.product_id: item for item in result.products}
        self.assertEqual(stored["sku-1"].description, "freshly fetched")

    def test_a_product_missing_content_is_requested(self):
        RefreshReuseTests.FakeNykaaClient.detail_calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._seed(
                root / "pricing.csv",
                scraped_at=datetime.now(timezone.utc).isoformat(),
                description="",
            )
            self._run(root)

        self.assertEqual(self.FakeNykaaClient.detail_calls, ["sku-1"])

    def test_disabling_the_check_requests_everything(self):
        RefreshReuseTests.FakeNykaaClient.detail_calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._seed(
                root / "pricing.csv",
                scraped_at=datetime.now(timezone.utc).isoformat(),
            )
            config = self._config(root)
            with patch(
                "pricing_scraper.dashboard_service.NykaaClient",
                self.FakeNykaaClient,
            ):
                collect_nykaa(
                    config,
                    ["Test"],
                    1,
                    enrich_details=True,
                    refresh_only_stale=False,
                )

        self.assertEqual(self.FakeNykaaClient.detail_calls, ["sku-1"])


class FinalSyncTests(unittest.TestCase):
    """Streaming only covers what a run actually scraped."""

    class Sink:
        def __init__(self, written=0, failures=0):
            from pricing_scraper.db_sink import SinkResult

            self.result = SinkResult(
                enabled=True, products_written=written, failures=failures
            )

    def config(self, enabled=True):
        return {"database": {"enabled": enabled}}

    def test_streaming_that_covered_the_export_needs_no_sync(self):
        from pricing_scraper.dashboard_service import _needs_final_sync

        self.assertFalse(
            _needs_final_sync(self.config(), self.Sink(written=100), 100)
        )

    def test_a_checkpoint_reused_run_still_needs_the_sync(self):
        """It streams almost nothing because almost nothing was scraped.

        Skipping the sync then leaves the export in the files and never in the
        database.
        """
        from pricing_scraper.dashboard_service import _needs_final_sync

        self.assertTrue(
            _needs_final_sync(self.config(), self.Sink(written=1), 10_721)
        )

    def test_a_failed_batch_forces_the_reconciling_sync(self):
        from pricing_scraper.dashboard_service import _needs_final_sync

        self.assertTrue(
            _needs_final_sync(
                self.config(), self.Sink(written=100, failures=1), 100
            )
        )

    def test_no_sink_means_the_export_must_do_it(self):
        from pricing_scraper.dashboard_service import _needs_final_sync

        self.assertTrue(_needs_final_sync(self.config(), None, 100))

    def test_a_disabled_database_is_never_written(self):
        from pricing_scraper.dashboard_service import _needs_final_sync

        self.assertFalse(
            _needs_final_sync(self.config(enabled=False), None, 100)
        )
