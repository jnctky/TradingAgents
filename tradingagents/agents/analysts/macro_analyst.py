"""Macro analyst: the market backdrop for one analysis date, written once.

Every other agent answers a question about a single ticker. This one answers
"what kind of market is this being asked in" — the rate path, the dollar, risk
appetite, the inflation trend — and hands the same answer to all of them.

It runs first and alone, so the analysts downstream read it rather than each
re-deriving a backdrop from their own tool calls. The expensive part (the
narrative) is cached per analysis date: the first ticker of the day pays for it,
the rest of a 20-ticker batch read the same text back off disk.

It also settles the rest of what belongs to the date rather than to the ticker —
the economic calendar, the political headlines and the snapshot odds — for the
same reason: one fetch, one set of facts, every agent reading them.

Failure is not fatal. A dead platform route leaves the readings empty and the
run continues without a macro section, because a missing backdrop is a smaller
problem than a batch that stops at ticker one.
"""

from __future__ import annotations

import logging

from langchain_core.messages import HumanMessage

from tradingagents.agents.context import get_language_instruction
from tradingagents.dataflows.date_context import build_date_context, prompt_block
from tradingagents.dataflows.vendors.macro_context import (
    build_snapshot,
    narrative_is_reusable,
    remember_report,
    snapshot_prompt_context,
)

logger = logging.getLogger(__name__)

_SYSTEM = """You are a macro strategist on a single-stock research desk.

You are given the readings and rule-based regime tags for one analysis date. They
were fetched once for that date and are shared by every ticker analysed that day,
so write about the environment, never about one company.

Cover, in this order, and keep it under 450 words:

1. **Policy and the rate path** — what the level and direction of the policy rate,
   the 2-year and the 10-year imply about how the market is pricing future policy.
2. **The dollar** — its direction, and which kinds of earnings it helps or hurts.
3. **Risk appetite** — what the volatility level and its recent move say about
   positioning and what that means for how much benefit of the doubt a stock gets.
4. **Inflation** — whether the latest prints support or undercut the rate path.
5. **What would flip this read** — the two or three specific prints to watch.

Hard rules:

- Every number you state must appear in the readings below, together with its
  observation date. Do not compute new figures, annualise anything, or estimate
  a reading that is missing.
- When a reading is marked unavailable, say so and move on. Never fill it in.
- The regime tags are fixed rules over the readings, not your opinion. You may
  disagree with one, but then say so explicitly and name the number that has you
  disagreeing. They arrive as identifiers (`on_hold`, `risk_on`): write them as
  ordinary words in the report's language, never as raw identifiers, and do not
  repeat the tag and then restate the same reading again.
- Do not recommend a position in any instrument.
- Use `###` for any subheading. Never open a line with `#` or `##`: this text is
  placed inside a section of a larger report.

End with a markdown table of three columns — dimension, reading with its
observation date, and what it means for equities — with the header written in the
report's language like the rest of the text."""


def _settled_date_context(trade_date: str) -> tuple[dict, str]:
    """The calendar, politics and odds for the date; never raises.

    Settled here rather than in each analyst so the batch asks the platform
    once a day instead of once per ticker, and so every agent reasons about
    the same releases and the same headlines.
    """
    try:
        context = build_date_context(trade_date)
    except Exception as exc:  # noqa: BLE001 — a missing calendar must not stop the run
        logger.warning("Date context unavailable for %s: %s", trade_date, exc)
        return {}, ""
    return context, prompt_block(context)


def create_macro_analyst(llm):
    """Node that settles the day's macro backdrop; returns the shared blocks."""

    def macro_analyst_node(state) -> dict:
        trade_date = state["trade_date"]
        date_payload, date_prompt = _settled_date_context(trade_date)
        shared = {"date_context": date_payload, "date_context_prompt": date_prompt}

        try:
            snapshot = build_snapshot(trade_date)
        except Exception as exc:  # noqa: BLE001 — a missing backdrop must not stop the run
            logger.warning("Macro snapshot unavailable for %s: %s", trade_date, exc)
            return {"macro_report": "", "macro_snapshot": {}, "macro_context": "", **shared}

        context = snapshot_prompt_context(snapshot)
        available = sum(1 for r in snapshot["readings"] if r.get("available"))

        if narrative_is_reusable(snapshot):
            logger.info("Macro narrative for %s reused from %s (%d/%d readings available)",
                        trade_date, snapshot.get("report_model"), available, len(snapshot["readings"]))
            return {"macro_report": snapshot["report"], "macro_snapshot": snapshot,
                    "macro_context": context, **shared}

        if not available:
            logger.warning("Macro snapshot for %s has no readings; skipping the narrative", trade_date)
            return {"macro_report": "", "macro_snapshot": snapshot,
                    "macro_context": context, **shared}

        instruction = (
            f"Analysis date: {trade_date}.\n\n{context}"
            + get_language_instruction()
        )
        try:
            response = llm.invoke([
                HumanMessage(content=f"{_SYSTEM}\n\n{instruction}"),
            ])
            report = response.content if isinstance(response.content, str) else str(response.content)
        except Exception as exc:  # noqa: BLE001 — degrade to readings-only, never abort
            logger.warning("Macro narrative failed for %s (%s: %s); serving readings only",
                           trade_date, type(exc).__name__, exc)
            return {"macro_report": "", "macro_snapshot": snapshot,
                    "macro_context": context, **shared}

        remember_report(trade_date, report)
        logger.info("Macro narrative for %s written (%d chars, %d/%d readings)",
                    trade_date, len(report), available, len(snapshot["readings"]))
        return {"macro_report": report, "macro_snapshot": snapshot,
                "macro_context": context, **shared}

    return macro_analyst_node
