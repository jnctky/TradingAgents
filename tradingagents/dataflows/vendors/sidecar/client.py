"""akshare sidecar (local FastAPI, default :47995) HTTP client.

The sidecar is the domestic data path: Tencent for quotes/klines, Sina and
Eastmoney for statements/news. It runs without a proxy, so every request here
must bypass the session proxy explicitly — routing 127.0.0.1 through a proxy is
the classic local-service failure on this machine.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import requests

from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:47995"
TIMEOUT = 30
RETRIES = 2

# Local traffic never goes through the session proxy (10808 / 55xxx).
_NO_PROXY = {"http": None, "https": None}


def base_url() -> str:
    return os.getenv("TRADINGAGENTS_SIDECAR_URL", DEFAULT_BASE_URL).rstrip("/")


def get_rows(model: str, what: str, params: dict[str, Any] | None = None) -> list[dict]:
    """GET /api/<model> and return its ``rows`` list.

    Raises ``VendorUnavailableError`` when the sidecar is down or keeps failing,
    and ``NoMarketDataError`` when it answers with no rows.
    """
    url = f"{base_url()}/api/{model}"
    last: Exception | None = None

    for attempt in range(RETRIES + 1):
        try:
            resp = requests.get(url, params=params or {}, timeout=TIMEOUT,
                                proxies=_NO_PROXY)
            if resp.status_code >= 500:
                raise VendorUnavailableError(
                    f"sidecar {model} returned HTTP {resp.status_code}")
            if resp.status_code >= 400:
                raise VendorUnavailableError(
                    f"sidecar {model} rejected the request: HTTP {resp.status_code}")
            payload = resp.json()
            rows = payload.get("rows") if isinstance(payload, dict) else None
            return list(rows or [])
        except VendorUnavailableError:
            raise
        except (requests.RequestException, ValueError) as exc:
            last = exc
            logger.debug("sidecar %s attempt %s failed: %s", model, attempt + 1, exc)

    raise VendorUnavailableError(
        f"akshare sidecar unreachable at {base_url()} while fetching {what}: {last}")


def assert_rows(symbol: str, rows: list, what: str) -> list:
    if not rows:
        raise NoMarketDataError(symbol, symbol, f"sidecar returned no {what}")
    return rows
