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
from typing import Annotated

import pandas as pd
from dateutil.relativedelta import relativedelta

from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError

from ._indicator_window import assert_supported, indicator_values, render_window
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


#: How the window text and the OHLCV header name this vendor.
SOURCE_NOTE = "Binance spot klines (USDT pair)."


def daily_bars(symbol: str, start: str, end: str) -> pd.DataFrame:
    """OHLCV frame in the shape every price vendor produces.

    Indexed by ``Date`` with lower-case ``open/high/low/close/volume`` columns,
    which is what the shared indicator module and the yahoo-shaped loaders
    expect.

    Raises
    ------
    VendorUnavailableError
        The proxy is down or Binance refused the request.
    NoMarketDataError
        Binance answered but the symbol was never mapped to a pair, or the
        window is empty.
    """
    base = base_symbol(symbol)
    pair = QUOTE_SYMBOL.get(base or "")

    if not pair:
        raise NoMarketDataError(
            symbol, symbol,
            f"no Binance USDT pair is mapped for '{base or symbol}' — add it to "
            f"QUOTE_SYMBOL rather than guessing one")

    query = urllib.parse.urlencode({
        "symbol": pair,
        "interval": "1d",
        "startTime": _ms(start),
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
    except Exception as exc:  # noqa: BLE001 — the caller decides how to degrade
        raise VendorUnavailableError(f"Binance klines unreachable for {pair}: {exc}") from exc

    bars: list[dict] = []
    for row in rows if isinstance(rows, list) else []:
        try:
            # [openTime, open, high, low, close, volume, closeTime, ...]
            day = datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc).date()
            bars.append({
                "Date": pd.Timestamp(day),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            })
        except (IndexError, TypeError, ValueError):
            continue

    if not bars:
        raise NoMarketDataError(symbol, symbol, pair,
                                f"Binance returned no daily bars between {start} and {end}")

    frame = pd.DataFrame(bars).set_index("Date").sort_index()
    # USDT is a 24/7 market, so a weekend row is real data — never drop it.
    return frame[~frame.index.duplicated(keep="last")]


def load_ohlcv(symbol: str, as_of_date: str, fill_gaps: bool = True) -> pd.DataFrame:
    """OHLCV frame on or before ``as_of_date``, shaped like the sidecar loader.

    Settlement, the verification snapshot and the report charts all read prices
    through :mod:`tradingagents.dataflows.vendors.ohlcv`, which asked only the
    equity vendors — so a coin's settling price was reported "unavailable" even
    when the Binance path upstream was healthy. Same contract as
    ``sidecar.market.load_ohlcv``: a ``Date`` column plus Open/High/Low/Close/
    Adj Close/Volume, forward-filled when asked.
    """
    start = (pd.Timestamp(as_of_date) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
    frame = daily_bars(symbol, start, as_of_date)

    out = frame.rename(columns={"open": "Open", "high": "High", "low": "Low",
                                "close": "Close", "volume": "Volume"})
    out["Adj Close"] = out["Close"]
    if fill_gaps:
        # A coin trades every day, so this only matters when Binance skipped a
        # candle; still, the callers assume no holes.
        out[["Open", "High", "Low", "Close", "Adj Close"]] = (
            out[["Open", "High", "Low", "Close", "Adj Close"]].ffill())
    out.index.name = "Date"
    return out.reset_index()


def get_stock_data(
    symbol: Annotated[str, "ticker symbol of the coin"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """Daily OHLCV history from Binance spot, shaped like the equity vendors."""
    datetime.strptime(start_date, "%Y-%m-%d")
    datetime.strptime(end_date, "%Y-%m-%d")

    data = daily_bars(symbol, start_date, end_date)
    out = data.rename(columns={"open": "Open", "high": "High", "low": "Low",
                               "close": "Close", "volume": "Volume"})

    header = f"# Crypto data for {symbol.upper()} from {start_date} to {end_date}\n"
    header += f"# Source: Binance spot {QUOTE_SYMBOL.get(base_symbol(symbol) or '', 'USDT')} daily klines (UTC days, 24/7 market)\n"
    header += f"# Total records: {len(out)}\n\n"
    return header + out.to_csv(index_label="Date")


def get_stock_stats_indicators_window(
    symbol: Annotated[str, "ticker symbol of the coin"],
    indicator: Annotated[str, "technical indicator to get the analysis and report of"],
    as_of_date: Annotated[str, "The current trading date you are trading on, YYYY-mm-dd"],
    look_back_days: Annotated[int, "how many days to look back"],
) -> str:
    """Technical indicator history computed locally from Binance bars."""
    assert_supported(indicator)

    as_of_dt = datetime.strptime(as_of_date, "%Y-%m-%d")
    # 400 extra days so a 200-day average is populated from the first reported day.
    fetch_start = (as_of_dt - relativedelta(days=400)).strftime("%Y-%m-%d")

    values = indicator_values(daily_bars(symbol, fetch_start, as_of_date), indicator)

    return render_window(values, indicator, as_of_date, look_back_days, SOURCE_NOTE)
