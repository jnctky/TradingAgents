"""Per-date context: the economic calendar and the political backdrop, fetched once.

Both are properties of the analysis date rather than of any one ticker, and both
cost a platform request. A 20-ticker batch must therefore ask once, not twenty
times — the same reason the macro snapshot lives on disk.

Freshness is asymmetric on purpose. A past date's calendar and headlines are
settled, so the file is reused for good. Today's file expires after
``REFRESH_MINUTES``, because a release prints at 08:30 and a run that started
before it should not keep describing the day as if the print had not happened.

Every part degrades on its own: a dead calendar leaves the political block
intact, and a dead news feed leaves the calendar intact. Callers render what
exists and say what does not.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.date_window import get_current_date, is_historical
from tradingagents.dataflows.trace import record_call, stopwatch
from tradingagents.dataflows.vendors.events import (
    events_markdown,
    events_prompt_block,
    fetch_events,
)
from tradingagents.dataflows.vendors.politics import (
    fetch_politics,
    odds_markdown,
    politics_markdown,
    politics_prompt_block,
    read_odds,
    record_odds,
)

logger = logging.getLogger(__name__)

CONTEXT_VERSION = 1
REFRESH_MINUTES = 45


def context_dir() -> Path:
    """Where per-date contexts live: ``date_context_dir`` if set, else the data cache."""
    configured = get_config().get("date_context_dir")
    if configured:
        return Path(configured)
    cache = get_config().get("data_cache_dir") or os.path.join(
        os.path.expanduser("~"), ".tradingagents", "cache")
    return Path(cache) / "date_context"


def context_path(trade_date: str) -> Path:
    return context_dir() / f"{trade_date}.json"


def _is_fresh(path: Path, trade_date: str) -> bool:
    if is_historical(trade_date):
        return True                    # a settled date's context cannot change
    return time.time() - path.stat().st_mtime < REFRESH_MINUTES * 60


def load_context(trade_date: str) -> dict[str, Any] | None:
    path = context_path(trade_date)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Ignoring unreadable date context %s: %s", path, exc)
        return None
    if payload.get("version") != CONTEXT_VERSION:
        logger.info("Date context %s is version %s, rebuilding", path, payload.get("version"))
        return None
    return payload


def save_context(context: dict[str, Any]) -> Path:
    path = context_path(context["trade_date"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)          # atomic: a half-written context must never be read back
    return path


def build_date_context(trade_date: str, *, refresh: bool = False) -> dict[str, Any]:
    """The calendar, the political headlines and the odds for one analysis date."""
    datetime.strptime(trade_date, "%Y-%m-%d")       # fail early on a malformed date
    if not refresh:
        path = context_path(trade_date)
        if path.exists() and _is_fresh(path, trade_date):
            cached = load_context(trade_date)
            if cached:
                logger.info("Date context for %s reused from %s", trade_date, path.name)
                return cached

    try:
        with stopwatch() as watch:
            events: dict[str, Any] = fetch_events(trade_date)
        record_call(method="economic_calendar", category="events", vendor="openbb",
                    chain=["openbb"], args=(trade_date,), kwargs={}, ms=watch.ms,
                    result=events)
    except Exception as exc:  # noqa: BLE001 — a dead calendar must not stop the run
        logger.warning("Economic calendar unavailable for %s: %s", trade_date, exc)
        events = {"error": f"{type(exc).__name__}: {exc}"}
        # Traced as a failure on purpose: the analyst body quotes the error, so
        # a table that stayed silent would read as "everything was fine".
        record_call(method="economic_calendar", category="events", vendor="openbb",
                    chain=["openbb"], args=(trade_date,), kwargs={}, ms=0,
                    error=str(exc))

    try:
        with stopwatch() as watch:
            politics: dict[str, Any] = fetch_politics(trade_date)
        # The vendor can answer without raising and still have nothing to give
        # (`available: False` for an empty or 0-byte feed). That is a miss, and
        # the trace must say so — otherwise the report table claims 政治头条 正常
        # above a body that says the feed could not be read.
        if politics.get("available") is False:
            record_call(method="political_headlines", category="politics", vendor="openbb",
                        chain=["openbb"], args=(trade_date,), kwargs={}, ms=watch.ms,
                        error=str(politics.get("reason") or "no headline returned"))
        else:
            record_call(method="political_headlines", category="politics", vendor="openbb",
                        chain=["openbb"], args=(trade_date,), kwargs={}, ms=watch.ms,
                        result=politics)
    except Exception as exc:  # noqa: BLE001 — a dead feed must not stop the run
        logger.warning("Political headlines unavailable for %s: %s", trade_date, exc)
        politics = {"error": f"{type(exc).__name__}: {exc}", "available": False,
                    "reason": "the news feed could not be read", "topics": [],
                    "window": [trade_date, trade_date], "source": "OpenBB", "scanned": 0}
        record_call(method="political_headlines", category="politics", vendor="openbb",
                    chain=["openbb"], args=(trade_date,), kwargs={}, ms=0,
                    error=str(exc))

    try:
        odds = read_odds(trade_date)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Read of the odds snapshot failed for %s: %s", trade_date, exc)
        odds = {"available": False, "reason": str(exc), "markets": []}

    # Today's odds are written for tomorrow's run — never for this one, which is
    # why the read above happened first.
    if not is_historical(trade_date):
        try:
            record_odds(trade_date)
        except Exception as exc:  # noqa: BLE001
            logger.info("No odds snapshot written for %s: %s", trade_date, exc)

    context = {
        "version": CONTEXT_VERSION,
        "trade_date": trade_date,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "events": events,
        "politics": politics,
        "odds": odds,
    }
    try:
        save_context(context)
    except OSError as exc:
        logger.warning("Could not cache the date context for %s: %s", trade_date, exc)
    return context


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #

def prompt_block(context: dict[str, Any]) -> str:
    """Calendar and politics as prompt text, for any agent reasoning about the date."""
    lines = [f"## The week around {context['trade_date']}"]
    events = context.get("events") or {}
    if events.get("error"):
        lines.append(f"- Economic calendar: unavailable ({events['error'][:120]}). "
                     f"Do not guess what was released.")
    else:
        lines.append("")
        lines.append(events_prompt_block(events))

    lines.append("")
    politics = context.get("politics") or {}
    if politics.get("error"):
        lines.append(f"Political headlines: unavailable ({politics['error'][:120]}). "
                     f"Do not characterise the political situation.")
    else:
        lines.append(politics_prompt_block(politics, context.get("odds") or {}))
    return "\n".join(lines)


def context_markdown(context: dict[str, Any]) -> str:
    """The report's event table and political block."""
    blocks = []
    events = context.get("events") or {}
    if events.get("error"):
        blocks.append(f"\n### Economic events\n\n_The calendar was unavailable for this run "
                      f"({events['error'][:120]}), so no release is listed._\n")
    else:
        blocks.append(events_markdown(events))

    politics = context.get("politics") or {}
    if politics.get("error"):
        blocks.append(f"\n### Political backdrop\n\n_The news feed was unavailable for this "
                      f"run ({politics['error'][:120]}), so no headline is listed._\n")
        blocks.append(odds_markdown(context.get("odds") or {}))
    else:
        blocks.append(politics_markdown(politics, context.get("odds") or {}))
    return "\n".join(blocks)
