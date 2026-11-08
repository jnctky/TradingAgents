"""Political backdrop: whitelisted headlines, plus yesterday's priced odds.

A report that free-associates about politics is worse than one that stays
silent, so this module does two narrow things and refuses the third:

* it collects headlines matching a fixed political whitelist — elections,
  Congress, party control, tariffs, fiscal deadlines, Fed independence,
  geopolitics — from the same market-wide feed the news analyst already reads,
  keeping every article's URL so a reader can check the claim;
* it reads a Polymarket snapshot **fetched on or before the analysis date**,
  never live odds, and says plainly when there is none;
* it writes no sentence about what any of it means. Interpretation is the
  analysts' job, and they see the same raw headlines.

Two failure modes are treated as normal, not as errors — a quiet news day and
an unreachable odds API — because both end in the same honest output: nothing
claimed.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.date_window import get_current_date

logger = logging.getLogger(__name__)

# (key, label, pattern). The patterns are the whitelist: a headline reaches the
# report only by matching one, so the section cannot wander into general
# commentary. 'house' alone would catch 'housing' and 'warehouse', hence the
# compound forms.
TOPICS: tuple[tuple[str, str, str], ...] = (
    ("elections", "Elections", r"\b(elections?|midterms?|ballots?|voters?)\b"),
    ("congress", "Congress", r"\b(congress|congressional|senate|house majority|"
                            r"speaker of the house|capitol hill)\b"),
    ("party_control", "Party control", r"\b(democrats?|republicans?|gop)\b"),
    ("trade_policy", "Tariffs and trade", r"\b(tariffs?|trade war|export controls?)\b"),
    ("fiscal", "Fiscal deadlines", r"\b(government shutdown|shutdown|debt ceiling|"
                                  r"sovereign default)\b"),
    ("fed_independence", "Fed independence", r"\b(fed independence|fire powell|"
                                             r"dismiss powell|central bank independence)\b"),
    ("geopolitics", "Geopolitics", r"\b(china|russia|iran|ukraine|israel|taiwan|sanctions)\b"),
)

# A single headline is not a backdrop: one mention of an election in a market
# wrap-up would otherwise become a "political section". The exceptions are
# events that are market-moving on their own announcement.
MIN_ARTICLES = 2
SINGLE_ARTICLE_TOPICS = frozenset({"fiscal", "fed_independence", "trade_policy"})

MAX_PER_TOPIC = 4
LOOK_BACK_DAYS = 14

# Words that say "this is a market story", not "this is the same event". Two
# headlines on one day that share only these are different stories.
_MARKET_WORDS = frozenset(
    "stock stocks equity equities market markets index indexes indices rally rallies "
    "surge surges jump jumps rise rises fall falls drop drops gain gains loss losses "
    "higher lower sharply today close closes closed dow nasdaq sp500 spx wall street "
    "futures points investors shares trading trade session week".split()
)

PROXY_SYMBOLS = ("SPY", "QQQ", "DIA", "TLT", "GLD", "USO", "UUP")

# What a US-equity desk actually prices: the near-term political risks. A bare
# "US election" search is omitted on purpose — with the midterms resolved, its
# volume leaders are 2028 novelty markets (celebrity candidates at 0%) that
# would crowd out everything informative.
ODDS_TOPICS = ("government shutdown", "US tariff", "trade war", "recession")
ODDS_PER_TOPIC = 2
# A market pricing under this is noise no matter how much volume it carries —
# the deepest such markets are exactly the celebrity-candidate jokes.
ODDS_FLOOR = 0.02
MAX_ODDS_AGE_DAYS = 3           # older odds are not "where the crowd is", they are history
ODDS_VERSION = 1

_STOPWORDS = frozenset(
    "a an and as at by for from in is it its of on or the to up with".split()
)


# --------------------------------------------------------------------------- #
# headlines
# --------------------------------------------------------------------------- #

def _pub_dt(row: dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(str(row.get("date", ""))[:19])
    except ValueError:
        return None


def _words(title: str) -> frozenset[str]:
    return frozenset(w for w in re.findall(r"[a-z0-9]+", title.lower())
                     if w not in _STOPWORDS)


def _same_story(a: str, b: str) -> bool:
    """Whether two headlines on the same day are the same story re-written.

    A wire rewrites one market wrap three times as the session moves — "Stocks
    Surge as Democrats Sweep Congress", then "... as Democrats Retake Congress" —
    and listing all three would read as three separate events. Word overlap alone
    is a poor judge, because that vocabulary is shared by every market headline;
    the test is overlap on the words that name the event.
    """
    distinctive = (_words(a) & _words(b)) - _MARKET_WORDS
    return len(distinctive) >= 2


def _trim(excerpt: str, limit: int = 220) -> str:
    """The first sentence or so: excerpts are cut mid-sentence by the feed."""
    text = " ".join((excerpt or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    stop = max(cut.rfind(". "), cut.rfind("; "))
    return (cut[: stop + 1] if stop > 80 else cut).strip()


def _topic_of(title: str) -> str | None:
    """The first whitelist topic the headline matches, or None.

    One headline belongs to one topic: the same article filed under both
    "Congress" and "Party control" would count twice and make one wire story
    look like two independent signals.
    """
    lowered = title.lower()
    for key, _label, pattern in TOPICS:
        if re.search(pattern, lowered):
            return key
    return None


def fetch_politics(trade_date: str, look_back_days: int | None = None) -> dict[str, Any]:
    """Whitelisted political headlines published in the window ending at ``trade_date``."""
    from tradingagents.dataflows.vendors.openbb.client import PROVIDER, get_json

    days = LOOK_BACK_DAYS if look_back_days is None else look_back_days
    end_dt = datetime.strptime(trade_date, "%Y-%m-%d")
    start_dt = end_dt - timedelta(days=days)
    labels = {key: label for key, label, _ in TOPICS}

    rows = get_json(
        "/api/v1/news/company",
        "political headlines",
        params={
            "symbol": ",".join(PROXY_SYMBOLS),
            "provider": PROVIDER,
            "limit": 400,
            "start_date": start_dt.strftime("%Y-%m-%d"),
            "end_date": trade_date,
        },
    )

    # Date first, then relevance: a row past the analysis date must never be
    # read, and the feed returns newest first anyway.
    dated = [(d, r) for r in rows if (d := _pub_dt(r)) is not None]
    in_window = [(d, r) for d, r in dated
                 if start_dt <= d <= end_dt.replace(hour=23, minute=59)]
    in_window.sort(key=lambda pair: pair[0], reverse=True)

    buckets: dict[str, list[dict[str, Any]]] = {key: [] for key in labels}
    for published, row in in_window:
        title = " ".join((row.get("title") or "").split())
        if not title:
            continue
        key = _topic_of(title)
        if key is None:
            continue
        article = {
            "date": published.strftime("%Y-%m-%d"),
            "title": title,
            "source": row.get("source") or PROVIDER,
            "url": row.get("url") or "",
            "excerpt": _trim(row.get("excerpt") or ""),
        }
        if not article["url"]:
            continue          # an unsourced claim cannot be checked, so it is not evidence
        already = [a for a in buckets[key] if a["date"] == article["date"]
                   and _same_story(a["title"], title)]
        if not already and len(buckets[key]) < MAX_PER_TOPIC:
            buckets[key].append(article)

    topics = [
        {"key": key, "label": labels[key], "articles": buckets[key]}
        for key, _label, _pattern in TOPICS if buckets[key]
    ]
    total = sum(len(t["articles"]) for t in topics)
    if total >= MIN_ARTICLES or (total == 1 and topics[0]["key"] in SINGLE_ARTICLE_TOPICS):
        available, reason = True, None
    elif total:
        available = False
        reason = (f"only {total} political headline in the window "
                  f"({topics[0]['label'].lower()}): not a backdrop, so none is described")
    else:
        available = False
        reason = f"no whitelisted political headline in {days} days"

    return {
        "trade_date": trade_date,
        "window": [start_dt.strftime("%Y-%m-%d"), trade_date],
        "source": f"OpenBB ({PROVIDER}) market-wide news via {', '.join(PROXY_SYMBOLS)}",
        "scanned": len(in_window),
        "available": available,
        "reason": reason,
        "topics": topics,
    }


# --------------------------------------------------------------------------- #
# prediction-market odds, snapshotted daily
# --------------------------------------------------------------------------- #

def _cache_root() -> Path:
    cache = get_config().get("data_cache_dir") or os.path.join(
        os.path.expanduser("~"), ".tradingagents", "cache")
    return Path(cache)


def odds_dir() -> Path:
    """Where odds snapshots live: ``politics_odds_dir`` if set, else the data cache."""
    configured = get_config().get("politics_odds_dir")
    return Path(configured) if configured else _cache_root() / "politics_odds"


def odds_path(fetched_on: date | str) -> Path:
    return odds_dir() / f"{fetched_on}.json"


def _dedup_markets(markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per question, keeping the deepest market.

    A single Polymarket question answers to several search topics ('Congress'
    and 'US election' overlap), and listing it twice would read as two
    independent signals.
    """
    best: dict[str, dict[str, Any]] = {}
    for market in markets:
        key = str(market.get("question") or "")
        if not key:
            continue
        if key not in best or (market.get("volume_usd") or 0) > (best[key].get("volume_usd") or 0):
            best[key] = market
    return sorted(best.values(), key=lambda m: m.get("volume_usd") or 0, reverse=True)


def read_odds(trade_date: str) -> dict[str, Any]:
    """The newest snapshot fetched on or before ``trade_date``, within the staleness budget.

    Reading only snapshots fetched by the analysis date is what keeps the odds
    point-in-time: a file written tomorrow prices tomorrow's information, so a
    run backdated to last week finds nothing and says so.
    """
    day = datetime.strptime(trade_date, "%Y-%m-%d").date()
    for back in range(MAX_ODDS_AGE_DAYS + 1):
        path = odds_path(day - timedelta(days=back))
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Ignoring unreadable odds snapshot %s: %s", path, exc)
            continue
        if payload.get("version") != ODDS_VERSION or not payload.get("markets"):
            continue
        return {
            "available": True,
            "as_of": payload.get("as_of"),
            "age_days": back,
            "markets": payload["markets"],
            "topics_missing": payload.get("errors") or [],
        }
    oldest = day - timedelta(days=MAX_ODDS_AGE_DAYS)
    return {
        "available": False,
        "reason": (f"no odds snapshot fetched between {oldest} and {day}; "
                   f"live odds are not served to a dated run"),
        "markets": [],
    }


def record_odds(trade_date: str) -> dict[str, Any]:
    """Fetch the live odds once and store them under today's date.

    Written today, read tomorrow: the filename is the fetch date, so a snapshot
    can only ever be read by a run dated on or after the day it was taken.
    A network failure is recorded as one line and nothing else — the odds are
    simply absent, not remembered from an earlier fetch.
    """
    from tradingagents.dataflows.vendors.polymarket import (
        PolymarketUnavailable,
        search_markets,
    )

    collected: list[dict[str, Any]] = []
    errors: list[str] = []
    for topic in ODDS_TOPICS:
        try:
            found = search_markets(topic, limit=ODDS_PER_TOPIC + 4)
        except PolymarketUnavailable as exc:
            errors.append(f"{topic}: {str(exc)[:160]}")
            continue
        except Exception as exc:                       # noqa: BLE001 - never break a run
            logger.warning("Odds lookup failed for %r: %s", topic, exc)
            errors.append(f"{topic}: {type(exc).__name__}")
            continue
        # The search is volume-ranked across everything matching the words, so
        # the deepest rows are often novelty markets; the topic keeps its best
        # *live* prices rather than its loudest jokes.
        relevant = [m for m in found if (m.get("probability") or 0) >= ODDS_FLOOR]
        if relevant:
            collected += relevant[:ODDS_PER_TOPIC]
        else:
            errors.append(f"{topic}: no open market priced above {ODDS_FLOOR:.0%}")

    markets = _dedup_markets(collected)
    if not markets:
        logger.info("No odds snapshot written for %s: %s", trade_date,
                    "; ".join(errors) or "no open market matched")
        return {"written": False, "reason": "; ".join(errors) or "no open market matched"}

    payload = {
        "version": ODDS_VERSION,
        "as_of": datetime.now(UTC).isoformat(timespec="seconds"),
        "trade_date": trade_date,
        "markets": markets[:16],
        "errors": errors,
    }
    path = odds_path(get_current_date())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)              # atomic: a half-written snapshot must never be read back
    return {"written": True, "path": str(path), "count": len(payload["markets"])}


# --------------------------------------------------------------------------- #
# rendering: the same facts for the prompt and for the report
# --------------------------------------------------------------------------- #

def odds_markdown(odds: dict[str, Any]) -> str:
    if not odds.get("available"):
        return (f"Prediction-market odds: none. _{odds.get('reason', 'unavailable')}_. "
                f"No odds are used anywhere below.")
    lines = []
    for market in odds["markets"]:
        move = market.get("week_change_pp")
        lines.append(
            f"| {market['question']} | {market['outcome']} "
            f"{market['probability']:.0%} | "
            f"{f'{move:+.1f}pp' if move is not None else '—'} | "
            f"${(market.get('volume_usd') or 0):,.0f} |"
        )
    age = odds.get("age_days") or 0
    stamp = f"snapshot taken {str(odds.get('as_of'))[:16].replace('T', ' ')} UTC"
    if age:
        stamp += f", read {age} day(s) later"
    return ("| Market | Priced | 1-week move | Volume |\n|---|---|---|---|\n"
            + "\n".join(lines)
            + f"\n\n_{stamp}. A probability is the crowd's priced odds, not a forecast._")


def politics_prompt_block(politics: dict[str, Any], odds: dict[str, Any]) -> str:
    """The political backdrop as prompt text, every line attributable."""
    lines = [f"Political headlines, {politics['window'][0]} to {politics['window'][1]} "
             f"(whitelist match on {politics['source']}):"]
    if politics["available"]:
        for topic in politics["topics"]:
            shown = "; ".join(f"{a['date']} \"{a['title']}\" ({a['source'] or 'wire'})"
                              for a in topic["articles"])
            lines.append(f"- {topic['label']}: {shown}")
    else:
        lines.append(f"- none reported ({politics['reason']})")

    if odds.get("available"):
        best = "; ".join(f"\"{m['question']}\" {m['probability']:.0%} "
                         f"(volume ${(m.get('volume_usd') or 0):,.0f})"
                         for m in odds["markets"][:5])
        lines.append(f"- priced odds, snapshot of {str(odds.get('as_of'))[:10]}: {best}")
    else:
        lines.append("- priced odds: unavailable, so do not state any probability")

    lines.append(
        "These are headlines and prices, not conclusions: attribute any political claim to "
        "the headline that carries it and its date. Anything not listed above is speculation "
        "and must be labelled as such — or left out. Never invent an election result, a "
        "legislative outcome, or a probability."
    )
    return "\n".join(lines)


def politics_markdown(politics: dict[str, Any], odds: dict[str, Any]) -> str:
    """The report's political block: sourced headlines, then the priced odds.

    Tolerates a context missing the politics half entirely (an older cache
    file, a fetch that died before writing): the block degrades to its
    "no political section" line instead of raising into the report writer.
    """
    window = politics.get("window") or ("", "")
    lines = ["", f"### Political backdrop ({window[0]} to {window[1]})", ""]
    if politics.get("available"):
        lines.append(f"Headlines matching a fixed political whitelist, from "
                     f"{politics.get('source', '?')} — {politics.get('scanned', 0)} articles scanned, "
                     f"{sum(len(t.get('articles', [])) for t in politics.get('topics', []))} matched.")
        lines.append("")
        for topic in politics.get("topics", []):
            lines.append(f"**{topic['label']}**")
            lines.append("")
            for article in topic.get("articles", []):
                excerpt = f" — {article['excerpt']}" if article.get("excerpt") else ""
                lines.append(f"- {article['date']} · [{article['title']}]({article['url']})"
                             f"{excerpt}")
            lines.append("")
    else:
        lines.append(f"No political section: {politics.get('reason', 'unavailable')}.")
        lines.append("")

    lines.append("**Market-implied odds (Polymarket)**")
    lines.append("")
    lines.append(odds_markdown(odds))
    lines.append("")
    lines.append("_Headlines are quoted as published, not endorsed. Reading them into a "
                 "trading decision is the analysts' inference, and the report labels it as "
                 "such._")
    return "\n".join(lines)
