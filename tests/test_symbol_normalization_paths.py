"""Symbol normalization and identity resolution on the *live* data paths.

Regression tests for #983 (instrument identity), #984 (reflection returns) and
the news path: a broker symbol like XAUUSD must resolve to the same instrument
the price path prices (GC=F), so identity, realized-return and news lookups hit
the right thing instead of failing or mismatching.

The identity/news lookups moved from yfinance to the akshare sidecar (with the
OpenBB platform as fallback), so these tests patch the vendors that are wired
today rather than the retired yahoo modules — an unpatched call would reach the
network and fail for reasons that have nothing to do with normalization.
"""
import pandas as pd
import pytest

import tradingagents.agents.context as au
import tradingagents.dataflows.vendors.openbb as openbb
import tradingagents.dataflows.vendors.sidecar.news as sidecar_news
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.memory import settlement


@pytest.fixture(autouse=True)
def _clear_identity_cache():
    au._identity.cache_clear()
    yield
    au._identity.cache_clear()


def test_identity_resolves_through_the_sidecar_profile_feed(monkeypatch):
    seen = {}

    def fake_profile(symbol):
        seen["symbol"] = symbol
        return {"longName": "Gold Futures", "quoteType": "FUTURE"}

    monkeypatch.setattr(au, "get_company_profile", fake_profile)

    identity = au.resolve_instrument_identity("XAUUSD")

    # The sidecar speaks Sina/Eastmoney symbols, so it takes the broker symbol
    # as-is; normalizing to GC=F is the price path's job (yahoo/ohlcv).
    assert seen["symbol"] == "XAUUSD"
    assert identity.get("company_name") == "Gold Futures"


def test_identity_falls_back_to_openbb_when_the_sidecar_has_nothing(monkeypatch):
    def dead_sidecar(symbol):
        raise VendorUnavailableError(f"sidecar has no profile for {symbol}")

    monkeypatch.setattr(au, "get_company_profile", dead_sidecar)
    monkeypatch.setattr(openbb, "get_company_profile", lambda symbol: {"longName": "Gold Futures"})

    identity = au.resolve_instrument_identity("XAUUSD")

    assert identity.get("company_name") == "Gold Futures"


def test_identity_fails_open_when_every_source_is_down(monkeypatch):
    def boom(symbol):
        raise RuntimeError("everything is down")

    monkeypatch.setattr(au, "get_company_profile", boom)
    monkeypatch.setattr(openbb, "get_company_profile", boom)

    # A missing identity must degrade to ticker-only context, never block a run.
    assert au.resolve_instrument_identity("XAUUSD") == {}


def test_fetch_returns_normalizes_symbol(monkeypatch):
    queried = []

    def fake_load_ohlcv(symbol, as_of_date, fill_gaps=True):
        queried.append(symbol)
        prices = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0]
        idx = pd.date_range(start="2025-01-02", periods=len(prices), freq="D")
        return pd.DataFrame({"Close": prices}, index=idx)

    monkeypatch.setattr(settlement, "load_ohlcv", fake_load_ohlcv)

    raw, alpha, days, resolved = settlement.fetch_returns(
        "XAUUSD", "2025-01-02", holding_days=5, benchmark="SPY"
    )

    assert queried[0] == "GC=F"  # stock symbol normalized (#984)
    assert queried[1] == "SPY"   # benchmark left as the canonical symbol
    assert raw is not None and days is not None
    assert resolved == "2025-01-08"  # resolution date recorded (#1251)


def test_news_keeps_the_users_ticker_and_only_the_window(monkeypatch):
    rows = [
        {"date": "2025-01-05 10:00:00", "title": "Gold rallies", "author": "Eastmoney",
         "excerpt": "Spot gold up on safe-haven demand", "url": "https://example.com/1"},
        # Outside the window — must not be rendered as if it were recent news.
        {"date": "2024-12-20 09:00:00", "title": "Stale headline", "author": "", "excerpt": "", "url": ""},
    ]
    seen = {}

    def fake_rows(model, label, params, *a, **k):
        seen["params"] = params
        return rows

    monkeypatch.setattr(sidecar_news, "get_rows", fake_rows)

    out = sidecar_news.get_news("XAUUSD", "2025-01-01", "2025-01-10")

    assert seen["params"] == {"symbol": "XAUUSD"}   # the user's symbol is what we asked for
    assert "XAUUSD" in out                          # and it stays in the report
    assert "Gold rallies" in out
    assert "Stale headline" not in out


def test_news_reports_an_empty_window_instead_of_blaming_the_vendor(monkeypatch):
    monkeypatch.setattr(
        sidecar_news, "get_rows",
        lambda *a, **k: [{"date": "2024-01-01 09:00:00", "title": "Old", "author": "", "excerpt": "", "url": ""}],
    )

    out = sidecar_news.get_news("XAUUSD", "2025-01-01", "2025-01-10")

    assert out.startswith("No news found for XAUUSD")
