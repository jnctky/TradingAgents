"""OpenBB: company fundamentals, statements, insider filings and profile."""

import logging
from typing import Annotated

import pandas as pd

from tradingagents.dataflows.errors import VendorUnavailableError

from .client import PROVIDER, assert_rows, get_json

logger = logging.getLogger(__name__)

_STATEMENT_ROUTES = {
    "Balance Sheet": "/api/v1/equity/fundamental/balance",
    "Cash Flow": "/api/v1/equity/fundamental/cash",
    "Income Statement": "/api/v1/equity/fundamental/income",
}

# Columns dropped from statement CSVs: OpenBB joins a few chart/currency
# metadata fields that only add noise to the agent's context.
_DROP_COLUMNS = {"symbol", "fiscal_year", "fiscal_period", "period_ending", "currency"}


def get_fundamentals(
    ticker: Annotated[str, "ticker symbol of the company"],
    as_of_date: Annotated[str, "analysis date in YYYY-MM-DD format"] = None,
):
    """Key fundamentals overview from the local OpenBB Platform."""
    profile = get_json("/api/v1/equity/profile", f"{ticker} profile",
                       params={"symbol": ticker, "provider": PROVIDER, "limit": 1})
    lines = [f"# Fundamentals for {ticker.upper()} (local OpenBB Platform, {PROVIDER})"]

    if profile:
        p = profile[0]
        name = p.get("name", ticker.upper())
        lines.append(f"## Company: {name}")
        for key, label in [("sector", "Sector"), ("industry_group", "Industry"),
                           ("hq_country" if "hq_country" in p else "country", "Country"),
                           ("currency" if "currency" in p else None, "Currency")]:
            if key and p.get(key):
                lines.append(f"- {label}: {p[key]}")
        if p.get("long_description"):
            lines.append(f"- Description: {p['long_description'][:600]}")
    else:
        lines.append(f"## Company: {ticker.upper()} (no profile data)")

    ratios = get_json("/api/v1/equity/fundamental/ratios", f"{ticker} ratios",
                      params={"symbol": ticker, "provider": PROVIDER, "limit": 2})
    assert_rows(ticker, ratios, "fundamental ratios")
    for row in ratios:
        period = row.get("period_ending", "?")
        lines.append(f"## Ratios (period ending {period})")
        for key in sorted(row):
            if key in _DROP_COLUMNS or row[key] is None:
                continue
            lines.append(f"- {key}: {row[key]}")

    return "\n".join(lines) + "\n"


def _statement(
    ticker: str,
    freq: str,
    as_of_date: str | None,
    title: str,
) -> str:
    """Render one OpenBB fundamental statement route as CSV, yfinance-style."""
    route = _STATEMENT_ROUTES[title]
    period = "annual" if freq == "annual" else "quarter"
    rows = get_json(route, f"{ticker} {title.lower()}",
                    params={"symbol": ticker, "provider": PROVIDER,
                            "period": period, "limit": 4})
    assert_rows(ticker, rows, f"{title.lower()} ({freq})")

    df = pd.DataFrame(rows).drop(columns=[c for c in _DROP_COLUMNS if c in rows[0]],
                                 errors="ignore")
    # Transpose to yfinance's statements shape: line items as rows, dates as
    # columns, so the agent reads a familiar layout.
    if "period_ending" in df.columns:
        df = df.set_index("period_ending")
    df = df.T
    df.columns = [str(c) for c in df.columns]
    csv_string = df.to_csv()

    header = f"# {title} for {ticker.upper()} ({freq}, local OpenBB Platform)\n\n"
    return header + csv_string


def get_balance_sheet(
    ticker: Annotated[str, "ticker symbol of the company"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    as_of_date: Annotated[str, "current date in YYYY-MM-DD format"] = None,
):
    """Balance sheet data from the local OpenBB Platform."""
    return _statement(ticker, freq, as_of_date, "Balance Sheet")


def get_cashflow(
    ticker: Annotated[str, "ticker symbol of the company"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    as_of_date: Annotated[str, "current date in YYYY-MM-DD format"] = None,
):
    """Cash flow data from the local OpenBB Platform."""
    return _statement(ticker, freq, as_of_date, "Cash Flow")


def get_income_statement(
    ticker: Annotated[str, "ticker symbol of the company"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    as_of_date: Annotated[str, "current date in YYYY-MM-DD format"] = None,
):
    """Income statement data from the local OpenBB Platform."""
    return _statement(ticker, freq, as_of_date, "Income Statement")


def get_insider_transactions(
    ticker: Annotated[str, "ticker symbol of the company"],
    as_of_date: Annotated[str | None, "analysis date, yyyy-mm-dd"] = None,
):
    """Insider transactions from the local OpenBB Platform."""
    rows = get_json("/api/v1/equity/ownership/insider_trading",
                    f"{ticker} insider transactions",
                    params={"symbol": ticker, "provider": PROVIDER, "limit": 100})
    # Empty insider filings are normal — report plainly, like the yahoo vendor.
    if not rows:
        return f"No insider transactions reported for symbol '{ticker}'"

    df = pd.DataFrame(rows)
    csv_string = df.to_csv(index=False)
    return f"# Insider Transactions data for {ticker}\n" + csv_string


def get_company_profile(ticker: str) -> dict:
    """OpenBB's current profile for ``ticker``: name, sector and the like."""
    rows = get_json("/api/v1/equity/profile", f"{ticker} profile",
                    params={"symbol": ticker, "provider": PROVIDER, "limit": 1})
    return rows[0] if rows else {}
