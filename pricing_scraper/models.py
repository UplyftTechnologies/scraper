"""Shared data models for normalized retailer products."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

GTIN_LENGTHS = (8, 12, 13, 14)


def brand_key(value: Any) -> str:
    """Fold a brand name into a comparable key for the configured filter.

    Retailers punctuate and capitalize brands differently (``d'Alba`` versus
    ``dAlba``, ``e.l.f.`` versus ``ELF``), so a filter entry copied from one
    storefront still matches the same brand on another.
    """
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def normalize_gtin(value: Any) -> str:
    """Return a retailer barcode as a bare GTIN-8/12/13/14, or an empty string.

    Retailers publish EAN/UPC values under several names and occasionally with
    separators or an internal item code in the same field. Only digit strings
    of a valid GTIN length whose GS1 mod-10 check digit matches are kept, so a
    seller SKU that merely looks numeric is never exported as a barcode.
    """
    digits = "".join(
        character
        for character in str(value or "")
        if character.isdigit()
    )
    if len(digits) not in GTIN_LENGTHS:
        return ""
    body = [int(character) for character in digits[:-1]]
    total = sum(
        digit * weight
        for digit, weight in zip(reversed(body), (3, 1) * len(body))
    )
    if (10 - total % 10) % 10 != int(digits[-1]):
        return ""
    return digits



def plausible_retail_barcode(gtin: str) -> bool:
    """Reject GS1 prefixes that are never a manufacturer's retail barcode.

    A store's internal identifier can carry a valid check digit and still not
    be a barcode. Purplle's ``master_product_id`` is the clearest case: values
    like ``9991308610002`` validate perfectly but sit in a range GS1 reserves
    for coupons and refund receipts, where a genuine EAN such as
    ``8906118410545`` (890 = India) does not.

    This matters beyond one field being wrong. The GTIN supervisor copies a
    barcode from the retailer that has one to the retailers that do not, so an
    internal code accepted here would be propagated across the catalogue.
    """
    if not gtin:
        return False
    prefix = int(gtin[-13:].zfill(13)[:3])
    restricted = (
        20 <= prefix <= 29        # restricted circulation within a company
        or 40 <= prefix <= 49     # restricted circulation within a region
        or 50 <= prefix <= 59     # coupons
        or 200 <= prefix <= 299   # in-store / variable measure
        or prefix >= 980          # refund receipts, coupons, ISSN/ISMN
    )
    return not restricted


@dataclass(slots=True)
class Product:
    """A normalized product/SKU observation from one retailer."""

    site: str
    product_id: str
    brand: str
    product_name: str
    categories: list[str] = field(default_factory=list)
    source_categories: list[str] = field(default_factory=list)
    parent_product_id: str = ""
    sku: str = ""
    gtin: str = ""
    variant: str = ""
    mrp: float | None = None
    selling_price: float | None = None
    discount_pct: float | None = None
    rating: float | None = None
    rating_count: int | None = None
    in_stock: bool | None = None
    product_url: str = ""
    image_url: str = ""
    image_urls: list[str] = field(default_factory=list)
    description: str = ""
    description_html: str = ""
    ingredients: str = ""
    key_ingredients: list[str] = field(default_factory=list)
    how_to_use: str = ""
    key_features: list[str] = field(default_factory=list)
    special_features: list[str] = field(default_factory=list)
    product_attributes: dict[str, Any] = field(default_factory=dict)
    review_count: int | None = None
    rating_breakdown: list[dict[str, Any]] = field(default_factory=list)
    top_reviews: list[dict[str, Any]] = field(default_factory=list)
    scraped_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a serialization-friendly dictionary."""
        return asdict(self)
