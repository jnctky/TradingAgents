"""Stream a TradingAgents analysis as JSONL events for the Copilot adapter.

Protocol: one JSON object per line on stdout, flushed immediately:

    {"type": "stage", "stage": "analysts", "label": "..."}
    {"type": "message", "sender": "market_analyst", "text": "..."}
    {"type": "report", "key": "market_report", "text": "..."}
    {"type": "decision", "rating": "Buy", "text": "..."}
    {"type": "done"}
    {"type": "error", "message": "..."}

Usage:
    .venv/Scripts/python.exe my_tools/ta_runner.py --ticker AAPL --date 2026-10-21
        [--analysts market,sentiment,news,fundamentals]

LLM provider/models/keys come from the project .env (same as WebUI/CLI).
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

MSG_MAX_CHARS = 1500

# Models pass natural analyst names ("sentiment") that are not the graph's
# canonical keys ("social"); normalise before handing them to the graph.
ANALYST_ALIASES = {
    "sentiment": "social",
    "emotion": "social",
    "social_media": "social",
    "fundamental": "fundamentals",
    "technical": "market",
    "technique": "market",
    "price": "market",
}
VALID_ANALYSTS = {"market", "social", "news", "fundamentals"}


def normalize_analysts(raw: str) -> list[str]:
    out: list[str] = []
    for part in raw.split(","):
        key = ANALYST_ALIASES.get(part.strip().lower(), part.strip().lower())
        if not key:
            continue
        if key not in VALID_ANALYSTS:
            raise ValueError(
                f"unknown analyst key: {key!r} (valid: {sorted(VALID_ANALYSTS)})"
            )
        if key not in out:
            out.append(key)
    return out or ["market", "social", "news", "fundamentals"]

# Which AgentState keys mark which pipeline stage (checked in order).
_STAGE_KEYS = [
    ("market_report", "analysts", "🧑‍💼 分析师研究（行情/情绪/新闻/基本面）"),
    ("investment_debate_state", "research_debate", "⚖️ 研究团队 Bull/Bear 辩论"),
    ("investment_plan", "research_manager", "🧑‍⚖️ 研究经理裁决"),
    ("trader_investment_plan", "trader", "💼 交易员制定交易计划"),
    ("risk_debate_state", "risk_debate", "🛡️ 风险团队辩论"),
    ("final_trade_decision", "decision", "🎯 组合经理最终决策"),
]

_REPORT_KEYS = ["market_report", "sentiment_report", "news_report",
                "fundamentals_report", "investment_plan",
                "trader_investment_plan", "final_trade_decision"]

_SENDER_LABELS = {
    "market_analyst": "市场分析师",
    "social_media_analyst": "情绪分析师",
    "news_analyst": "新闻分析师",
    "fundamentals_analyst": "基本面分析师",
    "bull_researcher": "Bull 研究员",
    "bear_researcher": "Bear 研究员",
    "research_manager": "研究经理",
    "trader": "交易员",
    "aggressive_risk_analyst": "激进风险分析师",
    "conservative_risk_analyst": "保守风险分析师",
    "neutral_risk_analyst": "中性风险分析师",
    "portfolio_manager": "组合经理",
}


def _stage_value_ready(key: str, value) -> bool:
    """True when a state chunk really carries content for this stage.

    Debate sub-states are initialised as dicts of empty strings at graph
    start, so bare truthiness fires the stage label far too early.
    """
    if isinstance(value, dict):
        return bool(value.get("history") or value.get("current_response")
                    or value.get("current_aggressive_response"))
    return bool(value)


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def msg_text(message) -> str:
    sender = getattr(message, "name", None) or type(message).__name__.replace("Message", "")
    content = getattr(message, "content", "") or ""
    if isinstance(content, list):
        content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    content = str(content).strip()
    return sender, content


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--date", required=True, help="analysis date, YYYY-MM-DD")
    parser.add_argument("--analysts", default="market,social,news,fundamentals",
                        help="comma-separated analyst subset")
    args = parser.parse_args()

    analysts = normalize_analysts(args.analysts)

    try:
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        config = dict(DEFAULT_CONFIG)
        graph = TradingAgentsGraph(selected_analysts=analysts, config=config, debug=False)

        init_state = graph.create_run_state(args.ticker, args.date, "stock", None)
        graph_args = graph.propagator.get_graph_args()

        seen_stages: set[str] = set()
        seen_reports: set[str] = set()
        final_state: dict = {}

        for messages, chunk in graph.stream_run(init_state, **graph_args):
            for message in messages or []:
                sender, text = msg_text(message)
                if not text:
                    continue
                emit({"type": "message", "sender": sender,
                      "label": _SENDER_LABELS.get(sender, sender),
                      "text": text[:MSG_MAX_CHARS]})

            if chunk:
                final_state.update(chunk)
                for key, stage, label in _STAGE_KEYS:
                    if chunk.get(key) and stage not in seen_stages \
                            and _stage_value_ready(key, chunk[key]):
                        seen_stages.add(stage)
                        emit({"type": "stage", "stage": stage, "label": label})
                for key in _REPORT_KEYS:
                    if chunk.get(key) and key not in seen_reports:
                        seen_reports.add(key)
                        emit({"type": "report", "key": key, "text": chunk[key]})

        decision = final_state.get("final_trade_decision", "")
        if not decision:
            emit({"type": "error", "message": "run finished without a final_trade_decision"})
            return 1

        try:
            from tradingagents.agents.rating import extract_rating
            rating = extract_rating(decision)
        except Exception:  # noqa: BLE001
            rating = None
        emit({"type": "decision", "rating": rating, "text": decision})

        graph.record_decision(args.ticker, args.date, final_state)
        graph.save_reports(final_state, args.ticker)
        emit({"type": "done"})
        return 0
    except Exception as exc:  # noqa: BLE001
        emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
