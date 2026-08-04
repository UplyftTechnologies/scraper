"""Nykaa category-listing JSON API client."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import unquote_plus, urlencode, urljoin, urlparse, urlunparse

from pricing_scraper.models import Product, brand_key, normalize_gtin

from .base import (
    BaseJsonClient,
    ConfigurationError,
    RequestFailed,
    RequestSpec,
    parse_curl_command,
)


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (Mapping, list, tuple, set)):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", _text(value))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _nested(record: Mapping[str, Any], path: str) -> Any:
    value: Any = record
    for part in path.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


def _first(record: Mapping[str, Any], paths: Sequence[str]) -> Any:
    for path in paths:
        value = _nested(record, path)
        if value is not None and value != "":
            return value
    return None


def _set_query_fields(url: str, fields: Mapping[str, Any]) -> str:
    parsed = urlparse(url)
    replacements = {key: str(value) for key, value in fields.items() if key}
    existing_parts = []
    for part in parsed.query.split("&"):
        if not part:
            continue
        raw_key = part.split("=", 1)[0]
        if unquote_plus(raw_key) not in replacements:
            existing_parts.append(part)
    replacement_query = urlencode(replacements)
    if replacement_query:
        existing_parts.append(replacement_query)
    return urlunparse(parsed._replace(query="&".join(existing_parts)))


class _ProductContentParser(HTMLParser):
    """Extract readable text and image sources from Nykaa content HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.image_urls: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        lowered = tag.casefold()
        if lowered in {"style", "script"}:
            self._ignored_depth += 1
            return
        if lowered == "img":
            source = dict(attrs).get("src")
            if source:
                self.image_urls.append(source)
        if lowered in {"br", "p", "div", "li", "h1", "h2", "h3", "h4"}:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered in {"style", "script"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif lowered in {"p", "div", "li", "h1", "h2", "h3", "h4"}:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.text_parts.append(data)


def _html_content(value: Any) -> tuple[str, list[str]]:
    html = str(value or "")
    if not html:
        return "", []
    parser = _ProductContentParser()
    try:
        parser.feed(html)
    except Exception:
        return _text(unescape(re.sub(r"<[^>]+>", " ", html))), []
    lines = [
        _text(line)
        for line in "".join(parser.text_parts).splitlines()
        if _text(line)
    ]
    return "\n".join(lines), parser.image_urls


_KEY_INGREDIENT_HEADING = re.compile(r"(?i)key\s+ingredients?\s*:?")
_SECTION_END = re.compile(
    r"(?i)(full\s+ingredient|ingredient\s+list|other\s+ingredients|"
    r"all\s+ingredients)"
)


def _key_ingredients(html: Any) -> list[str]:
    """Pull the named key ingredients out of Nykaa's ingredients HTML.

    Nykaa writes this section two ways: a bulleted list whose bold label names
    the ingredient, or one paragraph of ``Name:description.`` pairs. Products
    that publish only the full INCI list return nothing.
    """
    markup = str(html or "")
    heading = _KEY_INGREDIENT_HEADING.search(markup)
    if not heading:
        return []
    body = markup[heading.end():]
    end = _SECTION_END.search(body)
    if end:
        body = body[: end.start()]

    names: list[str] = []
    items = re.findall(r"(?is)<li\b[^>]*>(.*?)</li>", body)
    for item in items:
        label = re.search(r"(?is)<b\b[^>]*>(.*?)</b>", item)
        # Without a bold label the whole bullet is the name up to its colon.
        text = _text(unescape(re.sub(r"<[^>]+>", " ", label.group(1) if label else item)))
        names.append(text.split(":")[0])
    if not items:
        # Inline form: "Niacinamide:Brightens skin.Panthenol:Moisturizes."
        plain = _text(unescape(re.sub(r"<[^>]+>", " ", body)))
        for segment in plain.split(":")[:-1]:
            # The name is the tail of the previous description sentence.
            candidate = re.split(r"(?<=[.!])\s*", segment)[-1]
            names.append(candidate)

    unique: dict[str, str] = {}
    for name in names:
        cleaned = _text(name).strip(" .,;-–—&")
        if 1 < len(cleaned) <= 80:
            unique.setdefault(cleaned.casefold(), cleaned)
    return list(unique.values())


@dataclass(frozen=True, slots=True)
class CategoryScrapeResult:
    """Outcome of a resumable category pagination run."""

    products: list[Product]
    next_page: int
    completed: bool
    pages_scraped: int
    stop_reason: str


class NykaaClient(BaseJsonClient):
    """Scrape configured Nykaa categories through a captured frontend JSON API."""

    def __init__(
        self,
        site_config: Mapping[str, Any],
        request_config: Mapping[str, Any],
        brands: Iterable[str] = (),
        **base_kwargs: Any,
    ):
        self.site_config = dict(site_config)
        curl_command = str(site_config.get("curl_command") or "")
        curl_file = _text(site_config.get("curl_file"))
        if curl_file:
            try:
                curl_command = Path(curl_file).expanduser().read_text(
                    encoding="utf-8"
                )
            except OSError as exc:
                raise ConfigurationError(
                    f"Could not read nykaa.curl_file {curl_file!r}: {exc}"
                ) from exc
        self.request_spec: RequestSpec = parse_curl_command(curl_command)
        super().__init__(
            request_config=request_config,
            headers=self.request_spec.headers,
            **base_kwargs,
        )
        self.site_base_url = _text(
            site_config.get("site_base_url") or "https://www.nykaa.com"
        )
        self.detail_url = _text(
            site_config.get("detail_url")
            or "https://www.nykaa.com/app-api/index.php/products/details"
        )
        detail_settings = site_config.get("details")
        detail_settings = (
            detail_settings
            if isinstance(detail_settings, Mapping)
            else {}
        )
        self.include_top_reviews = bool(
            detail_settings.get("include_top_reviews", True)
        )
        self.category_url_template = _text(
            site_config.get("category_url_template")
        )
        self.category_field = _text(
            site_config.get("category_field") or "category_id"
        )
        self.category_location = _text(
            site_config.get("category_location") or "query"
        ).casefold()
        self.page_field = _text(site_config.get("page_field") or "page_no")
        self.page_location = _text(
            site_config.get("page_location") or "query"
        ).casefold()
        self.start_page = int(site_config.get("start_page", 1))
        self.page_limit = max(1, int(site_config.get("page_limit", 50)))
        self.max_consecutive_page_failures = max(
            1, int(site_config.get("max_consecutive_page_failures", 3))
        )
        self.products_paths = [
            _text(path)
            for path in site_config.get("products_paths", ())
            if _text(path)
        ]
        self.categories = [
            dict(category)
            for category in site_config.get("categories", ())
            if isinstance(category, Mapping)
        ]
        self.brand_filter = {
            brand_key(brand) for brand in brands if brand_key(brand)
        }
        self.page_failures = 0
        self.product_failures = 0
        self.detail_failures = 0
        self.products_seen = 0

        valid_locations = {"query", "json_body"}
        if self.category_location not in valid_locations:
            raise ConfigurationError(
                "nykaa.category_location must be query or json_body."
            )
        if self.page_location not in valid_locations:
            raise ConfigurationError(
                "nykaa.page_location must be query or json_body."
            )

    def configured_categories(self) -> list[dict[str, Any]]:
        """Return validated category IDs and human-readable names."""
        result: list[dict[str, Any]] = []
        for category in self.categories:
            if category.get("enabled", True) is False:
                continue
            category_id = _text(category.get("id"))
            name = _text(category.get("name"))
            if category_id and name:
                result.append(
                    {
                        "id": category_id,
                        "name": name,
                        "covers_all": bool(category.get("covers_all", False)),
                        "partitions": (
                            list(category.get("partitions", ()))
                            if isinstance(category.get("partitions"), list)
                            else []
                        ),
                    }
                )
            else:
                self.logger.warning("invalid_category skipped=%r", category)
        return result

    def select_categories(
        self, names: Iterable[str] | None = None
    ) -> list[dict[str, Any]]:
        """Resolve configured category names case-insensitively."""
        configured = self.configured_categories()
        requested = {_text(name).casefold() for name in names or () if _text(name)}
        if not requested:
            covering = [item for item in configured if item["covers_all"]]
            return covering or configured
        selected = [
            category
            for category in configured
            if category["name"].casefold() in requested
        ]
        missing = requested - {item["name"].casefold() for item in selected}
        if missing:
            available = ", ".join(item["name"] for item in configured)
            raise ConfigurationError(
                f"Unknown Nykaa categories: {', '.join(sorted(missing))}. "
                f"Configured categories: {available or 'none'}."
            )
        return selected

    def _request_for_page(
        self, category: Mapping[str, str], page: int
    ) -> tuple[str, str | None]:
        category_id = category["id"]
        url = self.request_spec.url.replace("{category_id}", category_id)
        if self.category_url_template:
            url = self.category_url_template.format(
                category_id=category_id,
                category_name=category["name"],
            )

        query_fields: dict[str, Any] = {}
        body_fields: dict[str, Any] = {}
        extra_query = category.get("query")
        if isinstance(extra_query, Mapping):
            query_fields.update(
                {
                    _text(key): value
                    for key, value in extra_query.items()
                    if _text(key)
                }
            )
        if self.category_location == "query":
            query_fields[self.category_field] = category_id
        else:
            body_fields[self.category_field] = category_id
        if self.page_location == "query":
            query_fields[self.page_field] = page
        else:
            body_fields[self.page_field] = page
        if query_fields:
            url = _set_query_fields(url, query_fields)

        body = self.request_spec.body
        if body_fields:
            if body:
                try:
                    parsed_body = json.loads(body)
                except json.JSONDecodeError as exc:
                    raise ConfigurationError(
                        "Nykaa pagination/category is configured for json_body, "
                        "but the cURL body is not valid JSON."
                    ) from exc
                if not isinstance(parsed_body, Mapping):
                    raise ConfigurationError(
                        "The Nykaa cURL JSON body must be an object."
                    )
                mutable_body = copy.deepcopy(dict(parsed_body))
            else:
                mutable_body = {}
            mutable_body.update(body_fields)
            body = json.dumps(mutable_body, separators=(",", ":"), ensure_ascii=False)
        return url, body

    def _records_at_path(
        self, payload: Any, path: str
    ) -> list[Mapping[str, Any]] | None:
        value = payload
        for part in path.split("."):
            if not isinstance(value, Mapping):
                return None
            if part not in value:
                return None
            value = value[part]
        if not isinstance(value, list):
            return None
        return [item for item in value if isinstance(item, Mapping)]

    def _find_records_recursively(self, payload: Any) -> list[Mapping[str, Any]]:
        identity_keys = {
            "id",
            "productId",
            "product_id",
            "sku",
            "skuId",
            "name",
            "productName",
            "title",
        }
        queue = [payload]
        while queue:
            value = queue.pop(0)
            if isinstance(value, list):
                mappings = [item for item in value if isinstance(item, Mapping)]
                if mappings and any(identity_keys.intersection(item) for item in mappings):
                    return mappings
                queue.extend(value)
            elif isinstance(value, Mapping):
                queue.extend(value.values())
        return []

    def _product_records(self, payload: Any) -> list[Mapping[str, Any]]:
        for path in self.products_paths:
            records = self._records_at_path(payload, path)
            if records is not None:
                return records
        return self._find_records_recursively(payload)

    @staticmethod
    def _expand_variants(
        record: Mapping[str, Any],
    ) -> list[Mapping[str, Any]]:
        for key in ("variants", "skus", "children", "options"):
            children = record.get(key)
            if isinstance(children, list):
                mappings = [child for child in children if isinstance(child, Mapping)]
                if mappings:
                    expanded: list[Mapping[str, Any]] = []
                    for child in mappings:
                        merged = dict(record)
                        merged.pop(key, None)
                        merged.update(child)
                        expanded.append(merged)
                    return expanded
        return [record]

    @staticmethod
    def _brand(record: Mapping[str, Any]) -> str:
        value = _first(
            record,
            (
                "brandName",
                "brand_name",
                "brand.name",
                "brand.title",
                "brand",
                "manufacturer",
            ),
        )
        if isinstance(value, Mapping):
            value = value.get("name") or value.get("title")
        return _text(value)

    @staticmethod
    def _stock(record: Mapping[str, Any]) -> bool | None:
        positive = _first(
            record,
            (
                "inStock",
                "in_stock",
                "isAvailable",
                "available",
                "gludo_stock",
                "is_saleable",
            ),
        )
        if isinstance(positive, bool):
            return positive
        negative = _first(record, ("isOutOfStock", "outOfStock", "soldOut"))
        if isinstance(negative, bool):
            return not negative
        quantity = _number(
            _first(record, ("inventory", "inventoryCount", "stock", "quantity"))
        )
        if quantity is not None:
            return quantity > 0
        status = _text(
            _first(record, ("availability", "inventoryStatus", "stockStatus"))
        ).casefold()
        if status:
            if any(value in status for value in ("in stock", "available")):
                return True
            if any(value in status for value in ("out of stock", "unavailable")):
                return False
        return None

    def _absolute_url(self, value: Any) -> str:
        text = _text(value)
        if not text:
            return ""
        return urljoin(f"{self.site_base_url.rstrip('/')}/", text)

    def _image_url(self, record: Mapping[str, Any]) -> str:
        value = _first(
            record,
            (
                "imageUrl",
                "image_url",
                "primaryImage",
                "image",
                "images",
                "media.images",
            ),
        )
        if isinstance(value, list):
            value = value[0] if value else ""
        if isinstance(value, Mapping):
            value = (
                value.get("url")
                or value.get("imageUrl")
                or value.get("src")
                or value.get("original")
            )
        return self._absolute_url(value)

    def _media_urls(self, *values: Any) -> list[str]:
        """Collect unique HTTP image URLs from common Nykaa media shapes."""
        urls: list[str] = []
        seen: set[str] = set()

        def visit(value: Any) -> None:
            if isinstance(value, str):
                text = value.strip()
                if not text.startswith(("http://", "https://", "//", "/")):
                    return
                url = self._absolute_url(text)
                if url and url not in seen:
                    seen.add(url)
                    urls.append(url)
                return
            if isinstance(value, Mapping):
                direct_keys = (
                    "url",
                    "imageUrl",
                    "image_url",
                    "src",
                    "original",
                )
                found_direct = False
                for key in direct_keys:
                    if key in value:
                        found_direct = True
                        visit(value.get(key))
                if not found_direct:
                    for nested in value.values():
                        visit(nested)
                return
            if isinstance(value, (list, tuple)):
                for nested in value:
                    visit(nested)

        for item in values:
            visit(item)
        return urls

    @staticmethod
    def _normalized_reviews(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        reviews: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, Mapping):
                continue
            meta = item.get("meta_data")
            meta = meta if isinstance(meta, Mapping) else {}
            reviews.append(
                {
                    "review_id": _text(
                        item.get("review_id") or item.get("id")
                    ),
                    "title": _text(item.get("title")),
                    "review": _text(
                        item.get("detail") or item.get("review")
                    ),
                    "rating": _number(
                        item.get("rating") or meta.get("value")
                    ),
                    "reviewer": _text(
                        item.get("nickname") or item.get("name")
                    ),
                    "verified_buyer": bool(
                        item.get("is_buyer")
                        or _text(item.get("label")).casefold()
                        == "verified buyer"
                    ),
                    "created_at": _text(
                        item.get("created_at")
                        or meta.get("createdAtText")
                    ),
                    "likes": _integer(item.get("likes")),
                    "images": [
                        _text(url)
                        for url in item.get("images", ())
                        if _text(url)
                    ]
                    if isinstance(item.get("images"), list)
                    else [],
                }
            )
        return reviews

    @staticmethod
    def _rating_breakdown(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        breakdown: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, Mapping):
                continue
            breakdown.append(
                {
                    "stars": _integer(
                        item.get("id") or item.get("rating")
                    ),
                    "count": _integer(item.get("count")),
                    "percentage": _number(
                        item.get("per") or item.get("percentage")
                    ),
                }
            )
        return breakdown

    @staticmethod
    def _gtin(record: Mapping[str, Any]) -> str:
        """Read the EAN/UPC barcode Nykaa publishes for a product or option."""
        return normalize_gtin(
            _first(record, ("gtin", "ean", "upc", "barcode"))
        )

    def _parse_detail_response(
        self,
        response: Mapping[str, Any],
        fallback: Product,
    ) -> list[Product]:
        """Normalize every SKU option from a Nykaa product-detail response."""
        description_html = str(response.get("description") or "")
        description, description_images = _html_content(description_html)
        ingredients, ingredient_images = _html_content(
            response.get("ingredients")
        )
        key_ingredients = _key_ingredients(response.get("ingredients"))
        how_to_use, usage_images = _html_content(response.get("use"))
        content_images = self._media_urls(
            description_images,
            ingredient_images,
            usage_images,
        )
        global_images = self._media_urls(
            response.get("all_images"),
            response.get("carousel"),
            response.get("parentMedia"),
            response.get("parentCarousel"),
        )
        top_reviews = (
            self._normalized_reviews(
                response.get("top_review")
                or response.get("latestReviews")
            )
            if self.include_top_reviews
            else []
        )
        rating_breakdown = self._rating_breakdown(
            response.get("review_splitup")
            or response.get("reviewSplitUp")
        )

        options = response.get("options")
        variants = (
            [item for item in options if isinstance(item, Mapping)]
            if isinstance(options, list)
            else []
        )
        if not variants:
            variants = [response]

        parent_id = _text(
            response.get("parent_id")
            or response.get("parentId")
            or response.get("id")
            or fallback.parent_product_id
            or fallback.product_id
        )
        brand = _text(
            response.get("brand_name")
            or response.get("brandName")
            or fallback.brand
        )
        scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        products: list[Product] = []

        for option in variants:
            product_id = _text(
                option.get("id")
                or option.get("product_id")
                or fallback.product_id
            )
            if not product_id:
                continue
            name = _text(
                option.get("name")
                or option.get("product_title")
                or response.get("name")
                or fallback.product_name
            )
            if not name:
                continue
            image_urls = self._media_urls(
                option.get("carousel"),
                option.get("all_images"),
                global_images,
                content_images,
            )
            product_url = self._absolute_url(
                option.get("share_url")
                or option.get("product_url")
                or option.get("slug")
                or response.get("share_url")
                or response.get("slug")
                or fallback.product_url
            )
            mrp = _number(
                option.get("price")
                or option.get("mrp")
                or response.get("price")
                or response.get("mrp")
            )
            selling_price = _number(
                option.get("final_price")
                or option.get("offerPrice")
                or response.get("final_price")
                or response.get("offerPrice")
            )
            discount = _number(
                option.get("discount") or response.get("discount")
            )
            if (
                discount is None
                and mrp is not None
                and selling_price is not None
                and mrp > 0
                and selling_price <= mrp
            ):
                discount = round(
                    ((mrp - selling_price) / mrp) * 100,
                    2,
                )
            in_stock = self._stock(option)
            if in_stock is None:
                in_stock = self._stock(response)
            # Each size carries its own barcode, so a response-level GTIN is
            # only reused for the SKU it actually describes.
            gtin = self._gtin(option)
            if not gtin and product_id == _text(response.get("id")):
                gtin = self._gtin(response)
            if not gtin and product_id == fallback.product_id:
                gtin = fallback.gtin

            products.append(
                Product(
                    site="nykaa",
                    product_id=product_id,
                    parent_product_id=_text(
                        option.get("parent_id")
                        or option.get("parentId")
                        or parent_id
                    ),
                    sku=_text(option.get("sku") or response.get("sku")),
                    gtin=gtin,
                    brand=brand,
                    product_name=name,
                    categories=list(fallback.categories),
                    source_categories=list(fallback.source_categories),
                    variant=_text(
                        option.get("pack_size")
                        or option.get("variant")
                        or option.get("option_text")
                        or response.get("selectedVariantName")
                        or fallback.variant
                    ),
                    mrp=mrp,
                    selling_price=selling_price,
                    discount_pct=discount,
                    rating=_number(
                        option.get("rating")
                        or response.get("rating")
                        or fallback.rating
                    ),
                    rating_count=_integer(
                        option.get("rating_count")
                        or response.get("rating_count")
                        or fallback.rating_count
                    ),
                    review_count=_integer(
                        option.get("review_count")
                        or response.get("review_count")
                    ),
                    in_stock=in_stock,
                    product_url=product_url,
                    image_url=(
                        image_urls[0]
                        if image_urls
                        else fallback.image_url
                    ),
                    image_urls=image_urls,
                    description=description,
                    description_html=description_html,
                    ingredients=ingredients,
                    key_ingredients=list(key_ingredients),
                    how_to_use=how_to_use,
                    key_features=list(fallback.key_features),
                    special_features=list(fallback.special_features),
                    product_attributes=dict(fallback.product_attributes),
                    rating_breakdown=rating_breakdown,
                    top_reviews=top_reviews,
                    scraped_at=scraped_at,
                )
            )
        return products

    def fetch_product_details(self, product: Product) -> list[Product]:
        """Fetch and normalize the public JSON details for one product parent."""
        parent_id = product.parent_product_id
        if not parent_id and product.product_url:
            match = re.search(r"/p/(\d+)", product.product_url)
            parent_id = match.group(1) if match else ""
        parent_id = parent_id or product.product_id
        url = _set_query_fields(
            self.detail_url,
            {
                "product_id": parent_id,
                "sku_id": product.product_id,
                "client": "react",
                "platform": "website",
            },
        )
        payload = self.request_json("GET", url)
        response = (
            payload.get("response")
            if isinstance(payload, Mapping)
            else None
        )
        if not isinstance(response, Mapping):
            raise ValueError(
                f"Nykaa detail response for {parent_id} has no object payload."
            )
        products = self._parse_detail_response(response, product)
        if not products:
            raise ValueError(
                f"Nykaa detail response for {parent_id} contains no SKUs."
            )
        return products

    def _parse_product(
        self,
        record: Mapping[str, Any],
        scraped_at: str,
        category: Mapping[str, Any] | None = None,
    ) -> Product | None:
        object_type = _text(record.get("object_type")).casefold()
        if object_type and object_type not in {"product", "sku"}:
            return None
        product_id = _text(
            _first(
                record,
                (
                    "skuId",
                    "sku_id",
                    "productId",
                    "product_id",
                    "id",
                    "sku",
                ),
            )
        )
        name = _text(
            _first(record, ("productName", "product_name", "name", "title"))
        )
        if not product_id or not name:
            raise ValueError("record has no product ID or product name")

        brand = self._brand(record)
        if self.brand_filter and brand_key(brand) not in self.brand_filter:
            return None

        mrp = _number(
            _first(
                record,
                (
                    "mrp",
                    "maxRetailPrice",
                    "originalPrice",
                    "marketPrice",
                    "priceInfo.mrp",
                    "pricing.mrp",
                    "price.mrp",
                    "price",
                ),
            )
        )
        selling_price = _number(
            _first(
                record,
                (
                    "sellingPrice",
                    "selling_price",
                    "discountedPrice",
                    "offerPrice",
                    "final_price",
                    "finalPrice",
                    "priceInfo.sellingPrice",
                    "pricing.sellingPrice",
                    "price.current",
                    "price",
                ),
            )
        )
        discount = _number(
            _first(
                record,
                (
                    "discountPercentage",
                    "discountPercent",
                    "discount_pct",
                    "discount",
                    "bucket_discount_percent",
                    "priceInfo.discount",
                ),
            )
        )
        if (
            discount is None
            and mrp is not None
            and selling_price is not None
            and mrp > 0
            and selling_price <= mrp
        ):
            discount = round(((mrp - selling_price) / mrp) * 100, 2)

        rating = _number(
            _first(
                record,
                (
                    "rating",
                    "averageRating",
                    "avgRating",
                    "ratings.average",
                    "ratingInfo.average",
                ),
            )
        )
        rating_count = _integer(
            _first(
                record,
                (
                    "ratingCount",
                    "rating_count",
                    "ratingsCount",
                    "reviewCount",
                    "ratings.count",
                    "ratingInfo.count",
                ),
            )
        )
        product_url = self._absolute_url(
            _first(
                record,
                (
                    "productUrl",
                    "product_url",
                    "dynamicUrl",
                    "url",
                    "slug",
                ),
            )
        )
        variant = _text(
            _first(
                record,
                (
                    "variant",
                    "variantName",
                    "size",
                    "packSize",
                    "volume",
                    "shadeName",
                    "option_text",
                ),
            )
        )
        image_urls = self._media_urls(
            record.get("media"),
            record.get("images"),
            record.get("plp_pdp_bridge"),
            record.get("imageUrl"),
            record.get("image_url"),
            record.get("new_image_url"),
        )
        return Product(
            site="nykaa",
            product_id=product_id,
            parent_product_id=_text(
                _first(
                    record,
                    (
                        "parentId",
                        "parent_id",
                        "parent_id_to_open",
                        "configurable_id",
                    ),
                )
                or product_id
            ),
            sku=_text(
                _first(
                    record,
                    ("sku", "vendor_sku", "psku"),
                )
            ),
            gtin=self._gtin(record),
            brand=brand,
            product_name=name,
            categories=(
                [_text((category or {}).get("name"))]
                if _text((category or {}).get("name"))
                else []
            ),
            source_categories=[
                value
                for value in (
                    _text(record.get("category_name")),
                    _text(record.get("category_values")),
                )
                if value
            ],
            variant=variant,
            mrp=mrp,
            selling_price=selling_price,
            discount_pct=discount,
            rating=rating,
            rating_count=rating_count,
            review_count=_integer(
                _first(
                    record,
                    ("reviewCount", "review_count"),
                )
            ),
            in_stock=self._stock(record),
            product_url=product_url,
            image_url=(
                image_urls[0]
                if image_urls
                else self._image_url(record)
            ),
            image_urls=image_urls,
            scraped_at=scraped_at,
        )

    def _normalize_page_records(
        self,
        category: Mapping[str, Any],
        page: int,
        records: Sequence[Mapping[str, Any]],
        seen_ids: set[str],
    ) -> list[Product]:
        """Normalize one API page while isolating malformed product records."""
        scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        products: list[Product] = []
        for parent in records:
            self.products_seen += 1
            for record in self._expand_variants(parent):
                try:
                    product = self._parse_product(
                        record,
                        scraped_at,
                        category,
                    )
                except Exception as exc:
                    self.product_failures += 1
                    self.logger.exception(
                        "nykaa_product category=%s page=%s "
                        "parse=failure error=%s record=%r",
                        category.get("name", ""),
                        page,
                        exc,
                        dict(record),
                    )
                    continue
                if product is None or product.product_id in seen_ids:
                    continue
                seen_ids.add(product.product_id)
                products.append(product)
        return products

    def scrape_category(
        self,
        category: Mapping[str, str],
        *,
        page_limit: int | None = None,
    ) -> list[Product]:
        """Paginate one configured category and normalize valid products."""
        limit = max(1, min(page_limit or self.page_limit, self.page_limit))
        products: list[Product] = []
        seen_ids: set[str] = set()
        repeated_pages = 0
        consecutive_page_failures = 0

        for page in range(self.start_page, self.start_page + limit):
            url, body = self._request_for_page(category, page)
            try:
                payload = self.request_json(
                    self.request_spec.method,
                    url,
                    data=body,
                )
            except RequestFailed as exc:
                self.page_failures += 1
                self.logger.error(
                    "nykaa_page category=%s page=%s parse=failure error=%s",
                    category.get("name", ""),
                    page,
                    exc,
                )
                consecutive_page_failures += 1
                if (
                    consecutive_page_failures
                    >= self.max_consecutive_page_failures
                ):
                    self.logger.error(
                        "nykaa_pagination_stopped category=%s "
                        "reason=consecutive_page_failures count=%s",
                        category.get("name", ""),
                        consecutive_page_failures,
                    )
                    break
                continue

            consecutive_page_failures = 0
            records = self._product_records(payload)
            response_metadata = (
                payload.get("response")
                if isinstance(payload, Mapping)
                and isinstance(payload.get("response"), Mapping)
                else {}
            )
            self.logger.info(
                "nykaa_page category=%s page=%s records=%s parse=success",
                category.get("name", ""),
                page,
                len(records),
            )
            if not records:
                break

            page_products = self._normalize_page_records(
                category,
                page,
                records,
                seen_ids,
            )
            page_new_ids = len(page_products)
            products.extend(page_products)

            if page_new_ids == 0:
                repeated_pages += 1
            else:
                repeated_pages = 0
            if repeated_pages >= 2:
                self.logger.warning(
                    "nykaa_pagination_stopped category=%s reason=repeated_pages",
                    category.get("name", ""),
                )
                break

        return products

    def scrape_category_resumable(
        self,
        category: Mapping[str, Any],
        *,
        start_page: int,
        seen_product_ids: Iterable[str] = (),
        on_page: Callable[[int, Sequence[Product]], None] | None = None,
    ) -> CategoryScrapeResult:
        """Scrape from a checkpoint and report whether pagination truly ended.

        Completion is reported only from an empty page or Nykaa's total/offset
        metadata. Three consecutive pages with identical record IDs are
        treated as an incomplete, capped result window so the caller can split
        the catalogue into smaller partitions rather than silently lose rows.
        """
        first_page = max(self.start_page, int(start_page))
        last_page = self.start_page + self.page_limit - 1
        seen_ids = {str(item) for item in seen_product_ids if str(item)}
        products: list[Product] = []
        identical_page_repeats = 0
        previous_record_ids: tuple[str, ...] | None = None
        pages_scraped = 0

        if first_page > last_page:
            return CategoryScrapeResult(
                products=[],
                next_page=first_page,
                completed=False,
                pages_scraped=0,
                stop_reason="page_limit",
            )

        for page in range(first_page, last_page + 1):
            url, body = self._request_for_page(category, page)
            try:
                payload = self.request_json(
                    self.request_spec.method,
                    url,
                    data=body,
                )
            except RequestFailed as exc:
                self.page_failures += 1
                self.logger.error(
                    "nykaa_page category=%s page=%s parse=failure "
                    "checkpoint=pending error=%s",
                    category.get("name", ""),
                    page,
                    exc,
                )
                return CategoryScrapeResult(
                    products=products,
                    next_page=page,
                    completed=False,
                    pages_scraped=pages_scraped,
                    stop_reason="request_failed",
                )

            records = self._product_records(payload)
            response_metadata = (
                payload.get("response")
                if isinstance(payload, Mapping)
                and isinstance(payload.get("response"), Mapping)
                else {}
            )
            self.logger.info(
                "nykaa_page category=%s page=%s records=%s parse=success",
                category.get("name", ""),
                page,
                len(records),
            )
            if not records:
                return CategoryScrapeResult(
                    products=products,
                    next_page=page,
                    completed=True,
                    pages_scraped=pages_scraped,
                    stop_reason="empty_page",
                )

            record_ids = tuple(
                _text(
                    _first(
                        record,
                        (
                            "skuId",
                            "sku_id",
                            "productId",
                            "product_id",
                            "id",
                            "sku",
                        ),
                    )
                )
                for record in records
            )
            if record_ids == previous_record_ids:
                identical_page_repeats += 1
            else:
                identical_page_repeats = 0
            previous_record_ids = record_ids

            page_products = self._normalize_page_records(
                category,
                page,
                records,
                seen_ids,
            )
            products.extend(page_products)
            pages_scraped += 1
            if on_page is not None:
                on_page(page, page_products)

            total_found = _integer(response_metadata.get("total_found"))
            offset = _integer(response_metadata.get("offset"))
            page_record_count = _integer(
                response_metadata.get("product_count")
            )
            if page_record_count is None:
                page_record_count = len(records)
            reached_reported_end = bool(
                response_metadata.get("stop_further_call")
            ) or (
                total_found is not None
                and offset is not None
                and offset + page_record_count >= total_found
            )
            if reached_reported_end:
                return CategoryScrapeResult(
                    products=products,
                    next_page=page + 1,
                    completed=True,
                    pages_scraped=pages_scraped,
                    stop_reason="end_of_results",
                )

            if identical_page_repeats >= 2:
                self.logger.warning(
                    "nykaa_pagination_stopped category=%s "
                    "reason=repeated_pages checkpoint_page=%s",
                    category.get("name", ""),
                    page + 1,
                )
                return CategoryScrapeResult(
                    products=products,
                    next_page=page + 1,
                    completed=False,
                    pages_scraped=pages_scraped,
                    stop_reason="repeated_pages",
                )

        return CategoryScrapeResult(
            products=products,
            next_page=last_page + 1,
            completed=False,
            pages_scraped=pages_scraped,
            stop_reason="page_limit",
        )

    def scrape(
        self, categories: Iterable[Mapping[str, str]]
    ) -> list[Product]:
        """Scrape multiple categories without letting one failure abort the run."""
        combined: list[Product] = []
        seen: set[str] = set()
        for category in categories:
            try:
                products = self.scrape_category(category)
            except Exception as exc:
                self.page_failures += 1
                self.logger.exception(
                    "nykaa_category category=%s failed=%s",
                    category.get("name", ""),
                    exc,
                )
                continue
            for product in products:
                if product.product_id not in seen:
                    seen.add(product.product_id)
                    combined.append(product)
        return combined
