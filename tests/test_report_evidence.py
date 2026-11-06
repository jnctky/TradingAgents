"""The evidence pack: same price rows the analysts saw, verifiable macro readings,
news links and per-call provenance -- prepended to the report as section 0."""

import json

import pandas as pd
import pytest

from tradingagents.reporting import write_report_tree


def _frame(n: int = 60, end: str = "2026-11-05") -> pd.DataFrame:
    dates = pd.bdate_range(end=end, periods=n)
    base = 200.0
    rows = []
    for i, d in enumerate(dates):
        close = base + i * 0.5
        rows.append({
            "Date": d, "Open": close - 1.0, "High": close + 2.0,
            "Low": close - 2.0, "Close": close, "Volume": 1e6 + i * 1e4,
        })
    return pd.DataFrame(rows)


@pytest.fixture
def price_env(monkeypatch):
    """load_ohlcv serves a synthetic frame; FRED and the platform are stubbed."""
    import tradingagents.dataflows.vendors.ohlcv as ohlcv_mod

    monkeypatch.setattr(ohlcv_mod, "load_ohlcv", lambda symbol, as_of, fill_gaps=True: _frame(),
                        raising=True)
    import tradingagents.dataflows.vendors.fred as fred_mod

    def fake_request(path, params):
        if path == "series/observations":
            series = params["series_id"]
            if series == "T10Y2Y":          # the vendor path is tried first; keep it
                return {"observations": [
                    {"date": "2026-10-01", "value": "0.40"},
                    {"date": "2026-11-05", "value": "0.38"},
                ]}
            return {"observations": [
                {"date": "2026-10-01", "value": "120.00"},
                {"date": "2026-11-05", "value": "121.67"},
            ]}
        return {}

    monkeypatch.setattr(fred_mod, "_request", fake_request)
    return monkeypatch


def _trace(tmp_path, lines):
    trace = tmp_path / "trace.jsonl"
    trace.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    return trace


@pytest.mark.unit
def test_build_evidence_assembles_price_macro_trace(tmp_path, price_env):
    from tradingagents.report_assets import build_evidence

    trace = _trace(tmp_path, [
        {"tool": "get_news", "vendor": "sidecar", "argsDisplay": "NVDA, 2026-11-01, 2026-11-05",
         "ok": True, "ms": 120, "sample": "item http://finance.example/a/1.html done"},
        {"tool": "get_indicators", "vendor": "sidecar", "argsDisplay": "NVDA, rsi, 2026-11-05, 30",
         "ok": False, "ms": 88, "error": "upstream 502"},
    ])
    evidence = build_evidence("NVDA", "2026-11-05", trace_path=trace)

    assert evidence["errors"] == []
    price = evidence["price"]
    assert price["latest"]["date"] == "2026-11-05"
    assert price["latest"]["close"] == pytest.approx(229.5)     # 200 + 59 * 0.5
    assert len(price["rows"]) == 60
    assert evidence["macro"][0]["id"] == "DTWEXBGS"
    assert evidence["macro"][0]["vintage"] is True
    assert evidence["news_links"] == ["http://finance.example/a/1.html"]
    calls = evidence["trace"]["calls"]
    assert len(calls) == 2 and calls[1]["ok"] is False


@pytest.mark.unit
def test_macro_falls_back_to_the_platform_without_a_fred_key(tmp_path, price_env, monkeypatch):
    import tradingagents.dataflows.vendors.fred as fred_mod
    import tradingagents.dataflows.vendors.openbb.client as client_mod

    def no_key(*_a, **_k):
        raise RuntimeError("FRED_API_KEY environment variable is not set")

    monkeypatch.setattr(fred_mod, "_request", no_key)
    monkeypatch.setattr(client_mod, "get_json", lambda *a, **k: [
        {"date": "2026-10-01", "DTWEXBGS": 118.0, "DGS10": 4.5, "T10Y2Y": 0.4, "DFF": 3.75},
        {"date": "2026-11-05", "DTWEXBGS": 121.67, "DGS10": 4.9, "T10Y2Y": 0.38, "DFF": 3.88},
    ])

    from tradingagents.report_assets import build_evidence

    evidence = build_evidence("NVDA", "2026-11-05")
    macro = {m["id"]: m for m in evidence["macro"]}
    assert set(macro) == {"DTWEXBGS", "DGS10", "T10Y2Y", "DFF"}
    assert all(m["vintage"] is False for m in macro.values())
    assert macro["DFF"]["points"][-1][1] == pytest.approx(3.88)


@pytest.mark.unit
def test_evidence_charts_and_renderers(tmp_path, price_env):
    from tradingagents.report_assets import (
        build_evidence,
        evidence_charts,
        evidence_html,
        evidence_markdown,
        write_evidence_files,
    )

    evidence = build_evidence("NVDA", "2026-11-05")
    charts = evidence_charts(evidence, inline=False)
    assert [c["name"] for c in charts] == ["price", "rsi", "macd", "macro"]
    for chart in charts:
        assert chart["svg"].startswith("<svg") and chart["svg"].endswith("</svg>")

    written = write_evidence_files(tmp_path, evidence)
    assert [p.name for p in written] == ["price.svg", "rsi.svg", "macd.svg", "macro.svg"]
    assert json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))["ticker"] == "NVDA"

    markdown = evidence_markdown(evidence)
    assert "## 0. Evidence & charts" in markdown
    assert f"close {evidence['price']['latest']['close']:,.2f}" in markdown
    assert "](charts/price.svg)" in markdown
    assert "fred.stlouisfed.org/series/DTWEXBGS" in markdown
    assert "vintage-pinned" in markdown                    # the stub pins vintage; the note says so

    html = evidence_html(evidence)
    assert html.count("<svg") == 4
    assert 'rel="noopener"' in html


@pytest.mark.unit
def test_evidence_survives_a_dead_price_vendor(tmp_path, price_env, monkeypatch):
    import tradingagents.dataflows.vendors.ohlcv as ohlcv_mod

    def boom(*_a, **_k):
        raise RuntimeError("vendor down")

    monkeypatch.setattr(ohlcv_mod, "load_ohlcv", boom)

    from tradingagents.report_assets import build_evidence

    evidence = build_evidence("NVDA", "2026-11-05")
    assert "price" not in evidence
    assert any(error.startswith("price:") for error in evidence["errors"])
    assert evidence["macro"]                              # the rest still serves


@pytest.mark.unit
def test_build_evidence_returns_none_when_nothing_serves(tmp_path, price_env, monkeypatch):
    import tradingagents.dataflows.vendors.ohlcv as ohlcv_mod

    monkeypatch.setattr(ohlcv_mod, "load_ohlcv", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))

    from tradingagents.report_assets import build_evidence

    # macro still answers here, so the pack is not None -- kill both:
    import tradingagents.report_assets as ra

    assert build_evidence("NVDA", "2026-11-05", trace_path=tmp_path / "missing.jsonl") is not None
    monkeypatch.setattr(ra, "_macro_block", lambda *_: None)
    assert build_evidence("NVDA", "2026-11-05", trace_path=tmp_path / "missing.jsonl") is None


@pytest.mark.unit
def test_write_report_tree_prepends_the_evidence_section(tmp_path, price_env):
    from tradingagents.report_assets import build_evidence

    state = {
        "trade_date": "2026-11-05",
        "market_report": "MKT", "final_trade_decision": "**Rating**: Overweight",
    }
    evidence = build_evidence("NVDA", "2026-11-05")
    out = write_report_tree(state, "NVDA", tmp_path, html=False, evidence=evidence)

    markdown = out.read_text(encoding="utf-8")
    # evidence section sits after the header, before the run sections
    assert markdown.index("# Trading Analysis Report") < markdown.index("## 0. Evidence & charts")
    assert markdown.index("## 0. Evidence & charts") < markdown.index("## I.")
    assert (tmp_path / "charts" / "price.svg").exists()
    assert (tmp_path / "evidence.json").exists()


@pytest.mark.unit
def test_the_html_report_carries_the_evidence_section(tmp_path, price_env):
    from tradingagents.report_assets import build_evidence

    state = {"trade_date": "2026-11-05", "market_report": "MKT",
             "final_trade_decision": "**Rating**: Overweight"}
    evidence = build_evidence("NVDA", "2026-11-05")
    write_report_tree(state, "NVDA", tmp_path, html=True, evidence=evidence)

    page = (tmp_path / "complete_report.html").read_text(encoding="utf-8")
    assert 'id="s0"' in page
    assert "Evidence &amp; charts" in page
    assert page.count('<figure class="chart">') == 4      # inline SVG, no external assets
    assert "default-src 'none'" in page                   # CSP unchanged: inline SVG needs no img-src
