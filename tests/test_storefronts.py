"""Tests for the three plain-HTTP storefront clients."""

import unittest
from unittest.mock import Mock

from pricing_scraper.clients.base import (
    _decoded_body,
    first_offer,
    html_to_text,
    linked_product,
)
from pricing_scraper.clients.broadway import BroadwayClient, gtin_from_sku
from pricing_scraper.clients.kindlife import KindlifeClient, gtin_from_images
from pricing_scraper.clients.purplle import PurplleClient, brand_from_slug
from pricing_scraper.models import plausible_retail_barcode

REQUEST_CONFIG = {
    "timeout_seconds": 5,
    "delay_min_seconds": 0,
    "delay_max_seconds": 0,
    "logs_dir": "logs",
    "max_requests_per_minute": 600,
}


def broadway(brands=("Innisfree",)) -> BroadwayClient:
    return BroadwayClient({}, REQUEST_CONFIG, brands=list(brands))


def purplle(brands=("COSRX",)) -> PurplleClient:
    return PurplleClient({}, REQUEST_CONFIG, brands=list(brands))


def kindlife(brands=("Anua",)) -> KindlifeClient:
    return KindlifeClient({}, REQUEST_CONFIG, brands=list(brands))


class BarcodePlausibilityTests(unittest.TestCase):
    """A valid check digit does not make a number a retail barcode."""

    def test_a_real_ean_is_accepted(self):
        self.assertTrue(plausible_retail_barcode("8906118410545"))

    def test_an_internal_code_on_a_restricted_prefix_is_rejected(self):
        # Purplle's master_product_id for its own catalogue entries. It passes
        # the GS1 check digit but sits in the coupon/refund range, and the
        # supervisor would copy it onto other retailers if it were stored.
        self.assertFalse(plausible_retail_barcode("9991308610002"))

    def test_an_in_store_prefix_is_rejected(self):
        self.assertFalse(plausible_retail_barcode("2001234567893"))

    def test_an_empty_value_is_not_a_barcode(self):
        self.assertFalse(plausible_retail_barcode(""))


class BroadwaySkuTests(unittest.TestCase):
    """Shopify skus here are barcodes, sometimes missing a leading zero."""

    def test_a_thirteen_digit_sku_is_used_directly(self):
        self.assertEqual(gtin_from_sku("8800294993574"), "8800294993574")

    def test_an_eleven_digit_upc_regains_its_leading_zero(self):
        # Estee Lauder and Clinique export UPC-A as a number, losing the zero.
        self.assertEqual(gtin_from_sku("20714222857"), "020714222857")

    def test_a_sku_that_is_not_a_barcode_stays_empty(self):
        self.assertEqual(gtin_from_sku("MPSS50"), "")

    def test_eleven_digits_that_fail_the_check_digit_are_refused(self):
        """Padding must not turn any eleven-digit code into a barcode."""
        self.assertEqual(gtin_from_sku("12345678901"), "")


class BroadwayNormalizationTests(unittest.TestCase):
    LISTING = {
        "id": 900,
        "handle": "innisfree-green-tea",
        "title": "Innisfree Green Tea Ceramide Milk",
        "vendor": "Innisfree",
        "product_type": "Toner / Essence",
        "body_html": "<p>Hydrating <b>milk</b></p>",
        "images": [{"src": "https://cdn.test/a.jpg"}],
        "variants": [
            {
                "id": 11,
                "title": "Default Title",
                "sku": "8800294993574",
                "price": "1450.00",
                "compare_at_price": "1600.00",
                "available": True,
            }
        ],
    }

    def test_a_listing_entry_becomes_a_product(self):
        [product] = broadway().to_products(self.LISTING)
        self.assertEqual(product.site, "broadway")
        self.assertEqual(product.gtin, "8800294993574")
        self.assertEqual(product.brand, "Innisfree")
        self.assertEqual(product.selling_price, 1450.0)
        self.assertEqual(product.mrp, 1600.0)
        self.assertEqual(product.discount_pct, 9.38)
        self.assertEqual(product.description, "Hydrating milk")
        self.assertTrue(product.in_stock)

    def test_the_default_variant_title_is_not_stored_as_a_variant(self):
        [product] = broadway().to_products(self.LISTING)
        self.assertEqual(product.variant, "")

    def test_a_compare_price_below_the_asking_price_is_discarded(self):
        """A stale compare_at_price would otherwise be a negative discount."""
        listing = dict(self.LISTING)
        listing["variants"] = [
            dict(self.LISTING["variants"][0], compare_at_price="1000.00")
        ]
        [product] = broadway().to_products(listing)
        self.assertIsNone(product.mrp)
        self.assertIsNone(product.discount_pct)

    def test_only_the_configured_brands_are_wanted(self):
        client = broadway(brands=["Innisfree"])
        self.assertTrue(client.wanted({"vendor": "Innisfree"}))
        self.assertFalse(client.wanted({"vendor": "The Pant Project"}))

    def test_an_empty_brand_filter_keeps_everything(self):
        self.assertTrue(broadway(brands=[]).wanted({"vendor": "Anything"}))

    def test_the_barcode_endpoint_is_not_called_when_the_sku_carries_one(self):
        client = broadway()
        client.fetch_barcode = Mock(return_value="")
        client.to_products(self.LISTING)
        client.fetch_barcode.assert_not_called()

    def test_the_barcode_endpoint_is_asked_once_per_product(self):
        """Not once per variant: the answer is the same for the whole product."""
        client = broadway()
        client.fetch_barcode = Mock(return_value="8800294993574")
        listing = dict(self.LISTING)
        listing["variants"] = [
            {"id": i, "sku": "NOT-A-BARCODE", "price": "10.00", "available": True}
            for i in range(4)
        ]
        products = client.to_products(listing)
        self.assertEqual(len(products), 4)
        self.assertEqual(client.fetch_barcode.call_count, 1)
        self.assertTrue(all(p.gtin == "8800294993574" for p in products))


class BroadwayPaginationTests(unittest.TestCase):
    def test_a_repeated_page_ends_the_walk(self):
        """A store that keeps answering with page one must not loop."""
        client = broadway()
        page = {"products": [{"id": 1, "handle": "a", "title": "A"}]}
        client.request_json = Mock(return_value=page)
        collected = list(client.iter_catalogue())
        self.assertEqual(len(collected), 1)
        self.assertLess(client.request_json.call_count, 4)

    def test_an_empty_page_ends_the_walk(self):
        client = broadway()
        client.request_json = Mock(side_effect=[{"products": []}])
        self.assertEqual(list(client.iter_catalogue()), [])


class PurplleDiscoveryTests(unittest.TestCase):
    BRANDS = {"cosrx": "COSRX", "thedermaco": "The Derma Co", "thederma": "The Derma"}

    def test_the_brand_is_read_from_the_front_of_the_slug(self):
        self.assertEqual(
            brand_from_slug("cosrx-snail-mucin-essence-100-ml", self.BRANDS), "COSRX"
        )

    def test_the_longest_matching_brand_wins(self):
        self.assertEqual(
            brand_from_slug("the-derma-co-1-percent-serum", self.BRANDS),
            "The Derma Co",
        )

    def test_matching_happens_on_whole_words(self):
        """A short brand must not claim a word that merely starts with it."""
        self.assertEqual(brand_from_slug("cosrxian-cream-50-ml", self.BRANDS), "")

    def test_a_brand_that_is_not_configured_is_not_matched(self):
        self.assertEqual(brand_from_slug("himalaya-neem-face-wash", self.BRANDS), "")

    def test_the_sitemap_yields_only_configured_brands(self):
        client = purplle(brands=["COSRX"])
        client.request_text = Mock(
            return_value=(
                "<urlset>"
                "<url><loc>https://www.purplle.com/product/cosrx-toner/reviews</loc></url>"
                "<url><loc>https://www.purplle.com/product/lakme-kajal/reviews</loc></url>"
                "<url><loc>https://www.purplle.com/skincare/face-wash</loc></url>"
                "</urlset>"
            )
        )
        self.assertEqual(client.discover_slugs(), [("cosrx-toner", "COSRX")])


class PurplleParsingTests(unittest.TestCase):
    def page(self, *, ld_price="0", master="8901030974090", our_price="299"):
        return (
            '<script type="application/ld+json">'
            '{"@type":"Product","name":"COSRX Toner",'
            '"brand":{"@type":"Thing","name":"COSRX"},'
            '"description":"<p>A toner</p>",'
            '"image":["https://img.test/1.jpg"],'
            '"sku":"PPLB8901030764622NM3",'
            '"aggregateRating":{"ratingValue":"4.3","ratingCount":"120"},'
            f'"offers":{{"@type":"Offer","price":"{ld_price}",'
            '"availability":"http://schema.org/InStock"}}'
            "</script>"
            "<script>window.state={"
            f"master_product_id:\"{master}\",mrp:\"350\","
            f"our_price:{our_price},l1_category_name:\"Skincare\","
            'l2_category_name:"Toner",stock_status:1'
            "}</script>"
        )

    def test_the_schema_block_supplies_the_core_fields(self):
        product = purplle().to_product("cosrx-toner", self.page(ld_price="280"))
        self.assertEqual(product.product_name, "COSRX Toner")
        self.assertEqual(product.brand, "COSRX")
        self.assertEqual(product.selling_price, 280.0)
        self.assertEqual(product.mrp, 350.0)
        self.assertEqual(product.rating, 4.3)
        self.assertEqual(product.rating_count, 120)
        self.assertEqual(product.categories, ["Skincare", "Toner"])
        self.assertTrue(product.in_stock)

    def test_a_zero_schema_price_falls_back_to_the_page_state(self):
        """Most pages leave the schema price at zero; the state has the real one."""
        product = purplle().to_product("cosrx-toner", self.page(ld_price="0"))
        self.assertEqual(product.selling_price, 299.0)

    def test_a_zero_price_is_never_reported_as_free(self):
        product = purplle().to_product(
            "cosrx-toner", self.page(ld_price="0", our_price="0")
        )
        self.assertIsNone(product.selling_price)

    def test_the_master_product_id_is_used_as_the_barcode(self):
        product = purplle().to_product("cosrx-toner", self.page())
        self.assertEqual(product.gtin, "8901030974090")

    def test_an_internal_master_id_falls_through_to_the_sku(self):
        """A restricted-prefix id is not a barcode; the sku still holds one."""
        product = purplle().to_product(
            "cosrx-toner", self.page(master="9991308610002")
        )
        self.assertEqual(product.gtin, "8901030764622")

    def test_a_page_without_a_product_block_is_skipped(self):
        self.assertIsNone(purplle().to_product("x", "<html>nothing</html>"))


class KindlifeTests(unittest.TestCase):
    PAGE = (
        '<script type="application/ld+json">'
        '{"@type":"http://schema.org/Product","name":"Heartleaf Cleansing Oil",'
        '"brand":{"@type":"Brand","name":"Anua"},'
        '"description":"Gentle oil","sku":"ANUA-1",'
        '"image":["https://cdn.kindlife.in/images/detailed/31/8906034883836_5.jpg"],'
        '"offers":[{"@type":"http://schema.org/Offer","price":1720,'
        '"availability":"InStock"}]}'
        "</script>"
    )

    def test_the_product_is_read_from_the_schema_block(self):
        product = kindlife().to_product(
            "https://www.kindlife.in/heartleaf-oil/", self.PAGE
        )
        self.assertEqual(product.brand, "Anua")
        self.assertEqual(product.selling_price, 1720.0)
        self.assertEqual(product.product_id, "heartleaf-oil")
        self.assertTrue(product.in_stock)

    def test_a_barcode_named_photograph_supplies_the_gtin(self):
        product = kindlife().to_product("https://x.test/p/", self.PAGE)
        self.assertEqual(product.gtin, "8906034883836")

    def test_no_barcode_is_invented_when_the_filename_is_ordinary(self):
        """A guessed barcode is worse than none: the supervisor spreads it."""
        page = self.PAGE.replace("8906034883836_5.jpg", "HO2A1364.jpg")
        product = kindlife().to_product("https://x.test/p/", page)
        self.assertEqual(product.gtin, "")

    def test_a_numeric_filename_that_fails_the_check_digit_is_refused(self):
        page = self.PAGE.replace("8906034883836_5", "1234567890123_5")
        self.assertEqual(
            kindlife().to_product("https://x.test/p/", page).gtin, ""
        )

    def test_image_scanning_handles_an_empty_list(self):
        self.assertEqual(gtin_from_images([]), "")

    def test_only_product_pages_are_taken_from_the_sitemap(self):
        client = kindlife()
        client.request_text = Mock(
            return_value=(
                "<urlset>"
                "<url><loc>https://www.kindlife.in/vitamin-c-mask/</loc></url>"
                "<url><loc>https://www.kindlife.in/skincare-l/lip-care/masks/</loc></url>"
                "<url><loc>https://www.kindlife.in/index.php?dispatch=x</loc></url>"
                "<url><loc>https://www.kindlife.in/serum/?selected_section=y</loc></url>"
                "</urlset>"
            )
        )
        self.assertEqual(
            client.discover_urls(), ["https://www.kindlife.in/vitamin-c-mask/"]
        )

    def test_products_of_other_brands_are_not_kept(self):
        client = kindlife(brands=["Anua"])
        keep = client.to_product("https://x.test/a/", self.PAGE)
        other = client.to_product(
            "https://x.test/b/", self.PAGE.replace('"Anua"', '"Brillare"')
        )
        self.assertTrue(client.wanted(keep))
        self.assertFalse(client.wanted(other))


class SharedMarkupHelperTests(unittest.TestCase):
    def test_a_schema_type_written_as_a_url_is_recognised(self):
        node = linked_product(
            '<script type="application/ld+json">'
            '{"@type":"http://schema.org/Product","name":"X"}</script>'
        )
        self.assertEqual(node.get("name"), "X")

    def test_a_product_inside_a_graph_is_found(self):
        node = linked_product(
            '<script type="application/ld+json">'
            '{"@graph":[{"@type":"WebPage"},{"@type":"Product","name":"Y"}]}</script>'
        )
        self.assertEqual(node.get("name"), "Y")

    def test_unparseable_json_is_ignored_rather_than_raising(self):
        self.assertEqual(
            linked_product('<script type="application/ld+json">{oops</script>'), {}
        )

    def test_an_offer_list_and_a_single_offer_read_the_same(self):
        self.assertEqual(first_offer({"offers": [{"price": 5}]}), {"price": 5})
        self.assertEqual(first_offer({"offers": {"price": 5}}), {"price": 5})
        self.assertEqual(first_offer({}), {})

    def test_scripts_do_not_leak_into_description_text(self):
        self.assertEqual(html_to_text("<p>Hi</p><script>x=1</script>"), "Hi")

    def test_a_body_without_a_declared_charset_is_read_as_utf8(self):
        """requests would assume ISO-8859-1 and turn UTF-8 into mojibake."""
        response = Mock()
        response.headers = {"Content-Type": "text/html"}
        response.content = "WishCare Rosemary".encode("utf-8")
        response.text = "must not be used"
        self.assertEqual(_decoded_body(response), "WishCare Rosemary")

    def test_a_declared_charset_is_honoured(self):
        response = Mock()
        response.headers = {"Content-Type": "text/html; charset=utf-8"}
        response.text = "decoded by requests"
        self.assertEqual(_decoded_body(response), "decoded by requests")


if __name__ == "__main__":
    unittest.main()
