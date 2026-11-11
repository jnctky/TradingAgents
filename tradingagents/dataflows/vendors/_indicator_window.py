"""Local technical-indicator rendering shared by the price vendors.

Both the akshare sidecar and the Binance crypto vendor compute indicators in
process with stockstats — there is no server-side indicator route to lean on.
The analysts read the windowed text, so the format must not drift between
vendors: a report that switches source (crypto falls back from Binance to the
sidecar) still has to answer the same question in the same shape.

Keeping it here also means a new price vendor gets indicators for free by
handing over its OHLCV frame.
"""

from __future__ import annotations

import pandas as pd
from dateutil.relativedelta import relativedelta

#: One description per supported TA indicator key, appended to every window.
INDICATOR_DESCRIPTIONS = {
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


def assert_supported(indicator: str) -> None:
    """Raise ValueError for an indicator key we do not compute."""
    if indicator not in INDICATOR_DESCRIPTIONS:
        raise ValueError(
            f"Indicator {indicator} is not supported. "
            f"Please choose from: {list(INDICATOR_DESCRIPTIONS)}")


def indicator_values(frame: pd.DataFrame, indicator: str) -> dict[str, float]:
    """``{YYYY-MM-DD: value}`` computed locally by stockstats.

    Parameters
    ----------
    frame : DataFrame
        OHLCV bars indexed by a ``Date`` column, lower-case ohlcv columns —
        the shape every price vendor already produces.
    indicator : str
        A key from :data:`INDICATOR_DESCRIPTIONS`.

    Returns
    -------
    dict
        One entry per session; NaN where the look-back was not yet long enough.
    """
    from stockstats import wrap

    assert_supported(indicator)

    work = wrap(frame.reset_index())
    work["Date"] = pd.to_datetime(work["Date"]).dt.strftime("%Y-%m-%d")
    work[indicator]  # trigger stockstats calculation

    return dict(zip(work["Date"], work[indicator], strict=True))


def render_window(
    values: dict[str, float],
    indicator: str,
    as_of_date: str,
    look_back_days: int,
    source_note: str,
) -> str:
    """Format the day-by-day window the analysts read.

    Sessions missing from ``values`` are labelled as non-trading days rather
    than dropped, so the model can tell "market closed" from "no data".
    """
    as_of_dt = pd.Timestamp(as_of_date).to_pydatetime()
    before_dt = as_of_dt - relativedelta(days=look_back_days)

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
        + f"Source: {source_note} {INDICATOR_DESCRIPTIONS[indicator]}"
    )
