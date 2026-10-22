"""Smoke test: OpenBB-served global news + vendor-routed verified snapshot.

Run with the local platform up:
    .venv/Scripts/python.exe my_tools/test_openbb_gapfill.py
"""

from __future__ import annotations

import sys
import time

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.dataflows import router
from tradingagents.dataflows.vendors import ohlcv as ohlcv_dispatch
from tradingagents.dataflows.vendors.openbb import news as openbb_news
from tradingagents.dataflows.vendors.yahoo import snapshot as snapshot_mod

AS_OF = "2026-10-21"
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILED.append(name)


def main() -> int:
    # 1. routing: openbb is registered and listed first for global news
    vendors = list(router.VENDOR_METHODS["get_global_news"])
    check("get_global_news vendor order", vendors[0] == "openbb", str(vendors))
    check(
        "news_data chain starts with openbb",
        DEFAULT_CONFIG["data_vendors"]["news_data"].split(",")[0] == "openbb",
        DEFAULT_CONFIG["data_vendors"]["news_data"],
    )
    check(
        "proxy basket configured",
        bool(DEFAULT_CONFIG.get("global_news_proxy_symbols")),
        ",".join(DEFAULT_CONFIG.get("global_news_proxy_symbols") or []),
    )

    # 2. global news through the router (i.e. what the news analyst calls)
    t0 = time.time()
    text = router.route_to_vendor("get_global_news", AS_OF, 7, 8)
    elapsed = time.time() - t0
    check("global news returns prose", isinstance(text, str) and "##" in text, f"{len(text)} chars in {elapsed:.1f}s")
    check("global news is not a fallback sentinel", "DATA_UNAVAILABLE" not in text and "NO_DATA_AVAILABLE" not in text)
    check("global news cites the OpenBB source", "OpenBB Platform" in text)
    check("global news has no rows past the analysis date", "| 2026-10-22 " not in text and " 2026-10-22 |" not in text)
    if text:
        print("\n--- global news preview ---")
        print("\n".join(text.splitlines()[:14]))
        print()

    # 3. verified snapshot: historical row + indicators, now vendor-routed
    snap = snapshot_mod.build_verified_market_snapshot("AAPL", AS_OF, look_back_days=5)
    check("snapshot has the verified OHLCV table", "### Latest verified OHLCV row" in snap)
    check("snapshot has indicators", "### Verified technical indicators" in snap)
    check("snapshot has the quote cross-check", "### Quote cross-check" in snap)
    check("snapshot 50-SMA is numeric", "close_50_sma | N/A" not in snap)
    check("snapshot flags consistency with the quote", "consistent" in snap or "DISCREPANCY" in snap)
    print("\n--- snapshot preview ---")
    print("\n".join(snap.splitlines()[:40]))
    print()

    # 4. the loader chain must not depend on yfinance when openbb answers
    original = ohlcv_dispatch._loaders

    def openbb_only() -> dict:
        loaders = original()
        def explode(*_a, **_k):
            raise AssertionError("yfinance loader was called although openbb should answer")
        return {**loaders, "yfinance": explode}

    ohlcv_dispatch._loaders = openbb_only  # type: ignore[assignment]
    try:
        frame = ohlcv_dispatch.load_ohlcv("AAPL", AS_OF, fill_gaps=False)
        check("openbb serves OHLCV without touching yfinance", len(frame) > 200, f"{len(frame)} rows")
        check("loader keeps the Date column", "Date" in frame.columns, str(list(frame.columns)))
        check("loader sorts ascending", frame["Date"].is_monotonic_increasing)
        check("loader respects the as_of cutoff", str(frame["Date"].max())[:10] <= AS_OF, str(frame["Date"].max())[:10])
    finally:
        ohlcv_dispatch._loaders = original  # type: ignore[assignment]

    # 5. a historical analysis date must not get a live quote section
    old = snapshot_mod.build_verified_market_snapshot("AAPL", "2026-03-02", look_back_days=3)
    check("historical snapshot skips the live quote", "Skipped:" in old, 
          next((ln for ln in old.splitlines() if "Skipped:" in ln), "")[:90])

    # 6. keyless macro indicators (no FRED key configured on this box)
    macro_vendors = list(router.VENDOR_METHODS["get_macro_indicators"])
    check("get_macro_indicators lists openbb", macro_vendors[0] == "openbb", str(macro_vendors))
    check(
        "macro_data chain starts with openbb",
        DEFAULT_CONFIG["data_vendors"]["macro_data"].split(",")[0] == "openbb",
        DEFAULT_CONFIG["data_vendors"]["macro_data"],
    )
    for alias, expect in (("cpi", "%"), ("unemployment", "%"), ("fed_funds_rate", "%"),
                          ("10y_treasury", "%"), ("yield_curve", "spread")):
        text = router.route_to_vendor("get_macro_indicators", alias, AS_OF, 540)
        ok = (
            isinstance(text, str)
            and "DATA_UNAVAILABLE" not in text
            and "could not load" not in text
            and expect in text
        )
        check(f"macro {alias} via openbb", ok,
              next((ln for ln in text.splitlines() if "Latest" in ln), text.splitlines()[0])[:100])

    bad = router.route_to_vendor("get_macro_indicators", "core_pce", AS_OF, 365)
    check("unknown macro alias returns guidance, not a crash",
          "Supported aliases" in bad and "FRED-only" in bad)

    print()
    if FAILED:
        print(f"FAILED ({len(FAILED)}): {FAILED}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
