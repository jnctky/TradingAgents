"""Reusable report-tree writer shared by the CLI and the programmatic API.

Writes a run's per-section markdown (analysts, research, trading, risk,
portfolio) plus a consolidated ``complete_report.md`` under ``save_path``. The
CLI and ``TradingAgentsGraph.save_reports`` both call this, so a headless / API
run produces the same on-disk report tree a CLI run does.
"""

from datetime import datetime
from pathlib import Path

from tradingagents.agents.rating import run_rating


def analyst_names(keys) -> list[str]:
    """The analysts as users select them: the sentiment analyst's key is "social"."""
    return ["sentiment" if key == "social" else key for key in keys or []]


def _header(ticker: str, final_state: dict, settings: dict | None,
            evidence: dict | None = None) -> str:
    """The report's title and what produced it: analysis date, version, models, analysts, vendors."""
    lines = [f"# Trading Analysis Report: {ticker}", ""]
    if final_state.get("trade_date"):
        lines.append(f"- Analysis date: {final_state['trade_date']}")
    if final_state.get("final_trade_decision"):
        lines.append(f"- Rating: {run_rating(final_state)}")
    lines.append(f"- Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if settings:
        s = settings.get
        deep_provider = s("deep_think_provider") or s("llm_provider", "?")
        quick_provider = s("quick_think_provider") or s("llm_provider", "?")
        if deep_provider == quick_provider:
            models = f"{deep_provider}, deep {s('deep_think_llm', '?')}, quick {s('quick_think_llm', '?')}"
        else:   # each tier on its own provider (#1440)
            models = (f"deep {deep_provider} {s('deep_think_llm', '?')}, "
                      f"quick {quick_provider} {s('quick_think_llm', '?')}")
        lines.append(f"- TradingAgents {s('version', '?')}: {models}")
        lines.append(f"- Analysts: {', '.join(analyst_names(s('analysts')))}; "
                     f"research debate rounds {s('max_debate_rounds', '?')}, "
                     f"risk debate rounds {s('max_risk_discuss_rounds', '?')}")
        vendors = {**(s("data_vendors") or {}), **(s("tool_vendors") or {})}
        if vendors:
            lines.append("- Data vendors: " + ", ".join(f"{k} {v}" for k, v in vendors.items()))
    if final_state.get("memory_note"):
        lines.append(f"- Memory log: {final_state['memory_note']}")
    # 数据完整性（2026-11-11 用户核心原则）：报告必须开门见山地声明数据状态。
    if evidence and evidence.get("data_integrity"):
        from tradingagents.dataflows.data_integrity import header_line

        line = header_line(evidence["data_integrity"])
        if line:
            lines.append(line)
    return "\n".join(lines) + "\n\n"


# The report's sections in order: (heading, folder, [(agent, file, state path)]).
_SECTIONS = (
    ("I. Analyst Team Reports", "1_analysts", (
        ("Market Analyst", "market.md", ("market_report",)),
        ("Sentiment Analyst", "sentiment.md", ("sentiment_report",)),
        ("News Analyst", "news.md", ("news_report",)),
        ("Fundamentals Analyst", "fundamentals.md", ("fundamentals_report",)),
    )),
    ("II. Research Team Decision", "2_research", (
        ("Bull Researcher", "bull.md", ("investment_debate_state", "bull_history")),
        ("Bear Researcher", "bear.md", ("investment_debate_state", "bear_history")),
        ("Research Manager", "manager.md", ("investment_plan",)),
    )),
    ("III. Trading Team Plan", "3_trading", (
        ("Trader", "trader.md", ("trader_investment_plan",)),
    )),
    ("IV. Risk Management Team Decision", "4_risk", (
        ("Aggressive Analyst", "aggressive.md", ("risk_debate_state", "aggressive_history")),
        ("Conservative Analyst", "conservative.md", ("risk_debate_state", "conservative_history")),
        ("Neutral Analyst", "neutral.md", ("risk_debate_state", "neutral_history")),
    )),
    ("V. Portfolio Manager Decision", "5_portfolio", (
        ("Portfolio Manager", "decision.md", ("final_trade_decision",)),
    )),
)


def _report_parts(final_state: dict) -> list[tuple[str, str, list[tuple[str, str, str]]]]:
    """The sections with something to say: (heading, folder, [(agent, file, text)])."""
    parts = []
    for heading, folder, agents in _SECTIONS:
        written = []
        for agent, filename, path in agents:
            value = final_state
            for key in path:
                value = (value or {}).get(key)
            if value:
                written.append((agent, filename, value))
        if written:
            parts.append((heading, folder, written))
    return parts


def write_report_tree(final_state: dict, ticker: str, save_path, settings: dict | None = None,
                      html: bool = True, evidence: dict | None = None) -> Path:
    """Save a completed run's reports to ``save_path``; return the complete-report path.

    ``settings`` (``TradingAgentsGraph.run_settings()``) adds what produced the run
    to the report's header. ``html`` also writes ``complete_report.html``, one
    self-contained page of the same report (#1419). ``evidence`` (``report_assets.
    build_evidence()``) prepends the "0. Evidence & charts" section: the charts are
    written to ``charts/*.svg`` and the raw pack to ``evidence.json``.
    """
    save_path = Path(save_path)
    save_path.mkdir(parents=True, exist_ok=True)

    evidence_md = ""
    if evidence:
        from tradingagents.report_assets import evidence_markdown, write_evidence_files

        write_evidence_files(save_path, evidence)
        evidence_md = evidence_markdown(evidence)

    # The macro backdrop lives on the run's state (settled before the analysts,
    # shared across tickers), so it needs no parameter here.
    from tradingagents.report_assets import macro_section_markdown

    macro_md = macro_section_markdown(final_state).strip()

    # The date's calendar, political headlines and priced odds: also settled
    # once per date (the macro node), rendered as subsections of the same
    # backdrop section — a report that argues from a CPI print should show the
    # print's numbers at the top, not make the reader trust the prose.
    date_md = ""
    date_ctx = final_state.get("date_context") or {}
    if date_ctx.get("events") or date_ctx.get("politics"):
        from tradingagents.dataflows.date_context import context_markdown

        date_md = context_markdown(date_ctx).strip()

    # 数据完整性条目必须排在一切正文之前（用户核心原则 2026-11-11）：
    # 读者先看到每项依赖是否正常，再读结论，缺失项才不会被叙事淹没。
    integrity_md = ""
    if evidence and evidence.get("data_integrity"):
        from tradingagents.dataflows.data_integrity import markdown as integrity_markdown

        integrity_md = integrity_markdown(evidence["data_integrity"]).strip()

    parts = _report_parts(final_state)
    sections = []
    for heading, folder, written in parts:
        (save_path / folder).mkdir(exist_ok=True)
        for _agent, filename, text in written:
            (save_path / folder / filename).write_text(text, encoding="utf-8")
        content = "\n\n".join(f"### {agent}\n{text}" for agent, _filename, text in written)
        sections.append(f"## {heading}\n\n{content}")

    backdrop_md = "\n\n".join(block for block in (macro_md, date_md) if block)
    body_md = "\n\n".join(
        ([integrity_md] if integrity_md else [])
        + ([evidence_md.strip()] if evidence_md else [])
        + ([f"## 0.5 Macro & event backdrop\n\n{backdrop_md}"] if backdrop_md else [])
        + sections)
    (save_path / "complete_report.md").write_text(
        _header(ticker, final_state, settings, evidence=evidence) + body_md, encoding="utf-8"
    )
    if html:
        from tradingagents.report_html import render_report

        (save_path / "complete_report.html").write_text(
            render_report(ticker, final_state, settings, parts, evidence=evidence),
            encoding="utf-8",
        )
    return save_path / "complete_report.md"
