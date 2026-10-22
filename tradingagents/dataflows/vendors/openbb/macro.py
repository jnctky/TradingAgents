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

# alias -> (label, units, value kind, route builder)
_MACRO_ROUTES: dict[str, tuple[str, str, str]] = {
    "cpi": ("US CPI inflation (YoY)", "% year-over-year", _PCT),
    "unemployment": ("US unemployment rate", "% of labour force", _PCT),
    "fed_funds_rate": ("Effective federal funds rate", "%", _PCT),
    "10y_treasury": ("US 10-year Treasury yield", "%", _PCT),
    "yield_curve": ("US Treasury yield curve (latest session)", "%", _PCT),
    "gdp": ("US real GDP", "USD, SAAR level", "level"),
    "inflation_expectations": ("US inflation expectations (1y)", "%", _PCT),
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
    return f"{float(value):,.2f}"


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
    if look_back_days is None:
        look_back_days = int(get_config().get("macro_lookback_days") or DEFAULT_LOOKBACK_DAYS)
    elif indicator.lower() in ("cpi", "unemployment", "gdp") and look_back_days < 365:
        # Monthly/quarterly series need headroom before a change is meaningful.
        look_back_days = 1095

    end_dt = datetime.strptime(as_of_date, "%Y-%m-%d")
    start_str = (end_dt - timedelta(days=int(look_back_days))).strftime("%Y-%m-%d")

    alias = _resolve(indicator)
    if alias not in _MACRO_ROUTES:
        supported = ", ".join(sorted(_MACRO_ROUTES))
        return (
            f"OpenBB macro: unknown indicator '{indicator}'. Supported aliases: "
            f"{supported}. ('core_pce' is FRED-only and needs FRED_API_KEY; "
            f"'cpi' is the available inflation series here.)"
        )

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
