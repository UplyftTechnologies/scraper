"""Retailer clients for the config-driven pricing scraper."""

from typing import Any

from .broadway import BroadwayClient
from .kindlife import KindlifeClient
from .nykaa import NykaaClient
from .purplle import PurplleClient
from .tira import TiraClient

__all__ = [
    "AmazonClient",
    "BroadwayClient",
    "KindlifeClient",
    "NykaaClient",
    "PurplleClient",
    "TiraClient",
]


def __getattr__(name: str) -> Any:
    """Import the Playwright-backed Amazon client only when it is requested.

    Every other client speaks HTTP, and the hosted dashboard runs without
    Playwright installed, so importing this package must not pull the browser
    dependency in with it.
    """
    if name == "AmazonClient":
        from .amazon import AmazonClient

        return AmazonClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
