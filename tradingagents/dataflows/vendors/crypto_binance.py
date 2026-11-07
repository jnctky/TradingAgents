"""Daily crypto bars from Binance's public kline endpoint.

Why this exists: the akshare sidecar can only reach CME bitcoin futures from
mainland China — every offshore exchange plus CoinGecko is RST on a direct
connection, so ``CryptoHistorical`` answers for ``BTC`` and returns 400 for
ETH/BNB. That made the portfolio pipeline's crypto coverage one coin deep.

Binance's REST API *is* reachable through the user's local proxy (verified:
200 in ~0.5 s via 10808), so the nightly batch reads its crypto bars here and
falls back to the sidecar only when the proxy is down.

No API key: ``/api/v3/klines`` is public, rate-limited by IP. One call per
symbol per run stays far below the limit.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone

from ._net import opener

logger = logging.getLogger(__name__)

BINANCE_BASE = "https://api.binance.com/api/v3/klines"
REQUEST_TIMEOUT = 20

# Watchlist symbols that are quoted against USDT on Binance. A symbol missing
# here is not "no data", it is "we have not mapped it yet" — the caller falls
# back to the sidecar and, failing that, skips the coin honestly.
QUOTE_SYMBOL = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "BNB": "BNBUSDT",
    "SOL": "SOLUSDT",
    "XRP": "XRPUSDT",
    "ADA": "ADAUSDT",
    "DOGE": "DOGEUSDT",
    "LTC": "LTCUSDT",
    "BCH": "BCHUSDT",
    "DOT": "DOTUSDT",
    "AVAX": "AVAXUSDT",
    "LINK": "LINKUSDT",
}


def base_symbol(raw: str) -> str | None:
    """``BTC-USD`` / ``BTCUSDT`` / ``btc`` -> ``BTC``; unknown shapes -> None."""
    if not isinstance(raw, str):
        return None
    compact = raw.strip().upper().replace("-", "").replace("/", "").replace("_", "")
    for quote in ("USDT", "USDC", "USD"):
        if compact.endswith(quote):
            compact = compact[: -len(quote)]
            break
    return compact or None


def daily_closes(symbol: str, start: str, end: str) -> list[tuple[str, float]]:
    """Ascending ``(YYYY-MM-DD, close)`` bars in ``[start, end]``, or ``[]``.

    Never raises: a dead proxy or a delisted pair returns an empty list so the
    batch can fall back to another source instead of losing the whole run.
    """
    base = base_symbol(symbol)
    pair = QUOTE_SYMBOL.get(base or "")
    if not pair:
        return []

    query = urllib.parse.urlencode({
        "symbol": pair,
        "interval": "1d",
        "startTime": _ms(start),
        # Binance treats endTime as inclusive-down-to-the-millisecond; one day
        # past the requested close keeps the final bar while dropping later ones.
        "endTime": _ms(end) + 86_400_000 - 1,
        "limit": 1000,
    })
    request = urllib.request.Request(
        f"{BINANCE_BASE}?{query}",
        headers={"User-Agent": "TradingAgents/1.0", "Accept": "application/json"},
    )
    try:
        with opener().open(request, timeout=REQUEST_TIMEOUT) as response:
            rows = json.loads(response.read().decode("utf-8", "replace"))
    except Exception as exc:                       # noqa: BLE001 — see docstring
        logger.warning("Binance klines failed for %s: %s", pair, exc)
        return []

    out: list[tuple[str, float]] = []
    for row in rows if isinstance(rows, list) else []:
        try:
            # [openTime, open, high, low, close, volume, closeTime, ...]
            day = datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc).date()
            out.append((day.isoformat(), float(row[4])))
        except (IndexError, TypeError, ValueError):
            continue
    out.sort(key=lambda item: item[0])
    return out


def _ms(day: str) -> int:
    """Midnight UTC of ``day`` in epoch milliseconds."""
    parsed = date.fromisoformat(day[:10])
    return int(datetime(parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc).timestamp() * 1000)
