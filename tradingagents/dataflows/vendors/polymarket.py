"""Polymarket prediction-market vendor.

Surfaces live, market-implied probabilities for forward-looking events (Fed
decisions, recession, elections, geopolitics, crypto) to the news analyst, as a
complement to news (what happened) and FRED macro data (where things stand):
what the crowd actually prices to happen next.

Uses Polymarket's public Gamma API (https://gamma-api.polymarket.com) — no key,
no auth. Each market's ``outcomePrices`` are the implied probabilities of its
outcomes (a "Yes" at 0.76 means the market prices a 76% chance).
"""
import json
import logging
from datetime import UTC, datetime

import requests

from tradingagents.dataflows.date_window import is_historical
from tradingagents.dataflows.vendors._net import requests_proxies

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"

# Network timeout (seconds), consistent with the other vendors.
REQUEST_TIMEOUT = 30

# Default number of markets to return, ranked by traded volume.
DEFAULT_LIMIT = 6


def _request(path: str, params: dict) -> dict:
    # Pinned to the user's local proxy for the same reason as Reddit and
    # StockTwits (_net.py): a direct connection is challenged from mainland
    # China, and the ambient proxy variables — when a session even sets them —
    # point at a proxy that refuses this host.
    response = requests.get(
        f"{GAMMA_BASE}/{path}", params=params, timeout=REQUEST_TIMEOUT,
        proxies=requests_proxies(),
    )
    response.raise_for_status()
    return response.json()


def _parse_json_list(value) -> list:
    """Gamma encodes ``outcomes``/``outcomePrices`` as JSON-string arrays."""
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []


class PolymarketUnavailable(RuntimeError):
    """The Gamma API answered with a network error, so no odds can be reported."""


def _is_forward_looking(market: dict, now: datetime) -> bool:
    """Keep only open markets that resolve in the future.

    ``closed`` is the reliable resolved flag (``active`` stays True even for
    settled markets), and a past ``endDate`` means the event already resolved —
    either way it is not a forward-looking signal.
    """
    if market.get("closed"):
        return False
    end_date = market.get("endDate")
    if end_date:
        try:
            if datetime.fromisoformat(end_date.replace("Z", "+00:00")) < now:
                return False
        except ValueError:
            pass
    return bool(_parse_json_list(market.get("outcomePrices"))) and bool(
        _parse_json_list(market.get("outcomes"))
    )


def search_markets(
    topic: str, limit: int | None = None, now: datetime | None = None
) -> list[dict]:
    """Open, forward-looking markets matching ``topic``, richest first.

    The structured twin of :func:`get_prediction_markets`, for callers that
    store odds instead of printing them. A network failure raises
    :class:`PolymarketUnavailable`, so a caller holding a snapshot can tell
    "the crowd has no view" from "we could not ask".
    """
    if limit is None:
        limit = DEFAULT_LIMIT
    try:
        data = _request("public-search", {"q": topic, "limit_per_type": 20})
    except requests.RequestException as e:
        raise PolymarketUnavailable(str(e)) from e

    now = now or datetime.now(UTC)
    candidates = [
        m
        for event in data.get("events", [])
        for m in event.get("markets", [])
        if _is_forward_looking(m, now)
    ]
    candidates.sort(key=lambda m: m.get("volumeNum") or 0, reverse=True)

    markets = []
    for m in candidates[:limit]:
        prices = _parse_json_list(m.get("outcomePrices"))
        outcomes = _parse_json_list(m.get("outcomes"))
        try:
            prob = float(prices[0])
        except (ValueError, IndexError):
            continue
        volume = m.get("volumeNum") or 0
        week = m.get("oneWeekPriceChange")
        markets.append({
            "question": m.get("question"),
            "outcome": outcomes[0] if outcomes else "Yes",
            "probability": round(prob, 4),
            "volume_usd": int(volume) if isinstance(volume, (int, float)) else 0,
            "resolves": (m.get("endDate") or "")[:10] or None,
            # Percentage points, the unit a move in a probability is read in.
            "week_change_pp": round(week * 100, 1)
            if isinstance(week, (int, float)) and week
            else None,
            "slug": m.get("slug") or (m.get("events") or [{}])[0].get("slug"),
        })
    return markets


def get_prediction_markets(topic: str, limit: int | None = None, as_of_date: str | None = None) -> str:
    """Return live prediction-market probabilities for an event topic.

    Args:
        topic: Event keyword(s), e.g. "Fed rate cut", "recession 2026",
            "US election", or a sector/company event.
        limit: Max markets to return (ranked by traded volume); ``None`` uses
            DEFAULT_LIMIT.
        as_of_date: The analysis date. Polymarket serves only live odds, so a
            date before today withholds them.

    Returns:
        A markdown report of the most-traded open markets matching the topic,
        each with its implied probability, traded volume, resolution date, and
        recent (1-week) move.
    """
    if is_historical(as_of_date):
        return (
            f"Prediction-market odds are withheld for {as_of_date}. Polymarket serves "
            f"only live odds on open markets, with no historical vintage, so serving "
            f"them would put post-decision information into a {as_of_date} analysis."
        )
    try:
        markets = search_markets(topic, limit)
    except PolymarketUnavailable as e:
        logger.warning("Polymarket search failed for %r: %s", topic, e)
        return (
            f"Polymarket data is currently unavailable (network error: {e}). "
            f"Proceed without prediction-market signal for '{topic}'."
        )

    header = (
        f'## Polymarket prediction markets: "{topic}"\n'
        f"Live, market-implied probabilities (higher traded volume = deeper, "
        f"more reliable). A probability is the crowd's priced odds of the event, "
        f"not a forecast you should take as certain.\n\n"
    )

    if not markets:
        return header + (
            f"No open prediction markets matched '{topic}'. Polymarket coverage "
            f"is concentrated in macro, political, geopolitical, and crypto "
            f"events; a specific equity may have none."
        )

    lines = []
    for m in markets:
        wk = m["week_change_pp"]
        wk_str = f", 1-week {wk:+.1f}pp" if wk is not None else ""
        lines.append(
            f"- **{m['question']}** — {m['outcome']} {m['probability']:.0%} "
            f"(${m['volume_usd']:,.0f} volume, resolves {m['resolves'] or ''}{wk_str})"
        )

    return header + "\n".join(lines) + "\n"
