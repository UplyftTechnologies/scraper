import unittest
from unittest.mock import Mock, patch

from pricing_scraper.database import (
    DatabaseConfigurationError,
    SupabaseCatalogStore,
)
from pricing_scraper.models import Product


class DatabaseTests(unittest.TestCase):
    def test_sync_upserts_current_products_and_price_history(self):
        response = Mock(status_code=201, text="")
        session = Mock()
        session.post.return_value = response
        store = SupabaseCatalogStore(
            url="https://project.supabase.co",
            service_role_key="secret",
            batch_size=100,
            session=session,
        )
        product = Product(
            site="tira",
            product_id="SKU-1",
            parent_product_id="PARENT-1",
            sku="SKU-1",
            brand="Test",
            product_name="Test Cream",
            categories=["Moisturizers"],
            variant="50 ml",
            mrp=500,
            selling_price=400,
            discount_pct=20,
            in_stock=True,
            scraped_at="2026-07-28T00:00:00+00:00",
        )

        result = store.sync([product])

        self.assertTrue(result.enabled)
        self.assertEqual(result.products_written, 1)
        self.assertEqual(result.price_points_written, 1)
        self.assertEqual(session.post.call_count, 2)
        first_call = session.post.call_args_list[0]
        self.assertIn("/rest/v1/retailer_products", first_call.args[0])
        self.assertEqual(
            first_call.kwargs["params"]["on_conflict"],
            "site,product_id",
        )
        self.assertEqual(
            first_call.kwargs["json"][0]["categories"],
            ["Moisturizers"],
        )

    def test_partial_sweep_never_ages_missing_products(self):
        session = Mock()
        session.post.return_value = Mock(status_code=201, text="")
        session.patch.return_value = Mock(status_code=204, text="")
        store = SupabaseCatalogStore(
            url="https://project.supabase.co",
            service_role_key="secret",
            session=session,
        )

        # A brand-filtered or blocked sweep reports complete_catalogue=False,
        # so the products it never looked at must keep their active state.
        store.incremental_sync(
            site="nykaa",
            run_id="00000000-0000-0000-0000-000000000000",
            rows=[],
            price_rows=[],
            seen_product_ids=[],
            complete_catalogue=False,
            missing_runs_before_inactive=3,
        )

        called = [call.args[0] for call in session.post.call_args_list]
        self.assertFalse(
            [url for url in called if "finalize_retailer_scrape_run" in url]
        )

    def test_incomplete_environment_is_rejected(self):
        with patch(
            "pricing_scraper.database._environment",
            return_value={"SUPABASE_URL": "https://project.supabase.co"},
        ):
            with self.assertRaises(DatabaseConfigurationError):
                SupabaseCatalogStore.from_environment()


if __name__ == "__main__":
    unittest.main()
