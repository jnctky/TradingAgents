"""Crypto routing (2026-11-11): coins must be answered by crypto vendors.

Why this exists: the equity chain cannot serve a coin. Before this routing, a
BNB request walked sidecar (400) → OpenBB (204) → alpha_vantage (no key) and the
run recorded "no data for the instrument", which is how a five-day price
blackout turned into a report without a price. The rule pinned here:

- prices and indicators for a coin go to Binance first, the sidecar second;
- fundamentals for a coin are answered "not applicable", never fetched;
- equities are untouched.
"""

import json

import pandas as pd
import pytest

from tradingagents.dataflows import router as router_mod
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.symbols import is_crypto_symbol
from tradingagents.dataflows.vendors import crypto_binance as binance_mod
from tradingagents.dataflows.vendors import ohlcv as vendor_ohlcv


class TestCryptoSymbolDetection:
    @pytest.mark.parametrize("symbol", ["BTC", "btc", "BNB", "ETH-USD", "BTCUSDT", "SOL/USD"])
    def test_recognises_coins_whatever_the_shape(self, symbol):
        assert is_crypto_symbol(symbol) is True

    @pytest.mark.parametrize("symbol", ["NVDA", "AAPL", "600519.SS", "0700.HK", ""])
    def test_leaves_equities_alone(self, symbol):
        assert is_crypto_symbol(symbol) is False

    def test_tolerates_non_strings(self):
        assert is_crypto_symbol(None) is False
        assert is_crypto_symbol(123) is False


@pytest.fixture
def calls(monkeypatch):
    """Record which vendor implementations the router reaches."""
    seen: list[tuple[str, tuple]] = []

    def _vendor(name):
        def _impl(*args, **kwargs):
            seen.append((name, args))
            return f"{name}:{args[0] if args else ''}"

        return _impl

    monkeypatch.setitem(router_mod.VENDOR_METHODS["get_stock_data"], "binance", _vendor("binance"))
    monkeypatch.setitem(router_mod.VENDOR_METHODS["get_stock_data"], "sidecar", _vendor("sidecar"))
    monkeypatch.setitem(router_mod.VENDOR_METHODS["get_stock_data"], "openbb", _vendor("openbb"))
    monkeypatch.setitem(router_mod.VENDOR_METHODS["get_indicators"], "binance", _vendor("binance_ind"))
    monkeypatch.setitem(router_mod.VENDOR_METHODS["get_indicators"], "sidecar", _vendor("sidecar_ind"))
    monkeypatch.setitem(router_mod.VENDOR_METHODS["get_indicators"], "openbb", _vendor("openbb_ind"))
    return seen


class TestCryptoChain:
    def test_prices_for_a_coin_do_not_touch_equity_vendors(self, calls):
        out = router_mod.route_to_vendor("get_stock_data", "BNB", "2026-10-01", "2026-11-10")

        assert out.startswith("binance")
        assert [name for name, _ in calls] == ["binance"]

    def test_indicators_for_a_coin_use_the_same_chain(self, calls):
        router_mod.route_to_vendor("get_indicators", "ETH", "rsi", "2026-11-10", 5)

        assert [name for name, _ in calls] == ["binance_ind"]

    def test_sidecar_is_the_declared_fallback(self, calls, monkeypatch):
        def _down(*args, **kwargs):
            calls.append(("binance", args))
            raise VendorUnavailableError("binance unreachable")

        monkeypatch.setitem(router_mod.VENDOR_METHODS["get_stock_data"], "binance", _down)

        out = router_mod.route_to_vendor("get_stock_data", "BTC", "2026-10-01", "2026-11-10")

        assert out.startswith("sidecar")
        assert [name for name, _ in calls] == ["binance", "sidecar"]

    def test_an_equity_still_walks_the_configured_chain(self, calls):
        router_mod.route_to_vendor("get_stock_data", "NVDA", "2026-10-01", "2026-11-10")

        assert [name for name, _ in calls] == ["sidecar"]

    def test_an_explicit_binance_in_config_still_wins(self, calls, monkeypatch):
        monkeypatch.setattr(router_mod, "get_vendor", lambda category, method=None: "binance,sidecar")
        monkeypatch.setitem(router_mod.VENDOR_METHODS["get_stock_data"], "binance",
                            lambda *a, **k: calls.append(("binance", a)) or "binance:configured")

        out = router_mod.route_to_vendor("get_stock_data", "BNB", "2026-10-01", "2026-11-10")

        assert out == "binance:configured"


class TestCryptoFundamentals:
    def test_answered_not_applicable_without_calling_any_vendor(self, monkeypatch):
        called: list[str] = []
        monkeypatch.setitem(router_mod.VENDOR_METHODS["get_fundamentals"], "sidecar",
                            lambda *a, **k: called.append("sidecar"))
        monkeypatch.setitem(router_mod.VENDOR_METHODS["get_fundamentals"], "openbb",
                            lambda *a, **k: called.append("openbb"))

        out = router_mod.route_to_vendor("get_fundamentals", "BNB", "2026-11-10")

        assert out.startswith("NOT_APPLICABLE")
        assert called == []

    def test_the_absence_is_traced_as_not_applicable(self, tmp_path, monkeypatch):
        trace = tmp_path / "trace.jsonl"
        monkeypatch.setenv("TA_TRACE_FILE", str(trace))

        for method in ("get_fundamentals", "get_balance_sheet"):
            router_mod.route_to_vendor(method, "BNB", "2026-11-10")

        records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]

        assert {r["vendor"] for r in records} == {"not_applicable"}
        assert {r["tool"] for r in records} == {"get_fundamentals", "get_balance_sheet"}

    def test_an_equity_fundamentals_call_is_untouched(self, monkeypatch):
        monkeypatch.setitem(router_mod.VENDOR_METHODS["get_fundamentals"], "sidecar",
                            lambda *a, **k: "fundamentals:sidecar")

        out = router_mod.route_to_vendor("get_fundamentals", "NVDA", "2026-11-10")

        assert out == "fundamentals:sidecar"


class TestSettlementPricePath:
    """The verification snapshot and settlement read prices outside the router.

    They call ``vendors.ohlcv.load_ohlcv`` directly, so its chain needs the same
    crypto rule — otherwise a coin's settling price is reported "unavailable"
    while the analyst's own price series came back fine.
    """

    def test_a_coin_takes_binance_first(self):
        loaders = {"sidecar": object(), "openbb": object(), "binance": object()}

        assert vendor_ohlcv.resolve_chain("BNB", loaders) == ["binance", "sidecar", "openbb"]

    def test_an_equity_keeps_the_configured_order(self):
        loaders = {"sidecar": object(), "openbb": object(), "binance": object()}

        assert vendor_ohlcv.resolve_chain("NVDA", loaders) == ["sidecar", "openbb"]

    def test_an_explicit_binance_in_config_keeps_its_place(self, monkeypatch):
        monkeypatch.setattr(vendor_ohlcv, "configured_chain", lambda: ["sidecar", "binance"])
        loaders = {"sidecar": object(), "openbb": object(), "binance": object()}

        assert vendor_ohlcv.resolve_chain("BNB", loaders) == ["sidecar", "binance"]

    def test_the_frame_speaks_the_loader_contract(self, monkeypatch):
        # Same columns as sidecar/openbb so settlement never sees a coin-shaped
        # frame that only one code path understands.
        frame = pd.DataFrame({
            "Date": pd.to_datetime(["2026-11-09", "2026-11-10"]),
            "open": [1.0, 2.0], "high": [1.5, 2.5], "low": [0.5, 1.5],
            "close": [1.2, 2.2], "volume": [10.0, 20.0],
        }).set_index("Date")
        monkeypatch.setattr(binance_mod, "daily_bars", lambda *a, **k: frame)

        out = binance_mod.load_ohlcv("BNB", "2026-11-10")

        assert list(out.columns) == ["Date", "Open", "High", "Low", "Close", "Volume", "Adj Close"]
        assert out["Adj Close"].tolist() == [1.2, 2.2]
        assert out["Date"].dt.strftime("%Y-%m-%d").tolist() == ["2026-11-09", "2026-11-10"]

    def test_a_coin_has_no_insiders(self, monkeypatch):
        called: list[str] = []
        monkeypatch.setitem(router_mod.VENDOR_METHODS["get_insider_transactions"], "openbb",
                            lambda *a, **k: called.append("openbb"))

        out = router_mod.route_to_vendor("get_insider_transactions", "BNB", "2026-11-10")

        assert out.startswith("NOT_APPLICABLE")
        assert called == []
