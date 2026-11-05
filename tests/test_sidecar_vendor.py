"""The akshare-sidecar vendor: prices, indicators, statements and news.

Every test stubs ``get_rows`` so nothing touches the network; what is asserted
is the mapping from the sidecar's row shape to the agent-facing strings.
"""

from __future__ import annotations

import pytest

from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.vendors.sidecar import fundamentals, market, news


def _bars(days: int, start="2026-01-05", base=100.0):
    import pandas as pd

    dates = pd.bdate_range(start, periods=days)
    return [
        {"date": d.strftime("%Y-%m-%d"), "open": base, "high": base + 1,
         "low": base - 1, "close": base + i * 0.5, "volume": 1_000_000.0, "vwap": None}
        for i, d in enumerate(dates)
    ]


@pytest.mark.unit
def test_stock_data_renders_csv_with_a_domestic_source_header(monkeypatch):
    monkeypatch.setattr(market, "get_rows", lambda *a, **k: _bars(5))
    market._OHLCV_CACHE.clear()

    out = market.get_stock_data("NVDA", "2026-01-05", "2026-01-12")

    assert out.startswith("# Stock data for NVDA from 2026-01-05 to 2026-01-12")
    assert "Tencent via akshare sidecar" in out
    assert "# Total records: 5" in out
    assert "Date,Open,High,Low,Close,Volume" in out
    assert "100.0,101.0,99.0,100.0,1000000.0" in out.splitlines()[5]


@pytest.mark.unit
def test_stock_data_walks_backwards_past_the_400_bar_cap(monkeypatch):
    """Tencent serves 400 bars per request; a longer range must be merged, not truncated."""
    calls = []

    def fake_rows(model, what, params):
        calls.append((params["start_date"], params["end_date"]))
        return _bars(3, start=params["start_date"])

    monkeypatch.setattr(market, "get_rows", fake_rows)
    monkeypatch.setattr(market, "_WINDOW_DAYS", 30)
    market._OHLCV_CACHE.clear()

    frame = market._fetch_ohlcv("NVDA", "2025-01-01", "2026-01-10")

    assert len(calls) > 1, "a multi-year range needs several windows"
    assert frame.index.min() >= __import__("pandas").Timestamp("2025-01-01")
    assert not frame.index.duplicated().any()


@pytest.mark.unit
def test_empty_bars_raise_no_market_data(monkeypatch):
    monkeypatch.setattr(market, "get_rows", lambda *a, **k: [])
    market._OHLCV_CACHE.clear()

    with pytest.raises(NoMarketDataError):
        market.get_stock_data("XXXXXFAKE", "2026-01-05", "2026-01-12")


@pytest.mark.unit
def test_indicator_window_is_computed_locally(monkeypatch):
    monkeypatch.setattr(market, "get_rows", lambda *a, **k: _bars(320))
    market._OHLCV_CACHE.clear()

    out = market.get_stock_stats_indicators_window("NVDA", "close_50_sma", "2026-03-20", 3)

    assert "## close_50_sma values from 2026-03-17 to 2026-03-20" in out
    # 320 bars of history means the 50-day average is populated, not N/A.
    assert any(line.split(": ")[1][0].isdigit() for line in out.splitlines() if ": " in line[10:])


@pytest.mark.unit
def test_statements_report_the_period_count_and_the_annual_only_feed(monkeypatch):
    rows = [
        {"period_ending": f"202{i}-09-27", "fiscal_period": "FY", "fiscal_year": 2020 + i,
         "revenue": 1_000_000_000.0 + i, "net_income": 500_000_000.0}
        for i in range(6)
    ]
    monkeypatch.setattr(fundamentals, "get_rows", lambda *a, **k: rows)

    out = fundamentals.get_income_statement("AAPL", "quarterly", "2026-11-02")

    assert "most recent" not in out.lower() or "annual" in out.lower()
    assert "# NOTE:" in out  # quarterly was asked for, the US feed only files annual
    assert out.count("2025-09-27") >= 1
    assert "1000000005.0" in out  # newest period first, no thousands separators


@pytest.mark.unit
def test_news_filters_to_the_requested_window(monkeypatch):
    monkeypatch.setattr(news, "get_rows", lambda *a, **k: [
        {"date": "2026-10-01 08:00:00", "title": "old", "excerpt": "", "url": "", "author": ""},
        {"date": "2026-10-28 09:00:00", "title": "inside", "excerpt": "x", "url": "u", "author": "东财"},
    ])

    out = news.get_news("NVDA", "2026-10-20", "2026-11-02")

    assert "inside" in out and "old" not in out
    assert "Eastmoney via akshare sidecar" in out


@pytest.mark.unit
def test_news_outside_the_window_is_reported_as_absence_not_failure(monkeypatch):
    monkeypatch.setattr(news, "get_rows", lambda *a, **k: [
        {"date": "2026-10-01 08:00:00", "title": "old", "excerpt": "", "url": "", "author": ""},
    ])

    out = news.get_news("NVDA", "2026-10-20", "2026-11-02")

    assert out.startswith("No news found for NVDA")
