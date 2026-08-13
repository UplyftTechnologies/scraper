import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pricing_scraper.exporter import export_products, load_products_csv
from pricing_scraper.gtin_scrape import collect_gtins
from pricing_scraper.models import Product


def config_for(root: Path, *, database: bool = False) -> dict:
    """Config for a test run.

    The database is off unless a test explicitly asks for it, and the tests
    that do also patch the sync. A test must never reach a real Supabase
    project: the credentials in .env belong to production.
    """
    return {
        "nykaa": {"page_limit": 1},
        "tira": {},
        "amazon": {},
        "request": {},
        "brands": [],
        "database": {"enabled": database},
        "output": {
            "excel_path": str(root / "pricing.xlsx"),
            "combined_csv_path": str(root / "pricing.csv"),
        },
    }


def seed(root: Path, *products: Product) -> None:
    export_products(list(products), root / "pricing.xlsx", root / "pricing.csv")


def amazon_product(product_id: str, model: str, gtin: str = "") -> Product:
    return Product(
        site="amazon",
        product_id=product_id,
        brand="Brand",
        product_name="Sunscreen",
        gtin=gtin,
        product_attributes={"Item model number": model},
    )


class GtinScrapeTests(unittest.TestCase):
    def test_it_fills_missing_barcodes_without_touching_other_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed(
                root,
                Product(
                    site="amazon",
                    product_id="A1",
                    brand="Brand",
                    product_name="Sunscreen",
                    description="keep me",
                    selling_price=499.0,
                    product_attributes={"Item model number": "8906087778462"},
                ),
            )

            result = collect_gtins(
                config_for(root), "amazon", open_amazon_pages=False
            )

            self.assertEqual(result.found, 1)
            stored = {p.product_id: p for p in load_products_csv(root / "pricing.csv")}
            self.assertEqual(stored["A1"].gtin, "8906087778462")
            # The sweep changes one column and nothing else.
            self.assertEqual(stored["A1"].description, "keep me")
            self.assertEqual(stored["A1"].selling_price, 499.0)

    def test_the_barcodes_reach_the_database_too(self):
        """A barcode sweep syncs like any other run, so Supabase stays current."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed(root, amazon_product("A1", "8906087778462"))
            with patch(
                "pricing_scraper.exporter.sync_products_to_database"
            ) as sync:
                collect_gtins(
                    config_for(root, database=True), "amazon", open_amazon_pages=False
                )
            sync.assert_called_once()
            synced = {item.product_id: item for item in sync.call_args.args[0]}
            self.assertEqual(synced["A1"].gtin, "8906087778462")

    def test_a_local_only_sweep_can_be_asked_for(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed(root, amazon_product("A1", "8906087778462"))
            with patch(
                "pricing_scraper.exporter.sync_products_to_database"
            ) as sync:
                collect_gtins(
                    config_for(root, database=True),
                    "amazon",
                    open_amazon_pages=False,
                    sync_database=False,
                )
            sync.assert_not_called()

    def test_the_database_flag_in_the_config_still_wins(self):
        """database.enabled: false must keep every run local."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed(root, amazon_product("A1", "8906087778462"))
            config = config_for(root, database=True)
            config["database"]["enabled"] = False
            with patch(
                "pricing_scraper.exporter.sync_products_to_database"
            ) as sync:
                collect_gtins(config, "amazon", open_amazon_pages=False)
            sync.assert_not_called()

    def test_products_that_already_have_one_are_not_touched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed(
                root,
                amazon_product("A1", "8904417306224", gtin="8906087778462"),
            )

            result = collect_gtins(
                config_for(root), "amazon", open_amazon_pages=False
            )

            self.assertEqual(result.found, 0)
            self.assertEqual(result.already_had, 1)
            stored = {p.product_id: p for p in load_products_csv(root / "pricing.csv")}
            self.assertEqual(stored["A1"].gtin, "8906087778462")

    def test_nothing_found_leaves_the_export_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed(root, amazon_product("A1", "NOT-A-BARCODE"))
            before = (root / "pricing.csv").stat().st_mtime_ns

            result = collect_gtins(
                config_for(root), "amazon", open_amazon_pages=False
            )

            self.assertEqual(result.found, 0)
            self.assertIsNone(result.export)
            self.assertEqual((root / "pricing.csv").stat().st_mtime_ns, before)

    def test_an_empty_catalogue_is_reported_clearly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed(root, amazon_product("A1", "8906087778462"))
            with self.assertRaises(ValueError) as caught:
                collect_gtins(config_for(root), "nykaa")
            self.assertIn("Run a normal collection first", str(caught.exception))

    def test_a_page_is_not_reopened_when_its_attributes_are_stored(self):
        """Amazon's barcode lives in the attributes we already hold.

        Re-opening the page returns the same product-information table, so a
        product with stored attributes and no barcode in them has nothing more
        to give. A full sweep spent 522 page opens proving exactly that.
        """
        from pricing_scraper import gtin_scrape

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed(
                root,
                # attributes stored, but no barcode in them
                amazon_product("A1", "SELLER-SKU-01"),
                # nothing stored at all, so the page is worth opening
                Product(
                    site="amazon",
                    product_id="A2",
                    brand="Brand",
                    product_name="Cream",
                ),
            )
            opened: list[str] = []

            def fake_pages(config, targets, **kwargs):
                opened.extend(item.product_id for item in targets)
                return {}, 0, 0

            with patch.object(gtin_scrape, "_collect_amazon_gtins", fake_pages):
                collect_gtins(config_for(root), "amazon", cross_fill=False)

        self.assertEqual(opened, ["A2"])

    def test_rechecking_pages_reopens_everything(self):
        from pricing_scraper import gtin_scrape

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed(root, amazon_product("A1", "SELLER-SKU-01"))
            opened: list[str] = []

            def fake_pages(config, targets, **kwargs):
                opened.extend(item.product_id for item in targets)
                return {}, 0, 0

            with patch.object(gtin_scrape, "_collect_amazon_gtins", fake_pages):
                collect_gtins(
                    config_for(root),
                    "amazon",
                    recheck_pages=True,
                    cross_fill=False,
                )

        self.assertEqual(opened, ["A1"])

    def test_a_matched_product_lends_its_barcode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed(
                root,
                Product(
                    site="amazon",
                    product_id="A1",
                    brand="Minimalist",
                    product_name="Niacinamide 10% Face Serum",
                    variant="30 ml",
                ),
                Product(
                    site="tira",
                    product_id="T1",
                    brand="Minimalist",
                    product_name="Niacinamide 10% Face Serum",
                    variant="30 ml",
                    gtin="8904417306224",
                ),
            )

            result = collect_gtins(
                config_for(root), "amazon", open_amazon_pages=False
            )

            stored = {p.product_id: p for p in load_products_csv(root / "pricing.csv")}

        self.assertEqual(result.borrowed, 1)
        self.assertEqual(stored["A1"].gtin, "8904417306224")
        self.assertEqual(result.found_by["A1"], "matched tira")

    def test_a_loose_match_does_not_lend_its_barcode(self):
        """Different pack sizes must not share a barcode."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed(
                root,
                Product(
                    site="amazon",
                    product_id="A1",
                    brand="Minimalist",
                    product_name="Niacinamide Serum",
                    variant="30 ml",
                ),
                Product(
                    site="tira",
                    product_id="T1",
                    brand="Minimalist",
                    product_name="Niacinamide Serum",
                    variant="60 ml",
                    gtin="8904417306224",
                ),
            )

            result = collect_gtins(
                config_for(root), "amazon", open_amazon_pages=False
            )

            stored = {p.product_id: p for p in load_products_csv(root / "pricing.csv")}

        self.assertEqual(result.borrowed, 0)
        self.assertEqual(stored["A1"].gtin, "")

    def test_brands_outside_the_filter_are_never_requested(self):
        """A barcode sweep reaches products by ID, bypassing the client filter.

        The saved catalogue can predate the current SCRAPE_BRANDS list, so it
        holds brands the collection would now skip. Without this the sweep
        spends requests on them.
        """
        from pricing_scraper import gtin_scrape

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed(
                root,
                Product(
                    site="nykaa", product_id="N1", brand="COSRX", product_name="Wanted"
                ),
                Product(
                    site="nykaa", product_id="N2", brand="CHANEL", product_name="Not"
                ),
            )
            config = config_for(root)
            config["brands"] = ["COSRX"]
            asked: list[str] = []

            def fake_nykaa(cfg, targets, **kwargs):
                asked.extend(item.product_id for item in targets)
                return {}, 0, 0

            with patch.object(gtin_scrape, "_collect_nykaa_gtins", fake_nykaa):
                result = collect_gtins(config, "nykaa", cross_fill=False)

        self.assertEqual(asked, ["N1"])
        self.assertEqual(result.filtered_out, 1)
        self.assertEqual(result.stored_products, 1)

    def test_an_empty_filter_keeps_every_brand(self):
        from pricing_scraper import gtin_scrape

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed(
                root,
                Product(site="nykaa", product_id="N1", brand="COSRX", product_name="A"),
                Product(site="nykaa", product_id="N2", brand="CHANEL", product_name="B"),
            )
            config = config_for(root)
            config["brands"] = []
            asked: list[str] = []

            def fake_nykaa(cfg, targets, **kwargs):
                asked.extend(item.product_id for item in targets)
                return {}, 0, 0

            with patch.object(gtin_scrape, "_collect_nykaa_gtins", fake_nykaa):
                result = collect_gtins(config, "nykaa", cross_fill=False)

        self.assertEqual(sorted(asked), ["N1", "N2"])
        self.assertEqual(result.filtered_out, 0)

    def test_cross_fill_also_respects_the_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed(
                root,
                Product(
                    site="amazon",
                    product_id="A1",
                    brand="CHANEL",
                    product_name="Serum",
                    variant="30 ml",
                ),
                Product(
                    site="tira",
                    product_id="T1",
                    brand="CHANEL",
                    product_name="Serum",
                    variant="30 ml",
                    gtin="8904417306224",
                ),
                Product(
                    site="amazon", product_id="A2", brand="COSRX", product_name="Keep"
                ),
            )
            config = config_for(root)
            config["brands"] = ["COSRX"]

            result = collect_gtins(config, "amazon", open_amazon_pages=False)

            stored = {p.product_id: p for p in load_products_csv(root / "pricing.csv")}

        self.assertEqual(result.borrowed, 0)
        self.assertEqual(stored["A1"].gtin, "")

    def test_an_unsupported_site_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                collect_gtins(config_for(root), "flipkart")


if __name__ == "__main__":
    unittest.main()
