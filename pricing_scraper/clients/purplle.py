"""Client for purplle.com.

Purplle has no public catalogue API. Its brand pages server-render only a
handful of products and lazy-load the rest through an internal endpoint, so
discovery instead comes from the review sitemap, which lists every product on
the site - 18,748 of them - in a single request. Product slugs begin with the
brand, so the sweep can pick out the 2,861 belonging to the configured brands
before fetching a single product page.

Each product page carries a schema.org Product block, which is the stable part
of the page and supplies the name, brand, description, images, price and
rating. Two things live only in the page's own state object: the MRP and
``master_product_id``, which is the product's EAN. The barcode is also encoded
in the schema sku, as ``PPLB`` followed by the thirteen digits, so there are
two independent sources for it.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

import requests

from pricing_scraper.clients.base import (
    BaseJsonClient,
    RequestFailed,
    html_to_text,
)
from pricing_scraper.models import (
    Product,
    brand_key,
    normalize_gtin,
    plausible_retail_barcode,
)

SITE = "purplle"
DEFAULT_BASE_URL = "https://www.purplle.com"
SITEMAP_PATH = "/sitemap/products/product-reviews.xml"

_LD_BLOCK = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_LOC = re.compile(r"<loc>([^<]+)</loc>")
# The page state is a JavaScript object literal, not JSON: the keys are bare.
_MRP = re.compile(r"\bmrp\s*:\s*\"?([0-9]+(?:\.[0-9]+)?)\"?")
_MASTER_ID = re.compile(r"\bmaster_product_id\s*:\s*\"?(\d{8,14})\"?")
_OUR_PRICE = re.compile(r"\bour_price\s*:\s*\"?([0-9]+(?:\.[0-9]+)?)\"?")
_CATEGORY = re.compile(r"\bl([123])_category_name\s*:\s*\"([^\"]{1,80})\"")
_SKU_BARCODE = re.compile(r"PPLB(\d{13})")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any) -> float | None:
    text = _text(value).replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def brand_from_slug(slug: str, brand_keys: Mapping[str, str]) -> str:
    """Match the leading words of a slug against the configured brands.

    Slugs are hyphen-joined words, so the brand is matched on whole-token
    boundaries rather than as a raw prefix. Without that, a short brand would
    claim any product whose first word merely begins with the same letters.
    The longest match wins, so "The Derma Co" is preferred over "The Derma"
    when both are configured.
    """
    tokens = [token for token in slug.split("-") if token]
    for count in range(min(len(tokens), 6), 0, -1):
        key = brand_key("".join(tokens[:count]))
        if key in brand_keys:
            return brand_keys[key]
    return ""


class PurplleClient(BaseJsonClient):
    """Collect Purplle products for the configured brands."""

    def __init__(
        self,
        site_config: Mapping[str, Any],
        request_config: Mapping[str, Any],
        *,
        brands: Sequence[str] = (),
        session: requests.Session | None = None,
        logger: logging.Logger | None = None,
        **kwargs: Any,
    ) -> None:
        headers = {
            "User-Agent": _text(site_config.get("user_agent"))
            or (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
        }
        super().__init__(
            request_config, headers, session=session, logger=logger, **kwargs
        )
        self.base_url = (
            _text(site_config.get("base_url")) or DEFAULT_BASE_URL
        ).rstrip("/")
        self.brand_keys = {
            brand_key(brand): _text(brand)
            for brand in brands
            if brand_key(brand)
        }
        self.product_failures = 0

    # -- discovery ---------------------------------------------------------
    def discover_slugs(self) -> list[tuple[str, str]]:
        """Return (slug, brand) for every product of a configured brand.

        One request covers the whole site. When no brands are configured every
        product is returned, which is deliberate: an empty filter means "do not
        filter" everywhere else in the project.
        """
        markup = self.request_text("GET", f"{self.base_url}{SITEMAP_PATH}")
        found: list[tuple[str, str]] = []
        seen: set[str] = set()
        for location in _LOC.findall(markup):
            if "/product/" not in location:
                continue
            slug = location.rsplit("/reviews", 1)[0].rsplit("/product/", 1)[-1]
            slug = slug.strip("/")
            if not slug or slug in seen:
                continue
            seen.add(slug)
            if not self.brand_keys:
                found.append((slug, ""))
                continue
            brand = brand_from_slug(slug, self.brand_keys)
            if brand:
                found.append((slug, brand))
        self.logger.info(
            "purplle_discovered slugs=%s of=%s", len(found), len(seen)
        )
        return found

    # -- parsing -----------------------------------------------------------
    @staticmethod
    def _linked_data(markup: str) -> dict[str, Any]:
        """Return the schema.org Product block, or an empty mapping."""
        for raw in _LD_BLOCK.findall(markup):
            try:
                payload = json.loads(raw.strip())
            except ValueError:
                continue
            candidates = payload if isinstance(payload, list) else [payload]
            for candidate in candidates:
                if (
                    isinstance(candidate, dict)
                    and _text(candidate.get("@type")).casefold() == "product"
                ):
                    return candidate
        return {}

    @staticmethod
    def _first_number(pattern: re.Pattern[str], markup: str) -> float | None:
        """Read the first match only.

        A product page also carries the recommendation carousel, so every one
        of these keys appears many times over. The product being viewed is
        rendered first, and taking any later match would report a neighbouring
        product's price as this one's.
        """
        match = pattern.search(markup)
        return _number(match.group(1)) if match else None

    def _gtin(self, markup: str, sku: str) -> str:
        """Read the barcode from either of the two places it appears.

        master_product_id is not always a barcode. For products Purplle
        catalogues itself - typically a parent with shade variants - it holds
        an internal identifier on a GS1-restricted prefix, which validates but
        is not an EAN. Those are rejected rather than stored, because the
        supervisor would otherwise copy them onto the other retailers.
        """
        match = _MASTER_ID.search(markup)
        if match:
            barcode = normalize_gtin(match.group(1))
            if barcode and plausible_retail_barcode(barcode):
                return barcode
        embedded = _SKU_BARCODE.search(sku or "")
        if embedded:
            barcode = normalize_gtin(embedded.group(1))
            if barcode and plausible_retail_barcode(barcode):
                return barcode
        return ""

    @staticmethod
    def _categories(markup: str) -> list[str]:
        levels: dict[str, str] = {}
        for level, name in _CATEGORY.findall(markup):
            levels.setdefault(level, name)
        return [levels[key] for key in sorted(levels) if levels.get(key)]

    def to_product(
        self, slug: str, markup: str, *, brand_hint: str = ""
    ) -> Product | None:
        """Build a Product from one rendered product page."""
        data = self._linked_data(markup)
        name = _text(data.get("name"))
        if not name:
            return None
        brand = data.get("brand")
        brand_name = (
            _text(brand.get("name")) if isinstance(brand, Mapping) else _text(brand)
        )
        offers = data.get("offers")
        offers = offers if isinstance(offers, Mapping) else {}
        # A price of zero marks a parent product whose shades each carry their
        # own price, not a free product. Reporting it as 0.00 would put a
        # meaningless row into the price history and drag any average down.
        selling = _number(offers.get("price")) or None
        if selling is None:
            # Most pages leave the schema price at zero and carry the real one
            # only in the page state. Over forty products the schema supplied
            # a price for nine; our_price supplied one for thirty-one more.
            # It is a fallback rather than the primary source because, on the
            # nine where both appeared, one disagreed - the state lists a
            # variant that is not the one the schema describes.
            selling = self._first_number(_OUR_PRICE, markup) or None
        mrp = self._first_number(_MRP, markup) or None
        if mrp is not None and selling is not None and mrp < selling:
            mrp = None
        discount = None
        if mrp and selling is not None and mrp > 0:
            discount = round((mrp - selling) / mrp * 100, 2)

        images = data.get("image")
        if isinstance(images, str):
            images = [images]
        image_urls = [_text(url) for url in (images or []) if _text(url)]

        rating = data.get("aggregateRating")
        rating = rating if isinstance(rating, Mapping) else {}
        rating_count = _number(rating.get("ratingCount"))
        sku = _text(data.get("sku"))
        availability = _text(offers.get("availability")).casefold()

        return Product(
            site=SITE,
            product_id=slug,
            sku=sku,
            gtin=self._gtin(markup, sku),
            brand=brand_name or brand_hint,
            product_name=name,
            categories=self._categories(markup),
            source_categories=self._categories(markup),
            mrp=mrp,
            selling_price=selling,
            discount_pct=discount,
            rating=_number(rating.get("ratingValue")),
            rating_count=int(rating_count) if rating_count is not None else None,
            in_stock=("outofstock" not in availability.replace("/", ""))
            if availability
            else None,
            product_url=f"{self.base_url}/product/{slug}",
            image_url=image_urls[0] if image_urls else "",
            image_urls=image_urls,
            description=html_to_text(_text(data.get("description"))),
            scraped_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    def fetch_product(self, slug: str, *, brand_hint: str = "") -> Product | None:
        markup = self.request_text("GET", f"{self.base_url}/product/{slug}")
        return self.to_product(slug, markup, brand_hint=brand_hint)

    # -- the sweep ---------------------------------------------------------
    def collect(
        self,
        *,
        on_product: Callable[[Product], None] | None = None,
        max_products: int = 0,
    ) -> list[Product]:
        """Collect every discovered product, skipping the ones that fail."""
        collected: list[Product] = []
        for slug, brand in self.discover_slugs():
            try:
                product = self.fetch_product(slug, brand_hint=brand)
            except (RequestFailed, ValueError) as exc:
                self.product_failures += 1
                self.logger.error(
                    "purplle_product_failed slug=%s error=%s", slug, exc
                )
                continue
            if product is None:
                self.product_failures += 1
                self.logger.warning("purplle_product_unparsed slug=%s", slug)
                continue
            collected.append(product)
            if on_product is not None:
                on_product(product)
            if max_products and len(collected) >= max_products:
                break
        self.logger.info(
            "purplle_collected products=%s failures=%s requests=%s",
            len(collected),
            self.product_failures,
            self.requests_made,
        )
        return collected
