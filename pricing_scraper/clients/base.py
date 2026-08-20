"""Shared HTTP behavior for retailer JSON API clients."""

from __future__ import annotations

import json
import logging
import random
import re
import shlex
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from html.parser import HTMLParser

import requests


class ConfigurationError(ValueError):
    """Raised when required scraper configuration is missing or invalid."""


class RequestFailed(RuntimeError):
    """Raised after a request exhausts its configured retry budget."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        attempts: int = 0,
        response_text: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.attempts = attempts
        self.response_text = response_text


@dataclass(slots=True)
class RequestSpec:
    """HTTP request details parsed from a DevTools cURL command."""

    method: str
    url: str
    headers: dict[str, str]
    body: str | None = None


def parse_curl_command(command: str) -> RequestSpec:
    """Convert a Chrome "Copy as cURL (bash)" command into a request spec.

    Header names and values are copied without normalization. Duplicate header
    names are rejected because ``requests.Session`` cannot represent them
    faithfully in a normal header mapping.
    """

    raw = command.strip()
    if not raw or raw.startswith("<PASTE "):
        raise ConfigurationError(
            "Nykaa cURL is missing. Paste the complete DevTools cURL command "
            "into nykaa.curl_command in config.yaml."
        )
    try:
        tokens = shlex.split(raw.replace("\\\n", " "), posix=True)
    except ValueError as exc:
        raise ConfigurationError(f"Could not parse cURL command: {exc}") from exc
    if not tokens or Path(tokens[0]).name.lower() not in {"curl", "curl.exe"}:
        raise ConfigurationError("curl_command must begin with curl or curl.exe.")

    method = "GET"
    url = ""
    headers: dict[str, str] = {}
    header_names: set[str] = set()
    body: str | None = None
    index = 1

    def add_header(name: str, value: str) -> None:
        lowered = name.casefold()
        if lowered in header_names:
            raise ConfigurationError(
                f"Duplicate header {name!r} cannot be preserved by requests."
            )
        header_names.add(lowered)
        headers[name] = value[1:] if value.startswith(" ") else value

    def next_value(option: str) -> str:
        nonlocal index
        index += 1
        if index >= len(tokens):
            raise ConfigurationError(f"{option} is missing its value.")
        return tokens[index]

    while index < len(tokens):
        token = tokens[index]
        if token in {"-H", "--header"}:
            header = next_value(token)
            if ":" not in header:
                raise ConfigurationError(f"Invalid cURL header: {header!r}")
            name, value = header.split(":", 1)
            add_header(name, value)
        elif token.startswith("--header="):
            header = token.split("=", 1)[1]
            if ":" not in header:
                raise ConfigurationError(f"Invalid cURL header: {header!r}")
            name, value = header.split(":", 1)
            add_header(name, value)
        elif token in {"-X", "--request"}:
            method = next_value(token).upper()
        elif token.startswith("--request="):
            method = token.split("=", 1)[1].upper()
        elif token in {"--data", "--data-raw", "--data-binary", "-d"}:
            body = next_value(token)
            if method == "GET":
                method = "POST"
        elif any(
            token.startswith(prefix)
            for prefix in ("--data=", "--data-raw=", "--data-binary=")
        ):
            body = token.split("=", 1)[1]
            if method == "GET":
                method = "POST"
        elif token in {"-A", "--user-agent"}:
            add_header("User-Agent", next_value(token))
        elif token in {"-b", "--cookie"}:
            add_header("Cookie", next_value(token))
        elif token == "--url":
            url = next_value(token)
        elif token.startswith("--url="):
            url = token.split("=", 1)[1]
        elif token.startswith(("http://", "https://")) and not url:
            url = token
        # Flags such as --compressed and --location do not alter request data.
        index += 1

    if not url:
        raise ConfigurationError("No HTTP(S) URL was found in curl_command.")
    if not url.startswith(("https://", "http://")):
        raise ConfigurationError("The cURL URL must be HTTP or HTTPS.")
    return RequestSpec(method=method, url=url, headers=headers, body=body)


class RequestRateLimiter:
    """Thread-safe rolling-window requests-per-minute limiter."""

    def __init__(
        self,
        max_requests_per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        if max_requests_per_minute < 1:
            raise ConfigurationError("max_requests_per_minute must be at least 1.")
        self.maximum = max_requests_per_minute
        self.clock = clock
        self.sleeper = sleeper
        self._request_times: deque[float] = deque()
        self._lock = threading.Lock()

    def wait(self) -> None:
        """Block until the next request fits inside the rolling minute."""
        with self._lock:
            now = self.clock()
            while self._request_times and now - self._request_times[0] >= 60:
                self._request_times.popleft()
            if len(self._request_times) >= self.maximum:
                delay = max(0.0, 60 - (now - self._request_times[0]))
                if delay:
                    self.sleeper(delay)
                now = self.clock()
                while self._request_times and now - self._request_times[0] >= 60:
                    self._request_times.popleft()
            self._request_times.append(self.clock())


# How much of the request log reaches the console. The file always gets
# everything; the console is a different audience. A foreground run prints two
# lines per request, which at the rate limit is sixty lines a minute and buries
# the progress the operator is actually watching for. The CLI lowers this to
# WARNING so only real problems interrupt, and restores INFO with --verbose.
# The detached worker leaves it at INFO, because its console *is* the run log
# the dashboard reads back.
CONSOLE_LOG_LEVEL = logging.INFO


def set_console_log_level(level: int) -> None:
    """Set how verbose the console is, for loggers built from now on and already."""
    global CONSOLE_LOG_LEVEL
    CONSOLE_LOG_LEVEL = level
    for logger in logging.Logger.manager.loggerDict.values():
        if not isinstance(logger, logging.Logger):
            continue
        for handler in logger.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(
                handler, logging.FileHandler
            ):
                handler.setLevel(level)


def build_logger(name: str, logs_dir: Path) -> logging.Logger:
    """Create an idempotent console and file logger."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
        )
        file_handler = logging.FileHandler(
            logs_dir / "scraper.log", encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.setLevel(CONSOLE_LOG_LEVEL)
        logger.addHandler(file_handler)
        logger.addHandler(console_handler)
    return logger



def _decoded_body(response: requests.Response) -> str:
    """Decode a page body, defaulting to UTF-8 rather than ISO-8859-1.

    When a text/* response carries no charset, RFC 2616 says to assume
    ISO-8859-1 and requests obeys it. Indian storefronts serve UTF-8 without
    declaring it, so that default turns every non-ASCII character into
    mojibake - a product name came back as "WishCareA Rosemary Essential OilA"
    where the site shows a non-breaking space. The declared charset is still
    honoured whenever the server actually sends one.
    """
    declared = "charset=" in response.headers.get("Content-Type", "").casefold()
    if not declared:
        return response.content.decode("utf-8", errors="replace")
    return response.text


class BaseJsonClient:
    """A rate-limited requests client with block detection and retries."""

    RETRYABLE_STATUSES = {403, 429, 500, 501, 502, 503, 504}
    SOFT_BLOCK_MARKERS = ("captcha", "access denied")

    def __init__(
        self,
        request_config: Mapping[str, Any],
        headers: Mapping[str, str],
        *,
        session: requests.Session | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        random_uniform: Callable[[float, float], float] = random.uniform,
        clock: Callable[[], float] = time.monotonic,
        logger: logging.Logger | None = None,
        rate_limiter: RequestRateLimiter | None = None,
    ):
        self.timeout_seconds = float(request_config.get("timeout_seconds", 35))
        self.delay_min = float(request_config.get("delay_min_seconds", 2.0))
        self.delay_max = float(request_config.get("delay_max_seconds", 5.0))
        self.max_retries = int(request_config.get("max_retries", 4))
        self.backoff_base = float(request_config.get("backoff_base_seconds", 2.0))
        self.backoff_max = float(request_config.get("backoff_max_seconds", 45.0))
        self.soft_block_backoff = float(
            request_config.get("soft_block_backoff_seconds", 60.0)
        )
        if self.delay_min < 0 or self.delay_max < self.delay_min:
            raise ConfigurationError("Invalid request delay range.")
        if self.max_retries < 0:
            raise ConfigurationError("max_retries cannot be negative.")

        self.sleeper = sleeper
        self.random_uniform = random_uniform
        self.session = session or requests.Session()
        self.session.headers.clear()
        self.session.headers.update(dict(headers))
        self.logs_dir = Path(str(request_config.get("logs_dir", "logs")))
        self.failures_dir = self.logs_dir / "failures"
        self.failures_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logger or build_logger(
            f"pricing_scraper.{self.__class__.__name__.lower()}", self.logs_dir
        )
        self.rate_limiter = rate_limiter or RequestRateLimiter(
            int(request_config.get("max_requests_per_minute", 12)),
            clock=clock,
            sleeper=sleeper,
        )
        self.requests_made = 0
        self.failures = 0
        self.blocks_encountered = 0

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self.session.close()

    def __enter__(self) -> "BaseJsonClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _backoff(self, attempt: int) -> float:
        base = min(self.backoff_max, self.backoff_base * (2**attempt))
        return min(self.backoff_max, base + self.random_uniform(0, max(0.1, base / 2)))

    def _dump_failure(self, body: str, label: str) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        safe_label = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in label
        )[:80]
        path = self.failures_dir / f"{timestamp}_{safe_label}.txt"
        path.write_text(body, encoding="utf-8", errors="replace")
        return path

    @classmethod
    def _soft_block_reason(
        cls, response: requests.Response, *, expect_html: bool = False
    ) -> str:
        sample = response.text[:10000].casefold()
        marker = next(
            (value for value in cls.SOFT_BLOCK_MARKERS if value in sample), ""
        )
        if marker:
            return f"body contains {marker!r}"
        if expect_html:
            # A page request wants HTML, so receiving it is the success case,
            # not a block. Only the markers above mean anything here.
            return ""
        content_type = response.headers.get("Content-Type", "").casefold()
        stripped = sample.lstrip()
        # Some retailer JSON APIs incorrectly respond with text/html. A body
        # that actually begins as JSON must still be passed to response.json().
        if stripped.startswith(("{", "[")):
            return ""
        if "text/html" in content_type or stripped.startswith(
            ("<!doctype html", "<html")
        ):
            return "HTML returned where JSON was expected"
        return ""

    def request_json(
        self,
        method: str,
        url: str,
        *,
        data: str | bytes | None = None,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """Request and decode JSON, retrying transient errors and soft blocks."""
        return self._request(
            method,
            url,
            parse=lambda response: response.json(),
            parse_label="json_parse",
            data=data,
            json_body=json_body,
            headers=headers,
        )

    def request_text(
        self,
        method: str,
        url: str,
        *,
        data: str | bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> str:
        """Request a page body, with the same pacing and block handling.

        Not every storefront publishes a catalogue API. The ones that only
        answer in HTML still need the rate limiting, retry, and soft-block
        detection that the JSON path already implements, so both share one
        request loop rather than growing a second copy of it.
        """
        return self._request(
            method,
            url,
            parse=_decoded_body,
            parse_label="text_read",
            expect_html=True,
            data=data,
            headers=headers,
        )

    def _request(
        self,
        method: str,
        url: str,
        *,
        parse: Callable[[requests.Response], Any],
        parse_label: str,
        expect_html: bool = False,
        data: str | bytes | None = None,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        last_error = ""
        last_status_code: int | None = None
        last_response_text = ""
        attempts_made = 0
        for attempt in range(self.max_retries + 1):
            attempts_made = attempt + 1
            if self.delay_max:
                self.sleeper(self.random_uniform(self.delay_min, self.delay_max))
            self.rate_limiter.wait()
            response: requests.Response | None = None
            try:
                response = self.session.request(
                    method=method,
                    url=url,
                    data=data,
                    json=json_body,
                    headers=dict(headers) if headers else None,
                    timeout=self.timeout_seconds,
                )
                self.requests_made += 1
                size = len(response.content)
                last_status_code = response.status_code
                last_response_text = response.text[:1_500]
                self.logger.info(
                    "request url=%s status=%s bytes=%s attempt=%s",
                    response.url,
                    response.status_code,
                    size,
                    attempt + 1,
                )
            except requests.RequestException as exc:
                self.requests_made += 1
                last_error = str(exc)
                self.logger.warning(
                    "request_failed url=%s attempt=%s error=%s",
                    url,
                    attempt + 1,
                    last_error,
                )
                if attempt < self.max_retries:
                    self.sleeper(self._backoff(attempt))
                    continue
                break

            if response.status_code in self.RETRYABLE_STATUSES:
                if response.status_code in {403, 429}:
                    self.blocks_encountered += 1
                last_error = f"HTTP {response.status_code}"
                self.logger.warning(
                    "retryable_response url=%s status=%s parse=failure",
                    response.url,
                    response.status_code,
                )
                if attempt < self.max_retries:
                    self.sleeper(self._backoff(attempt))
                    continue
                break

            if not response.ok:
                last_error = f"HTTP {response.status_code}"
                path = self._dump_failure(response.text, f"http_{response.status_code}")
                self.logger.error(
                    "request_rejected url=%s status=%s parse=failure dump=%s",
                    response.url,
                    response.status_code,
                    path,
                )
                break

            soft_block = self._soft_block_reason(
                response, expect_html=expect_html
            )
            if soft_block:
                self.blocks_encountered += 1
                last_error = soft_block
                path = self._dump_failure(response.text, "soft_block")
                self.logger.warning(
                    "soft_block url=%s status=%s reason=%s dump=%s",
                    response.url,
                    response.status_code,
                    soft_block,
                    path,
                )
                if attempt < self.max_retries:
                    self.sleeper(self.soft_block_backoff)
                    continue
                break

            try:
                payload = parse(response)
            except ValueError as exc:
                # requests.JSONDecodeError and json.JSONDecodeError are both
                # ValueError subclasses, so this still covers what the JSON
                # path caught before the two request kinds were merged.
                last_error = f"{parse_label} failed: {exc}"
                path = self._dump_failure(response.text, parse_label)
                self.logger.error(
                    "response url=%s status=%s bytes=%s parse=failure dump=%s",
                    response.url,
                    response.status_code,
                    len(response.content),
                    path,
                )
                if attempt < self.max_retries:
                    self.sleeper(self._backoff(attempt))
                    continue
                break

            self.logger.info(
                "response url=%s status=%s bytes=%s parse=success",
                response.url,
                response.status_code,
                len(response.content),
            )
            return payload

        self.failures += 1
        raise RequestFailed(
            f"Request failed after {attempts_made} attempt(s): "
            f"{url} ({last_error or 'unknown error'})",
            status_code=last_status_code,
            attempts=attempts_made,
            response_text=last_response_text,
        )


class _TextExtractor(HTMLParser):
    """Collect readable text from a markup fragment, dropping scripts."""

    SKIP = {"script", "style", "noscript"}
    BREAK_AFTER = {"p", "br", "li", "div", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag.casefold() in self.SKIP:
            self._skipping += 1

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered in self.SKIP and self._skipping:
            self._skipping -= 1
        if lowered in self.BREAK_AFTER:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if data and not self._skipping:
            self._parts.append(data)

    def text(self) -> str:
        joined = "".join(self._parts)
        lines = [line.strip() for line in joined.splitlines()]
        return "\n".join(line for line in lines if line).strip()


def html_to_text(markup: str) -> str:
    """Flatten a description fragment into plain text.

    Storefronts return descriptions as HTML. The catalogue stores a readable
    string, and a malformed fragment must not end a run, so a parse failure
    degrades to the markup with its tags stripped rather than raising.
    """
    if not markup:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # noqa: BLE001 - a bad fragment is not worth a failed run
        return re.sub(r"<[^>]+>", " ", markup).strip()
    return parser.text()


_LD_BLOCK = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def _is_product(node: Any) -> bool:
    """Does this schema.org node describe a product?

    The @type is written either bare ("Product") or as the full vocabulary URL
    ("http://schema.org/Product") depending on the storefront, and may be a
    list when a node claims several types at once.
    """
    if not isinstance(node, Mapping):
        return False
    declared = node.get("@type")
    values = declared if isinstance(declared, list) else [declared]
    return any(
        str(value or "").rstrip("/").rsplit("/", 1)[-1].casefold() == "product"
        for value in values
    )


def linked_product(markup: str) -> dict[str, Any]:
    """Return the schema.org Product block from a page, or an empty mapping.

    Structured data is the stable part of a storefront page: the markup around
    it is redesigned freely, but this block is maintained because search
    engines read it. Nodes can arrive bare, inside a list, or wrapped in an
    @graph, so all three are searched.
    """
    for raw in _LD_BLOCK.findall(markup or ""):
        try:
            payload = json.loads(raw.strip())
        except ValueError:
            continue
        queue = payload if isinstance(payload, list) else [payload]
        while queue:
            node = queue.pop(0)
            if isinstance(node, Mapping) and isinstance(node.get("@graph"), list):
                queue.extend(node["@graph"])
            if _is_product(node):
                return dict(node)
    return {}


def first_offer(node: Mapping[str, Any]) -> dict[str, Any]:
    """Return a product's offer, whether it is given singly or as a list."""
    offers = node.get("offers")
    if isinstance(offers, list):
        for offer in offers:
            if isinstance(offer, Mapping):
                return dict(offer)
        return {}
    return dict(offers) if isinstance(offers, Mapping) else {}
