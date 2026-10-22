"""OpenBB: daily OHLCV history as a yfinance-shaped frame.

Both the public ``get_stock_data`` tool and the deterministic verification
snapshot read prices through this module, so an analysis can never verify a
number against a different price series than the one it reasoned about.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime

import pandas as pd

from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.symbols import normalize_symbol

from .client import PROVIDER, assert_rows, get_json

logger = logging.getLogger(__name__)

# A vendor's latest row this many calendar days before the requested date is
# treated as stale. Mirrors the yahoo loader so both vendors reject the
# year-old frames that would otherwise feed wrong prices into indicators.
MAX_OHLCV_STALE_DAYS = 10

# Indicators each re-read the same price window (a 200-SMA needs ~350 days of
# warm-up), and the snapshot re-reads 5 years. Serving those repeats from a
# short-lived cache cuts the platform's request volume by an order of magnitude,
# which is what kept it from dropping responses mid-analysis.
CACHE_TTL = float(os.getenv("OPENBB_OHLCV_CACHE_TTL", "180"))
_CACHE: dict[tuple[str, str, str], tuple[float, pd.DataFrame]] = {}
_CACHE_MAX_ENTRIES = 64


def _cached(key: tuple[str, str, str]) -> pd.DataFrame | None:
    hit = _CACHE.get(key)
    if hit is None:
        return None
    stamped, frame = hit
    if time.monotonic() - stamped > CACHE_TTL:
        _CACHE.pop(key, None)
        return None
    return frame.copy()  # callers reshape/mutate; never hand out the cached frame


def _store(key: tuple[str, str, str], frame: pd.DataFrame) -> None:
    if len(_CACHE) >= _CACHE_MAX_ENTRIES:
        oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
        _CACHE.pop(oldest, None)
    _CACHE[key] = (time.monotonic(), frame)


def clear_cache() -> None:
    """Drop cached frames (tests, or after the platform restarts)."""
    _CACHE.clear()


def fetch_ohlcv(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Daily OHLCV rows in ``[start_date, end_date]``, indexed by date.

    Columns match the yfinance shape (Open/High/Low/Close/Adj Close/Volume) so
    callers can switch vendors without reshaping. Identical windows inside
    ``CACHE_TTL`` are served from memory.
    """
    key = (symbol.upper(), start_date, end_date)
    cached = _cached(key)
    if cached is not None:
        return cached

    rows = get_json(
        "/api/v1/equity/price/historical",
        f"{symbol} OHLCV",
        params={
            "symbol": symbol,
            "provider": PROVIDER,
            "start_date": start_date,
            "end_date": end_date,
        },
    )
    assert_rows(symbol, rows, f"OHLCV rows between {start_date} and {end_date}")

    data = pd.DataFrame(rows)
    data["date"] = pd.to_datetime(data["date"]).dt.tz_localize(None)
    data = data.set_index("date").sort_index()
    # nasdaq rows carry adjusted prices already; expose the yfinance column set.
    data["Adj Close"] = data["close"]
    data = data.rename(
        columns={"open": "Open", "high": "High", "low": "Low",
                 "close": "Close", "volume": "Volume"}
    )
    keep = [c for c in ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
            if c in data.columns]
    data = data[keep].round(2)

    # Staleness guard, mirroring the yahoo vendor: the latest row must sit
    # near the requested end date, otherwise the answer would be silently old.
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    last = data.index.max()
    if last < pd.Timestamp(end_dt) - pd.Timedelta(days=MAX_OHLCV_STALE_DAYS):
        raise NoMarketDataError(
            symbol, detail=f"latest row is {last.date():%Y-%m-%d}, "
            f"more than {MAX_OHLCV_STALE_DAYS} days behind the requested end {end_date}"
        )
    _store(key, data)
    return data.copy()


def load_ohlcv(symbol: str, as_of_date: str, fill_gaps: bool = True) -> pd.DataFrame:
    """OHLCV on or before ``as_of_date``, yfinance-loader compatible.

    Drop-in replacement for ``vendors.yahoo.ohlcv.load_ohlcv``: same ``Date``
    column, same column set, same look-ahead filter, so the verification
    snapshot and the indicator path keep working unchanged.

    ``fill_gaps`` carries prices forward over gaps so indicators compute on a
    continuous series. Pass ``False`` to read the values as the vendor reported
    them, leaving a cell that was never reported empty.
    """
    as_of_dt = pd.to_datetime(as_of_date).normalize()
    start_str = (as_of_dt - pd.DateOffset(years=5)).strftime("%Y-%m-%d")

    data = fetch_ohlcv(symbol, start_str, as_of_dt.strftime("%Y-%m-%d"))

    frame = data.reset_index().rename(columns={"date": "Date"})
    frame["Date"] = pd.to_datetime(frame["Date"])
    # Defensive: this is a verification path, it must not trust its input to be
    # pre-filtered (the platform could return a bar past the requested date).
    frame = frame[frame["Date"] <= as_of_dt].sort_values("Date")
    if frame.empty:
        raise NoMarketDataError(
            symbol, normalize_symbol(symbol), f"no price rows on or before {as_of_date}"
        )

    price_cols = [c for c in ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
                  if c in frame.columns]
    if fill_gaps:
        frame[price_cols] = frame[price_cols].ffill().bfill()
    else:
        frame = frame.dropna(subset=["Close"]).copy()
    return frame.reset_index(drop=True)
