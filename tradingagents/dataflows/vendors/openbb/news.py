"""OpenBB: company news within a date window."""

import logging
from datetime import datetime
from typing import Annotated

from .client import PROVIDER, get_json

logger = logging.getLogger(__name__)


def get_news_openbb(
    ticker: Annotated[str, "ticker symbol of the company"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """News about ``ticker`` published in the window, from the local OpenBB
    Platform (nasdaq provider)."""
    datetime.strptime(start_date, "%Y-%m-%d")
    datetime.strptime(end_date, "%Y-%m-%d")

    rows = get_json("/api/v1/news/company", f"{ticker} news",
                    params={"symbol": ticker, "provider": PROVIDER, "limit": 250})

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59)
    in_range = []
    for row in rows:
        pub = str(row.get("date", ""))[:19]
        try:
            pub_dt = datetime.fromisoformat(pub)
        except ValueError:
            continue
        if start_dt <= pub_dt <= end_dt:
            in_range.append(row)

    if not in_range:
        return (f"No news found for {ticker} between {start_date} and {end_date} "
                f"(OpenBB {PROVIDER} news feed)")

    lines = [f"## {ticker} News, from {start_date} to {end_date} (OpenBB {PROVIDER}):", ""]
    for row in in_range:
        pub = str(row.get("date", ""))[:10]
        title = (row.get("title") or "").strip()
        excerpt = (row.get("excerpt") or "").strip()
        source = row.get("source") or row.get("provider") or ""
        url = row.get("url") or ""
        entry = f"- {pub} | {title}"
        if source:
            entry += f" [{source}]"
        if excerpt:
            entry += f"\n  {excerpt[:300]}"
        if url:
            entry += f"\n  {url}"
        lines.append(entry)

    return "\n".join(lines) + "\n"
