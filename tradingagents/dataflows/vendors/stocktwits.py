"""StockTwits public symbol-stream fetcher.

StockTwits exposes a per-symbol message stream at
``api.stocktwits.com/api/2/streams/symbol/{ticker}.json`` that requires no
API key, no OAuth, and no registration. Each message includes a
user-labeled sentiment field (``Bullish``/``Bearish``/null), the message
body, timestamp, and posting user.

The function is deliberately self-contained: short timeout, graceful
degradation on any HTTP or parse failure, and a string return type so
the calling agent gets a uniform interface regardless of whether the
network call succeeded.

Transport note: ``api.stocktwits.com`` sits behind Cloudflare, which judges the
TLS/JA3 and HTTP/2 frame fingerprint rather than the user agent or the exit IP.
From one IP, curl and a browser-impersonating client get 200 while urllib gets
403'd — so requests go through ``curl_cffi`` with a rotating profile list and
fall back to plain urllib only when that client is not installed.
"""

from __future__ import annotations

import contextlib
import html
import http.client
import json
import logging
from datetime import datetime
from urllib.request import Request

from tradingagents.dataflows.date_window import coverage_gap, in_window
from tradingagents.dataflows.symbols import crypto_base
from tradingagents.dataflows.vendors._net import opener, requests_proxies

try:  # optional: browser-fingerprinted HTTP client
    from curl_cffi import requests as _cffi
except Exception:  # pragma: no cover - exercised only in stripped environments
    _cffi = None


def urlopen(req, timeout=None):
    """Open a request through the sentiment proxy (10808 by default)."""
    return opener().open(req, timeout=timeout)

logger = logging.getLogger(__name__)

_API = "https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
_UA = "tradingagents/0.2 (+https://github.com/TauricResearch/TradingAgents)"

# Cloudflare's allowlist tracks *recent* builds: the generic "chrome" alias (and
# current Safari/Firefox/Edge profiles) pass, while a pinned old build such as
# "chrome124" is refused. So the list leads with the alias that follows the
# newest Chrome and keeps two other engines as fallbacks for the day the alias
# itself is reclassified.
_FINGERPRINTS = ("chrome", "safari17_0", "firefox133")
_CHALLENGE_STATUSES = frozenset({403, 429})

# The profile that last worked, tried first so only the first fetch of a run
# pays for the 403 round-trip.
_working_profile: str | None = None


class StockTwitsUnavailable(OSError):
    """The stream could not be read; ``reason`` is a short human-readable label."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _payload_via_urllib(url: str, timeout: float) -> dict:
    """Plain-urllib path: used only when curl_cffi is not installed."""
    req = Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _payload_via_fingerprint(url: str, timeout: float) -> dict:
    """Fetch through curl_cffi, retrying the profiles Cloudflare still allows."""
    global _working_profile
    order = (([_working_profile] if _working_profile else [])
             + [p for p in _FINGERPRINTS if p != _working_profile])
    status = 0
    for profile in order:
        try:
            resp = _cffi.get(
                url,
                impersonate=profile,
                timeout=timeout,
                headers={"Accept": "application/json"},
                proxies=requests_proxies(),
            )
        except Exception as exc:  # curl_cffi errors are not OSErrors
            raise StockTwitsUnavailable(type(exc).__name__) from exc
        status = resp.status_code
        if status == 200:
            _working_profile = profile
            try:
                return json.loads(resp.content)
            except ValueError as exc:
                raise StockTwitsUnavailable("BadJSON") from exc
        if status not in _CHALLENGE_STATUSES:
            # Connect worked, this symbol/route is simply not served: trying
            # another fingerprint cannot change the answer.
            raise StockTwitsUnavailable(f"HTTP {status}")
    raise StockTwitsUnavailable(f"blocked ({status})")


def _fetch_payload(url: str, timeout: float) -> dict:
    """GET ``url`` as JSON, bounded to a single transport attempt chain."""
    if _cffi is None:
        return _payload_via_urllib(url, timeout)
    return _payload_via_fingerprint(url, timeout)


def _created_at(message) -> datetime | None:
    """Parse a message's ISO 8601 ``created_at``; None when missing or malformed."""
    raw = message.get("created_at")
    if not raw:
        return None
    with contextlib.suppress(ValueError, TypeError):
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    return None


def _within_window(messages, start_date, end_date):
    """Keep only messages published in [start_date, end_date] (look-ahead safe).

    No window (both None) leaves the list untouched for live callers. A message
    whose ``created_at`` (ISO 8601) is unparseable is dropped in a historical
    window, since we can't prove it isn't from after the as-of date (#1220).
    """
    if not (start_date and end_date):
        return messages
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    return [m for m in messages if in_window(_created_at(m), start_dt, end_dt)]


def _body(message) -> str:
    """The message text; the API serves it HTML-escaped (``&amp;``, ``&#39;``)."""
    return html.unescape(message.get("body") or "")


def _stocktwits_symbol(ticker: str) -> str:
    """Map a crypto pair to StockTwits' ``<BASE>.X`` convention.

    StockTwits lists crypto as ``BTC.X`` (Yahoo's ``BTC-USD`` form 404s), so any
    crypto symbol resolves to its base plus ``.X``; other symbols pass through
    upper-cased.
    """
    base = crypto_base(ticker)
    return f"{base}.X" if base else ticker.strip().upper()


def fetch_stocktwits_messages(
    ticker: str,
    limit: int = 30,
    timeout: float = 10.0,
    start_date: str | None = None,
    end_date: str | None = None,
    screen=None,
) -> str:
    """Fetch recent StockTwits messages for ``ticker`` and return them as a
    formatted plaintext block ready for prompt injection.

    When ``start_date``/``end_date`` (yyyy-mm-dd) are given, messages are trimmed
    to that window, so a historical run never sees today's chatter (#1220). The
    public stream only serves recent messages, so a window it cannot reach is
    reported as unavailable rather than as silence.

    ``screen``, when given, takes the message bodies and returns a keep flag per
    message and a note line that heads the block.

    Returns a placeholder string when the endpoint is unreachable, the
    symbol has no messages, or the response shape is unexpected — the
    caller never has to special-case None or exceptions.
    """
    url = _API.format(ticker=_stocktwits_symbol(ticker))
    try:
        data = _fetch_payload(url, timeout)
    except StockTwitsUnavailable as exc:
        # A refusal is an expected state for this feed, not a pipeline error:
        # degrade to a placeholder the agent can read and move past.
        logger.warning("StockTwits unavailable for %s: %s", ticker, exc.reason)
        return f"<stocktwits unavailable: {exc.reason}>"
    except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
        # OSError covers URLError/TimeoutError/connection resets; HTTPException
        # covers chunked-transfer errors (IncompleteRead/BadStatusLine, #1024).
        logger.warning("StockTwits fetch failed for %s: %s", ticker, exc)
        return f"<stocktwits unavailable: {type(exc).__name__}>"

    fetched = data.get("messages", []) if isinstance(data, dict) else []
    messages = _within_window(fetched, start_date, end_date)
    if not messages:
        if start_date and end_date:
            gap = coverage_gap(
                (_created_at(m) for m in fetched), start_date, end_date,
                "StockTwits", f"messages about ${ticker.upper()}",
            )
            return gap or (
                f"<no StockTwits messages for ${ticker.upper()} within "
                f"{start_date}..{end_date}>"
            )
        return f"<no StockTwits messages found for ${ticker.upper()}>"

    note = ""
    if screen:
        keep, note = screen([_body(m) for m in messages])
        screened = len(messages)
        messages = [m for m, kept in zip(messages, keep, strict=True) if kept]
        if not messages:
            return f"{note}\n\n<none of the {screened} StockTwits messages is about ${ticker.upper()}>"

    lines = []
    bullish = bearish = unlabeled = 0
    for m in messages[:limit]:
        created = m.get("created_at", "")
        user = (m.get("user") or {}).get("username", "?")
        entities = m.get("entities") or {}
        sentiment_obj = entities.get("sentiment") or {}
        sentiment = sentiment_obj.get("basic") if isinstance(sentiment_obj, dict) else None
        body = _body(m).replace("\n", " ").strip()
        if len(body) > 280:
            body = body[:280] + "…"

        if sentiment == "Bullish":
            bullish += 1
            tag = "Bullish"
        elif sentiment == "Bearish":
            bearish += 1
            tag = "Bearish"
        else:
            unlabeled += 1
            tag = "no-label"
        lines.append(f"[{created} · @{user} · {tag}] {body}")

    total = bullish + bearish + unlabeled
    bull_pct = round(100 * bullish / total) if total else 0
    bear_pct = round(100 * bearish / total) if total else 0
    summary = (
        f"Bullish: {bullish} ({bull_pct}%) · "
        f"Bearish: {bearish} ({bear_pct}%) · "
        f"Unlabeled: {unlabeled} · "
        f"Total: {total} most-recent messages"
    )
    return (f"{note}\n\n" if note else "") + summary + "\n\n" + "\n".join(lines)
