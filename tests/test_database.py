import unittest
from unittest.mock import Mock, patch

import requests

from pricing_scraper.database import (
    DatabaseConfigurationError,
    DatabaseSyncError,
    SupabaseCatalogStore,
)
from pricing_scraper.models import Product


class FetchSiteProductsTests(unittest.TestCase):
    def store_with(self, session) -> SupabaseCatalogStore:
        return SupabaseCatalogStore(
            url="https://project.supabase.co",
            service_role_key="secret",
            session=session,
        )

    def test_pages_on_the_primary_key_rather_than_an_offset(self):
        # An offset window over an unordered query can repeat or skip rows, so
        # every page must ask for ids above the last one already read.
        pages = [
            [{"id": index, "product_id": f"P{index}"} for index in range(1, 501)],
            [{"id": index, "product_id": f"P{index}"} for index in range(501, 700)],
        ]
        session = Mock()
        session.get.side_effect = [
            Mock(status_code=200, json=Mock(return_value=page), text="")
            for page in pages
        ]
        store = self.store_with(session)

        rows = store.fetch_site_products("tira", page_size=500)

        self.assertEqual(len(rows), 699)
        self.assertEqual(session.get.call_count, 2)
        first, second = session.get.call_args_list
        self.assertEqual(first.kwargs["params"]["order"], "id.asc")
        self.assertEqual(first.kwargs["params"]["id"], "gt.0")
        self.assertEqual(second.kwargs["params"]["id"], "gt.500")
        self.assertNotIn("Range", second.kwargs["headers"])

    def test_a_statement_timeout_retries_with_smaller_pages(self):
        rows = [{"id": 1, "product_id": "P1"}]
        session = Mock()
        session.get.side_effect = [
            Mock(status_code=500, text="statement timeout", json=Mock(return_value={})),
            Mock(status_code=200, text="", json=Mock(return_value=rows)),
        ]
        store = self.store_with(session)

        fetched = store.fetch_site_products("nykaa", page_size=500)

        self.assertEqual(fetched, rows)
        asked = [call.kwargs["params"]["limit"] for call in session.get.call_args_list]
        self.assertEqual(asked, ["500", "125"])

    def test_a_persistent_error_is_reported_rather_than_truncating(self):
        session = Mock()
        session.get.return_value = Mock(
            status_code=401, text="unauthorized", json=Mock(return_value={})
        )
        store = self.store_with(session)

        with self.assertRaises(DatabaseSyncError):
            store.fetch_site_products("tira")

    def test_selected_columns_always_include_the_paging_key(self):
        session = Mock()
        session.get.return_value = Mock(
            status_code=200, text="", json=Mock(return_value=[])
        )
        store = self.store_with(session)

        store.fetch_site_products("tira", columns=("brand", "selling_price"))

        selection = session.get.call_args.kwargs["params"]["select"]
        self.assertEqual(selection, "id,brand,selling_price")


class RetryTests(unittest.TestCase):
    """Transient network failures must not discard a finished collection."""

    def store_with(self, session) -> SupabaseCatalogStore:
        return SupabaseCatalogStore(
            url="https://project.supabase.co",
            service_role_key="secret",
            session=session,
            sleeper=lambda _seconds: None,
        )

    def test_an_upsert_survives_a_dropped_connection(self):
        session = Mock()
        session.post.side_effect = [
            requests.ConnectionError("The write operation timed out"),
            Mock(status_code=201, text=""),
        ]
        store = self.store_with(session)

        written = store._upsert(
            "retailer_products",
            [{"site": "tira", "product_id": "SKU-1"}],
            "site,product_id",
        )

        self.assertEqual(written, 1)
        self.assertEqual(session.post.call_count, 2)

    def test_a_transient_gateway_error_is_repeated(self):
        session = Mock()
        session.post.side_effect = [
            Mock(status_code=503, text="upstream unavailable"),
            Mock(status_code=201, text=""),
        ]
        store = self.store_with(session)

        written = store._upsert(
            "retailer_products",
            [{"site": "tira", "product_id": "SKU-1"}],
            "site,product_id",
        )

        self.assertEqual(written, 1)
        self.assertEqual(session.post.call_count, 2)

    def test_a_rejected_request_is_not_repeated(self):
        # 401 describes the request itself; repeating it only wastes time.
        session = Mock()
        session.post.return_value = Mock(status_code=401, text="unauthorized")
        store = self.store_with(session)

        with self.assertRaises(DatabaseSyncError):
            store._upsert("retailer_products", [{"site": "tira"}], "site")
        self.assertEqual(session.post.call_count, 1)

    def test_persistent_failure_is_reported_after_the_attempt_budget(self):
        session = Mock()
        session.post.side_effect = requests.ConnectionError("timed out")
        store = self.store_with(session)

        with self.assertRaises(DatabaseSyncError):
            store._upsert("retailer_products", [{"site": "tira"}], "site")
        self.assertEqual(session.post.call_count, store.max_attempts)

    def test_the_finalize_call_is_never_repeated(self):
        """finalize_retailer_scrape_run increments a counter per call.

        Repeating it after a timeout would age products towards inactive twice
        for one sweep, so it must be sent exactly once even though it failed.
        """
        session = Mock()
        session.post.side_effect = [
            Mock(status_code=201, text=""),  # products upsert
            Mock(status_code=201, text=""),  # price history upsert
            requests.ConnectionError("timed out"),  # the rpc call
        ]
        session.patch.return_value = Mock(status_code=204, text="")
        store = self.store_with(session)

        with self.assertRaises(requests.ConnectionError):
            store.incremental_sync(
                site="nykaa",
                run_id="00000000-0000-0000-0000-000000000000",
                rows=[{"site": "nykaa", "product_id": "P1"}],
                price_rows=[{"site": "nykaa", "product_id": "P1"}],
                seen_product_ids=["P1"],
                complete_catalogue=True,
                missing_runs_before_inactive=3,
            )

        rpc_calls = [
            call
            for call in session.post.call_args_list
            if "finalize_retailer_scrape_run" in str(call.args[0])
        ]
        self.assertEqual(len(rpc_calls), 1)


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
