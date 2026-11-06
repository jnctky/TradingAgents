"""OpenBB: keyless macro indicators (inflation, labour, rates, growth).

The local platform ships open-data providers (OECD, BLS, Federal Reserve,
Eurostat) that need no API key, so ``get_macro_indicators`` no longer has to
wait for a FRED key. Output mirrors the FRED vendor's markdown shape so the
analysts read either source the same way.

Caveat stated in every report: these routes serve the *latest published* values.
They do not pin a data vintage, so a backtest sees today's revisions. Use the
FRED vendor when revision-free point-in-time data matters.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Annotated

from tradingagents.dataflows.config import get_config

from .client import assert_rows, get_json

logger = logging.getLogger(__name__)

DEFAULT_LOOKBACK_DAYS = 365
MAX_ROWS = 18

# Percent-valued series: the platform returns decimals (0.0479), FRED reports
# percent (4.79). Convert on the way out so both vendors read the same.
_PCT = "pct"

# Transforms applied to a level series before it means what the caller asked for.
_NONE = "none"
_YOY = "yoy"            # price index -> % change vs the same month a year earlier
_MOM = "mom"            # payroll level -> change vs the previous month

# Series that need a longer window to carry any meaning: a year-over-year read
# needs 13 months of points, a monthly change needs 2.
_LONG_WINDOW_ALIASES = ("cpi", "unemployment", "gdp", "core_pce", "nfp")

# alias -> (label, units, value kind)
_MACRO_ROUTES: dict[str, tuple[str, str, str]] = {
    "cpi": ("US CPI inflation (YoY)", "% year-over-year", _PCT),
    "unemployment": ("US unemployment rate", "% of labour force", _PCT),
    "fed_funds_rate": ("Effective federal funds rate", "%", _PCT),
    "10y_treasury": ("US 10-year Treasury yield", "%", _PCT),
    "yield_curve": ("US Treasury yield curve (latest session)", "%", _PCT),
    "gdp": ("US real GDP", "USD, SAAR level", "level"),
    "inflation_expectations": ("US inflation expectations (1y)", "%", _PCT),
}

# The platform also mirrors FRED series one-for-one (#keyless), which covers the
# rates/dollar/risk block the OECD routes above do not. Two differences from the
# routes above decide the formatting:
#   * values arrive as FRED publishes them (a 4.90% yield is 4.90, not 0.049), so
#     these use the "level" formatter and are never rescaled;
#   * level series (PCE price index, payroll counts) need a transform before they
#     read as the thing analysts ask for ("core PCE inflation", "payrolls added").
# alias -> (label, units, series id, transform)
_FRED_SERIES: dict[str, tuple[str, str, str, str]] = {
    "dollar_index": ("US dollar broad index (DTWEXBGS)", "index", "DTWEXBGS", _NONE),
    "2y_treasury": ("US 2-year Treasury yield", "%", "DGS2", _NONE),
    "30y_treasury": ("US 30-year Treasury yield", "%", "DGS30", _NONE),
    "real_10y": ("US 10-year real yield (TIPS)", "%", "DFII10", _NONE),
    "breakeven_10y": ("US 10-year breakeven inflation", "%", "T10YIE", _NONE),
    "vix": ("CBOE volatility index (VIX)", "index", "VIXCLS", _NONE),
    "jobless_claims": ("US initial jobless claims", "persons", "ICSA", _NONE),
    "core_pce": ("US core PCE inflation (YoY)", "% year-over-year", "PCEPILFE", _YOY),
    "nfp": ("US nonfarm payrolls (monthly change)", "thousand persons", "PAYEMS", _MOM),
}

# Input aliases the LLM tends to use.
_ALIASES = {
    "inflation": "cpi",
    "cpi_yoy": "cpi",
    "core_cpi": "cpi",
    "unemployment_rate": "unemployment",
    "jobless_rate": "unemployment",
    "fed_funds": "fed_funds_rate",
    "fed_rate": "fed_funds_rate",
    "federal_funds_rate": "fed_funds_rate",
    "policy_rate": "fed_funds_rate",
    "10y": "10y_treasury",
    "dgs10": "10y_treasury",
    "treasury_10y": "10y_treasury",
    "yield_curve": "yield_curve",
    "curve": "yield_curve",
    "real_gdp": "gdp",
    # dollar block
    "dxy": "dollar_index",
    "dollar": "dollar_index",
    "usd": "dollar_index",
    "dollar_broad_index": "dollar_index",
    "dtwexbgs": "dollar_index",
    # rates block
    "2y": "2y_treasury",
    "dgs2": "2y_treasury",
    "treasury_2y": "2y_treasury",
    "30y": "30y_treasury",
    "dgs30": "30y_treasury",
    "treasury_30y": "30y_treasury",
    "real_yield": "real_10y",
    "tips_10y": "real_10y",
    "dfii10": "real_10y",
    "breakeven": "breakeven_10y",
    "breakeven_inflation": "breakeven_10y",
    "t10yie": "breakeven_10y",
    # inflation / labour block
    "pce": "core_pce",
    "core_pce_yoy": "core_pce",
    "pcepilfe": "core_pce",
    "nonfarm": "nfp",
    "nonfarm_payrolls": "nfp",
    "payrolls": "nfp",
    "payems": "nfp",
    "claims": "jobless_claims",
    "initial_claims": "jobless_claims",
    "initial_jobless_claims": "jobless_claims",
    "icsa": "jobless_claims",
    # risk block
    "fear_index": "vix",
    "volatility": "vix",
    "vixcls": "vix",
}

_CURVE_MATURITIES = ("month_1", "month_3", "month_6", "year_1", "year_2",
                     "year_3", "year_5", "year_7", "year_10", "year_20", "year_30")


def _resolve(indicator: str) -> str:
    key = (indicator or "").strip().lower().replace("-", "_").replace(" ", "_")
    return _ALIASES.get(key, key)


def _fmt(kind: str, value) -> str:
    if value is None:
        return "N/A"
    if kind == _PCT:
        return f"{float(value) * 100:.2f}%"
    if float(value) >= 1e12:
        return f"{float(value) / 1e12:.2f}T"
    if abs(float(value)) >= 10_000:
        # Counts and index levels (jobless claims, payroll levels): the decimals
        # are noise at this magnitude and make the number harder to read.
        return f"{float(value):,.0f}"
    return f"{float(value):,.2f}"


def _yoy(points: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Percent change against the same month a year earlier.

    Keyed by month rather than by exact date: monthly series publish on the
    first of the month, but a holiday shift or a late release can move that day,
    and an exact-date lookup would silently drop those observations.
    """
    by_month: dict[str, float] = {}
    for date, value in points:
        by_month.setdefault(date[:7], value)      # first observation in the month
    out = []
    for date, value in points:
        year, month = int(date[:4]), date[5:7]
        base = by_month.get(f"{year - 1}-{month}")
        if base:
            out.append((date, (value / base - 1) * 100))
    return out


def _month_index(date: str) -> int:
    """A date's month as a running count, so consecutive months differ by 1."""
    return int(date[:4]) * 12 + int(date[5:7])


def _mom(points: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Change against the previous observation, only for consecutive months.

    A gap means the previous point is not the previous month, and a two-month
    difference reported as a one-month change overstates the move.
    """
    return [
        (date, value - prev_value)
        for (prev_date, prev_value), (date, value) in zip(points, points[1:])
        if _month_index(date) - _month_index(prev_date) == 1
    ]


def _series_points(alias: str, start_date: str, end_date: str) -> tuple[list[tuple[str, float]], str]:
    """(date, value) points for a scalar series; (curve rows, date) for yield_curve."""
    if alias == "cpi":
        rows = get_json("/api/v1/economy/cpi", "US CPI", params={
            "provider": "oecd", "country": "united_states", "transform": "yoy",
            "frequency": "monthly", "start_date": start_date, "end_date": end_date,
        })
    elif alias == "unemployment":
        rows = get_json("/api/v1/economy/unemployment", "US unemployment", params={
            "provider": "oecd", "country": "united_states", "frequency": "monthly",
            "start_date": start_date, "end_date": end_date,
        })
    elif alias == "gdp":
        rows = get_json("/api/v1/economy/gdp/real", "US real GDP", params={
            "provider": "oecd", "country": "united_states", "frequency": "quarter",
            "start_date": start_date, "end_date": end_date,
        })
    elif alias == "inflation_expectations":
        rows = get_json("/api/v1/economy/survey/inflation_expectations",
                        "US inflation expectations", params={
                            "provider": "federal_reserve",
                            "start_date": start_date, "end_date": end_date,
                        })
    elif alias == "fed_funds_rate":
        rows = get_json("/api/v1/fixedincome/rate/effr", "Effective fed funds rate", params={
            "provider": "federal_reserve", "start_date": start_date, "end_date": end_date,
        })
    elif alias in _FRED_SERIES:
        # One wide table per request: the platform returns a `date` column plus
        # one column per series id, with None where FRED had no print that day.
        _label, _units, series_id, transform = _FRED_SERIES[alias]
        rows = get_json("/api/v1/economy/fred_series", f"FRED {series_id}", params={
            "provider": "fred", "symbol": series_id,
            "start_date": start_date, "end_date": end_date,
        })
        assert_rows("macro", rows, f"{alias} observations")
        points = [(str(r.get("date"))[:10], r.get(series_id)) for r in rows]
        points = [(d, float(v)) for d, v in points if v is not None]
        points = _yoy(points) if transform == _YOY else _mom(points) if transform == _MOM else points
        return points, (points[-1][0] if points else "")
    else:  # 10y_treasury / yield_curve share the treasury curve series
        rows = get_json("/api/v1/fixedincome/government/treasury_rates",
                        "Treasury par yield curve", params={
                            "provider": "federal_reserve",
                            "start_date": start_date, "end_date": end_date,
                        })

    assert_rows("macro", rows, f"{alias} observations")

    if alias == "fed_funds_rate":
        points = [(str(r.get("date"))[:10], r.get("rate")) for r in rows]
    elif alias in ("10y_treasury", "yield_curve"):
        points = [(str(r.get("date"))[:10], r.get("year_10") if alias == "10y_treasury" else r)
                  for r in rows]
    else:
        points = [(str(r.get("date"))[:10], r.get("value")) for r in rows]

    return [(d, v) for d, v in points if v is not None], (points[-1][0] if points else "")


def get_macro_data(
    indicator: Annotated[str, "macro indicator alias, e.g. 'cpi', 'unemployment', 'fed_funds_rate', '10y_treasury'"],
    as_of_date: str,
    look_back_days: int | None = None,
) -> str:
    """Keyless macro series from the local OpenBB Platform."""
    alias = _resolve(indicator)
    if look_back_days is None:
        look_back_days = int(get_config().get("macro_lookback_days") or DEFAULT_LOOKBACK_DAYS)
    # Monthly/quarterly series need headroom before a change is meaningful: a
    # year-over-year read needs 13 months of points, which 365 days does not buy.
    if alias in _LONG_WINDOW_ALIASES:
        look_back_days = max(int(look_back_days), 1095)

    end_dt = datetime.strptime(as_of_date, "%Y-%m-%d")
    start_str = (end_dt - timedelta(days=int(look_back_days))).strftime("%Y-%m-%d")

    if alias not in _MACRO_ROUTES and alias not in _FRED_SERIES:
        supported = ", ".join(sorted({*_MACRO_ROUTES, *_FRED_SERIES}))
        return f"OpenBB macro: unknown indicator '{indicator}'. Supported aliases: {supported}."

    if alias in _FRED_SERIES:
        fr_label, fr_units, _series_id, _transform = _FRED_SERIES[alias]
        label, units, kind = fr_label, fr_units, "level"
    else:
        label, units, kind = _MACRO_ROUTES[alias]
    try:
        points, latest_date = _series_points(alias, start_str, as_of_date)
    except Exception as exc:  # noqa: BLE001 — degrade to guidance, never abort the run
        return (f"OpenBB macro: could not load '{alias}' from the local platform "
                f"({type(exc).__name__}: {exc}). Report the series as unavailable.")

    header = (
        f"## OpenBB macro: {label}\n"
        f"- Units: {units}\n"
        f"- Provider: local OpenBB Platform ({alias} route)\n"
        f"- Window: {start_str} to {as_of_date}\n"
        f"- Note: latest published values — the platform does not pin a data "
        f"vintage, so revisions after {as_of_date} are visible.\n"
    )
    if not points:
        return header + f"\nNo observations for '{alias}' in this window."

    if alias == "yield_curve":
        curve = points[-1][1]
        rows = [(m, curve.get(m)) for m in _CURVE_MATURITIES if curve.get(m) is not None]
        table = "\n| Maturity | Yield |\n| --- | --- |\n" + "\n".join(
            f"| {m.replace('month_', '').replace('year_', '')}"
            f"{'M' if m.startswith('month') else 'Y'} | {_fmt(kind, v)} |"
            for m, v in rows
        )
        spread = None
        if curve.get("year_10") is not None and curve.get("year_2") is not None:
            spread = (curve["year_10"] - curve["year_2"]) * 100
        summary = f"\n**Latest curve:** {latest_date}"
        if spread is not None:
            summary += f" | **10y-2y spread:** {spread:+.2f} pp" + (
                " (inverted)" if spread < 0 else ""
            )
        return header + summary + "\n" + table + "\n"

    first_date, first_val = points[0]
    last_date, last_val = points[-1]
    summary = f"\n**Latest:** {_fmt(kind, last_val)} ({last_date})"
    if len(points) > 1:
        try:
            delta = float(last_val) - float(first_val)
            summary += (f" | **Change from {first_date}:** "
                        + (f"{delta * 100:+.2f} pp" if kind == _PCT else f"{delta:+,.2f}")
                        + f", from {_fmt(kind, first_val)}")
        except (TypeError, ValueError):
            pass
    summary += "\n"

    shown = points[-MAX_ROWS:]
    note = ""
    if len(points) > MAX_ROWS:
        note = f"\n_(showing the most recent {MAX_ROWS} of {len(points)} observations)_\n"
    table = ("\n| Date | Value |\n| --- | --- |\n"
             + "\n".join(f"| {d} | {_fmt(kind, v)} |" for d, v in shown) + "\n")
    return header + summary + note + table
