"""akshare sidecar: company profile, ratios and the three statements.

Sina / Eastmoney F10 data through the sidecar. Coverage note: the US statement
feed is annual-only (fiscal years), so a quarterly request is reported as
annual rather than silently relabelled. SEC EDGAR stays the primary source for
statements; this vendor is the domestic fallback.
"""

from __future__ import annotations

import logging
from typing import Annotated

import pandas as pd

from tradingagents.dataflows.errors import VendorUnavailableError

from .client import assert_rows, get_rows

logger = logging.getLogger(__name__)

_STATEMENT_MODELS = {
    "Balance Sheet": "BalanceSheet",
    "Cash Flow": "CashFlowStatement",
    "Income Statement": "IncomeStatement",
}

# Same noise columns the OpenBB renderer drops.
_DROP_COLUMNS = {"symbol", "fiscal_year", "fiscal_period", "period_ending", "currency"}


def _fmt(value) -> str:
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)


def get_fundamentals(
    ticker: Annotated[str, "ticker symbol of the company"],
    as_of_date: Annotated[str, "analysis date in YYYY-MM-DD format"] = None,
):
    """Company profile plus the latest key metrics and valuation ratios."""
    info = get_rows("EquityInfo", f"{ticker} profile", {"symbol": ticker})
    metrics = get_rows("KeyMetrics", f"{ticker} key metrics", {"symbol": ticker})
    ratios = get_rows("FinancialRatios", f"{ticker} ratios", {"symbol": ticker})

    if not info and not metrics and not ratios:
        raise VendorUnavailableError(f"sidecar has no fundamentals for {ticker}")

    lines = [f"# Fundamentals for {ticker.upper()} (akshare sidecar: Sina/Eastmoney F10)"]

    if info:
        row = info[0]
        lines.append(f"## Company: {row.get('name') or ticker.upper()}")
        for key, label in [("sector", "Sector"),
                           ("industry_category", "Industry"),
                           ("stock_exchange", "Exchange"),
                           ("isin", "ISIN")]:
            if row.get(key):
                lines.append(f"- {label}: {row[key]}")
        description = row.get("long_description") or row.get("short_description")
        if description:
            lines.append(f"- Description: {str(description)[:600]}")
    else:
        lines.append(f"## Company: {ticker.upper()} (no profile data)")

    for label, rows in (("Key metrics", metrics), ("Valuation ratios", ratios)):
        if not rows:
            continue
        row = rows[0]
        lines.append(f"## {label} (period ending {row.get('period_ending', '?')})")
        for key in sorted(row):
            if key in _DROP_COLUMNS or row[key] is None:
                continue
            lines.append(f"- {key}: {_fmt(row[key])}")

    return "\n".join(lines) + "\n"


def get_company_profile(ticker: str) -> dict:
    """Identity fields (name/sector/industry/exchange) in the yahoo-shaped keys
    the instrument-identity resolver reads."""
    rows = get_rows("EquityInfo", f"{ticker} profile", {"symbol": ticker})
    if not rows:
        raise VendorUnavailableError(f"sidecar has no profile for {ticker}")
    row = rows[0]
    name = row.get("name") or ticker.upper()
    return {
        "symbol": row.get("symbol") or ticker.upper(),
        "longName": name,
        "shortName": name,
        "sector": row.get("sector"),
        "industry": row.get("industry_category"),
        "exchange": row.get("stock_exchange"),
        "quoteType": "EQUITY",
        "longBusinessSummary": row.get("long_description") or row.get("short_description"),
    }


def _statement(
    ticker: str,
    freq: str,
    as_of_date: str | None,
    title: str,
    limit: int = 4,
) -> str:
    model = _STATEMENT_MODELS[title]
    rows = get_rows(model, f"{ticker} {title.lower()}",
                    {"symbol": ticker, "period": "annual" if freq == "annual" else "quarterly"})
    assert_rows(ticker, rows, f"{title.lower()} ({freq})")

    # Newest first: the feed's order is not guaranteed, but the agent needs the
    # most recent periods, not the oldest ones.
    rows = sorted(rows, key=lambda r: str(r.get("period_ending", "")), reverse=True)[:limit]
    periods = [str(r.get("period_ending", "?")) for r in rows]
    actual = {str(r.get("fiscal_period") or "").upper() for r in rows}
    freq_note = "annual" if actual <= {"FY", ""} else freq

    frame = pd.DataFrame(rows).drop(
        columns=[c for c in _DROP_COLUMNS if c in rows[0]], errors="ignore")

    # Line items as rows, periods as columns (the layout the agents expect).
    frame = frame.T
    frame.columns = periods
    csv_string = frame.to_csv()

    header = (f"# {title} for {ticker.upper()} ({freq_note}, source: akshare sidecar "
              f"Sina/Eastmoney F10)\n")
    if freq_note != freq:
        header += ("# NOTE: the domestic US statement feed publishes fiscal-year "
                   "figures only; quarterly detail comes from SEC EDGAR.\n")
    return header + "\n" + csv_string


def get_balance_sheet(
    ticker: Annotated[str, "ticker symbol of the company"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    as_of_date: Annotated[str, "current date in YYYY-MM-DD format"] = None,
):
    return _statement(ticker, freq, as_of_date, "Balance Sheet")


def get_cashflow(
    ticker: Annotated[str, "ticker symbol of the company"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    as_of_date: Annotated[str, "current date in YYYY-MM-DD format"] = None,
):
    return _statement(ticker, freq, as_of_date, "Cash Flow")


def get_income_statement(
    ticker: Annotated[str, "ticker symbol of the company"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    as_of_date: Annotated[str, "current date in YYYY-MM-DD format"] = None,
):
    return _statement(ticker, freq, as_of_date, "Income Statement")
