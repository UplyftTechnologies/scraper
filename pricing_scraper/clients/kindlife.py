"""Client for kindlife.in, a CS-Cart storefront.

Kindlife publishes no catalogue API - the Shopify-style ``products.json`` that
its markup hints at answers 404 - but its sitemap lists product pages directly,
and every product page carries a schema.org Product block with the name, brand,
price, availability and images.

The barcode is the weak point. Kindlife exposes no barcode field anywhere;
where a GTIN is recoverable at all it is because the merchandising team named
the product photograph after it, as ``8906034883836_5.jpg``. That worked on two
of eight products sampled, so this site is a good price source and only an
occasional GTIN source. Nothing is inferred when the filename is absent: a
guessed barcode is worse than none, because the supervisor propagates GTINs
between retailers and a wrong one would spread.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

import requests

from pricing_scraper.clients.base import (
    BaseJsonClient,
    RequestFailed,
    first_offer,
    html_to_text,
    linked_product,
)
from pricing_scraper.models import Product, brand_key, normalize_gtin

SITE = "kindlife"
DEFAULT_BASE_URL = "https://www.kindlife.in"
SITEMAP_PATH = "/sitemap.xml"

_LOC = re.compile(r"<loc>([^<]+)</loc>")
# Product photographs are sometimes named after the barcode they depict.
_IMAGE_BARCODE = re.compile(r"/(\d{8,14})(?:[_-]\d+)?\.(?:jpg|jpeg|png|webp)", re.I)


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


def gtin_from_images(urls: Sequence[str]) -> str:
    """Recover a barcode from an image filename, when there is one.

    Only a filename whose digits satisfy the GS1 check digit is accepted, so an
    ordinary numeric asset name cannot be mistaken for a barcode.
    """
    for url in urls:
        for candidate in _IMAGE_BARCODE.findall(url or ""):
            barcode = normalize_gtin(candidate)
            if barcode:
                return barcode
    return ""


class KindlifeClient(BaseJsonClient):
    """Collect Kindlife products for the configured brands."""

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
        self.brand_filter = {brand_key(b) for b in brands if brand_key(b)}
        self.product_failures = 0
        self.skipped_other_brands = 0

    # -- discovery ---------------------------------------------------------
    def discover_urls(self) -> list[str]:
        """Return the product page URLs listed in the sitemap.

        The sitemap mixes product pages with category listings and stored
        searches, and the search entries in particular would each cost a
        request and parse to nothing, so anything carrying a query string or
        the CS-Cart dispatch parameter is dropped up front.
        """
        markup = self.request_text("GET", f"{self.base_url}{SITEMAP_PATH}")
        urls: list[str] = []
        seen: set[str] = set()
        for location in _LOC.findall(markup):
            candidate = _text(location)
            if not candidate or "?" in candidate or "index.php" in candidate:
                continue
            path = candidate[len(self.base_url) :].strip("/")
            # A product page is a single path segment; anything deeper is a
            # category tree such as /skincare-l/lip-care/lip-masks/.
            if not path or "/" in path:
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            urls.append(candidate)
        self.logger.info("kindlife_discovered urls=%s", len(urls))
        return urls

    # -- parsing -----------------------------------------------------------
    def to_product(self, url: str, markup: str) -> Product | None:
        data = linked_product(markup)
        name = _text(data.get("name"))
        if not name:
            return None
        brand = data.get("brand")
        brand_name = (
            _text(brand.get("name")) if isinstance(brand, Mapping) else _text(brand)
        )
        offer = first_offer(data)
        images = data.get("image")
        if isinstance(images, str):
            images = [images]
        image_urls = [_text(item) for item in (images or []) if _text(item)]
        availability = _text(offer.get("availability")).casefold()
        selling = _number(offer.get("price"))
        slug = url.rstrip("/").rsplit("/", 1)[-1]

        return Product(
            site=SITE,
            product_id=slug,
            sku=_text(data.get("sku")),
            gtin=gtin_from_images(image_urls),
            brand=brand_name,
            product_name=name,
            selling_price=selling,
            in_stock=("outofstock" not in availability.replace("/", ""))
            if availability
            else None,
            product_url=url,
            image_url=image_urls[0] if image_urls else "",
            image_urls=image_urls,
            description=html_to_text(_text(data.get("description"))),
            scraped_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    def wanted(self, product: Product) -> bool:
        if not self.brand_filter:
            return True
        return brand_key(product.brand) in self.brand_filter

    def fetch_product(self, url: str) -> Product | None:
        return self.to_product(url, self.request_text("GET", url))

    # -- the sweep ---------------------------------------------------------
    def collect(
        self,
        *,
        on_product: Callable[[Product], None] | None = None,
        max_products: int = 0,
    ) -> list[Product]:
        """Collect the products whose brand is one being tracked.

        Unlike the other storefronts, Kindlife's URLs do not name the brand, so
        the filter can only be applied once the page has been read. The request
        is spent either way; only the row is discarded.
        """
        collected: list[Product] = []
        for url in self.discover_urls():
            try:
                product = self.fetch_product(url)
            except (RequestFailed, ValueError) as exc:
                self.product_failures += 1
                self.logger.error(
                    "kindlife_product_failed url=%s error=%s", url, exc
                )
                continue
            if product is None:
                continue
            if not self.wanted(product):
                self.skipped_other_brands += 1
                continue
            collected.append(product)
            if on_product is not None:
                on_product(product)
            if max_products and len(collected) >= max_products:
                break
        self.logger.info(
            "kindlife_collected products=%s other_brands=%s failures=%s requests=%s",
            len(collected),
            self.skipped_other_brands,
            self.product_failures,
            self.requests_made,
        )
        return collected
