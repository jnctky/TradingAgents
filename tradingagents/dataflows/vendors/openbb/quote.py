"""OpenBB: live quote snapshot used to cross-check verified market data.

The platform's quote route is a *current* snapshot, so it must never be used to
verify a historical run — anything it reports could post-date the analysis date.
``build_quote_crosscheck`` therefore only renders the block when the quote's own
timestamp is in the neighbourhood of the requested analysis date.
"""

from __future__ import annotations

import logging
from datetime import datetime

from tradingagents.dataflows.errors import NoMarketDataError

from .client import PROVIDER, assert_rows, get_json

logger = logging.getLogger(__name__)

# The quote may lag the analysis date by a weekend/holiday; beyond this it is
# reporting a different session, and beyond one day ahead it is look-ahead.
MAX_QUOTE_LAG_DAYS = 5
MAX_QUOTE_LEAD_DAYS = 1

# (field, label, formatter). None-safe: a missing value renders as "-".
_NUM = lambda v, f="{:,.2f}": "-" if v is None else f.format(v)
_INT = lambda v: "-" if v is None else f"{int(v):,}"
_PCT = lambda v: "-" if v is None else f"{v * 100:+.2f}%"
_MONEY = lambda v: "-" if v is None else f"{v / 1e9:,.1f}B" if abs(v) >= 1e9 else f"{v / 1e6:,.0f}M"


def get_quote(symbol: str) -> dict:
    """Latest quote row for ``symbol`` from the local platform."""
    rows = get_json("/api/v1/equity/price/quote", f"{symbol} quote",
                    params={"symbol": symbol, "provider": PROVIDER})
    assert_rows(symbol, rows, "quote row")
    return rows[0]


def build_quote_crosscheck(symbol: str, as_of_date: str, row_close: float | None = None) -> str:
    """Render the live-quote cross-check block, or a one-line skip notice.

    Silently degrading (rather than raising) is deliberate: the snapshot's core
    job is the historical verification below it, and a quote outage must not
    take that down.

    The date guard runs *before* the request: a live quote is worthless (and
    misleading) for a historical run, so those runs never touch the network.
    """
    try:
        requested = datetime.strptime(as_of_date, "%Y-%m-%d").date()
    except ValueError:
        return "### Quote cross-check\n\nUnavailable (unparsable analysis date).\n"

    age = (datetime.now().date() - requested).days
    if age > MAX_QUOTE_LAG_DAYS:
        return (
            f"### Quote cross-check\n\nSkipped: the analysis date {as_of_date} is "
            f"{age} days before today, so a live quote would post-date it.\n"
        )

    try:
        quote = get_quote(symbol)
    except Exception as exc:  # noqa: BLE001 — cross-check is optional enrichment
        logger.warning("Quote cross-check unavailable for %s: %s", symbol, exc)
        return f"### Quote cross-check\n\nUnavailable ({type(exc).__name__}).\n"

    stamp = str(quote.get("last_timestamp") or "")[:10]
    if not stamp:
        return "### Quote cross-check\n\nUnavailable (quote carried no timestamp).\n"
    try:
        quoted = datetime.strptime(stamp, "%Y-%m-%d").date()
    except ValueError:
        return "### Quote cross-check\n\nUnavailable (unparsable quote timestamp).\n"

    lead = (quoted - requested).days
    lag = (requested - quoted).days
    if lead > MAX_QUOTE_LEAD_DAYS:
        return (
            f"### Quote cross-check\n\nSkipped: the live quote is dated {stamp}, "
            f"after the analysis date {as_of_date} (look-ahead).\n"
        )
    if lag > MAX_QUOTE_LAG_DAYS:
        return (
            f"### Quote cross-check\n\nSkipped: the live quote is dated {stamp}, "
            f"{lag} days before the analysis date {as_of_date}.\n"
        )

    name = quote.get("name") or symbol.upper()
    lines = [
        "### Quote cross-check (platform quote snapshot)",
        "",
        f"- Instrument: {name} ({quote.get('exchange') or 'n/a'})"
        f" — {quote.get('asset_type') or 'n/a'}, market status "
        f"{quote.get('market_status') or 'n/a'}",
        f"- Quote timestamp: {stamp}",
        "",
        "| Field | Value |",
        "|---|---:|",
        f"| Last price | {_NUM(quote.get('last_price'))} |",
        f"| Previous close | {_NUM(quote.get('prev_close'))} |",
        f"| Change | {_NUM(quote.get('change'))} ({_PCT(quote.get('change_percent'))}) |",
        f"| 52-week range | {_NUM(quote.get('year_low'))} – {_NUM(quote.get('year_high'))} |",
        f"| Volume / 20d average | {_INT(quote.get('volume'))} / {_INT(quote.get('average_volume'))} |",
        f"| Market cap | {_MONEY(quote.get('market_cap'))} |",
        f"| Analyst price target | {_NUM(quote.get('price_target'))} |",
        f"| Dividend yield | {_PCT(quote.get('dividend_yield'))} |",
    ]

    last_price = quote.get("last_price")
    if row_close is not None and last_price is not None:
        gap = abs(last_price - row_close) / row_close * 100 if row_close else None
        verdict = "consistent" if gap is not None and gap < 0.5 else "DISCREPANCY — flag it"
        lines += [
            "",
            f"Quote last price {last_price:.2f} vs verified row close {row_close:.2f}"
            + (f" ({gap:.2f}% apart): {verdict}." if gap is not None else "."),
        ]

    return "\n".join(lines) + "\n"
