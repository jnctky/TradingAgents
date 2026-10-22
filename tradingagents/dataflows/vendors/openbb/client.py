"""OpenBB Platform (local REST) as a TradingAgents data vendor.

Talks to the local OpenBB API (default ``http://127.0.0.1:6900``) instead of
importing the SDK, so this vendor needs no extra dependencies beyond
``requests``.  Errors follow the shared vendor contract: empty results raise
``NoMarketDataError`` (loud, no silent fabrication), connection/HTTP failures
raise ``VendorUnavailableError`` (router falls through to the next vendor).
"""

import logging
import os
import time
from typing import Any

import requests

from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorUnavailableError,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:6900"
# Provider choice for every endpoint this vendor serves.  The local platform
# ships the keyless providers; nasdaq covers US equities for prices,
# fundamentals and news.
PROVIDER = "nasdaq"

TIMEOUT = float(os.getenv("OPENBB_HTTP_TIMEOUT", "60"))

# A busy platform occasionally answers with an empty body or drops the
# connection mid-response. Treating that as "vendor down" pushed every call to
# the yfinance fallback — which is rate limited — so a transient blip surfaced
# as DATA_UNAVAILABLE. Retrying in place keeps the primary vendor primary.
RETRIES = int(os.getenv("OPENBB_HTTP_RETRIES", "2"))
RETRY_BACKOFF = float(os.getenv("OPENBB_HTTP_RETRY_BACKOFF", "1.5"))


def base_url() -> str:
    return os.getenv("OPENBB_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _raise_unavailable(what: str, exc: Exception) -> VendorUnavailableError:
    return VendorUnavailableError(f"OpenBB Platform request failed for {what}: {exc}")


def get_json(path: str, what: str, params: dict[str, Any] | None = None,
             json_body: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """GET/POST an OpenBB route and return ``results`` (empty list if absent).

    Transport errors, 5xx answers and unparsable bodies are retried with a short
    backoff; a 404 means the symbol has no data on that route and is final.
    """
    url = f"{base_url()}{path}"
    last_exc: Exception | None = None

    for attempt in range(RETRIES + 1):
        retryable = True
        try:
            if json_body is not None:
                resp = requests.post(url, json=json_body, timeout=TIMEOUT,
                                     headers={"Content-Type": "application/json"})
            else:
                resp = requests.get(url, params=params or {}, timeout=TIMEOUT)
        except requests.RequestException as exc:
            last_exc = exc
        else:
            if resp.status_code == 404:
                # OpenBB answers 404 when a symbol has no data on a route.
                raise NoMarketDataError(what, detail=f"OpenBB returned 404 for {path}")
            if resp.status_code >= 500:
                last_exc = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            elif resp.status_code >= 400:
                # Client error (bad params, unknown route): retrying cannot help.
                raise _raise_unavailable(what, RuntimeError(
                    f"HTTP {resp.status_code}: {resp.text[:200]}"))
            else:
                try:
                    payload = resp.json()
                except ValueError as exc:
                    last_exc = ValueError(
                        f"empty/invalid body ({len(resp.content)} bytes) from {path}: {exc}")
                else:
                    results = payload.get("results") or []
                    if not isinstance(results, list):
                        raise _raise_unavailable(
                            what, RuntimeError(
                                f"unexpected payload type {type(results).__name__}"))
                    return results

        if attempt < RETRIES:
            logger.warning("%s: attempt %d/%d failed (%s); retrying in %.1fs",
                           what, attempt + 1, RETRIES + 1, last_exc, RETRY_BACKOFF)
            time.sleep(RETRY_BACKOFF * (attempt + 1))

    raise _raise_unavailable(what, last_exc if last_exc else RuntimeError("unknown error"))


def assert_rows(symbol: str, rows: list, what: str) -> list:
    """Typed error on empty results so the router never sees silent emptiness."""
    if not rows:
        raise NoMarketDataError(symbol, detail=f"OpenBB returned no rows for {what}")
    return rows
