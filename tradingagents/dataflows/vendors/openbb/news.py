"""OpenBB: company news and global/macro news from the local platform.

The platform exposes no dedicated world-news route, so global news is assembled
from the company-news route over a basket of market-wide proxy tickers (broad
index ETFs, plus rates / gold / energy / dollar ETFs). Those pages carry the
macro headlines a news analyst needs — Fed policy, index moves, oil and rates —
without depending on a rate-limited third party.
"""

import logging
from datetime import datetime, timedelta
from typing import Annotated

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import NoMarketDataError

from .client import PROVIDER, get_json

logger = logging.getLogger(__name__)

# Fallback basket if the config does not provide one. Broad market first, then
# the cross-asset lenses a macro read needs.
DEFAULT_PROXY_SYMBOLS: tuple[str, ...] = (
    "SPY", "QQQ", "DIA", "TLT", "GLD", "USO", "UUP",
)

PROXY_LABELS = {
    "SPY": "S&P 500", "QQQ": "Nasdaq 100", "DIA": "Dow 30", "IWM": "Russell 2000",
    "TLT": "US Treasuries", "GLD": "Gold", "USO": "Crude oil", "UUP": "US dollar",
    "^GSPC": "S&P 500 index", "^IXIC": "Nasdaq Composite",
}


def _pub_dt(row: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(str(row.get("date", ""))[:19])
    except ValueError:
        return None


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
    lines += [_format_entry(row) for row in in_range]

    return "\n".join(lines) + "\n"


def _format_entry(row: dict) -> str:
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
    return entry


def get_global_news_openbb(
    as_of_date: Annotated[str, "Current date in yyyy-mm-dd format"],
    look_back_days: Annotated[int | None, "Days to look back"] = None,
    limit: Annotated[int | None, "Max articles to return"] = None,
) -> str:
    """Global / macro news assembled from market-wide proxy tickers.

    Each proxy's company-news page carries the macro stories that move the tape
    (index moves, Fed policy, rates, oil, gold, the dollar). Results are merged,
    de-duplicated by headline and clipped to the look-back window, so the news
    analyst gets a broad macro read from one vendor that has no tight rate limit.
    """
    config = get_config()
    if look_back_days is None:
        look_back_days = config.get("global_news_lookback_days", 7)
    if limit is None:
        limit = config.get("global_news_article_limit", 10)
    proxies = list(config.get("global_news_proxy_symbols") or DEFAULT_PROXY_SYMBOLS)

    as_of_dt = datetime.strptime(as_of_date, "%Y-%m-%d")
    start_dt = as_of_dt - timedelta(days=int(look_back_days))
    end_dt = as_of_dt.replace(hour=23, minute=59)
    start_str = start_dt.strftime("%Y-%m-%d")

    # Ask each proxy for more than we need: most of a basket's headlines repeat
    # across proxies, and out-of-window rows must not consume the budget.
    per_proxy = max(int(limit) * 2, 15)
    collected: list[dict] = []
    seen: set[str] = set()
    answered = 0

    for symbol in proxies:
        try:
            rows = get_json(
                "/api/v1/news/company",
                f"global news via {symbol}",
                params={
                    "symbol": symbol,
                    "provider": PROVIDER,
                    "limit": per_proxy,
                    "start_date": start_str,
                    "end_date": as_of_date,
                },
            )
        except NoMarketDataError:
            continue  # one proxy without a news page is not a vendor failure
        answered += 1
        for row in rows:
            pub_dt = _pub_dt(row)
            if pub_dt is None or not (start_dt <= pub_dt <= end_dt):
                continue  # never leak a row past the analysis date
            key = " ".join((row.get("title") or "").lower().split())
            if not key or key in seen:
                continue
            seen.add(key)
            collected.append({**row, "_proxy": symbol})

    if not answered:
        # Every proxy failed: let the router fall through to the next vendor.
        raise NoMarketDataError(
            ",".join(proxies),
            detail="OpenBB company-news route returned no data for any macro proxy",
        )

    collected.sort(key=lambda r: _pub_dt(r) or datetime.min, reverse=True)
    selected = collected[: int(limit)]

    if not selected:
        return (f"No global news found between {start_str} and {as_of_date} "
                f"(OpenBB {PROVIDER} via market proxies {', '.join(proxies)})")

    lenses = ", ".join(
        f"{s} ({PROXY_LABELS[s]})" if s in PROXY_LABELS else s for s in proxies
    )
    lines = [
        f"## Global / macro news, {start_str} to {as_of_date}",
        f"Source: OpenBB Platform ({PROVIDER}) market-wide news via {lenses}.",
        "",
    ]
    lines += [_format_entry(row) for row in selected]
    return "\n".join(lines) + "\n"

