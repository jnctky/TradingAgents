"""akshare sidecar: company news from Eastmoney's search feed.

Domestic feed, no key, reachable without a proxy. Headlines are Chinese-language
coverage of the ticker, which is what a China-based reader actually gets; the
trade-off is fewer English primary sources than a US feed would carry.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

from tradingagents.dataflows.errors import NoMarketDataError

from .client import get_rows

logger = logging.getLogger(__name__)


def _parse_dt(value) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:len(fmt) + 2].strip(), fmt)
        except ValueError:
            continue
    return None


def get_news(
    ticker: Annotated[str, "ticker symbol of the company"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """News about ``ticker`` published inside the window, from the sidecar."""
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59)

    rows = get_rows("CompanyNews", f"{ticker} news", {"symbol": ticker})
    if not rows:
        raise NoMarketDataError(ticker, ticker, "sidecar returned no news rows")

    in_range, undated = [], []
    for row in rows:
        pub_dt = _parse_dt(row.get("date"))
        if pub_dt is None:
            undated.append(row)
            continue
        if start_dt <= pub_dt <= end_dt:
            in_range.append(row)

    if not in_range:
        # Every row fell outside the window: that is a real "no news" answer, not
        # a vendor failure, so say so instead of falling through to another feed.
        return (f"No news found for {ticker} between {start_date} and {end_date} "
                f"(Eastmoney feed via akshare sidecar)")

    lines = [f"## {ticker} News, from {start_date} to {end_date} "
             f"(Eastmoney via akshare sidecar):", ""]
    for row in in_range:
        day = str(row.get("date", ""))[:10]
        title = (row.get("title") or "").strip()
        source = row.get("author") or ""
        excerpt = (row.get("excerpt") or "").strip()
        url = (row.get("url") or "").strip()
        entry = f"- {day} | {title}"
        if source:
            entry += f" [{source}]"
        if excerpt:
            entry += f"\n  {excerpt[:300]}"
        if url:
            entry += f"\n  {url}"
        lines.append(entry)

    return "\n".join(lines) + "\n"
