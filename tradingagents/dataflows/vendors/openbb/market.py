"""OpenBB: OHLCV history and technical indicators for US equities."""

import logging
from datetime import datetime, timedelta
from typing import Annotated

from tradingagents.dataflows.errors import VendorUnavailableError

from .client import PROVIDER, assert_rows, get_json
from .ohlcv import fetch_ohlcv as _ohlcv

logger = logging.getLogger(__name__)


def get_stock_data(
    symbol: Annotated[str, "ticker symbol of the company"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """Stock OHLCV history from the local OpenBB Platform (nasdaq provider)."""
    datetime.strptime(start_date, "%Y-%m-%d")
    datetime.strptime(end_date, "%Y-%m-%d")

    data = _ohlcv(symbol, start_date, end_date)
    csv_string = data.to_csv(index_label="Date")

    header = f"# Stock data for {symbol.upper()} from {start_date} to {end_date}\n"
    header += f"# Total records: {len(data)}\n\n"
    return header + csv_string


# TA indicator key -> OpenBB technical route + params + which result column(s)
# to report.  Anything unmapped raises ValueError just like the yahoo vendor.
_INDICATOR_ROUTES = {
    "close_50_sma": ("sma", {"length": 50}, ["sma"]),
    "close_200_sma": ("sma", {"length": 200}, ["sma"]),
    "close_10_ema": ("ema", {"length": 10}, ["ema"]),
    "macd": ("macd", {}, ["macd"]),
    "macds": ("macd", {}, ["signal"]),
    "macdh": ("macd", {}, ["histogram"]),
    "rsi": ("rsi", {}, ["rsi"]),
    "boll": ("bbands", {}, ["bb_lower", "bb_upper", "bb_mid"]),  # mapped to mid below
    "boll_ub": ("bbands", {}, ["bb_upper"]),
    "boll_lb": ("bbands", {}, ["bb_lower"]),
    "atr": ("atr", {}, ["atr"]),
    "vwma": ("vwma", {}, ["vwma"]),
    "mfi": ("mfi", {}, ["mfi"]),
}

_INDICATOR_DESC = {
    "close_50_sma": "50 SMA: medium-term trend direction; dynamic support/resistance. Lags price.",
    "close_200_sma": "200 SMA: long-term trend benchmark; confirms golden/death crosses.",
    "close_10_ema": "10 EMA: responsive short-term average; watch for quick momentum shifts.",
    "macd": "MACD: EMA-difference momentum; crossovers and divergence signal turns.",
    "macds": "MACD Signal: EMA smoothing of MACD; its crossovers trigger trades.",
    "macdh": "MACD Histogram: gap between MACD and its signal; visualizes momentum strength.",
    "rsi": "RSI: momentum flagged via 70/30 overbought/oversold thresholds.",
    "boll": "Bollinger Middle: 20 SMA baseline for the bands.",
    "boll_ub": "Bollinger Upper: 2 std above the middle; rides band in strong trends.",
    "boll_lb": "Bollinger Lower: 2 std below the middle; potential oversound bounce zone.",
    "atr": "ATR: average true range; volatility gauge for stop placement.",
    "vwma": "VWMA: volume-weighted moving average; confirm trends with volume.",
    "mfi": "MFI: money flow index; >80 overbought / <20 oversold.",
}


def _bbands_column_mapping(indicator: str) -> list[str]:
    # The middle band key "boll" reports the mid line.
    if indicator == "boll":
        return ["bb_mid"]
    return _INDICATOR_ROUTES[indicator][2]


def get_stock_stats_indicators_window(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicator: Annotated[str, "technical indicator to get the analysis and report of"],
    as_of_date: Annotated[str, "The current trading date you are trading on, YYYY-mm-dd"],
    look_back_days: Annotated[int, "how many days to look back"],
) -> str:
    """Daily indicator values from the local OpenBB Platform technical routes."""
    if indicator not in _INDICATOR_ROUTES:
        raise ValueError(
            f"Indicator {indicator} is not supported by the OpenBB vendor. "
            f"Please choose from: {list(_INDICATOR_ROUTES.keys())}"
        )

    route, extra_params, _columns = _INDICATOR_ROUTES[indicator]
    columns = _bbands_column_mapping(indicator)

    as_of_dt = datetime.strptime(as_of_date, "%Y-%m-%d")
    before = as_of_dt - timedelta(days=look_back_days)
    # Fetch the full window (plus warm-up for the slowest indicator, 200 SMA)
    # in one request, then POST the rows to the technical route.
    fetch_start = (before - timedelta(days=350)).strftime("%Y-%m-%d")
    rows = get_json(
        "/api/v1/equity/price/historical",
        f"{symbol} OHLCV",
        params={
            "symbol": symbol,
            "provider": PROVIDER,
            "start_date": fetch_start,
            "end_date": as_of_date,
        },
    )
    assert_rows(symbol, rows, "OHLCV rows for indicator calculation")
    slim = [{"date": r["date"], "open": r["open"], "high": r["high"],
             "low": r["low"], "close": r["close"], "volume": r.get("volume", 0)}
            for r in rows]

    out_rows = get_json(
        f"/api/v1/technical/{route}",
        f"{symbol} {indicator}",
        json_body={"data": slim, **extra_params},
    )
    assert_rows(symbol, out_rows, f"{indicator} values")

    by_date = {}
    for row in out_rows:
        by_date[str(row["date"])[:10]] = row

    ind_string = ""
    current = as_of_dt
    while current >= before:
        date_str = current.strftime("%Y-%m-%d")
        row = by_date.get(date_str)
        if row is None:
            ind_string += f"{date_str}: N/A: Not a trading day (weekend or holiday)\n"
        else:
            value = row.get(columns[0]) if len(columns) == 1 else None
            ind_string += f"{date_str}: {value if value is not None else 'N/A'}\n"
        current -= timedelta(days=1)

    return (
        f"## {indicator} values from {before.strftime('%Y-%m-%d')} to {as_of_date}:\n\n"
        + ind_string + "\n\n"
        + _INDICATOR_DESC.get(indicator, "")
    )
