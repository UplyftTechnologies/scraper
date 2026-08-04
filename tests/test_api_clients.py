import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from pricing_scraper.clients.base import BaseJsonClient, parse_curl_command
from pricing_scraper.clients.amazon import (
    AmazonClient,
    _asin,
    _count,
    _money,
    _search_asins_from_html,
    _section,
)
from pricing_scraper.clients.nykaa import NykaaClient, _key_ingredients
from pricing_scraper.clients.tira import TiraClient
from pricing_scraper.models import Product


def quiet_logger(name):
    logger = logging.Logger(name)
    logger.addHandler(logging.NullHandler())
    return logger


def response(
    payload,
    *,
    status=200,
    content_type="application/json",
    url="https://api.example.test/list",
):
    item = requests.Response()
    item.status_code = status
    item.url = url
    item.headers["Content-Type"] = content_type
    if isinstance(payload, (dict, list)):
        item._content = json.dumps(payload).encode("utf-8")
    else:
        item._content = str(payload).encode("utf-8")
    return item


def request_config(logs_dir):
    return {
        "timeout_seconds": 2,
        "delay_min_seconds": 0,
        "delay_max_seconds": 0,
        "max_requests_per_minute": 100,
        "max_retries": 1,
        "backoff_base_seconds": 0,
        "backoff_max_seconds": 0,
        "soft_block_backoff_seconds": 0,
        "logs_dir": str(logs_dir),
    }


class CurlParsingTests(unittest.TestCase):
    def test_preserves_curl_method_headers_and_body(self):
        spec = parse_curl_command(
            """curl 'https://api.example.test/list?q=skin' \\
              -H 'accept: application/json' \\
              -H 'x-client-version: 42' \\
              --data-raw '{"page":1}'"""
        )
        self.assertEqual(spec.method, "POST")
        self.assertEqual(spec.url, "https://api.example.test/list?q=skin")
        self.assertEqual(
            spec.headers,
            {"accept": "application/json", "x-client-version": "42"},
        )
        self.assertEqual(spec.body, '{"page":1}')


class BaseClientTests(unittest.TestCase):
    def test_retries_soft_block_then_parses_json(self):
        with tempfile.TemporaryDirectory() as directory:
            session = requests.Session()
            session.request = Mock(
                side_effect=[
                    response("<html>captcha</html>", content_type="text/html"),
                    response({"ok": True}),
                ]
            )
            sleeps = []
            client = BaseJsonClient(
                request_config(Path(directory)),
                {"accept": "application/json"},
                session=session,
                sleeper=sleeps.append,
                random_uniform=lambda low, _high: low,
                logger=quiet_logger("base-client-test"),
            )
            payload = client.request_json("GET", "https://api.example.test/list")
            self.assertEqual(payload, {"ok": True})
            self.assertEqual(client.blocks_encountered, 1)
            self.assertEqual(session.request.call_count, 2)
            self.assertTrue(list((Path(directory) / "failures").glob("*soft_block.txt")))

    def test_accepts_json_body_mislabeled_as_html(self):
        with tempfile.TemporaryDirectory() as directory:
            session = requests.Session()
            session.request = Mock(
                return_value=response({"status": "success"}, content_type="text/html")
            )
            client = BaseJsonClient(
                request_config(Path(directory)),
                {"accept": "application/json"},
                session=session,
                sleeper=lambda _seconds: None,
                random_uniform=lambda low, _high: low,
                logger=quiet_logger("mislabeled-json-test"),
            )
            self.assertEqual(
                client.request_json("GET", "https://api.example.test/list"),
                {"status": "success"},
            )
            self.assertEqual(client.blocks_encountered, 0)


class NykaaClientTests(unittest.TestCase):
    def test_resumable_scrape_stops_at_reported_partition_end(self):
        with tempfile.TemporaryDirectory() as directory:
            session = requests.Session()
            session.request = Mock(
                return_value=response(
                    {
                        "response": {
                            "products": [
                                {
                                    "productId": "last-product",
                                    "productName": "Last product",
                                    "brandName": "Brand",
                                    "price": 100,
                                }
                            ],
                            "total_found": 101,
                            "offset": 100,
                            "product_count": 1,
                            "stop_further_call": 0,
                        }
                    }
                )
            )
            client = NykaaClient(
                {
                    "curl_command": (
                        "curl 'https://api.example.test/list' "
                        "-H 'accept: application/json'"
                    ),
                    "start_page": 1,
                    "page_limit": 700,
                    "products_paths": ["response.products"],
                    "categories": [{"id": "8377", "name": "Partition"}],
                },
                request_config(Path(directory)),
                brands=[],
                session=session,
                sleeper=lambda _seconds: None,
                random_uniform=lambda low, _high: low,
                logger=quiet_logger("nykaa-partition-end-test"),
            )

            result = client.scrape_category_resumable(
                client.select_categories(["Partition"])[0],
                start_page=6,
            )

            self.assertTrue(result.completed)
            self.assertEqual(result.stop_reason, "end_of_results")
            self.assertEqual(result.next_page, 7)
            self.assertEqual(session.request.call_count, 1)

    def test_resumable_scrape_keeps_repeated_tail_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            repeated_payload = {
                "response": {
                    "products": [
                        {
                            "productId": "tail-product",
                            "productName": "Tail product",
                            "brandName": "Brand",
                            "price": 100,
                        }
                    ]
                }
            }
            session = requests.Session()
            session.request = Mock(
                side_effect=[
                    response(repeated_payload),
                    response(repeated_payload),
                    response(repeated_payload),
                ]
            )
            client = NykaaClient(
                {
                    "curl_command": (
                        "curl 'https://api.example.test/list' "
                        "-H 'accept: application/json'"
                    ),
                    "start_page": 1,
                    "page_limit": 10,
                    "products_paths": ["response.products"],
                    "categories": [{"id": "8377", "name": "All Skincare"}],
                },
                request_config(Path(directory)),
                brands=[],
                session=session,
                sleeper=lambda _seconds: None,
                random_uniform=lambda low, _high: low,
                logger=quiet_logger("nykaa-repeated-tail-test"),
            )

            result = client.scrape_category_resumable(
                client.select_categories(["All Skincare"])[0],
                start_page=1,
            )

            self.assertFalse(result.completed)
            self.assertEqual(result.stop_reason, "repeated_pages")
            self.assertEqual(result.next_page, 4)

    def test_parses_product_details_into_separate_enriched_skus(self):
        with tempfile.TemporaryDirectory() as directory:
            client = NykaaClient(
                {
                    "curl_command": (
                        "curl 'https://api.example.test/list' "
                        "-H 'accept: application/json'"
                    ),
                    "details": {"include_top_reviews": True},
                },
                request_config(Path(directory)),
                brands=[],
                session=requests.Session(),
                sleeper=lambda _seconds: None,
                logger=quiet_logger("nykaa-detail-parser-test"),
            )
            fallback = Product(
                site="nykaa",
                product_id="sku-default",
                parent_product_id="parent-1",
                brand="Test Brand",
                product_name="Test Cleanser",
            )
            products = client._parse_detail_response(
                {
                    "id": "parent-1",
                    "parent_id": "parent-1",
                    "gtin": "8809416470009",
                    "brand_name": "Test Brand",
                    "description": (
                        "<p>Gentle cleanser.</p>"
                        '<img src="https://img.test/content.jpg">'
                    ),
                    "ingredients": (
                        "<p><b>Key Ingredients:</b></p><ul>"
                        "<li><b>Ceramide:</b> Repairs the barrier.</li>"
                        "<li><b>Niacinamide:</b> Evens tone.</li></ul>"
                        "<p><b>Full Ingredient List:</b> Water, Ceramide, "
                        "Niacinamide, Glycerin</p>"
                    ),
                    "use": "<p>Massage and rinse.</p>",
                    "rating": 4.4,
                    "rating_count": 100,
                    "review_count": "25",
                    "review_splitup": [
                        {"id": 5, "count": "80", "per": 80}
                    ],
                    "top_review": [
                        {
                            "review_id": "r1",
                            "detail": "Works well",
                            "nickname": "Buyer",
                            "is_buyer": True,
                            "meta_data": {"value": "5"},
                        }
                    ],
                    "options": [
                        {
                            "id": "sku-88",
                            "parent_id": "parent-1",
                            "sku": "SKU88",
                            "gtin": "8809803586047",
                            "name": "Test Cleanser",
                            "pack_size": "88ml",
                            "price": 559,
                            "final_price": 475,
                            "gludo_stock": True,
                            "carousel": [
                                {
                                    "url": "https://img.test/88-1.jpg",
                                    "mediaType": "image",
                                }
                            ],
                        },
                        {
                            "id": "sku-236",
                            "parent_id": "parent-1",
                            "sku": "SKU236",
                            "name": "Test Cleanser",
                            "pack_size": "236ml",
                            "price": 1249,
                            "final_price": 1062,
                            "gludo_stock": True,
                            "carousel": [
                                {
                                    "url": "https://img.test/236-1.jpg",
                                    "mediaType": "image",
                                }
                            ],
                        },
                    ],
                },
                fallback,
            )

            self.assertEqual(
                [item.product_id for item in products],
                ["sku-88", "sku-236"],
            )
            self.assertEqual(
                [item.variant for item in products],
                ["88ml", "236ml"],
            )
            self.assertEqual(products[0].selling_price, 475)
            self.assertEqual(products[0].review_count, 25)
            self.assertEqual(products[0].description, "Gentle cleanser.")
            self.assertEqual(
                products[0].key_ingredients,
                ["Ceramide", "Niacinamide"],
            )
            # The full INCI list stays in the ingredients column.
            self.assertIn("Glycerin", products[0].ingredients)
            self.assertIn(
                "https://img.test/content.jpg",
                products[0].image_urls,
            )
            self.assertTrue(products[0].top_reviews[0]["verified_buyer"])
            self.assertEqual(products[0].gtin, "8809803586047")
            # The parent barcode describes the parent SKU, so the second size
            # stays blank instead of inheriting a barcode that is not its own.
            self.assertEqual(products[1].gtin, "")

    def test_resumable_scrape_stops_only_after_empty_page(self):
        with tempfile.TemporaryDirectory() as directory:
            session = requests.Session()
            session.request = Mock(
                side_effect=[
                    response(
                        {
                            "response": {
                                "products": [
                                    {
                                        "productId": "p-5",
                                        "productName": "Face cream",
                                        "brandName": "Any Brand",
                                        "price": 499,
                                    }
                                ]
                            }
                        }
                    ),
                    response({"response": {"products": []}}),
                ]
            )
            client = NykaaClient(
                {
                    "curl_command": (
                        "curl 'https://api.example.test/list' "
                        "-H 'accept: application/json'"
                    ),
                    "start_page": 1,
                    "page_limit": 10,
                    "products_paths": ["response.products"],
                    "categories": [
                        {
                            "id": "8377",
                            "name": "All Skincare",
                            "covers_all": True,
                        }
                    ],
                },
                request_config(Path(directory)),
                brands=[],
                session=session,
                sleeper=lambda _seconds: None,
                random_uniform=lambda low, _high: low,
                logger=quiet_logger("nykaa-resume-test"),
            )
            saved_pages = []
            result = client.scrape_category_resumable(
                client.select_categories()[0],
                start_page=5,
                on_page=lambda page, products: saved_pages.append(
                    (page, len(products))
                ),
            )

            self.assertTrue(result.completed)
            self.assertEqual(result.stop_reason, "empty_page")
            self.assertEqual(result.next_page, 6)
            self.assertEqual(saved_pages, [(5, 1)])
            called_urls = [
                call.kwargs["url"] for call in session.request.call_args_list
            ]
            self.assertIn("page_no=5", called_urls[0])
            self.assertIn("page_no=6", called_urls[1])

    def test_paginates_expands_variants_filters_brands_and_continues(self):
        with tempfile.TemporaryDirectory() as directory:
            first_page = {
                "response": {
                    "products": [
                        {
                            "productId": "parent-1",
                            "productName": "Minimalist Face Serum",
                            "brandName": "Minimalist",
                            "dynamicUrl": "/minimalist-serum/p/1",
                            "imageUrl": "//images.example.test/serum.jpg",
                            "rating": "4.4",
                            "ratingCount": "1,234",
                            "variants": [
                                {
                                    "skuId": "sku-30",
                                    "size": "30 ml",
                                    "mrp": "₹599",
                                    "sellingPrice": "₹539",
                                    "inStock": True,
                                },
                                {
                                    "skuId": "sku-60",
                                    "size": "60 ml",
                                    "mrp": 999,
                                    "sellingPrice": 899,
                                    "inStock": False,
                                },
                            ],
                        },
                        {
                            "productId": "excluded",
                            "productName": "Other Brand Cream",
                            "brandName": "Other Brand",
                        },
                        {"unexpected": "malformed"},
                    ]
                }
            }
            session = requests.Session()
            session.request = Mock(
                side_effect=[response(first_page), response({"response": {"products": []}})]
            )
            site_config = {
                "curl_command": (
                    "curl 'https://api.example.test/list?sort=popularity' "
                    "-H 'accept: application/json' -H 'user-agent: Test Browser'"
                ),
                "site_base_url": "https://www.nykaa.com",
                "category_field": "category_id",
                "category_location": "query",
                "page_field": "page_no",
                "page_location": "query",
                "start_page": 1,
                "page_limit": 5,
                "products_paths": ["response.products"],
                "categories": [{"id": "8377", "name": "skincare"}],
            }
            client = NykaaClient(
                site_config,
                request_config(Path(directory)),
                # Punctuation and spacing differ across storefronts, so the
                # filter matches "Minimalist" from a loosely typed entry.
                brands=[" minimalist. "],
                session=session,
                sleeper=lambda _seconds: None,
                random_uniform=lambda low, _high: low,
                logger=quiet_logger("nykaa-client-test"),
            )
            products = client.scrape(client.select_categories(["SKINCARE"]))

            self.assertEqual([item.product_id for item in products], ["sku-30", "sku-60"])
            self.assertEqual(products[0].variant, "30 ml")
            self.assertEqual(products[0].mrp, 599.0)
            self.assertEqual(products[0].selling_price, 539.0)
            self.assertAlmostEqual(products[0].discount_pct, 10.02)
            self.assertEqual(products[0].rating_count, 1234)
            self.assertTrue(products[0].product_url.startswith("https://www.nykaa.com/"))
            self.assertEqual(client.product_failures, 1)
            called_urls = [
                call.kwargs["url"] for call in session.request.call_args_list
            ]
            self.assertIn("category_id=8377", called_urls[0])
            self.assertIn("page_no=1", called_urls[0])
            self.assertIn("page_no=2", called_urls[1])

    def test_parses_live_nykaa_price_shape_and_skips_content_tiles(self):
        with tempfile.TemporaryDirectory() as directory:
            session = requests.Session()
            session.request = Mock()
            client = NykaaClient(
                {
                    "curl_command": (
                        "curl 'https://www.nykaa.com/app-api/list' "
                        "-H 'accept: application/json'"
                    ),
                    "products_paths": ["response.products"],
                    "categories": [{"id": "8393", "name": "moisturizers"}],
                },
                request_config(Path(directory)),
                brands=["Neutrogena"],
                session=session,
                sleeper=lambda _seconds: None,
                logger=quiet_logger("nykaa-live-shape-test"),
            )
            product = client._parse_product(
                {
                    "object_type": "product",
                    "id": "875156",
                    "brand_name": "Neutrogena",
                    "name": "Hydro Boost",
                    "price": 1190,
                    "final_price": 750,
                    "discount": 37,
                    "option_text": "2 sizes",
                    "rating_count": 161034,
                    "gludo_stock": True,
                    "quantity": 10,
                },
                "2026-07-27T00:00:00+00:00",
            )
            self.assertIsNotNone(product)
            self.assertEqual(product.mrp, 1190.0)
            self.assertEqual(product.selling_price, 750.0)
            self.assertEqual(product.variant, "2 sizes")
            self.assertEqual(product.rating_count, 161034)
            self.assertIsNone(
                client._parse_product(
                    {"object_type": "tiptile", "position": 11},
                    "2026-07-27T00:00:00+00:00",
                )
            )


class NykaaKeyIngredientTests(unittest.TestCase):
    """Nykaa writes key ingredients in more than one shape on the same field."""

    def test_reads_the_bulleted_form(self):
        self.assertEqual(
            _key_ingredients(
                "<p><b>Key Ingredients:</b></p><ul>"
                "<li><b>Snail Secretion Filtrate:</b> Hydrates.</li>"
                "<li><b>Sodium Hyaluronate (Hyaluronic Acid):</b> Plumps.</li>"
                "</ul><p><b>Full Ingredient List:</b> Betaine, Carbomer</p>"
            ),
            ["Snail Secretion Filtrate", "Sodium Hyaluronate (Hyaluronic Acid)"],
        )

    def test_reads_the_inline_form(self):
        self.assertEqual(
            _key_ingredients(
                "<p><b>Key Ingredients: </b>Niacinamide:Brightens skin tone."
                "Panthenol:Moisturizes and soothes.</p>"
            ),
            ["Niacinamide", "Panthenol"],
        )

    def test_ignores_products_that_publish_only_an_inci_list(self):
        self.assertEqual(_key_ingredients("<p>Water, Glycerin, Betaine</p>"), [])
        # A bulleted shade breakdown has no heading, so it is not mistaken
        # for a key-ingredient list.
        self.assertEqual(
            _key_ingredients(
                "<ul><li><b>Strawberry Ade:</b> Water, Butylene Glycol</li></ul>"
            ),
            [],
        )
        self.assertEqual(_key_ingredients(""), [])
        self.assertEqual(_key_ingredients(None), [])


class TiraClientTests(unittest.TestCase):
    @staticmethod
    def site_config():
        return {
            "listing_url_template": (
                "https://catalog.example.test/collections/{collection}/items"
            ),
            "detail_url_template": (
                "https://api.example.test/products/{slug}/sizes/"
            ),
            "application_id": "app-id",
            "application_token": "app-token",
            "page_size": 2,
            "page_limit": 2,
            "categories": [
                {"id": "skin", "name": "All Skin", "covers_all": True}
            ],
            "details": {"include_top_reviews": True},
        }

    def test_expands_variants_and_extracts_product_content(self):
        with tempfile.TemporaryDirectory() as directory:
            client = TiraClient(
                self.site_config(),
                request_config(Path(directory)),
                session=requests.Session(),
                sleeper=lambda _seconds: None,
                logger=quiet_logger("tira-parse-test"),
            )
            products = client._parse_item(
                {
                    "uid": 101,
                    "name": "Barrier Cleanser 50 ml",
                    "slug": "barrier-cleanser-50ml",
                    "brand": {"name": "Test Brand"},
                    "sellable": True,
                    "price": {
                        "marked": {"min": 800},
                        "effective": {"min": 600},
                    },
                    "medias": [{"type": "image", "url": "https://img/root.jpg"}],
                    "attributes": {
                        "identifier": {"sku_code": ["SKU-50"]},
                        "pack-size": "50 ml",
                        "preference": ["Cruelty Free"],
                        "super-ingredients": ["Ceramide", "Glycerin"],
                        "description": (
                            "<h3>Description</h3><p>Gentle daily cleanser.</p>"
                            "<h3>Ingredients</h3><p>Water, glycerin.</p>"
                            "<h3>How To Use</h3><p>Massage and rinse.</p>"
                        ),
                    },
                    "variants": [
                        {
                            "items": [
                                {
                                    "uid": 101,
                                    "name": "Barrier Cleanser 50 ml",
                                    "slug": "barrier-cleanser-50ml",
                                    "value": "50 ml",
                                },
                                {
                                    "uid": 102,
                                    "name": "Barrier Cleanser 100 ml",
                                    "slug": "barrier-cleanser-100ml",
                                    "value": "100 ml",
                                    "medias": [
                                        {
                                            "type": "image",
                                            "url": "https://img/100.jpg",
                                        }
                                    ],
                                },
                            ]
                        }
                    ],
                },
                "2026-07-28T00:00:00+00:00",
            )

            self.assertEqual([item.product_id for item in products], ["101", "102"])
            self.assertEqual(products[0].mrp, 800)
            self.assertEqual(products[0].selling_price, 600)
            self.assertEqual(products[0].discount_pct, 25)
            self.assertEqual(products[0].sku, "SKU-50")
            self.assertIsNone(products[1].selling_price)
            self.assertEqual(products[1].variant, "100 ml")
            self.assertIn("Gentle daily cleanser", products[0].description)
            self.assertEqual(products[0].ingredients, "Water, glycerin.")
            self.assertEqual(products[0].how_to_use, "Massage and rinse.")
            # Super-ingredients are ingredient names, so they leave the
            # special-features column and stand on their own.
            self.assertEqual(
                products[0].key_ingredients,
                ["Ceramide", "Glycerin"],
            )
            self.assertEqual(products[0].special_features, ["Cruelty Free"])

    def test_selects_every_enabled_collection_without_a_covering_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.site_config()
            config["categories"] = [
                {"id": "moisturizers", "name": "Moisturizers"},
                {"id": "cleansers", "name": "Cleansers"},
            ]
            client = TiraClient(
                config,
                request_config(Path(directory)),
                session=requests.Session(),
                sleeper=lambda _seconds: None,
                logger=quiet_logger("tira-category-selection-test"),
            )

            selected = client.select_categories(None)

            self.assertEqual(
                [category["name"] for category in selected],
                ["Moisturizers", "Cleansers"],
            )

    def test_paginates_and_enriches_an_additional_variant(self):
        with tempfile.TemporaryDirectory() as directory:
            session = requests.Session()
            session.request = Mock(
                side_effect=[
                    response(
                        {
                            "items": [
                                {
                                    "uid": 102,
                                    "name": "Barrier Cleanser 100 ml",
                                    "slug": "barrier-cleanser-100ml",
                                    "brand": {"name": "Test Brand"},
                                    "sellable": True,
                                    "attributes": {},
                                }
                            ],
                            "page": {"has_next": False, "item_total": 1},
                        }
                    ),
                    response(
                        {
                            "sellable": True,
                            "price": {
                                "marked": {"min": 1000},
                                "effective": {"min": 750},
                            },
                            "sizes": [
                                {
                                    "display": "100 ml",
                                    "is_available": True,
                                    "seller_identifiers": ["SKU-100"],
                                }
                            ],
                        }
                    ),
                ]
            )
            client = TiraClient(
                self.site_config(),
                request_config(Path(directory)),
                session=session,
                sleeper=lambda _seconds: None,
                logger=quiet_logger("tira-pagination-test"),
            )

            run = client.scrape_category_resumable(
                client.select_categories(["All Skin"])[0],
                start_page=1,
            )
            enriched = client.fetch_variant_price(run.products[0])

            self.assertTrue(run.completed)
            self.assertEqual(run.next_page, 2)
            self.assertEqual(enriched.mrp, 1000)
            self.assertEqual(enriched.selling_price, 750)
            self.assertEqual(enriched.discount_pct, 25)
            self.assertEqual(enriched.sku, "SKU-100")
            self.assertTrue(enriched.in_stock)


class AmazonClientTests(unittest.TestCase):
    def test_extracts_asins_from_both_current_search_card_layouts(self):
        html = """
        <div data-component-type="s-search-result"
             data-asin="B0D4YSFKLC">
          <a href="/dot-key/dp/B0ABC12345"><h2>Wrapped title</h2></a>
        </div>
        """

        self.assertEqual(
            _search_asins_from_html(html),
            ["B0D4YSFKLC", "B0ABC12345"],
        )

    def test_normalizes_public_page_values_and_category_selection(self):
        self.assertEqual(_asin("https://www.amazon.in/dp/B0D4YSFKLC"), "B0D4YSFKLC")
        self.assertEqual(_asin("PARENTASIN"), "")
        self.assertEqual(_money("₹1,299.00"), 1299.0)
        self.assertEqual(_count("14.9K ratings"), 14900)
        important = (
            "Ingredients: Water, Glycerin Directions: Apply twice daily "
            "Safety Information: Patch test first"
        )
        self.assertEqual(
            _section(
                important,
                "Ingredients",
                ("Directions", "Safety Information"),
            ),
            "Water, Glycerin",
        )
        client = AmazonClient(
            {
                "categories": [
                    {"name": "Moisturizers", "query": "face moisturizer"},
                    {"name": "Sun Care", "query": "sunscreen"},
                ],
                "delay_min_seconds": 0,
                "delay_max_seconds": 0,
            },
            {
                "logs_dir": "logs",
                "max_requests_per_minute": 12,
            },
            sleeper=lambda _seconds: None,
            logger=quiet_logger("amazon-normalization-test"),
        )
        selected = client.select_categories(["sun care"])
        self.assertEqual(selected[0]["query"], "sunscreen")

    def test_parses_split_visible_price_spans(self):
        with patch.object(
            AmazonClient,
            "_first_text",
            return_value="-30% ₹42000 M.R.P.: ₹59900",
        ):
            selling, mrp, discount = AmazonClient._core_price_fallback(
                object()
            )

        self.assertEqual(selling, 420)
        self.assertEqual(mrp, 599)
        self.assertEqual(discount, 30)


if __name__ == "__main__":
    unittest.main()
