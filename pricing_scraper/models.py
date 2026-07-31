"""Shared data models for normalized retailer products."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


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
