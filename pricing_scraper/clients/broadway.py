"""Client for shop.broadwaylive.in, a Shopify storefront.

Shopify publishes the whole catalogue through ``/products.json``, paginated,
which makes discovery unusually cheap: the entire store - 1,750 products at the
time of writing - arrives in seven requests, already carrying titles, prices,
stock, images and the vendor to filter on.

The barcode needs one note. Shopify omits ``barcode`` from ``products.json``,
but this store puts the same value in the variant ``sku``: measured across the
103 products belonging to the configured brands, 102 carry a valid GTIN there,
and on every one sampled it matched the ``barcode`` that
``/products/<handle>.js`` reports. So the listing alone is normally enough, and
the per-product request is made only for the rare product whose sku is not a
barcode.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Mapping, Sequence

import requests

from pricing_scraper.clients.base import (
    BaseJsonClient,
    RequestFailed,
    html_to_text,
)
from pricing_scraper.models import Product, brand_key, normalize_gtin

SITE = "broadway"
DEFAULT_BASE_URL = "https://shop.broadwaylive.in"
# Shopify caps products.json at 250 per page and ignores anything larger.
PAGE_SIZE = 250
# A guard against paginating for ever if the store starts echoing pages.
MAX_PAGES = 40


def _text(value: Any) -> str:
    return str(value or "").strip()


def gtin_from_sku(sku: str) -> str:
    """Read a barcode out of a Shopify sku, restoring a lost leading zero.

    Some vendors store a 12-digit UPC-A as 11 digits, because whatever
    exported it treated the code as a number and dropped the leading zero.
    Clinique and Estee Lauder skus here are all of that shape - 20714222857
    for what is really 020714222857, on the 020714 prefix that belongs to the
    Estee Lauder Companies.

    Padding is only accepted when the result satisfies the GS1 check digit, so
    an ordinary 11-character item code cannot be promoted into a barcode by
    accident: it would have to pass a check it was never built to satisfy.
    """
    barcode = normalize_gtin(sku)
    if barcode:
        return barcode
    digits = _text(sku)
    if len(digits) == 11 and digits.isdigit():
        return normalize_gtin(digits.zfill(12))
    return ""


def _price(value: Any) -> float | None:
    """Read a Shopify money string, which is rupees with two decimals."""
    text = _text(value)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


class BroadwayClient(BaseJsonClient):
    """Collect the Broadway catalogue, filtered to the configured brands."""

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
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-IN,en;q=0.9",
        }
        super().__init__(
            request_config, headers, session=session, logger=logger, **kwargs
        )
        self.base_url = (
            _text(site_config.get("base_url")) or DEFAULT_BASE_URL
        ).rstrip("/")
        self.brand_filter = {brand_key(b) for b in brands if brand_key(b)}
        self.max_pages = max(1, int(site_config.get("max_pages", MAX_PAGES)))
        self.product_failures = 0

    # -- discovery ---------------------------------------------------------
    def iter_catalogue(self) -> Iterator[Mapping[str, Any]]:
        """Yield every published product, one page of 250 at a time."""
        seen_ids: set[Any] = set()
        for page in range(1, self.max_pages + 1):
            url = f"{self.base_url}/products.json?limit={PAGE_SIZE}&page={page}"
            payload = self.request_json("GET", url)
            products = (payload or {}).get("products") or []
            if not products:
                return
            fresh = 0
            for raw in products:
                identifier = raw.get("id")
                if identifier in seen_ids:
                    # A store that keeps answering with the same page would
                    # otherwise loop until max_pages, re-collecting as it went.
                    continue
                seen_ids.add(identifier)
                fresh += 1
                yield raw
            if fresh == 0:
                return

    def wanted(self, raw: Mapping[str, Any]) -> bool:
        """Is this product one of the brands being collected?

        Broadway is a general marketplace - most of it is apparel and event
        tickets - so without a filter the sweep would store thousands of rows
        that have nothing to do with the beauty catalogue.
        """
        if not self.brand_filter:
            return True
        return brand_key(raw.get("vendor")) in self.brand_filter

    # -- barcode -----------------------------------------------------------
    def fetch_barcode(self, handle: str) -> str:
        """Ask the product endpoint for a barcode the listing did not carry.

        ``/products/<handle>.js`` is a few kilobytes against the megabyte the
        rendered page costs, and unlike ``products.json`` it does report the
        variant barcode.
        """
        if not handle:
            return ""
        try:
            payload = self.request_json(
                "GET", f"{self.base_url}/products/{handle}.js"
            )
        except RequestFailed as exc:
            self.logger.warning(
                "broadway_barcode_failed handle=%s error=%s", handle, exc
            )
            return ""
        for variant in (payload or {}).get("variants") or []:
            barcode = normalize_gtin(variant.get("barcode"))
            if barcode:
                return barcode
        return ""

    # -- normalization -----------------------------------------------------
    def to_products(
        self,
        raw: Mapping[str, Any],
        *,
        resolve_barcode: bool = True,
    ) -> list[Product]:
        """Turn one catalogue entry into a Product for each of its variants."""
        handle = _text(raw.get("handle"))
        title = _text(raw.get("title"))
        vendor = _text(raw.get("vendor"))
        if not handle or not title:
            return []
        images = [
            _text(image.get("src"))
            for image in raw.get("images") or []
            if _text(image.get("src"))
        ]
        description_html = _text(raw.get("body_html"))
        description = html_to_text(description_html)
        product_type = _text(raw.get("product_type"))
        categories = [product_type] if product_type else []
        observed = datetime.now(timezone.utc).isoformat(timespec="seconds")
        url = f"{self.base_url}/products/{handle}"

        # Asked for at most once per product, never once per variant.
        fallback_barcode: str | None = None
        products: list[Product] = []
        for variant in raw.get("variants") or []:
            sku = _text(variant.get("sku"))
            gtin = gtin_from_sku(sku)
            if not gtin and resolve_barcode:
                if fallback_barcode is None:
                    fallback_barcode = self.fetch_barcode(handle)
                gtin = fallback_barcode
            selling = _price(variant.get("price"))
            mrp = _price(variant.get("compare_at_price"))
            if mrp is not None and selling is not None and mrp < selling:
                # Some products carry a stale compare_at_price below the
                # asking price; a negative discount is worse than none.
                mrp = None
            discount = None
            if mrp and selling is not None and mrp > 0:
                discount = round((mrp - selling) / mrp * 100, 2)
            variant_title = _text(variant.get("title"))
            products.append(
                Product(
                    site=SITE,
                    product_id=_text(variant.get("id")),
                    parent_product_id=_text(raw.get("id")),
                    sku=sku,
                    gtin=gtin,
                    brand=vendor,
                    product_name=title,
                    categories=list(categories),
                    source_categories=list(categories),
                    variant=(
                        "" if variant_title == "Default Title" else variant_title
                    ),
                    mrp=mrp,
                    selling_price=selling,
                    discount_pct=discount,
                    in_stock=bool(variant.get("available")),
                    product_url=url,
                    image_url=images[0] if images else "",
                    image_urls=list(images),
                    description=description,
                    description_html=description_html,
                    scraped_at=observed,
                )
            )
        return products

    # -- the sweep ---------------------------------------------------------
    def collect(
        self,
        *,
        on_product: Callable[[Product], None] | None = None,
        max_products: int = 0,
    ) -> list[Product]:
        """Collect every product belonging to the configured brands."""
        collected: list[Product] = []
        for raw in self.iter_catalogue():
            # Checked before the work as well as after it. The limit used to be
            # tested only at the end of the body, where a product that raised
            # jumped straight past it, and a sample run then collected the
            # whole catalogue.
            if max_products and len(collected) >= max_products:
                break
            if not self.wanted(raw):
                continue
            try:
                parsed = self.to_products(raw)
            except Exception as exc:  # noqa: BLE001 - one product cannot stop a sweep
                self.product_failures += 1
                self.logger.error(
                    "broadway_product_failed handle=%s error=%s",
                    raw.get("handle"),
                    exc,
                )
                continue
            # Deliberately outside the guard above: a sink or reporter that
            # raises is a fault in this program, not in the retailer's data,
            # and counting it as a product failure hid exactly that.
            for product in parsed:
                collected.append(product)
                if on_product is not None:
                    on_product(product)
            if max_products and len(collected) >= max_products:
                break
        self.logger.info(
            "broadway_collected products=%s failures=%s requests=%s",
            len(collected),
            self.product_failures,
            self.requests_made,
        )
        return collected
