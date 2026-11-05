"""akshare sidecar (domestic data path): prices, indicators, fundamentals, news.

Runs against the local FastAPI sidecar shared with OpenAlice (:47995), which
fronts Tencent (quotes/klines), Sina and Eastmoney (statements, news). No API
key, no proxy, no rate limit — the replacement for the throttled yfinance path.
"""

from .fundamentals import (
    get_balance_sheet,
    get_cashflow,
    get_fundamentals,
    get_income_statement,
)
from .market import get_stock_data, get_stock_stats_indicators_window
from .news import get_news

__all__ = [
    "get_balance_sheet",
    "get_cashflow",
    "get_fundamentals",
    "get_income_statement",
    "get_news",
    "get_stock_data",
    "get_stock_stats_indicators_window",
]
