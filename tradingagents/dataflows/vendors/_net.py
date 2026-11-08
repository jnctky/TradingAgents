"""Outbound-network helpers for vendors that live behind the mainland firewall.

StockTwits, Reddit and Polymarket's Gamma API are blocked or challenged from
mainland China on a direct connection, so these vendors dial the user's local
proxy explicitly instead of relying on ambient proxy variables (the WorkBuddy
session injects its own proxy on 55xxx/64xxx, which these hosts reject, and a
plain terminal has none set at all).

Override the proxy with ``TRADINGAGENTS_SENTIMENT_PROXY``; set it to an empty
string to force a direct connection.
"""

from __future__ import annotations

import os
from urllib.request import ProxyHandler, build_opener

DEFAULT_SENTIMENT_PROXY = "http://127.0.0.1:10808"


def sentiment_proxy() -> str | None:
    raw = os.getenv("TRADINGAGENTS_SENTIMENT_PROXY", DEFAULT_SENTIMENT_PROXY)
    return raw.strip() or None


def opener():
    """A urllib opener pinned to the configured proxy (direct when unset)."""
    proxy = sentiment_proxy()
    handler = ProxyHandler({"http": proxy, "https": proxy}) if proxy else ProxyHandler({})
    return build_opener(handler)


def requests_proxies() -> dict[str, str] | None:
    """The same proxy as a ``requests`` argument; None means direct."""
    proxy = sentiment_proxy()
    return {"http": proxy, "https": proxy} if proxy else None
