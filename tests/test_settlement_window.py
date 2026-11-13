"""The settlement window must never ask for prices that cannot exist yet.

``fetch_returns`` reserves a calendar span for the holding days: about 7 days
per 5 sessions, plus a week for holidays. That span may reach into the future.
Vendors refuse a frame whose latest row sits far behind the requested end
(``MAX_OHLCV_STALE_DAYS``, #1021), so a future end made them reject perfectly
correct prices — and every decision whose window was still open was reported as
"could not be settled" instead of quietly staying pending (#1169).

These tests reproduce that guard, so the regression is pinned to the real
mechanism rather than to the patch.
"""

import pandas as pd

from tradingagents.memory import settlement

STALE_DAYS = 10


def _patch_market(monkeypatch, prices_by_symbol, start="2026-11-09"):
    """Serve closes from memory, mirroring the vendors' staleness guard.

    Returns the ``(symbol, start_date, end_date)`` calls the settlement path
    made, so a test can assert what it asked for.
    """
    calls = []
    series = {
        symbol: pd.Series(prices, index=pd.date_range(start, periods=len(prices), freq="D"))
        for symbol, prices in prices_by_symbol.items()
    }

    def fake(symbol, start_date, end_date):
        calls.append((symbol, start_date, end_date))
        s = series.get(symbol)
        if s is None:
            return pd.Series(dtype=float)
        window = s[(s.index >= pd.Timestamp(start_date)) & (s.index <= pd.Timestamp(end_date))]
        if not window.empty:
            latest = window.index.max()
            if latest < pd.Timestamp(end_date) - pd.Timedelta(days=STALE_DAYS):
                raise RuntimeError(
                    f"latest row is {latest:%Y-%m-%d}, more than {STALE_DAYS} days behind "
                    f"the requested end {end_date}"
                )
        return window[window.index < pd.Timestamp(end_date)]

    monkeypatch.setattr(settlement, "get_closes", fake)
    return calls


def test_open_window_stays_pending_instead_of_failing(monkeypatch):
    """A decision made yesterday has not traded its window yet.

    Before the cap, the reserved end (trade_date + 15 days) was in the future,
    so the guard raised on the newest — and only — row available and the run
    reported a settlement failure for a decision that was simply not due.
    """
    _patch_market(monkeypatch, {"NVDA": [100.0, 101.0, 102.0, 103.0],
                                "SPY": [400.0, 401.0, 402.0, 403.0]})

    assert settlement.fetch_returns("NVDA", "2026-11-09", today="2026-11-12") == (
        None, None, None, None)


def test_requested_end_never_exceeds_today(monkeypatch):
    """The reserved span is a reservation, not a request for future prices."""
    calls = _patch_market(monkeypatch, {"NVDA": [100.0, 101.0, 102.0],
                                        "SPY": [400.0, 401.0, 402.0]})

    settlement.fetch_returns("NVDA", "2026-11-09", today="2026-11-11")

    assert calls, "expected the market to be queried"
    assert [end for _, _, end in calls] == ["2026-11-11", "2026-11-11"]


def test_today_before_trade_date_stays_pending(monkeypatch):
    """A run dated ahead of today settles nothing and must not crash."""
    _patch_market(monkeypatch, {"NVDA": [100.0, 101.0], "SPY": [400.0, 401.0]})

    assert settlement.fetch_returns("NVDA", "2026-11-20", today="2026-11-12") == (
        None, None, None, None)


def test_completed_window_still_settles(monkeypatch):
    """Capping the end must not change a window that has already traded."""
    prices = [100.0 + i for i in range(10)]          # 2026-11-09 .. 2026-11-18
    bench = [400.0 + i for i in range(10)]
    _patch_market(monkeypatch, {"NVDA": prices, "SPY": bench})

    raw, alpha, days, resolved = settlement.fetch_returns(
        "NVDA", "2026-11-09", today="2026-11-19")

    assert days == 5
    # Entry is the first session after the decision date (#1518); the fifth
    # session after it is 11-15, the point in time the outcome became known.
    assert resolved == "2026-11-15"
    assert raw is not None and alpha is not None


def test_default_today_uses_the_current_date(monkeypatch):
    """Without an override the cap follows the clock, not a hard-coded date."""
    calls = _patch_market(monkeypatch, {"NVDA": [100.0], "SPY": [400.0]})

    settlement.fetch_returns("NVDA", "2026-11-09")

    ends = {end for _, _, end in calls}
    assert len(ends) == 1
    (end,) = ends
    assert end <= pd.Timestamp.now().strftime("%Y-%m-%d")
