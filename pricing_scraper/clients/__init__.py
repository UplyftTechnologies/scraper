"""Retailer clients for the config-driven pricing scraper."""

from typing import Any

from .nykaa import NykaaClient
from .tira import TiraClient

__all__ = ["AmazonClient", "NykaaClient", "TiraClient"]


def __getattr__(name: str) -> Any:
    """Import the Playwright-backed Amazon client only when it is requested.

    Nykaa, Tira, and the hosted dashboard run without Playwright installed, so
    importing this package must not pull the browser dependency in with it.
    """
    if name == "AmazonClient":
        from .amazon import AmazonClient

        return AmazonClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
