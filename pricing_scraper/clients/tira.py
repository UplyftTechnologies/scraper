"""Tira storefront JSON API client."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import quote, urlencode, urljoin, urlparse

from pricing_scraper.models import Product

from .base import (
    BaseJsonClient,
    ConfigurationError,
    RequestFailed,
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


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _first_text(values: Iterable[Any]) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return ""


def _text_list(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    return list(
        dict.fromkeys(
            text
            for item in values
            if (text := _text(item))
        )
    )


def _positive_max(values: Iterable[Any]) -> float | None:
    numbers = [
        number
        for value in values
        if (number := _number(value)) is not None and number > 0
    ]
    return max(numbers) if numbers else None


def _meta_map(value: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in _list(value):
        if not isinstance(item, Mapping):
            continue
        key = _text(item.get("key"))
        if key:
            result[key.casefold()] = item.get("value")
    return result


class _TiraContentParser(HTMLParser):
    """Extract text, images, ingredients, and usage from Tira content HTML."""

    BLOCK_TAGS = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6"}
    HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._tag = ""
        self._parts: list[str] = []
        self.blocks: list[tuple[str, str]] = []
        self.images: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        lowered = tag.casefold()
        if lowered in self.BLOCK_TAGS:
            self._flush()
            self._tag = lowered
        if lowered == "br":
            self._parts.append("\n")
        if lowered == "img":
            attributes = dict(attrs)
            url = _text(
                attributes.get("src")
                or attributes.get("data-src")
                or attributes.get("data-original")
            )
            if url and url not in self.images:
                self.images.append(url)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in self.BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if data:
            self._parts.append(data)

    def close(self) -> None:
        super().close()
        self._flush()

    def _flush(self) -> None:
        text = _text(unescape(" ".join(self._parts)))
        if text:
            self.blocks.append((self._tag, text))
        self._parts = []
        self._tag = ""


def _content_fields(html_value: Any) -> tuple[str, str, str, list[str]]:
    html = str(html_value or "")
    if not html:
        return "", "", "", []
    parser = _TiraContentParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        plain = _text(unescape(re.sub(r"<[^>]+>", " ", html)))
        return plain, "", "", []

    all_text = "\n".join(text for _tag, text in parser.blocks)
    ingredient_parts: list[str] = []
    usage_parts: list[str] = []
    section = ""
    for tag, text in parser.blocks:
        folded = text.casefold().strip(" :")
        if tag in parser.HEADING_TAGS:
            if "ingredient" in folded:
                section = "ingredients"
            elif (
                "how to use" in folded
                or "directions for use" in folded
                or folded == "directions"
            ):
                section = "usage"
            else:
                section = ""
            continue
        if section == "ingredients":
            ingredient_parts.append(text)
        elif section == "usage":
            usage_parts.append(text)
    return (
        all_text,
        "\n".join(ingredient_parts),
        "\n".join(usage_parts),
        parser.images,
    )


@dataclass(frozen=True, slots=True)
class CategoryScrapeResult:
    """Outcome of one resumable Tira collection pagination run."""

    products: list[Product]
    next_page: int
    completed: bool
    pages_scraped: int
    stop_reason: str


class TiraClient(BaseJsonClient):
    """Collect Tira skincare data from its storefront JSON APIs."""

    def __init__(
        self,
        site_config: Mapping[str, Any],
        request_config: Mapping[str, Any],
        brands: Iterable[str] = (),
        **base_kwargs: Any,
    ) -> None:
        self.site_config = dict(site_config)
        headers = {
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.tirabeauty.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/150.0.0.0 Safari/537.36"
            ),
        }
        curl_command = str(site_config.get("curl_command") or "").strip()
        curl_file = _text(site_config.get("curl_file"))
        if curl_file:
            try:
                curl_command = Path(curl_file).expanduser().read_text(
                    encoding="utf-8"
                )
            except OSError as exc:
                raise ConfigurationError(
                    f"Could not read tira.curl_file {curl_file!r}: {exc}"
                ) from exc
        if curl_command and not curl_command.startswith("<PASTE "):
            headers = parse_curl_command(curl_command).headers

        super().__init__(request_config, headers, **base_kwargs)
        self.site_base_url = _text(
            site_config.get("site_base_url")
            or "https://www.tirabeauty.com"
        )
        self.listing_url_template = _text(
            site_config.get("listing_url_template")
            or (
                "https://fp-cdn.tirabeauty.com/ext/plpoffers/"
                "application/api/v1.0/collections/{collection}/items"
            )
        )
        self.detail_url_template = _text(
            site_config.get("detail_url_template")
            or (
                "https://api.tirabeauty.com/service/application/"
                "catalog/v1.0/products/{slug}/sizes/"
            )
        )
        application_id = _text(site_config.get("application_id"))
        application_token = _text(site_config.get("application_token"))
        if not application_id or not application_token:
            raise ConfigurationError(
                "tira.application_id and tira.application_token are required "
                "for additional SKU size prices."
            )
        encoded = base64.b64encode(
            f"{application_id}:{application_token}".encode("utf-8")
        ).decode("ascii")
        self.detail_headers = {"Authorization": f"Bearer {encoded}"}
        self.page_size = max(1, min(100, int(site_config.get("page_size", 50))))
        self.page_limit = max(1, int(site_config.get("page_limit", 200)))
        self.start_page = 1
        self.categories = [
            dict(category)
            for category in site_config.get("categories", ())
            if isinstance(category, Mapping)
            and category.get("enabled", True) is not False
            and category.get("id")
            and category.get("name")
        ]
        self.brand_filter = {
            _text(brand).casefold() for brand in brands if _text(brand)
        }
        detail_config = _mapping(site_config.get("details"))
        self.include_top_reviews = bool(
            detail_config.get("include_top_reviews", True)
        )
        self.page_failures = 0
        self.product_failures = 0
        self.detail_failures = 0
        self.products_seen = 0

    def select_categories(
        self,
        names: Sequence[str] | None,
    ) -> list[dict[str, Any]]:
        """Resolve case-insensitive configured Tira collection names."""
        if not self.categories:
            raise ConfigurationError("No Tira collections are configured.")
        if not names:
            parent = next(
                (
                    category
                    for category in self.categories
                    if category.get("covers_all")
                ),
                None,
            )
            return (
                [dict(parent)]
                if parent
                else [dict(category) for category in self.categories]
            )
        by_name = {
            _text(category["name"]).casefold(): category
            for category in self.categories
        }
        selected: list[dict[str, Any]] = []
        unknown: list[str] = []
        for name in names:
            category = by_name.get(_text(name).casefold())
            if category is None:
                unknown.append(str(name))
            elif category not in selected:
                selected.append(dict(category))
        if unknown:
            available = ", ".join(
                str(category["name"]) for category in self.categories
            )
            raise ConfigurationError(
                f"Unknown Tira collection(s): {', '.join(unknown)}. "
                f"Available: {available}"
            )
        return selected

    @staticmethod
    def _media_urls(*values: Any) -> list[str]:
        urls: list[str] = []

        def visit(value: Any) -> None:
            if isinstance(value, str):
                text = _text(value)
                if text.startswith("//"):
                    text = f"https:{text}"
                if text.startswith(("http://", "https://")) and text not in urls:
                    urls.append(text)
                return
            if isinstance(value, Mapping):
                media_type = _text(
                    value.get("type") or value.get("mediaType")
                ).casefold()
                if media_type and media_type not in {"image", "photo"}:
                    return
                direct = False
                for key in ("url", "src", "original", "image_url"):
                    if key in value:
                        direct = True
                        visit(value.get(key))
                if not direct:
                    for nested in value.values():
                        visit(nested)
                return
            if isinstance(value, (list, tuple)):
                for nested in value:
                    visit(nested)

        for value in values:
            visit(value)
        return urls

    @staticmethod
    def _rating_data(*values: Any) -> tuple[
        float | None,
        int | None,
        int | None,
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        meta: dict[str, Any] = {}
        custom: dict[str, Any] = {}
        root: Mapping[str, Any] = {}
        for value in values:
            if not isinstance(value, Mapping):
                continue
            root = value
            meta.update(_meta_map(value.get("_custom_meta")))
            custom.update(_mapping(value.get("_custom_json")))
        details = _mapping(
            custom.get("reviewsCountDetails")
            or custom.get("ratingsCountDetails")
        )
        breakdown = [
            {
                "stars": _integer(stars),
                "count": _integer(count),
                "percentage": None,
            }
            for stars, count in details.items()
            if _integer(stars) is not None and _integer(count) is not None
        ]
        breakdown_total = sum(
            int(item["count"] or 0) for item in breakdown
        )
        rating = _positive_max(
            (
                meta.get("averagerating"),
                custom.get("customAvgRating"),
                custom.get("averageRating"),
                root.get("rating"),
                _mapping(root.get("attributes")).get("customaveragerating"),
                _mapping(root.get("attributes")).get("customnumber2"),
            )
        )
        rating_count_value = _positive_max(
            (
                meta.get("ratingscount"),
                custom.get("ratingsCount"),
                breakdown_total,
            )
        )
        review_count_value = _positive_max(
            (
                meta.get("reviewscount"),
                custom.get("reviewsCount"),
                breakdown_total,
            )
        )
        top_reviews: list[dict[str, Any]] = []
        review_text = _text(custom.get("reviewContent"))
        if review_text:
            top_reviews.append(
                {
                    "review_id": "",
                    "title": _text(custom.get("reviewTitle")),
                    "review": review_text,
                    "rating": _number(custom.get("averageRating")),
                    "reviewer": _text(custom.get("reviewerName")),
                    "verified_buyer": False,
                    "created_at": _text(custom.get("reviewCreatedAt")),
                    "likes": None,
                    "images": [],
                }
            )
        return (
            rating,
            int(rating_count_value) if rating_count_value else None,
            int(review_count_value) if review_count_value else None,
            breakdown,
            top_reviews,
        )

    @staticmethod
    def _price(value: Any) -> tuple[float | None, float | None, float | None]:
        price = _mapping(value)
        marked = _mapping(price.get("marked"))
        effective = _mapping(price.get("effective"))
        selling = _mapping(price.get("selling"))
        mrp = _number(marked.get("min") or marked.get("max"))
        sale = _number(
            effective.get("min")
            or selling.get("min")
            or effective.get("max")
            or selling.get("max")
        )
        discount = None
        if mrp is not None and sale is not None and mrp > 0 and sale <= mrp:
            discount = round(((mrp - sale) / mrp) * 100, 2)
        return mrp, sale, discount

    @staticmethod
    def _variant_records(item: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        found: dict[str, Mapping[str, Any]] = {}
        root_uid = _text(item.get("uid"))
        if root_uid:
            found[root_uid] = item
        variants_value = item.get("variants")
        groups = (
            [variants_value]
            if isinstance(variants_value, Mapping)
            else [
                group
                for group in _list(variants_value)
                if isinstance(group, Mapping)
            ]
        )
        for group in groups:
            for variant in _list(group.get("items")):
                if not isinstance(variant, Mapping):
                    continue
                uid = _text(variant.get("uid"))
                if uid:
                    found[uid] = variant
        return list(found.values())

    def _parse_item(
        self,
        item: Mapping[str, Any],
        scraped_at: str,
        category: Mapping[str, Any] | None = None,
    ) -> list[Product]:
        attributes = _mapping(item.get("attributes"))
        brand_value = item.get("brand")
        brand = (
            _text(_mapping(brand_value).get("name"))
            if isinstance(brand_value, Mapping)
            else _text(brand_value)
        ) or _text(
            attributes.get("brand-name") or attributes.get("brand_name")
        )
        if self.brand_filter and brand.casefold() not in self.brand_filter:
            return []
        description_html = str(
            attributes.get("description")
            or attributes.get("product_details")
            or ""
        )
        description, ingredients, how_to_use, content_images = (
            _content_fields(description_html)
        )
        canonical_category = _text((category or {}).get("name"))
        source_categories = _text_list(
            (
                attributes.get("category-l1"),
                attributes.get("category-l2"),
                attributes.get("category-l3"),
            )
        )
        product_attributes = {
            label: value
            for key, label in (
                ("generic-name-of-commodity", "Generic name"),
                ("concern", "Concern"),
                ("benefits", "Benefits"),
                ("gender", "Gender"),
                ("shelf-life-in-months", "Shelf life"),
                ("net-quantity", "Net quantity"),
                ("formulation", "Formulation"),
                ("preference", "Preference"),
                ("skin-type", "Skin type"),
                ("country_of_origin", "Country of origin"),
                ("spf", "SPF"),
            )
            if (value := attributes.get(key)) not in (None, "", [], {})
        }
        key_features = _text_list(attributes.get("benefits"))
        special_features = _text_list(attributes.get("preference"))
        special_features.extend(
            value
            for value in _text_list(attributes.get("super-ingredients"))
            if value not in special_features
        )
        root_uid = _text(item.get("uid"))
        variants = self._variant_records(item)
        variant_uids = sorted(
            (_text(variant.get("uid")) for variant in variants),
            key=lambda value: (not value.isdigit(), int(value) if value.isdigit() else value),
        )
        parent_id = variant_uids[0] if variant_uids else root_uid
        root_price = self._price(item.get("price"))
        root_identifiers = _mapping(attributes.get("identifier"))
        root_skus = _list(root_identifiers.get("sku_code"))
        products: list[Product] = []
        for variant in variants:
            product_id = _text(variant.get("uid"))
            name = _text(variant.get("name") or item.get("name"))
            if not product_id or not name:
                raise ValueError("Tira record has no product ID or name.")
            is_root = product_id == root_uid
            variant_meta = _meta_map(variant.get("_custom_meta"))
            rating, rating_count, review_count, breakdown, reviews = (
                self._rating_data(item, variant)
            )
            image_urls = self._media_urls(
                variant.get("medias"),
                item.get("medias") if is_root else (),
                content_images,
            )
            variant_value = _first_text(
                (
                    variant.get("value"),
                    variant_meta.get("pack-size"),
                    attributes.get("pack-size") if is_root else "",
                    item.get("sizes") if is_root else "",
                )
            )
            slug = _text(variant.get("slug") or item.get("slug"))
            mrp, selling_price, discount = (
                root_price if is_root else (None, None, None)
            )
            sku = _text(root_skus[0]) if is_root and root_skus else ""
            available = variant.get("is_available")
            if available is None:
                available = item.get("sellable")
            products.append(
                Product(
                    site="tira",
                    product_id=product_id,
                    parent_product_id=parent_id,
                    sku=sku,
                    brand=brand,
                    product_name=name,
                    categories=(
                        [canonical_category] if canonical_category else []
                    ),
                    source_categories=source_categories,
                    variant=variant_value,
                    mrp=mrp,
                    selling_price=selling_price,
                    discount_pct=discount,
                    rating=rating,
                    rating_count=rating_count,
                    review_count=review_count,
                    in_stock=bool(available) if available is not None else None,
                    product_url=urljoin(
                        f"{self.site_base_url.rstrip('/')}/",
                        f"product/{slug}",
                    )
                    if slug
                    else "",
                    image_url=image_urls[0] if image_urls else "",
                    image_urls=image_urls,
                    description=description,
                    description_html=description_html,
                    ingredients=ingredients,
                    how_to_use=how_to_use,
                    key_features=key_features,
                    special_features=special_features,
                    product_attributes=product_attributes,
                    rating_breakdown=breakdown,
                    top_reviews=reviews if self.include_top_reviews else [],
                    scraped_at=scraped_at,
                )
            )
        return products

    def _listing_url(self, category: Mapping[str, Any], page: int) -> str:
        collection = quote(
            _text(category.get("collection") or category.get("id")),
            safe="-_",
        )
        base = self.listing_url_template.format(collection=collection)
        query = {
            "filters": "false",
            "page_id": max(0, int(page) - 1),
            "page_size": self.page_size,
        }
        configured_query = category.get("query")
        if isinstance(configured_query, Mapping):
            query.update(
                {
                    str(key): value
                    for key, value in configured_query.items()
                    if value is not None
                }
            )
        return f"{base}?{urlencode(query)}"

    @staticmethod
    def _matches_category(
        item: Mapping[str, Any],
        category: Mapping[str, Any],
    ) -> bool:
        """Apply an optional keyword filter to a broader Tira collection."""
        keywords = [
            _text(value).casefold()
            for value in _list(category.get("include_keywords"))
            if _text(value)
        ]
        if not keywords:
            return True

        searchable: list[str] = []

        def visit(value: Any) -> None:
            if isinstance(value, Mapping):
                for nested in value.values():
                    visit(nested)
            elif isinstance(value, (list, tuple, set)):
                for nested in value:
                    visit(nested)
            elif value not in (None, ""):
                searchable.append(_text(value).casefold())

        visit(item)
        haystack = " ".join(searchable)
        return any(keyword in haystack for keyword in keywords)

    def scrape_category_resumable(
        self,
        category: Mapping[str, Any],
        *,
        start_page: int,
        seen_product_ids: Iterable[str] = (),
        on_page: Callable[[int, Sequence[Product]], None] | None = None,
    ) -> CategoryScrapeResult:
        """Paginate a Tira collection and checkpoint every successful page."""
        first_page = max(self.start_page, int(start_page))
        last_page = self.start_page + self.page_limit - 1
        seen_ids = {str(value) for value in seen_product_ids if str(value)}
        products: list[Product] = []
        pages_scraped = 0
        previous_ids: tuple[str, ...] | None = None
        repeated = 0
        if first_page > last_page:
            return CategoryScrapeResult(
                products=[],
                next_page=first_page,
                completed=False,
                pages_scraped=0,
                stop_reason="page_limit",
            )

        for page in range(first_page, last_page + 1):
            url = self._listing_url(category, page)
            try:
                payload = self.request_json("GET", url)
            except RequestFailed as exc:
                self.page_failures += 1
                self.logger.error(
                    "tira_page collection=%s page=%s parse=failure error=%s",
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
            records = [
                record
                for record in _list(
                    payload.get("items")
                    if isinstance(payload, Mapping)
                    else None
                )
                if isinstance(record, Mapping)
            ]
            if not records:
                return CategoryScrapeResult(
                    products=products,
                    next_page=page,
                    completed=True,
                    pages_scraped=pages_scraped,
                    stop_reason="empty_page",
                )
            record_ids = tuple(_text(record.get("uid")) for record in records)
            repeated = repeated + 1 if record_ids == previous_ids else 0
            previous_ids = record_ids
            scraped_at = datetime.now(timezone.utc).isoformat(
                timespec="microseconds"
            )
            page_products: list[Product] = []
            for record in records:
                self.products_seen += 1
                if not self._matches_category(record, category):
                    continue
                try:
                    parsed = self._parse_item(record, scraped_at, category)
                except Exception as exc:
                    self.product_failures += 1
                    self.logger.exception(
                        "tira_product collection=%s page=%s failed=%s",
                        category.get("name", ""),
                        page,
                        exc,
                    )
                    continue
                for product in parsed:
                    if product.product_id not in seen_ids:
                        seen_ids.add(product.product_id)
                        page_products.append(product)
            products.extend(page_products)
            pages_scraped += 1
            if on_page is not None:
                on_page(page, page_products)
            page_info = _mapping(
                payload.get("page") if isinstance(payload, Mapping) else None
            )
            self.logger.info(
                "tira_page collection=%s page=%s records=%s skus=%s "
                "total=%s parse=success",
                category.get("name", ""),
                page,
                len(records),
                len(page_products),
                page_info.get("item_total", ""),
            )
            if not bool(page_info.get("has_next")):
                return CategoryScrapeResult(
                    products=products,
                    next_page=page + 1,
                    completed=True,
                    pages_scraped=pages_scraped,
                    stop_reason="end_of_results",
                )
            if repeated >= 2:
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

    def fetch_variant_price(self, product: Product) -> Product:
        """Fetch price, stock, and seller SKU for one Tira variant slug."""
        path = urlparse(product.product_url).path
        match = re.search(r"/product/([^/?#]+)", path)
        if not match:
            raise ValueError(
                f"Tira product {product.product_id} has no variant slug."
            )
        slug = quote(match.group(1), safe="-_")
        url = self.detail_url_template.format(slug=slug)
        payload = self.request_json(
            "GET",
            url,
            headers=self.detail_headers,
        )
        if not isinstance(payload, Mapping):
            raise ValueError(
                f"Tira size response for {product.product_id} is not an object."
            )
        sizes = [
            size
            for size in _list(payload.get("sizes"))
            if isinstance(size, Mapping)
        ]
        selected = next(
            (size for size in sizes if bool(size.get("is_available"))),
            sizes[0] if sizes else {},
        )
        if not selected:
            raise ValueError(
                f"Tira size response for {product.product_id} has no SKU."
            )
        mrp, selling_price, discount = self._price(payload.get("price"))
        identifiers = _list(selected.get("seller_identifiers"))
        if not identifiers:
            identifiers = _list(selected.get("all_identifiers"))
        return replace(
            product,
            sku=_text(identifiers[0]) if identifiers else product.sku,
            variant=product.variant
            or _text(selected.get("display") or selected.get("value")),
            mrp=mrp,
            selling_price=selling_price,
            discount_pct=discount,
            in_stock=bool(
                selected.get("is_available", payload.get("sellable"))
            ),
            scraped_at=datetime.now(timezone.utc).isoformat(
                timespec="microseconds"
            ),
        )
