"""Report parity: the shared writer produces the report tree for the CLI and the
programmatic API alike (#1037)."""

import re
from types import SimpleNamespace

import pytest

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.reporting import write_report_tree


def _state():
    return {
        "market_report": "MKT",
        "news_report": "NEWS",
        "investment_debate_state": {"bull_history": "BULL"},
        "investment_plan": "RM PLAN",
        "trader_investment_plan": "TRADE",
        "risk_debate_state": {"neutral_history": "NEUTRAL"},
        "final_trade_decision": "PM DECISION",
    }


SETTINGS = {"version": "0.5.2", "llm_provider": "openai", "deep_think_llm": "gpt-6-sol",
            "quick_think_llm": "gpt-6-luna", "analysts": ["market", "news"], "max_debate_rounds": 1,
            "max_risk_discuss_rounds": 2, "data_vendors": {"core_stock_apis": "yfinance"}}


@pytest.mark.unit
def test_write_report_tree_creates_files(tmp_path):
    out = write_report_tree(_state(), "AAPL", tmp_path)
    assert out.name == "complete_report.md"
    assert (tmp_path / "1_analysts" / "market.md").read_text() == "MKT"
    assert (tmp_path / "1_analysts" / "news.md").read_text() == "NEWS"
    assert (tmp_path / "2_research" / "manager.md").read_text() == "RM PLAN"
    assert (tmp_path / "3_trading" / "trader.md").read_text() == "TRADE"
    assert (tmp_path / "5_portfolio" / "decision.md").read_text() == "PM DECISION"
    complete = out.read_text()
    assert "Trading Analysis Report: AAPL" in complete
    assert "MKT" in complete and "PM DECISION" in complete


@pytest.mark.unit
def test_save_reports_explicit_path(tmp_path):
    graph = SimpleNamespace(run_settings=lambda: SETTINGS)
    out = TradingAgentsGraph.save_reports(graph, _state(), "AAPL", save_path=tmp_path)
    assert (tmp_path / "complete_report.md").exists()
    assert out == tmp_path / "complete_report.md"


@pytest.mark.unit
def test_save_reports_defaults_under_results_dir(tmp_path):
    graph = object.__new__(TradingAgentsGraph)
    graph.config = {"results_dir": str(tmp_path)}
    graph.run_settings = lambda: SETTINGS
    out = graph.save_reports(_state(), "AAPL")
    assert out.exists()
    assert out.parent.parent.name == "reports"  # results_dir/reports/AAPL_<stamp>/...
    assert out.parent.name.startswith("AAPL_")



@pytest.mark.unit
def test_the_report_names_the_analysis_date_and_what_produced_it(tmp_path):
    state = dict(_state(), trade_date="2026-09-23")

    header = write_report_tree(state, "NVDA", tmp_path, settings=SETTINGS).read_text().split("## ")[0]

    assert "Analysis date: 2026-09-23" in header
    assert "TradingAgents 0.5.2" in header
    assert "openai, deep gpt-6-sol, quick gpt-6-luna" in header
    assert "Analysts: market, news" in header
    assert "research debate rounds 1, risk debate rounds 2" in header
    assert "core_stock_apis yfinance" in header


@pytest.mark.unit
def test_run_settings_record_the_run_without_endpoints_or_paths():
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    graph = object.__new__(TradingAgentsGraph)
    graph.selected_analysts = ("market", "news")
    graph.config = {"llm_provider": "openai", "deep_think_llm": "gpt-6-sol", "quick_think_llm": "gpt-6-luna",
                    "max_debate_rounds": 1, "max_risk_discuss_rounds": 1, "output_language": "English",
                    "data_vendors": {"core_stock_apis": "yfinance"}, "tool_vendors": {},
                    "backend_url": "https://user:secret@relay.example/v1", "results_dir": "/home/me/results"}

    settings = graph.run_settings()

    assert settings["analysts"] == ["market", "news"]
    assert settings["deep_think_llm"] == "gpt-6-sol"
    assert "secret" not in str(settings) and "/home/me" not in str(settings)


@pytest.mark.unit
def test_a_partial_settings_dict_still_writes_the_report(tmp_path):
    out = write_report_tree(_state(), "AAPL", tmp_path, settings={"llm_provider": "openai"})
    assert "openai" in out.read_text()


@pytest.mark.unit
def test_run_settings_name_the_version_of_the_running_code():
    import tradingagents
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    graph = object.__new__(TradingAgentsGraph)
    graph.selected_analysts, graph.config = ("market",), {}
    assert graph.run_settings()["version"] == tradingagents.__version__


@pytest.mark.unit
@pytest.mark.parametrize("decision, rating", [
    ("**Rating**: Overweight\n\nAdd on weakness.", "Overweight"),
    ("Add on weakness.", "REVIEW"),           # no readable rating is said, not left out
])
def test_the_report_header_names_the_rating(tmp_path, decision, rating):
    state = dict(_state(), final_trade_decision=decision)

    header = write_report_tree(state, "NVDA", tmp_path, settings=SETTINGS).read_text().split("## ")[0]

    assert f"- Rating: {rating}" in header


@pytest.mark.unit
def test_the_header_names_the_analysts_as_users_select_them(tmp_path):
    settings = {**SETTINGS, "analysts": ["market", "social", "news"]}

    write_report_tree(_state(), "NVDA", tmp_path, settings=settings, html=True)

    markdown = (tmp_path / "complete_report.md").read_text(encoding="utf-8")
    assert "market, sentiment, news" in markdown and "social" not in markdown
    page = (tmp_path / "complete_report.html").read_text(encoding="utf-8")
    fields = re.findall(r'<span class="field">([^<]*)</span>', page)
    assert {"market", "sentiment", "news"} <= set(fields) and "social" not in page


def _date_context():
    """The shape build_date_context() caches: events, politics, odds."""
    return {
        "trade_date": "2026-11-06",
        "events": {"published": [{"date": "2026-11-05", "event": "CPI", "actual": "3.1%",
                                  "consensus": "3.2%", "previous": "3.0%",
                                  "surprise": {"direction": "below", "delta": -0.1, "unit": "pp"}}],
                   "upcoming": [{"date": "2026-11-10", "event": "FOMC", "consensus": "-",
                                 "previous": "-"}]},
        "politics": {"available": True, "source": "OpenBB", "scanned": 40,
                     "window": ["2026-11-03", "2026-11-06"],
                     "topics": [{"label": "Government shutdown",
                                 "articles": [{"date": "2026-11-06", "title": "Talks resume",
                                               "url": "https://example.com/a"}]}]},
        "odds": {"available": True, "as_of": "2026-11-05T14:00:00Z", "markets":
                 [{"question": "Shutdown by Dec 12?", "outcome": "Yes",
                   "probability": 0.26, "volume_usd": 1234, "week_change_pp": None}]},
    }


@pytest.mark.unit
def test_the_backdrop_section_carries_the_dates_events_politics_and_odds(tmp_path):
    state = dict(_state(), date_context=_date_context())

    complete = write_report_tree(state, "NVDA", tmp_path, html=True).read_text()

    assert "## 0.5 Macro & event backdrop" in complete
    assert "### Economic events" in complete and "| CPI |" in complete
    assert "### Political backdrop (2026-11-03 to 2026-11-06)" in complete
    assert "Talks resume" in complete
    assert "Shutdown by Dec 12?" in complete
    # The HTML page carries the same subsections.
    page = (tmp_path / "complete_report.html").read_text(encoding="utf-8")
    assert "Macro &amp; event backdrop" in page and "Talks resume" in page


@pytest.mark.unit
def test_a_run_without_a_date_context_keeps_the_old_backdrop_section(tmp_path):
    complete = write_report_tree(_state(), "NVDA", tmp_path, html=True).read_text()

    assert "0.5" not in complete.split("## I.")[0]
    page = (tmp_path / "complete_report.html").read_text(encoding="utf-8")
    assert "s05" not in page


@pytest.mark.unit
def test_a_date_context_with_only_a_dead_feed_still_degrades_per_part(tmp_path):
    ctx = _date_context()
    ctx["politics"] = {"error": "HTTPError", "available": False, "reason": "feed down",
                       "topics": [], "window": ["2026-11-06", "2026-11-06"],
                       "source": "OpenBB", "scanned": 0}
    ctx["odds"] = {"available": False, "reason": "no snapshot", "markets": []}

    complete = write_report_tree(dict(_state(), date_context=ctx), "NVDA", tmp_path).read_text()

    assert "| CPI |" in complete
    # The politics half carries an error, so the block degrades on its own…
    assert "The news feed was unavailable for this run (HTTPError)" in complete
    # …and the odds half still renders its own "none" line, not an exception.
    assert "Prediction-market odds: none" in complete and "no snapshot" in complete
