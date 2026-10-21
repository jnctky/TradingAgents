"""Smoke-test the OpenBB vendor: every function for AAPL + router fallback.

Usage:
    .venv/Scripts/python.exe my_tools/test_openbb_vendor.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tradingagents.dataflows.config import set_config  # noqa: E402
from tradingagents.dataflows.router import route_to_vendor  # noqa: E402

SYMBOL = "AAPL"
START, END = "2026-09-01", "2026-10-21"


def run(name, fn, *args, **kwargs):
    try:
        out = fn(*args, **kwargs)
        text = str(out)
        print(f"[OK]   {name}: {len(text)} chars | head: {text[:120]!r}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
        return False


def main() -> int:
    from tradingagents.dataflows.vendors import openbb

    ok = True
    ok &= run("get_stock_data", openbb.get_stock_data, SYMBOL, START, END)
    ok &= run("get_indicators/rsi", openbb.get_stock_stats_indicators_window,
              SYMBOL, "rsi", END, 14)
    ok &= run("get_indicators/macd", openbb.get_stock_stats_indicators_window,
              SYMBOL, "macd", END, 30)
    ok &= run("get_indicators/close_200_sma", openbb.get_stock_stats_indicators_window,
              SYMBOL, "close_200_sma", END, 10)
    ok &= run("get_fundamentals", openbb.get_fundamentals, SYMBOL)
    ok &= run("get_balance_sheet", openbb.get_balance_sheet, SYMBOL, "quarterly")
    ok &= run("get_cashflow", openbb.get_cashflow, SYMBOL, "annual")
    ok &= run("get_income_statement", openbb.get_income_statement, SYMBOL, "quarterly")
    ok &= run("get_insider_transactions", openbb.get_insider_transactions, SYMBOL)
    ok &= run("get_news", openbb.get_news_openbb, SYMBOL, "2026-10-14", END)

    # Router: configured chain starts with openbb.
    set_config({})  # ensure defaults initialised
    print("\n== router through configured chain (openbb first) ==")
    ok &= run("route get_stock_data", route_to_vendor, "get_stock_data", SYMBOL, START, END)
    ok &= run("route get_fundamentals", route_to_vendor, "get_fundamentals", SYMBOL)

    # Fallback: point openbb at a dead port; the chain must land on yfinance.
    print("\n== fallback test (openbb unreachable) ==")
    os.environ["OPENBB_BASE_URL"] = "http://127.0.0.1:9"
    ok &= run("route get_stock_data -> yfinance",
              route_to_vendor, "get_stock_data", SYMBOL, START, END)

    # Invalid symbol must surface NO_DATA, not silence.
    print("\n== invalid symbol ==")
    out = route_to_vendor("get_stock_data", "ZZZZZZZZ", START, END)
    looks_right = "NO_DATA_AVAILABLE" in str(out) or "DATA_UNAVAILABLE" in str(out)
    print(f"[{'OK' if looks_right else 'FAIL'}] invalid symbol -> {str(out)[:100]}")
    ok &= looks_right

    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
