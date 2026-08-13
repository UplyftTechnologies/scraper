import csv
import tempfile
import unittest
from pathlib import Path

from pricing_scraper.exporter import export_products
from pricing_scraper.manual import (
    ManualImportError,
    insert_manual_products,
    load_manual_products,
    select_new,
)
from pricing_scraper.models import Product


def config_for(root: Path, *, database: bool = False) -> dict:
    return {
        "nykaa": {},
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


def write_sheet(path: Path, rows: list[list[str]], header: list[str]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return path


def seed(root: Path, *products: Product) -> None:
    export_products(list(products), root / "pricing.xlsx", root / "pricing.csv")


class LoadManualTests(unittest.TestCase):
    def test_it_reads_flexible_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            sheet = write_sheet(
                Path(directory) / "m.csv",
                [["Minimalist", "Vitamin C Serum", "499", "30 ml"]],
                ["Vendor", "Title", "Price", "Size"],
            )
            products, rejected = load_manual_products(sheet)

        self.assertEqual(rejected, [])
        self.assertEqual(products[0].brand, "Minimalist")
        self.assertEqual(products[0].product_name, "Vitamin C Serum")
        self.assertEqual(products[0].selling_price, 499.0)
        self.assertEqual(products[0].variant, "30 ml")
        self.assertEqual(products[0].site, "manual")

    def test_a_row_without_a_brand_or_name_is_reported_not_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            sheet = write_sheet(
                Path(directory) / "m.csv",
                [["", "No brand"], ["Brand", ""], ["Brand", "Fine"]],
                ["brand", "product_name"],
            )
            products, rejected = load_manual_products(sheet)

        self.assertEqual(len(products), 1)
        self.assertEqual([line for line, _ in rejected], [2, 3])

    def test_the_site_column_is_honoured(self):
        with tempfile.TemporaryDirectory() as directory:
            sheet = write_sheet(
                Path(directory) / "m.csv",
                [["NYKAA", "Brand", "Thing"], ["", "Brand", "Other"]],
                ["site", "brand", "product_name"],
            )
            products, _ = load_manual_products(sheet, default_site="manual")

        self.assertEqual([item.site for item in products], ["nykaa", "manual"])

    def test_rows_without_an_id_get_a_stable_one(self):
        """Re-importing the same sheet must not create a second copy."""
        with tempfile.TemporaryDirectory() as directory:
            sheet = write_sheet(
                Path(directory) / "m.csv",
                [["Minimalist", "Vitamin C Serum"]],
                ["brand", "product_name"],
            )
            first, _ = load_manual_products(sheet)
            second, _ = load_manual_products(sheet)

        self.assertEqual(first[0].product_id, second[0].product_id)
        self.assertTrue(first[0].product_id.startswith("manual-"))

    def test_a_missing_file_is_reported_clearly(self):
        with self.assertRaises(ManualImportError):
            load_manual_products(Path("nope/missing.csv"))

    def test_a_sheet_without_the_required_columns_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            sheet = write_sheet(
                Path(directory) / "m.csv", [["1", "2"]], ["colour", "weight"]
            )
            with self.assertRaises(ManualImportError):
                load_manual_products(sheet)


class FakeStore:
    """Stands in for Supabase: holds rows and records what was upserted."""

    products_table = "retailer_products"

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.upserted = []

    def fetch_site_products(self, site, *, columns=None, page_size=500):
        del columns, page_size
        return [row for row in self.rows if row.get("site") == site]

    def _upsert(self, table, rows, conflict):
        del table, conflict
        self.upserted.extend(rows)
        return len(rows)


class SelectNewTests(unittest.TestCase):
    def test_only_products_absent_from_supabase_are_kept(self):
        store = FakeStore(
            [{"site": "tira", "product_id": "T1", "brand": "Akind",
              "product_name": "Soothing Toner"}]
        )
        candidates = [
            Product(site="tira", product_id="T1", brand="X", product_name="Y"),
            Product(
                # Same product, different capitalisation and no retailer id.
                site="tira",
                product_id="manual-akind-soothing-toner",
                brand="AKIND",
                product_name="soothing toner",
            ),
            Product(site="tira", product_id="T9", brand="New", product_name="New"),
        ]

        fresh, existing = select_new(candidates, store=store)

        self.assertEqual([item.product_id for item in fresh], ["T9"])
        self.assertEqual(
            [reason for _, reason in existing],
            ["product_id already stored", "brand and name already stored"],
        )

    def test_duplicates_inside_the_sheet_are_caught(self):
        store = FakeStore()
        twice = [
            Product(site="tira", product_id="N1", brand="New", product_name="Thing"),
            Product(site="tira", product_id="N1", brand="New", product_name="Thing"),
        ]

        fresh, existing = select_new(twice, store=store)

        self.assertEqual(len(fresh), 1)
        self.assertIn("duplicate", existing[0][1])

    def test_the_local_csv_is_not_consulted(self):
        """A row in the file but missing from Supabase is exactly the one to add."""
        store = FakeStore()
        candidate = [
            Product(site="tira", product_id="IN-CSV-ONLY", brand="A", product_name="B")
        ]

        fresh, existing = select_new(candidate, store=store)

        self.assertEqual(len(fresh), 1)
        self.assertEqual(existing, [])


class InsertTests(unittest.TestCase):
    def sheet(self, root, rows, header):
        return write_sheet(root / "m.csv", rows, header)

    def test_new_rows_are_upserted_and_duplicates_are_not(self):
        store = FakeStore(
            [{"site": "manual", "product_id": "manual-akind-toner",
              "brand": "Akind", "product_name": "Toner"}]
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sheet = self.sheet(
                root,
                [["Akind", "Toner"], ["Minimalist", "Vitamin C Serum"]],
                ["brand", "product_name"],
            )

            result = insert_manual_products(sheet, store=store)

        self.assertEqual(len(result.inserted), 1)
        self.assertEqual(len(result.skipped_existing), 1)
        self.assertEqual(len(store.upserted), 1)
        self.assertEqual(store.upserted[0]["product_name"], "Vitamin C Serum")
        self.assertTrue(store.upserted[0]["is_active"])
        self.assertTrue(store.upserted[0]["first_seen_at"])

    def test_a_check_only_run_writes_nothing(self):
        store = FakeStore()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sheet = self.sheet(root, [["New", "Thing"]], ["brand", "product_name"])

            result = insert_manual_products(sheet, store=store, dry_run=True)

        self.assertEqual(len(result.inserted), 1)
        self.assertFalse(result.written)
        self.assertEqual(store.upserted, [])

    def test_running_the_same_sheet_twice_inserts_once(self):
        store = FakeStore()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sheet = self.sheet(root, [["New", "Thing"]], ["brand", "product_name"])

            first = insert_manual_products(sheet, store=store)
            # The fake now holds what the first call wrote.
            store.rows.extend(store.upserted)
            second = insert_manual_products(sheet, store=store)

        self.assertEqual(len(first.inserted), 1)
        self.assertEqual(len(second.inserted), 0)
        self.assertFalse(second.written)

    def test_rows_filed_under_a_retailer_are_flagged(self):
        store = FakeStore()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sheet = self.sheet(
                root, [["nykaa", "New", "Thing"]], ["site", "brand", "product_name"]
            )

            result = insert_manual_products(sheet, store=store, dry_run=True)

        self.assertEqual(result.retailer_sites, {"nykaa"})

    def test_an_uploaded_csv_is_read_from_memory(self):
        store = FakeStore()
        payload = b"brand,product_name\nMinimalist,Vitamin C Serum\n"

        result = insert_manual_products(
            payload, filename="upload.csv", store=store, dry_run=True
        )

        self.assertEqual(len(result.inserted), 1)
        self.assertEqual(result.inserted[0].brand, "Minimalist")

    def test_an_unsupported_upload_is_refused(self):
        with self.assertRaises(ManualImportError):
            insert_manual_products(b"x", filename="notes.txt", store=FakeStore())


if __name__ == "__main__":
    unittest.main()
