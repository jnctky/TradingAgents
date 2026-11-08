"""Key economic events around the analysis date, with surprises called out.

The calendar serves ~130 US rows a week; a report that dumped all of them would
hide the three that matter inside noise. This module keeps a whitelist of the
recurring releases that actually move rates and equities, and — for the ones
already published — states how far the print landed from consensus.

Two rules keep the numbers honest:

* the raw strings travel with the row ("7.523M", "0.3%"), so the reader sees the
  units the calendar published, never a re-derived figure;
* a release whose consensus was never published cannot surprise, so it is listed
  without a direction rather than compared against nothing.

This is data, not judgement: whether a hot CPI print is bullish or bearish for a
given ticker depends on the ticker, and that call is left to the analysts.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# Days of already-published releases to scan for surprises, and days ahead to
# scan for what is still pending. The forward window must reach the next CPI:
# a run on the Friday before a Tuesday CPI should see it coming.
PAST_WINDOW_DAYS = 7
UPCOMING_WINDOW_DAYS = 10

# (event name, match mode). 'exact' guards against substring traps ('CPI' would
# also catch 'Cleveland CPI'); 'contains' for names that carry qualifiers.
WHITELIST: tuple[tuple[str, str], ...] = (
    ("CPI", "exact"),
    ("Core CPI", "exact"),
    ("Initial Jobless Claims", "exact"),
    ("Continuing Jobless Claims", "exact"),
    ("JOLTS", "contains"),
    ("Non-Farm Payrolls", "contains"),
    ("Nonfarm Payrolls", "contains"),
    ("ADP Employment Change", "exact"),
    ("Fed Interest Rate Decision", "exact"),
    ("FOMC Statement", "exact"),
    ("Core PCE", "startswith"),
    ("PCE Price Index", "contains"),
    ("Retail Sales", "startswith"),
    ("GDP", "startswith"),
    ("ISM Manufacturing PMI", "exact"),
    ("ISM Non-Manufacturing PMI", "exact"),
    ("Unit Labor Costs", "exact"),
)


def _matches(event: str, name: str, mode: str) -> bool:
    lowered = event.lower()
    if mode == "exact":
        return lowered == name.lower()
    if mode == "startswith":
        return lowered.startswith(name.lower())
    return name.lower() in lowered


def _number(raw: Any) -> float | None:
    """Parse a calendar quantity the way Nasdaq writes it: '7.523M', '1,695K', '0.3%'."""
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "")
    if text in ("", "-"):
        return None
    if text.endswith("%"):
        text = text[:-1].strip()
    multiplier = 1.0
    for suffix, factor in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if text.endswith(suffix):
            text, multiplier = text[:-1], factor
            break
    try:
        return float(text) * multiplier
    except ValueError:
        return None


def _surprise(actual_raw: Any, consensus_raw: Any) -> dict[str, Any] | None:
    """How a print landed versus consensus, in the unit it was published in.

    Percent-quoted series are compared in percentage points: a 2.2 vs 3.2 print
    is 1.0pp below, and expressing that as "31% below consensus" would make a
    rounding-distance miss read as a collapse.
    """
    actual, consensus = _number(actual_raw), _number(consensus_raw)
    if actual is None or consensus is None or consensus == 0:
        return None
    direction = "above" if actual > consensus else "below" if actual < consensus else "in line"
    percent_quoted = (str(actual_raw or "").strip().endswith("%")
                      or str(consensus_raw or "").strip().endswith("%"))
    if percent_quoted:
        return {"direction": direction, "delta": round(actual - consensus, 3), "unit": "pp"}
    return {"direction": direction,
            "delta": round((actual - consensus) / abs(consensus) * 100, 2), "unit": "%"}


def _surprise_text(surprise: dict[str, Any] | None) -> str:
    if not surprise:
        return "—"
    if surprise["direction"] == "in line":
        return "in line"
    return f"{surprise['direction']} by {abs(surprise['delta']):,.2f}{surprise['unit']}"


def _is_surprise(entry: dict[str, Any]) -> bool:
    """Whether a print landed away from consensus; a match is not a surprise."""
    surprise = entry.get("surprise")
    return bool(surprise) and surprise["direction"] != "in line"


def _picked(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """One row per event name: the consensus-bearing line wins.

    The calendar repeats an event name for its variants (CPI MoM and YoY), and
    only the variant the market prices — the one with a consensus — can surprise.
    """
    with_consensus = [r for r in rows if _number(r.get("consensus")) is not None]
    chosen = with_consensus or [r for r in rows if r.get("actual") not in (None, "-", "")]
    return (chosen or rows)[0] if rows else None


def fetch_events(trade_date: str) -> dict[str, Any]:
    """Whitelisted events in [date-7, date+10]: published ones with surprises, then pending."""
    from tradingagents.dataflows.vendors.openbb.client import assert_rows, get_json

    end = datetime.strptime(trade_date, "%Y-%m-%d")
    start = end - timedelta(days=PAST_WINDOW_DAYS)
    horizon = end + timedelta(days=UPCOMING_WINDOW_DAYS)
    rows = get_json("/api/v1/economy/calendar", "economic calendar", params={
        "provider": "nasdaq",
        "start_date": start.strftime("%Y-%m-%d"),
        "end_date": horizon.strftime("%Y-%m-%d"),
    })
    assert_rows("events", rows, "calendar rows")
    us = [r for r in rows if r.get("country") == "United States"
          and isinstance(r.get("date"), str)]

    matched: dict[str, list[dict[str, Any]]] = {}
    for row in us:
        event = str(row.get("event") or "")
        for name, mode in WHITELIST:
            if _matches(event, name, mode):
                # The calendar also spells one release two ways ("PCE Price index"
                # and "PCE Price Index"); grouping case-insensitively keeps them
                # on one row, and the first spelling seen is the one printed.
                matched.setdefault(event.strip().lower(), []).append(row)
                break

    published, pending = [], []
    for group in matched.values():
        row = _picked(group)
        event = str(row.get("event") or "").strip()
        date = str(row.get("date"))[:10]
        entry = {
            "date": date,
            "event": event,
            "actual": str(row.get("actual") or "").strip() or None,
            "consensus": str(row.get("consensus") or "").strip() or None,
            "previous": str(row.get("previous") or "").strip() or None,
        }
        # A statement with neither a print nor a consensus ("FOMC Statement -")
        # carries nothing a reader can check, and the rate decision row beside it
        # already says what happened.
        if entry["actual"] in (None, "-") and entry["consensus"] is None:
            continue
        if date <= trade_date:
            entry["surprise"] = _surprise(row.get("actual"), row.get("consensus"))
            published.append(entry)
        else:
            pending.append(entry)

    published.sort(key=lambda e: (e["date"], e["event"]), reverse=True)
    pending.sort(key=lambda e: (e["date"], e["event"]))

    return {
        "trade_date": trade_date,
        "window": [start.strftime("%Y-%m-%d"), horizon.strftime("%Y-%m-%d")],
        "published": published,
        "upcoming": pending,
    }


def events_prompt_block(events: dict[str, Any]) -> str:
    """The event calendar as prompt text; surprises carry their arithmetic."""
    lines = ["Economic events around the analysis date (Nasdaq calendar, US releases, whitelist of recurring market movers):"]

    surprising = [e for e in events["published"] if _is_surprise(e)]
    plain_past = [e for e in events["published"] if not _is_surprise(e)]
    if surprising:
        shown = ", ".join(
            f"{e['event']} {e['actual']} vs {e['consensus']} expected "
            f"({_surprise_text(e['surprise'])}) on {e['date']}"
            for e in surprising[:6]
        )
        lines.append(f"- released with a surprise: {shown}")
    if plain_past:
        shown = ", ".join(
            f"{e['event']} {e['actual']}"
            + (f" vs {e['consensus']} expected (in line)" if e.get("surprise") else "")
            + f" on {e['date']}"
            for e in plain_past[:6])
        lines.append(f"- released in line or without a published consensus: {shown}")
    if events["upcoming"]:
        shown = ", ".join(
            f"{e['event']} on {e['date']}" + (f" (consensus {e['consensus']})" if e.get("consensus") else "")
            for e in events["upcoming"][:8]
        )
        lines.append(f"- still pending: {shown}")
    if len(lines) == 1:
        lines.append("- none in the window")

    lines.append(
        "A surprise direction is arithmetic, not an interpretation: state it, and let the "
        "ticker-specific analysis decide what it means. Do not invent events that are not listed."
    )
    return "\n".join(lines)


def events_markdown(events: dict[str, Any]) -> str:
    """The event table for the report: released ones first, then the pending ones.

    Tolerates a context missing the events half (older cache, dead fetch):
    renders as "no whitelisted release" rather than raising into the writer.
    """
    published = events.get("published") or []
    upcoming = events.get("upcoming") or []
    lines = ["", "### Economic events (Nasdaq calendar, US, market movers only)", ""]
    if published or upcoming:
        lines += ["| Date | Event | Actual | Consensus | Previous | vs consensus |",
                  "|---|---|---|---|---|---|"]
        for e in published:
            surprise = e.get("surprise")
            verdict = _surprise_text(surprise)
            lines.append(f"| {e['date']} | {e['event']} | {e['actual'] or '—'} | "
                         f"{e['consensus'] or '—'} | {e['previous'] or '—'} | {verdict} |")
        for e in upcoming:
            lines.append(f"| {e['date']} | {e['event']} | pending | {e['consensus'] or '—'} | "
                         f"{e['previous'] or '—'} | — |")
        lines += ["", "_'vs consensus' is arithmetic on the calendar's own numbers; interpreting "
                  "the direction is left to the analysis._", ""]
    else:
        lines.append("No whitelisted release in the window.")
    return "\n".join(lines)
