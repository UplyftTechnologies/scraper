"""Amazon India browser client for category discovery and product details."""

from __future__ import annotations

import logging
import json
import random
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence, TypeVar
from urllib.parse import quote_plus

from playwright.sync_api import Browser, BrowserContext, Page, Playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

from pricing_scraper.models import Product, brand_key, normalize_gtin

from .base import ConfigurationError, build_logger

T = TypeVar("T")
ProgressCallback = Callable[[str, int, int, str], None]

ASIN_PATTERN = re.compile(r"(?<![A-Z0-9])([A-Z0-9]{10})(?![A-Z0-9])")
SEARCH_DATA_ASIN_PATTERN = re.compile(
    r"""data-asin\s*=\s*["']([A-Z0-9]{10})["']""",
    re.IGNORECASE,
)
SEARCH_DP_PATTERN = re.compile(
    r"""href\s*=\s*["'][^"']*/dp/([A-Z0-9]{10})(?:[/?#"']|$)""",
    re.IGNORECASE,
)
MONEY_PATTERN = re.compile(
    r"(?:₹|INR\s*)?([\d,]+(?:\.\d{1,2})?)",
    re.IGNORECASE,
)
COUNT_PATTERN = re.compile(r"([\d,.]+)\s*([KkMm]?)")


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _attribute_label(key: Any) -> str:
    """Fold a product-information label, which carries bidi marks and colons."""
    return re.sub(r"[^a-z0-9]", "", _text(key).casefold())


def _by_label(attributes: Mapping[str, Any]) -> dict[str, Any]:
    return {_attribute_label(key): value for key, value in attributes.items()}


def _attribute_gtin(attributes: Mapping[str, Any]) -> str:
    """Read a barcode from the product-information table when Amazon lists one.

    Amazon India normally publishes only ASIN and manufacturer model numbers,
    so most beauty products leave this empty.
    """
    labelled = _by_label(attributes)
    for label in ("upc", "ean", "ean13", "gtin", "isbn", "barcode"):
        gtin = normalize_gtin(labelled.get(label))
        if gtin:
            return gtin
    return ""


def _attribute_ingredients(attributes: Mapping[str, Any]) -> list[str]:
    """Split Amazon's ingredient rows into individual ingredient names.

    ``Special Ingredients`` is Amazon's key-ingredient row; ``Active
    Ingredients`` is used only as a fallback because it often holds the full
    INCI list instead.
    """
    labelled = _by_label(attributes)
    raw = _text(
        labelled.get("specialingredients")
        or labelled.get("keyingredients")
        or labelled.get("activeingredients")
    )
    names: list[str] = []
    for part in re.split(r"[;,]", raw):
        name = _text(part).strip(" .;-–—")
        if 1 < len(name) <= 80:
            names.append(name)
    return _unique(names)


def _money(value: Any) -> float | None:
    match = MONEY_PATTERN.search(_text(value))
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _count(value: Any) -> int | None:
    match = COUNT_PATTERN.search(_text(value))
    if not match:
        return None
    try:
        number = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    suffix = match.group(2).casefold()
    if suffix == "k":
        number *= 1_000
    elif suffix == "m":
        number *= 1_000_000
    return int(number)


def _asin(value: str) -> str:
    match = ASIN_PATTERN.search(value.upper())
    candidate = match.group(1) if match else ""
    return candidate if any(character.isdigit() for character in candidate) else ""


def _original_image_url(value: Any) -> str:
    """Remove Amazon thumbnail resize tokens while preserving the image URL."""
    url = _text(value)
    return re.sub(
        r"\._[^./]+_\.(?=[A-Za-z0-9]{2,5}(?:$|\?))",
        ".",
        url,
    )


def _search_asins_from_html(html: str) -> list[str]:
    """Extract result ASINs from both current Amazon search-card layouts."""
    return _unique(
        candidate.upper()
        for pattern in (SEARCH_DATA_ASIN_PATTERN, SEARCH_DP_PATTERN)
        for candidate in pattern.findall(html)
        if _asin(candidate)
    )


def _section(text: str, label: str, following: Sequence[str]) -> str:
    """Extract a labeled text section from Amazon's important information."""
    pattern = re.compile(
        rf"{re.escape(label)}\s*:?\s*(.*?)(?="
        + "|".join(re.escape(item) + r"\s*:?" for item in following)
        + r"|$)",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(text)
    return _text(match.group(1)) if match else ""


@dataclass(frozen=True, slots=True)
class AmazonScrapeResult:
    """Products and diagnostics from one Amazon category/detail run."""

    products: list[Product]
    discovered_asins: int
    processed_asins: int
    failed_asins: tuple[str, ...]
    completed: bool


class AmazonClient:
    """Collect public Amazon India search and product pages with Playwright."""

    def __init__(
        self,
        site_config: Mapping[str, Any],
        request_config: Mapping[str, Any],
        brands: Iterable[str] = (),
        *,
        sleeper: Callable[[float], None] = time.sleep,
        random_uniform: Callable[[float, float], float] = random.uniform,
        logger: logging.Logger | None = None,
    ) -> None:
        self.site_config = dict(site_config)
        self.categories = [
            dict(item)
            for item in site_config.get("categories", ())
            if isinstance(item, Mapping)
            and item.get("name")
            and item.get("query")
            and item.get("enabled", True) is not False
        ]
        self.products_config = list(site_config.get("products", ()))
        self.headless = bool(site_config.get("headless", True))
        self.browser_channel = _text(site_config.get("browser_channel"))
        self.search_page_limit = max(
            1, int(site_config.get("search_page_limit", 2))
        )
        self.max_products_per_category = max(
            1, int(site_config.get("max_products_per_category", 40))
        )
        self.max_variants_per_product = max(
            0, int(site_config.get("max_variants_per_product", 12))
        )
        self.max_retries = max(0, int(site_config.get("max_retries", 2)))
        self.navigation_timeout_ms = max(
            5_000, int(site_config.get("navigation_timeout_ms", 45_000))
        )
        self.search_result_timeout_ms = max(
            1_000, int(site_config.get("search_result_timeout_ms", 12_000))
        )
        self.challenge_wait_ms = max(
            0, int(site_config.get("challenge_wait_ms", 7_000))
        )
        self.warmup_enabled = bool(
            site_config.get("warmup_enabled", True)
        )
        self.warmup_wait_ms = max(
            0, int(site_config.get("warmup_wait_ms", 3_000))
        )
        self.delay_min = max(
            0.0,
            float(
                site_config.get(
                    "delay_min_seconds",
                    request_config.get("delay_min_seconds", 2.0),
                )
            ),
        )
        self.delay_max = max(
            self.delay_min,
            float(
                site_config.get(
                    "delay_max_seconds",
                    request_config.get("delay_max_seconds", 5.0),
                )
            ),
        )
        self.max_requests_per_minute = max(
            1, int(request_config.get("max_requests_per_minute", 12))
        )
        self.brand_filter = {
            brand_key(brand) for brand in brands if brand_key(brand)
        }
        self.logs_dir = Path(
            str(request_config.get("logs_dir") or "logs")
        ).resolve()
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logger or build_logger("amazonclient", self.logs_dir)
        self.sleeper = sleeper
        self.random_uniform = random_uniform
        self.requests_made = 0
        self.failures = 0
        self.blocks_encountered = 0
        self.product_failures = 0
        self.page_failures = 0
        self.detail_failures = 0
        self._manager: Any = None
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._request_times: deque[float] = deque()

    def __enter__(self) -> AmazonClient:
        self._manager = Stealth().use_sync(sync_playwright())
        self._playwright = self._manager.__enter__()
        launch: dict[str, Any] = {"headless": self.headless}
        if self.browser_channel:
            launch["channel"] = self.browser_channel
        try:
            self._browser = self._playwright.chromium.launch(**launch)
        except Exception:
            if "channel" not in launch:
                raise
            self.logger.warning(
                "amazon_browser channel=%s unavailable; using bundled chromium",
                self.browser_channel,
            )
            self._browser = self._playwright.chromium.launch(
                headless=self.headless
            )
        if self.warmup_enabled:
            try:
                self._browse(
                    "https://www.amazon.in/",
                    self._parse_warmup_page,
                )
            except Exception as exc:
                self.logger.warning("amazon_warmup failed=%s", exc)
        return self

    def __exit__(self, *_args: Any) -> None:
        if self._context is not None:
            self._context.close()
        if self._browser is not None:
            self._browser.close()
        if self._manager is not None:
            self._manager.__exit__(*_args)
        self._browser = None
        self._context = None
        self._playwright = None

    def select_categories(
        self,
        names: Sequence[str] | None,
    ) -> list[dict[str, Any]]:
        """Resolve requested Amazon search categories case-insensitively."""
        if not self.categories:
            raise ConfigurationError("No Amazon categories are configured.")
        if not names:
            return [dict(item) for item in self.categories]
        lookup = {
            _text(item["name"]).casefold(): item for item in self.categories
        }
        selected: list[dict[str, Any]] = []
        missing: list[str] = []
        for name in names:
            item = lookup.get(_text(name).casefold())
            if item is None:
                missing.append(str(name))
            elif item not in selected:
                selected.append(dict(item))
        if missing:
            raise ConfigurationError(
                "Unknown Amazon categories: "
                + ", ".join(missing)
                + ". Available: "
                + ", ".join(item["name"] for item in self.categories)
            )
        return selected

    def _delay(self) -> None:
        now = time.monotonic()
        while self._request_times and now - self._request_times[0] >= 60:
            self._request_times.popleft()
        if len(self._request_times) >= self.max_requests_per_minute:
            wait_for = max(0.0, 60 - (now - self._request_times[0]))
            if wait_for:
                self.sleeper(wait_for)
            now = time.monotonic()
            while self._request_times and now - self._request_times[0] >= 60:
                self._request_times.popleft()
        delay = self.random_uniform(self.delay_min, self.delay_max)
        if delay:
            self.sleeper(delay)
        self._request_times.append(time.monotonic())

    def _new_context(self) -> BrowserContext:
        if self._browser is None:
            raise RuntimeError("AmazonClient must be used as a context manager.")
        return self._browser.new_context(
            viewport={"width": 1440, "height": 1000},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/150.0.0.0 Safari/537.36"
            ),
        )

    def _active_context(self) -> BrowserContext:
        """Reuse cookies during a run; failed attempts get a fresh context."""
        if self._context is None:
            self._context = self._new_context()
        return self._context

    def _parse_warmup_page(self, page: Page) -> str:
        """Allow Amazon to establish ordinary storefront session cookies."""
        return page.title()

    @staticmethod
    def _is_captcha(page: Page) -> bool:
        body = _text(page.locator("body").inner_text(timeout=5_000)).casefold()
        html = page.content().casefold()
        return any(
            marker in body or marker in html
            for marker in (
                "enter the characters you see",
                "type the characters you see in this image",
                "sorry, we just need to make sure you're not a robot",
                "click the button below to continue shopping",
                "bm-verify",
                "validatecaptcha",
            )
        )

    def _browse(self, url: str, parser: Callable[[Page], T]) -> T:
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 2):
            context = self._active_context()
            page = context.new_page()
            succeeded = False
            try:
                self._delay()
                response = page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self.navigation_timeout_ms,
                )
                self.requests_made += 1
                status = response.status if response is not None else 0
                if (
                    self.warmup_wait_ms
                    and url.rstrip("/") == "https://www.amazon.in"
                ):
                    page.wait_for_timeout(self.warmup_wait_ms)
                if (
                    self.challenge_wait_ms
                    and "bm-verify" in page.content().casefold()
                ):
                    page.wait_for_timeout(self.challenge_wait_ms)
                if self._is_captcha(page):
                    self.blocks_encountered += 1
                    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                    page.screenshot(
                        path=str(self.logs_dir / f"amazon_captcha_{stamp}.png"),
                        full_page=True,
                    )
                    raise RuntimeError("Amazon CAPTCHA detected")
                if status >= 400:
                    raise RuntimeError(f"Amazon returned HTTP {status}")
                result = parser(page)
                body_size = len(page.content().encode("utf-8"))
                self.logger.info(
                    "amazon_page url=%s status=%s bytes=%s parse=success",
                    url,
                    status,
                    body_size,
                )
                succeeded = True
                return result
            except Exception as exc:
                last_error = exc
                self.failures += 1
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                try:
                    failure_dir = self.logs_dir / "failures"
                    failure_dir.mkdir(parents=True, exist_ok=True)
                    (failure_dir / f"amazon_{stamp}.html").write_text(
                        page.content(),
                        encoding="utf-8",
                    )
                except Exception:
                    pass
                self.logger.warning(
                    "amazon_page url=%s attempt=%s failed=%s",
                    url,
                    attempt,
                    exc,
                )
            finally:
                try:
                    page.close()
                except Exception:
                    pass
                if not succeeded:
                    try:
                        context.close()
                    except Exception:
                        pass
                    if self._context is context:
                        self._context = None
        raise RuntimeError(
            f"Amazon page failed after {self.max_retries + 1} attempts: {url}"
        ) from last_error

    @staticmethod
    def _first_text(page: Page, selectors: Sequence[str]) -> str:
        for selector in selectors:
            locator = page.locator(selector)
            try:
                if locator.count():
                    value = _text(locator.first.inner_text(timeout=2_000))
                    if value:
                        return value
            except Exception:
                continue
        return ""

    @staticmethod
    def _texts(page: Page, selector: str) -> list[str]:
        try:
            return _unique(
                _text(value)
                for value in page.locator(selector).all_inner_texts()
            )
        except Exception:
            return []

    @classmethod
    def _first_price_text(
        cls,
        page: Page,
        selectors: Sequence[str],
        *,
        reject_unit_price: bool = False,
    ) -> str:
        for selector in selectors:
            locator = page.locator(selector)
            try:
                for index in range(locator.count()):
                    value = _text(locator.nth(index).inner_text(timeout=2_000))
                    folded = value.casefold().replace(" ", "")
                    if (
                        reject_unit_price
                        and (
                            "/100" in folded
                            or "per100" in folded
                            or "/1" in folded
                        )
                    ):
                        continue
                    if _money(value) is not None:
                        return value
            except Exception:
                continue
        return ""

    @staticmethod
    def _amazon_price(
        page: Page,
        containers: Sequence[str],
        *,
        strike: bool = False,
    ) -> float | None:
        """Read a composed Amazon price without mistaking a unit price."""
        for container_selector in containers:
            container = page.locator(container_selector)
            try:
                count = min(container.count(), 8)
            except Exception:
                continue
            for index in range(count):
                node = container.nth(index)
                try:
                    text = _text(node.inner_text(timeout=2_000))
                    folded = text.casefold().replace(" ", "")
                    if any(
                        marker in folded
                        for marker in ("/100", "per100", "/1kg", "/1g", "/1ml")
                    ):
                        continue
                    if strike and not any(
                        marker in folded
                        for marker in ("m.r.p", "mrp", "listprice")
                    ):
                        classes = _text(node.get_attribute("class")).casefold()
                        if "a-text-price" not in classes:
                            continue
                    whole = node.locator(".a-price-whole")
                    fraction = node.locator(".a-price-fraction")
                    if whole.count():
                        whole_text = re.sub(
                            r"[^\d,]",
                            "",
                            _text(whole.first.inner_text(timeout=1_000)),
                        )
                        fraction_text = (
                            re.sub(
                                r"\D",
                                "",
                                _text(
                                    fraction.first.inner_text(timeout=1_000)
                                ),
                            )
                            if fraction.count()
                            else ""
                        )
                        if whole_text:
                            numeric = whole_text.replace(",", "")
                            if fraction_text:
                                numeric += "." + fraction_text[:2]
                            return float(numeric)
                    price = _money(text)
                    if price is not None:
                        return price
                except Exception:
                    continue
        return None

    @classmethod
    def _core_price_fallback(
        cls,
        page: Page,
    ) -> tuple[float | None, float | None, float | None]:
        """Parse visible core price text used by Amazon's newer layout."""
        def parsed_price(token: str) -> float:
            normalized = token.replace(",", "")
            value = float(normalized)
            # Amazon sometimes concatenates separate whole/fraction spans in
            # innerText (₹420 + 00 becomes ₹42000).
            if (
                "." not in token
                and normalized.endswith("00")
                and value >= 10_000
            ):
                value /= 100
            return value

        core_text = cls._first_text(
            page,
            (
                "#corePriceDisplay_desktop_feature_div",
                "#apex_desktop",
                "#centerCol",
                "#ppd",
            ),
        )
        if not core_text:
            return None, None, None
        discount_match = re.search(
            r"[-−]\s*(\d{1,2}(?:\.\d+)?)\s*%",
            core_text,
        )
        discount = (
            float(discount_match.group(1)) if discount_match else None
        )
        mrp_match = re.search(
            r"M\.?\s*R\.?\s*P\.?\s*:?\s*(?:₹|INR)?\s*"
            r"([\d,]+(?:\.\d{1,2})?)",
            core_text,
            re.IGNORECASE,
        )
        mrp = (
            parsed_price(mrp_match.group(1))
            if mrp_match
            else None
        )
        selling_match = re.search(
            r"[-−]\s*\d{1,2}(?:\.\d+)?\s*%\s*"
            r"(?:₹|INR)?\s*([\d,]+(?:\.\d{1,2})?)",
            core_text,
        )
        selling = (
            parsed_price(selling_match.group(1))
            if selling_match
            else None
        )
        return selling, mrp, discount

    def _discover_search_page(self, page: Page) -> list[str]:
        combined_selector = (
            "[data-component-type='s-search-result'][data-asin], "
            "[data-component-type='s-search-result'] a[href*='/dp/'], "
            "div[data-asin] a[href*='/dp/']"
        )
        try:
            page.locator(combined_selector).first.wait_for(
                state="attached",
                timeout=self.search_result_timeout_ms,
            )
        except PlaywrightTimeoutError:
            pass

        asins: list[str] = []
        cards = page.locator(
            "[data-component-type='s-search-result'][data-asin]"
        )
        try:
            for index in range(cards.count()):
                candidate = _asin(
                    cards.nth(index).get_attribute("data-asin") or ""
                )
                if candidate:
                    asins.append(candidate)
        except Exception:
            pass

        if not asins:
            for selector in (
                "[data-component-type='s-search-result'] a[href*='/dp/']",
                "div[data-asin] a[href*='/dp/']",
                "a[href*='/dp/']",
            ):
                locator = page.locator(selector)
                try:
                    for index in range(locator.count()):
                        candidate = _asin(
                            locator.nth(index).get_attribute("href") or ""
                        )
                        if candidate:
                            asins.append(candidate)
                except Exception:
                    continue
                if asins:
                    break

        if not asins:
            asins = _search_asins_from_html(page.content())
        if not asins:
            body = _text(
                page.locator("body").inner_text(timeout=5_000)
            ).casefold()
            if any(
                marker in body
                for marker in (
                    "no results for",
                    "did not match any products",
                )
            ):
                return []
            raise RuntimeError(
                "Amazon search page contained no discoverable ASINs"
            )
        return [
            f"https://www.amazon.in/dp/{asin}"
            for asin in _unique(asins)
        ]

    def discover_category_urls(
        self,
        category: Mapping[str, Any],
    ) -> list[str]:
        """Discover capped Amazon product URLs from one configured search."""
        query = quote_plus(_text(category.get("query")))
        urls: list[str] = []
        last_error: Exception | None = None
        for page_number in range(1, self.search_page_limit + 1):
            url = (
                "https://www.amazon.in/s?"
                f"k={query}&i=beauty&page={page_number}"
            )
            try:
                page_urls = self._browse(url, self._discover_search_page)
            except Exception as exc:
                last_error = exc
                self.page_failures += 1
                self.logger.error(
                    "amazon_search category=%s page=%s failed=%s",
                    category.get("name", ""),
                    page_number,
                    exc,
                )
                break
            for product_url in page_urls:
                if product_url not in urls:
                    urls.append(product_url)
                if len(urls) >= self.max_products_per_category:
                    return urls
            if not page_urls:
                break
        if last_error is not None and not urls:
            raise RuntimeError(
                f"Amazon search failed for {category.get('name', '')}"
            ) from last_error
        return urls

    @staticmethod
    def _attributes(page: Page) -> dict[str, str]:
        attributes: dict[str, str] = {}
        selectors = (
            "#productOverview_feature_div tr",
            "#productDetails_detailBullets_sections1 tr",
            "#productDetails_techSpec_section_1 tr",
            "#detailBullets_feature_div li",
        )
        for selector in selectors:
            try:
                rows = page.locator(selector).all_inner_texts()
            except Exception:
                continue
            for row in rows:
                lines = [_text(line) for line in row.splitlines() if _text(line)]
                if len(lines) >= 2:
                    key, value = lines[0].rstrip(" :"), " ".join(lines[1:])
                elif ":" in row:
                    key, value = row.split(":", 1)
                    key, value = _text(key), _text(value)
                else:
                    continue
                if key and value:
                    attributes.setdefault(key, value)
        return attributes

    @staticmethod
    def _reviews(page: Page) -> list[dict[str, Any]]:
        reviews: list[dict[str, Any]] = []
        locator = page.locator("[data-hook='review']")
        try:
            count = min(locator.count(), 5)
        except Exception:
            return reviews
        for index in range(count):
            review = locator.nth(index)

            def value(selector: str) -> str:
                child = review.locator(selector)
                try:
                    return (
                        _text(child.first.inner_text(timeout=1_000))
                        if child.count()
                        else ""
                    )
                except Exception:
                    return ""

            rating = _money(value("[data-hook='review-star-rating']"))
            reviews.append(
                {
                    "review_id": review.get_attribute("id") or "",
                    "title": value("[data-hook='review-title']"),
                    "review": value("[data-hook='review-body']"),
                    "rating": rating,
                    "reviewer": value(".a-profile-name"),
                    "verified_buyer": bool(
                        value("[data-hook='avp-badge']")
                    ),
                    "created_at": value("[data-hook='review-date']"),
                    "likes": None,
                    "images": [],
                }
            )
        return reviews

    def _parse_product_page(
        self,
        page: Page,
        url: str,
        categories: Sequence[str],
        parent_asin: str = "",
    ) -> tuple[Product, list[str]]:
        title = self._first_text(page, ("#productTitle", "h1"))
        asin = _asin(url) or _text(
            page.locator("input#ASIN").get_attribute("value")
            if page.locator("input#ASIN").count()
            else ""
        )
        if not asin or not title:
            raise ValueError("Amazon product page has no ASIN or title.")

        brand = self._first_text(
            page,
            ("#bylineInfo", "#productOverview_feature_div tr:has-text('Brand') td"),
        )
        brand = re.sub(
            r"^(?:Visit the |Brand:\s*)| Store$",
            "",
            brand,
            flags=re.IGNORECASE,
        ).strip()
        selling_price = self._amazon_price(
            page,
            (
                "#corePriceDisplay_desktop_feature_div .priceToPay",
                "#corePriceDisplay_desktop_feature_div .apexPriceToPay",
                "#corePriceDisplay_desktop_feature_div "
                ".a-price:not(.a-text-price)",
                "#apex_desktop .priceToPay",
                "#apex_desktop .a-price:not(.a-text-price)",
                "#desktop_buybox .a-price:not(.a-text-price)",
                "#buybox .a-price:not(.a-text-price)",
            ),
        )
        selling_text = self._first_price_text(
            page,
            (
                ".a-price.priceToPay .a-offscreen",
                ".reinventPricePriceToPayMargin .a-offscreen",
                "#corePriceDisplay_desktop_feature_div "
                ".a-price:not(.a-text-price) .a-offscreen",
                "#apex_desktop .a-price:not(.a-text-price) .a-offscreen",
                "#desktop_buybox .a-price:not(.a-text-price) .a-offscreen",
                "#buybox .a-price:not(.a-text-price) .a-offscreen",
                "#price_inside_buybox",
                "#newBuyBoxPrice",
                "#tp_price_block_total_price_ww",
                "#priceblock_ourprice",
                "#priceblock_dealprice",
            ),
        )
        mrp = self._amazon_price(
            page,
            (
                "#corePriceDisplay_desktop_feature_div "
                ".basisPrice .a-price",
                "#corePriceDisplay_desktop_feature_div "
                ".a-price.a-text-price",
                "#apex_desktop .basisPrice .a-price",
                "#apex_desktop .a-price.a-text-price",
            ),
            strike=True,
        )
        mrp_text = self._first_price_text(
            page,
            (
                ".a-price.a-text-price[data-a-strike='true'] .a-offscreen",
                "[data-a-strike='true'] .a-offscreen",
                "#corePriceDisplay_desktop_feature_div "
                ".a-price.a-text-price .a-offscreen",
                "#priceblock_listprice",
                ".basisPrice .a-offscreen",
            ),
            reject_unit_price=True,
        )
        selling_price = selling_price or _money(selling_text)
        mrp = mrp or _money(mrp_text)
        fallback_selling, fallback_mrp, fallback_discount = (
            self._core_price_fallback(page)
        )
        if (
            fallback_selling is not None
            and (
                fallback_mrp is None
                or fallback_selling <= fallback_mrp
            )
        ):
            selling_price = fallback_selling
        mrp = fallback_mrp or mrp or selling_price
        discount = (
            round(((mrp - selling_price) / mrp) * 100, 2)
            if mrp and selling_price is not None and selling_price <= mrp
            else fallback_discount
        )
        rating_text = self._first_text(
            page,
            ("#acrPopover", "[data-hook='rating-out-of-text']"),
        )
        rating = _money(rating_text)
        rating_count = _count(
            self._first_text(
                page,
                ("#acrCustomerReviewText", "[data-hook='total-review-count']"),
            )
        )
        availability = self._first_text(
            page,
            ("#availability", "#outOfStock", "#deliveryBlockMessage"),
        )
        availability_folded = availability.casefold()
        in_stock = (
            False
            if any(
                marker in availability_folded
                for marker in ("unavailable", "out of stock", "currently unavailable")
            )
            else True if availability else None
        )
        variant = self._first_text(
            page,
            (
                "#variation_size_name .selection",
                "#variation_style_name .selection",
                "#variation_color_name .selection",
                "#inline-twister-expanded-dimension-text-size_name",
                "#inline-twister-dim-title-value-size_name",
                "[data-csa-c-content-id="
                "'inline-twister-dim-title-value-size_name']",
            ),
        )
        key_features = [
            value
            for value in self._texts(page, "#feature-bullets li span")
            if value.casefold() != "see more"
        ]
        description_parts = [
            self._first_text(page, ("#productDescription",)),
            *key_features,
        ]
        description = "\n".join(_unique(description_parts))
        important = self._first_text(
            page,
            ("#importantInformation", "#important-information"),
        )
        ingredients = _section(
            important,
            "Ingredients",
            ("Directions", "Safety Information", "Product Description"),
        )
        how_to_use = _section(
            important,
            "Directions",
            ("Safety Information", "Ingredients", "Product Description"),
        )
        attributes = self._attributes(page)
        if not variant:
            for key, value in attributes.items():
                if key.casefold().strip() in {
                    "size",
                    "net quantity",
                    "item volume",
                    "item weight",
                }:
                    variant = _text(value)
                    if variant:
                        break
        # Ingredient rows are excluded here: they are names, not features, and
        # now populate key_ingredients instead.
        special_features = _unique(
            value
            for key, value in attributes.items()
            if _attribute_label(key)
            in {
                "specialfeature",
                "materialfeature",
                "skintype",
                "itemform",
            }
        )
        key_ingredients = _attribute_ingredients(attributes)
        image_urls: list[str] = []
        landing = page.locator("#landingImage")
        if landing.count():
            dynamic = _text(landing.get_attribute("data-a-dynamic-image"))
            if dynamic:
                try:
                    image_urls.extend(
                        _original_image_url(value)
                        for value in json.loads(dynamic)
                        if str(value).startswith("http")
                    )
                except (json.JSONDecodeError, TypeError):
                    pass
            for attribute in ("data-old-hires", "src"):
                value = _text(landing.get_attribute(attribute))
                if value.startswith("http"):
                    image_urls.append(_original_image_url(value))
        for image in page.locator("#altImages img").all():
            for attribute in (
                "data-old-hires",
                "data-a-dynamic-image",
                "src",
            ):
                value = _text(image.get_attribute(attribute))
                if value.startswith("http"):
                    image_urls.append(_original_image_url(value))
                elif value.startswith("{"):
                    try:
                        image_urls.extend(
                            _original_image_url(candidate)
                            for candidate in json.loads(value)
                            if str(candidate).startswith("http")
                        )
                    except (json.JSONDecodeError, TypeError):
                        pass
        image_urls = _unique(image_urls)
        breadcrumbs = self._texts(
            page,
            "#wayfinding-breadcrumbs_feature_div a",
        )
        html_parts: list[str] = []
        for selector in (
            "#productDescription",
            "#aplus",
            "#importantInformation",
            "#important-information",
        ):
            locator = page.locator(selector)
            try:
                if locator.count():
                    html_parts.append(locator.first.inner_html(timeout=2_000))
            except Exception:
                continue

        variant_asins: list[str] = []
        for selector in (
            "#variation_size_name [data-defaultasin]",
            "#variation_size_name [data-asin]",
            "#variation_size_name li[data-asin]",
            "#twister [data-asin]",
            "#native_dropdown_selected_size_name option",
            "#variation_size_name a[href]",
            "#variation_size_name [data-dp-url]",
            "#variation_size_name [data-csa-c-item-id]",
            "#twister a[href]",
            "#twister [data-dp-url]",
            "#twister [data-csa-c-item-id]",
        ):
            locator = page.locator(selector)
            try:
                for index in range(locator.count()):
                    candidate = _asin(
                        (locator.nth(index).get_attribute("data-defaultasin") or "")
                        + " "
                        + (locator.nth(index).get_attribute("data-asin") or "")
                        + " "
                        + (locator.nth(index).get_attribute("value") or "")
                        + " "
                        + (locator.nth(index).get_attribute("href") or "")
                        + " "
                        + (locator.nth(index).get_attribute("data-dp-url") or "")
                        + " "
                        + (
                            locator.nth(index).get_attribute(
                                "data-csa-c-item-id"
                            )
                            or ""
                        )
                    )
                    if candidate and candidate != asin:
                        variant_asins.append(candidate)
            except Exception:
                continue
        for selector in ("#variation_size_name", "#twister"):
            locator = page.locator(selector)
            try:
                if locator.count():
                    markup = locator.first.inner_html(timeout=2_000).upper()
                    variant_asins.extend(
                        candidate
                        for candidate in ASIN_PATTERN.findall(markup)
                        if candidate != asin
                    )
            except Exception:
                continue
        try:
            for script_text in page.locator("script").all_text_contents():
                if not any(
                    marker in script_text
                    for marker in (
                        "dimensionValuesDisplayData",
                        "dimensionToAsinMap",
                        "asinVariationValues",
                    )
                ):
                    continue
                variant_asins.extend(
                    candidate
                    for candidate in ASIN_PATTERN.findall(
                        script_text.upper()
                    )
                    if candidate != asin and candidate.startswith("B0")
                )
        except Exception:
            pass

        product = Product(
            site="amazon",
            product_id=asin,
            parent_product_id=parent_asin or asin,
            sku=asin,
            gtin=_attribute_gtin(attributes),
            brand=brand,
            product_name=title,
            categories=_unique(categories),
            source_categories=breadcrumbs,
            variant=variant,
            mrp=mrp,
            selling_price=selling_price,
            discount_pct=discount,
            rating=rating,
            rating_count=rating_count,
            review_count=rating_count,
            in_stock=in_stock,
            product_url=f"https://www.amazon.in/dp/{asin}",
            image_url=image_urls[0] if image_urls else "",
            image_urls=image_urls,
            description=description,
            description_html="\n".join(html_parts),
            ingredients=ingredients,
            key_ingredients=key_ingredients,
            how_to_use=how_to_use,
            key_features=key_features,
            special_features=special_features,
            product_attributes=attributes,
            top_reviews=self._reviews(page),
            scraped_at=datetime.now(timezone.utc).isoformat(
                timespec="microseconds"
            ),
        )
        return product, _unique(variant_asins)[: self.max_variants_per_product]

    def fetch_product(
        self,
        url_or_asin: str,
        categories: Sequence[str],
        *,
        parent_asin: str = "",
    ) -> tuple[Product, list[str]]:
        """Fetch one Amazon product and discover its selectable ASIN variants."""
        asin = _asin(url_or_asin)
        if not asin:
            raise ValueError(f"Invalid Amazon URL or ASIN: {url_or_asin!r}")
        url = f"https://www.amazon.in/dp/{asin}"
        return self._browse(
            url,
            lambda page: self._parse_product_page(
                page,
                url,
                categories,
                parent_asin,
            ),
        )

    def scrape(
        self,
        categories: Sequence[Mapping[str, Any]],
        *,
        processed_asins: Iterable[str] = (),
        on_product: Callable[[Product], None] | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> AmazonScrapeResult:
        """Discover category products and collect normalized product details."""
        asin_categories: dict[str, set[str]] = {}
        discovery_failed = False
        for category_index, category in enumerate(categories, start=1):
            name = _text(category.get("name"))
            if progress_callback is not None:
                progress_callback(
                    "listing",
                    category_index - 1,
                    len(categories),
                    f"Amazon search {category_index}/{len(categories)}: {name}",
                )
            try:
                urls = self.discover_category_urls(category)
            except Exception:
                self.page_failures += 1
                discovery_failed = True
                urls = []
            for url in urls:
                asin = _asin(url)
                if asin:
                    asin_categories.setdefault(asin, set()).add(name)
            if progress_callback is not None:
                progress_callback(
                    "listing_products",
                    len(asin_categories),
                    0,
                    f"{len(asin_categories):,} unique Amazon ASINs discovered",
                )

        for configured in self.products_config:
            if isinstance(configured, Mapping):
                value = _text(
                    configured.get("url") or configured.get("asin")
                )
                labels = [
                    _text(configured.get("category"))
                ] if configured.get("category") else []
            else:
                value, labels = _text(configured), []
            asin = _asin(value)
            if asin:
                asin_categories.setdefault(asin, set()).update(labels)

        processed = {str(value) for value in processed_asins}
        queue = list(asin_categories)
        queued = set(queue)
        parent_by_asin = {asin: asin for asin in queue}
        products: list[Product] = []
        failed: list[str] = []
        completed_now = 0
        index = 0
        while index < len(queue):
            asin = queue[index]
            index += 1
            if asin in processed:
                continue
            labels = sorted(asin_categories.get(asin, set()))
            try:
                product, variants = self.fetch_product(
                    asin,
                    labels,
                    parent_asin=parent_by_asin.get(asin, asin),
                )
                if (
                    self.brand_filter
                    and brand_key(product.brand) not in self.brand_filter
                ):
                    processed.add(asin)
                    continue
                products.append(product)
                processed.add(asin)
                completed_now += 1
                if on_product is not None:
                    on_product(product)
                for variant_asin in variants:
                    asin_categories.setdefault(variant_asin, set()).update(
                        labels
                    )
                    if variant_asin not in queued:
                        queued.add(variant_asin)
                        parent_by_asin[variant_asin] = parent_by_asin.get(
                            asin,
                            asin,
                        )
                        queue.append(variant_asin)
            except Exception as exc:
                self.detail_failures += 1
                failed.append(asin)
                self.logger.error(
                    "amazon_product asin=%s failed=%s",
                    asin,
                    exc,
                )
            if progress_callback is not None:
                progress_callback(
                    "details",
                    completed_now,
                    len(queue),
                    (
                        f"Amazon product pages: {completed_now:,}/"
                        f"{len(queue):,} completed"
                    ),
                )
                progress_callback(
                    "sku_rows",
                    completed_now,
                    len(queue),
                    f"{completed_now:,} Amazon ASIN rows ready",
                )

        return AmazonScrapeResult(
            products=products,
            discovered_asins=len(queued),
            processed_asins=completed_now,
            failed_asins=tuple(failed),
            completed=not discovery_failed and not failed,
        )
