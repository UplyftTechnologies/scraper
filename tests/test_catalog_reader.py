import json
import tempfile
import unittest
from pathlib import Path

from pricing_scraper.catalog_reader import (
    checkpoint_signature,
    load_checkpoint_products,
    products_to_csv_bytes,
)
from pricing_scraper.models import Product


class CatalogReaderTests(unittest.TestCase):
    def test_reads_live_checkpoints_and_skips_partial_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "tira_test.details.products.jsonl"
            product = Product(
                site="tira",
                product_id="sku-1",
                brand="Brand",
                product_name="Face cream",
                categories=["Moisturizers"],
                selling_price=499,
            )
            path.write_bytes(
                (
                    json.dumps(product.to_dict(), ensure_ascii=False)
                    + "\n"
                ).encode("utf-8")
                + b"\x00" * 64
                + b"\n"
            )

            snapshot = load_checkpoint_products(root)

            self.assertEqual(len(snapshot.products), 1)
            self.assertEqual(snapshot.products[0].product_id, "sku-1")
            self.assertEqual(snapshot.invalid_rows, 1)
            self.assertEqual(len(checkpoint_signature(root)), 1)

    def test_filtered_products_can_be_downloaded_as_csv(self):
        content = products_to_csv_bytes(
            [
                Product(
                    site="amazon",
                    product_id="B000000001",
                    brand="Brand",
                    product_name="Serum",
                    categories=["Serums"],
                )
            ]
        ).decode("utf-8-sig")

        self.assertIn("product_id", content)
        self.assertIn("B000000001", content)
        self.assertIn('[""Serums""]', content)


if __name__ == "__main__":
    unittest.main()
