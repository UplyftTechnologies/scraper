import unittest

from pricing_scraper.models import brand_key, normalize_gtin


class BrandKeyTests(unittest.TestCase):
    def test_ignores_case_spacing_and_punctuation(self):
        self.assertEqual(brand_key("d'Alba"), brand_key("dAlba"))
        self.assertEqual(brand_key("e.l.f."), brand_key("ELF"))
        self.assertEqual(brand_key(" COSRX "), brand_key("Cosrx"))
        self.assertEqual(brand_key("Dot & Key"), brand_key("dot&key"))

    def test_keeps_distinct_brands_apart(self):
        self.assertNotEqual(brand_key("Nykaa"), brand_key("Nykaa Cosmetics"))
        self.assertEqual(brand_key(""), "")
        self.assertEqual(brand_key(None), "")


class NormalizeGtinTests(unittest.TestCase):
    def test_keeps_valid_barcodes_and_strips_separators(self):
        # Real Nykaa EAN-13 values, one of them punctuated by the retailer.
        self.assertEqual(normalize_gtin("8809416470009"), "8809416470009")
        self.assertEqual(normalize_gtin(" 8809803586047 "), "8809803586047")
        self.assertEqual(normalize_gtin("880-980-3532549"), "8809803532549")
        self.assertEqual(normalize_gtin(8806182550997), "8806182550997")

    def test_accepts_every_gtin_length(self):
        self.assertEqual(normalize_gtin("40170725"), "40170725")
        self.assertEqual(normalize_gtin("036000291452"), "036000291452")
        self.assertEqual(normalize_gtin("10614141000415"), "10614141000415")

    def test_rejects_seller_codes_and_broken_check_digits(self):
        # Tira item codes and Amazon model numbers are the values most likely
        # to reach this helper by mistake.
        self.assertEqual(normalize_gtin("1157765"), "")
        self.assertEqual(normalize_gtin("B08XYZ1234"), "")
        self.assertEqual(normalize_gtin("8809416470008"), "")
        self.assertEqual(normalize_gtin(""), "")
        self.assertEqual(normalize_gtin(None), "")


if __name__ == "__main__":
    unittest.main()
