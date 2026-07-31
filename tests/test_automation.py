import unittest

from pricing_scraper.automation import source_fingerprint
from pricing_scraper.models import Product


class AutomationTests(unittest.TestCase):
    def test_source_fingerprint_is_order_stable_but_tracks_price(self):
        common = {
            "site": "nykaa",
            "product_id": "sku-1",
            "brand": "Brand",
            "product_name": "Serum",
            "mrp": 500,
        }
        first = Product(
            **common,
            categories=["Serums", "Korean Beauty"],
            selling_price=450,
        )
        reordered = Product(
            **common,
            categories=["Korean Beauty", "Serums"],
            selling_price=450,
        )
        discounted = Product(
            **common,
            categories=["Serums", "Korean Beauty"],
            selling_price=425,
        )

        self.assertEqual(source_fingerprint(first), source_fingerprint(reordered))
        self.assertNotEqual(source_fingerprint(first), source_fingerprint(discounted))


if __name__ == "__main__":
    unittest.main()
