"""akshare sidecar: OHLCV history and technical indicators.

Prices come from Tencent (``web.ifzq.gtimg.cn``) through the sidecar: adjusted
daily bars for US equities and ETFs, no key, no rate limit, reachable without a
proxy. This replaces the yfinance path, which is throttled to death from this
network even behind a VPN.

Tencent serves at most 400 bars per request, so longer ranges are fetched
backwards in windows and merged by date.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Annotated

import pandas as pd
from dateutil.relativedelta import relativedelta
from stockstats import wrap

from tradingagents.dataflows.errors import NoMarketDataError, VendorError

from .client import get_rows

logger = logging.getLogger(__name__)

# ~390 trading days: one bar under Tencent's 400-bar cap, so every window is a
# single complete answer.
_WINDOW_DAYS = 550

_OHLCV_CACHE: dict[tuple[str, str, str], pd.DataFrame] = {}
_CACHE_LIMIT = 32


def _fetch_window(symbol: str, start_date: str, end_date: str) -> list[dict]:
    return get_rows(
        "EquityHistorical",
        f"{symbol} daily bars",
        {"symbol": symbol, "interval": "1d",
         "start_date": start_date, "end_date": end_date},
    )


def _fetch_ohlcv(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Daily adjusted bars between the two dates, oldest first."""
    key = (symbol.upper(), start_date, end_date)
    cached = _OHLCV_CACHE.get(key)
    if cached is not None:
        return cached

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    cursor = min(end_dt, datetime.today())

    by_date: dict[str, dict] = {}
    while cursor >= start_dt:
        window_start = max(start_dt, cursor - timedelta(days=_WINDOW_DAYS))
        rows = _fetch_window(symbol, window_start.strftime("%Y-%m-%d"),
                             cursor.strftime("%Y-%m-%d"))
        if not rows:
            break
        for row in rows:
            day = str(row.get("date", ""))[:10]
            if day:
                by_date[day] = row

        earliest = min(datetime.strptime(d, "%Y-%m-%d") for d in by_date)
        next_cursor = earliest - timedelta(days=1)
        if next_cursor >= cursor:  # no progress — stop instead of looping
            break
        cursor = next_cursor
        if len(by_date) > 4000:  # hard stop; 15+ years is plenty for TA
            break

    if not by_date:
        raise NoMarketDataError(symbol, symbol,
                                f"sidecar returned no daily bars between {start_date} and {end_date}")

    frame = pd.DataFrame([
        {"Date": pd.to_datetime(day),
         "open": float(row["open"]), "high": float(row["high"]),
         "low": float(row["low"]), "close": float(row["close"]),
         "volume": float(row.get("volume") or 0)}
        for day, row in sorted(by_date.items())
    ]).set_index("Date")

    frame = frame[frame.index <= pd.to_datetime(end_date)]
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()

    if frame.empty:
        raise NoMarketDataError(symbol, symbol,
                                f"no bars on or before {end_date}")

    if len(_OHLCV_CACHE) >= _CACHE_LIMIT:
        _OHLCV_CACHE.pop(next(iter(_OHLCV_CACHE)))
    _OHLCV_CACHE[key] = frame
    return frame


def load_ohlcv(symbol: str, as_of_date: str, fill_gaps: bool = True) -> pd.DataFrame:
    """OHLCV frame on or before ``as_of_date``, shaped like the yahoo loader.

    Used by the vendor-dispatched loader (settlement / verification), which needs
    a DataFrame rather than the agent-facing string.
    """
    frame = _fetch_ohlcv(symbol, (pd.Timestamp(as_of_date) - pd.Timedelta(days=400)).strftime("%Y-%m-%d"),
                         as_of_date)
    out = frame.rename(columns={"open": "Open", "high": "High", "low": "Low",
                                "close": "Close", "volume": "Volume"})
    out["Adj Close"] = out["Close"]
    if fill_gaps:
        out[["Open", "High", "Low", "Close", "Adj Close"]] = (
            out[["Open", "High", "Low", "Close", "Adj Close"]].ffill())
    out.index.name = "Date"
    # Callers (the verification snapshot) read the session date from a column.
    return out.reset_index()


def get_stock_data(
    symbol: Annotated[str, "ticker symbol of the company"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """Stock OHLCV history from the local akshare sidecar (Tencent, forward-adjusted)."""
    datetime.strptime(start_date, "%Y-%m-%d")
    datetime.strptime(end_date, "%Y-%m-%d")

    data = _fetch_ohlcv(symbol, start_date, end_date)
    out = data.rename(columns={"open": "Open", "high": "High", "low": "Low",
                               "close": "Close", "volume": "Volume"})
    csv_string = out.to_csv(index_label="Date")

    header = f"# Stock data for {symbol.upper()} from {start_date} to {end_date}\n"
    header += "# Source: Tencent via akshare sidecar (qfq-adjusted, domestic, no key)\n"
    header += f"# Total records: {len(out)}\n\n"
    return header + csv_string


_INDICATOR_DESCRIPTIONS = {
    "close_50_sma": "50 SMA: medium-term trend direction and dynamic support/resistance.",
    "close_200_sma": "200 SMA: long-term trend benchmark; golden/death cross confirmation.",
    "close_10_ema": "10 EMA: responsive short-term average for momentum shifts.",
    "macd": "MACD: momentum via EMA differences; crossovers and divergence.",
    "macds": "MACD Signal: EMA smoothing of MACD; crossover trigger.",
    "macdh": "MACD Histogram: gap between MACD and signal; momentum strength.",
    "rsi": "RSI: overbought/oversold momentum gauge (70/30 thresholds).",
    "boll": "Bollinger Middle: 20 SMA basis for the bands.",
    "boll_ub": "Bollinger Upper Band: 2 sigma above the middle.",
    "boll_lb": "Bollinger Lower Band: 2 sigma below the middle.",
    "atr": "ATR: average true range, for stop placement and position sizing.",
    "vwma": "VWMA: volume-weighted moving average, confirms price with volume.",
    "mfi": "MFI: money flow index using price and volume (80/20 thresholds).",
}


def _indicator_series(symbol: str, indicator: str, as_of_date: str) -> pd.DataFrame:
    as_of_dt = datetime.strptime(as_of_date, "%Y-%m-%d")
    # 400 extra days so a 200-day average is populated from the first reported day.
    fetch_start = (as_of_dt - relativedelta(days=400)).strftime("%Y-%m-%d")
    data = _fetch_ohlcv(symbol, fetch_start, as_of_date)
    frame = wrap(data.reset_index())
    frame["Date"] = pd.to_datetime(frame["Date"]).dt.strftime("%Y-%m-%d")
    frame[indicator]  # trigger stockstats calculation
    return frame


def get_stock_stats_indicators_window(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicator: Annotated[str, "technical indicator to get the analysis and report of"],
    as_of_date: Annotated[str, "The current trading date you are trading on, YYYY-mm-dd"],
    look_back_days: Annotated[int, "how many days to look back"],
) -> str:
    """Technical indicator history computed locally from sidecar prices."""
    if indicator not in _INDICATOR_DESCRIPTIONS:
        raise ValueError(
            f"Indicator {indicator} is not supported. "
            f"Please choose from: {list(_INDICATOR_DESCRIPTIONS)}")

    as_of_dt = datetime.strptime(as_of_date, "%Y-%m-%d")
    before_dt = as_of_dt - relativedelta(days=look_back_days)

    try:
        frame = _indicator_series(symbol, indicator, as_of_date)
    except VendorError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface as a vendor error
        raise NoMarketDataError(symbol, symbol,
                               f"{indicator} could not be computed: {exc}") from exc

    # Both series come from the same frame, so a length mismatch is a bug worth
    # raising rather than silently truncating the indicator series.
    values = dict(zip(frame["Date"], frame[indicator], strict=True))

    lines = []
    cursor = as_of_dt
    while cursor >= before_dt:
        day = cursor.strftime("%Y-%m-%d")
        if day in values:
            value = values[day]
            lines.append(f"{day}: {value if not pd.isna(value) else 'N/A'}")
        else:
            lines.append(f"{day}: N/A: Not a trading day (weekend or holiday)")
        cursor = cursor - relativedelta(days=1)

    return (
        f"## {indicator} values from {before_dt.strftime('%Y-%m-%d')} to {as_of_date}:\n\n"
        + "\n".join(lines)
        + "\n\n"
        + f"Source: akshare sidecar (Tencent qfq). {_INDICATOR_DESCRIPTIONS[indicator]}"
    )
