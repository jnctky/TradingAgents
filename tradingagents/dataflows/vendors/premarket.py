"""Extended-session snapshot: what the tape looks like before the open.

Why this exists (2026-11-12): the daily batch now runs twice — once before the
US open and once after the close. The close run has the day's bars; the
pre-open run does not, and its whole question is "what is the market doing
right now, ahead of the open". Answering that from the previous close alone
would make the pre-market slot a copy of the previous evening's run.

nasdaq.com publishes exactly this: one row per symbol for the extended
session, with the consolidated price, the change against the prior close, the
session volume, the session high/low with timestamps, and a "data last
updated" line. No key, no quota problem at 22 symbols a day.

Three rules, all of them consequences of the data-integrity principle:

1. The snapshot is an *entry reference*, never a substitute for the close.
   Every indicator keeps using the daily bars; the analysts are told the
   snapshot may only inform the open/gap and the timing of an entry.
2. A crypto pair has no pre-market. That is ``not_applicable``, not a
   failure, and the analyst is told so instead of being handed a hedge.
3. A failed fetch returns the ``DATA_UNAVAILABLE`` sentinel and is traced as
   a failure. It is never filled with the previous close — a stale number
   labelled "pre-market" is exactly the substitution this project forbids.

The endpoint answers ``informational`` data with ``assetclass=stocks``; ETFs
come back empty there and need ``assetclass=etf``, so the lookup falls
through rather than reporting a failure for something that exists.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from typing import Any

from tradingagents.dataflows.data_integrity import NOT_APPLICABLE_VENDOR, UNAVAILABLE_PREFIX
from tradingagents.dataflows.trace import record_call

logger = logging.getLogger(__name__)

VENDOR = "nasdaq"
CATEGORY = "premarket"

#: Browser-ish headers. The host is the same one the OpenBB nasdaq provider
#: talks to, and it serves this endpoint only to a plausible client.
_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}

#: Asset classes to try, in order. An ETF answers nothing under "stocks".
_ASSET_CLASS_CHAIN = ("stocks", "etf")

_MONEY = re.compile(r"\$?(-?[\d,]+(?:\.\d+)?)")
_CHANGE = re.compile(r"\(\s*([+-]?[\d.]+)%\s*\)")
_HIGH_LOW_TIME = re.compile(r"\(\s*(\d{1,2}:\d{2}:\d{2}\s*[AP]M)\s*\)")


def _num(text: Any) -> float | None:
    if not isinstance(text, str):
        return None
    match = _MONEY.search(text)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _session_of(slot: str) -> str:
    return "pre" if str(slot).lower() == "pre" else "post"


def fetch_extended_session(ticker: str, *, asset_class: str = "equity",
                           slot: str = "pre") -> dict:
    """One symbol's extended-session row, or a described miss.

    Returns a dict, never raises: the caller records it on the row and keeps
    going. ``status`` is ``ok`` / ``na`` / ``failed``.
    """
    session = _session_of(slot)
    started = time.perf_counter()

    if str(asset_class).lower() == "crypto":
        # Nothing to ask: coins trade around the clock. Traced as a design
        # answer so the integrity table shows 不适用 rather than 失败.
        record_call(
            method="get_extended_session", category=CATEGORY,
            vendor=NOT_APPLICABLE_VENDOR, chain=[VENDOR],
            args=(ticker,), kwargs={"slot": session},
            ms=(time.perf_counter() - started) * 1000,
            result="not applicable: crypto trades continuously",
        )
        return {"status": "na", "slot": session, "ticker": ticker,
                "reason": "加密资产 24 小时连续交易，无盘前/盘后时段"}

    last_error = ""
    for asset in _ASSET_CLASS_CHAIN:
        url = (f"https://api.nasdaq.com/api/quote/{ticker}/extended-trading"
               f"?assetclass={asset}&markettype={session}")
        try:
            from curl_cffi import requests as curl_requests

            response = curl_requests.get(url, impersonate="chrome124", timeout=20,
                                         headers=_HEADERS)
            if response.status_code != 200:
                last_error = f"HTTP {response.status_code}"
                continue
            payload = response.json()
            data = (payload or {}).get("data") or {}
            rows = ((data.get("infoTable") or {}).get("rows")) or []
            if not rows:
                # An ETF under "stocks" answers 200 with an empty table, not an
                # error — fall through to the next asset class instead of
                # calling a listed fund a data outage.
                last_error = last_error or "无扩展时段成交"
                continue
            parsed = _parse_row(rows[0], data, session, ticker)
            record_call(
                method="get_extended_session", category=CATEGORY,
                vendor=VENDOR, chain=[VENDOR],
                args=(ticker,), kwargs={"slot": session, "assetclass": asset},
                ms=(time.perf_counter() - started) * 1000,
                result=json.dumps(parsed, ensure_ascii=False),
            )
            logger.info("[premarket] %s %s %s", ticker, session, parsed.get("as_of"))
            return parsed
        except Exception as exc:                      # noqa: BLE001 - never break a run
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("[premarket] %s %s 失败：%s", ticker, asset, last_error)

    record_call(
        method="get_extended_session", category=CATEGORY,
        vendor=VENDOR, chain=[VENDOR],
        args=(ticker,), kwargs={"slot": session},
        ms=(time.perf_counter() - started) * 1000,
        error=last_error or "未知错误",
    )
    return {"status": "failed", "slot": session, "ticker": ticker,
            "error": last_error or "未知错误"}


def _parse_row(row: dict, data: dict, session: str, ticker: str) -> dict:
    """Endpoint row → flat scalars. Strings stay verbatim for the reader too."""
    consolidated = str(row.get("consolidated") or "")
    change_pct = None
    match = _CHANGE.search(consolidated)
    if match:
        try:
            change_pct = float(match.group(1))
        except ValueError:
            change_pct = None
    previous = None
    previous_line = str(data.get("previousInfo") or "")
    if previous_line:
        previous = _num(previous_line)

    updates = data.get("lastUpdateInfo") or []
    as_of = updates[0] if updates and isinstance(updates[0], str) else ""
    # "Data last updated Nov 11, 2026 09:30 AM ET."
    as_of = as_of.replace("Data last updated ", "").rstrip(".")

    high = str(row.get("highPrice") or "")
    low = str(row.get("lowPrice") or "")
    return {
        "status": "ok",
        "slot": session,
        "ticker": ticker,
        # "$349.49 +0.98 (+0.28%)": the first money token is the price, whatever
        # the change's sign.
        "price": _num(consolidated),
        "change_pct": change_pct,
        "volume": _num(row.get("volume")),
        "high": _num(high),
        "low": _num(low),
        "high_at": (_HIGH_LOW_TIME.search(high).group(1) if _HIGH_LOW_TIME.search(high) else ""),
        "low_at": (_HIGH_LOW_TIME.search(low).group(1) if _HIGH_LOW_TIME.search(low) else ""),
        "previous_close": previous,
        "as_of": as_of,
        "raw": consolidated,
    }


_SESSION_LABEL = {"pre": "盘前", "post": "盘后"}


def render_extended_session(snapshot: dict | None) -> str:
    """Markdown block for the analyst prompt.

    The instructions matter as much as the numbers: without them the snapshot
    invites "the stock is 3% higher pre-market, therefore momentum is bullish",
    which is a different question from the one the daily bars answer.
    """
    if not snapshot:
        return ""
    status = snapshot.get("status")
    session = _SESSION_LABEL.get(str(snapshot.get("slot")), "盘前")

    if status == "na":
        return (f"### {session}快照\n\n不适用：{snapshot.get('reason') or '该标的没有扩展时段'}。"
                "现价请以对应交易所的连续行情为准。\n")

    if status != "ok":
        return (f"### {session}快照\n\n{UNAVAILABLE_PREFIX}: {session}快照未取得"
                f"（{snapshot.get('error') or '数据源未返回'}）。"
                f"本次分析不含{session}成交信息；不得用前一交易日收盘价冒充"
                f"{session}价，也不得据此推断开盘方向。\n")

    price = snapshot.get("price")
    change = snapshot.get("change_pct")
    volume = snapshot.get("volume")
    high, low = snapshot.get("high"), snapshot.get("low")
    previous = snapshot.get("previous_close")
    as_of = snapshot.get("as_of") or "时间未标注"

    bits = [f"- 最新 {price:.2f}" if isinstance(price, (int, float)) else "- 最新价未提供"]
    if isinstance(change, (int, float)):
        bits.append(f"（{change:+.2f}%）")
    line = "".join(bits)
    if isinstance(volume, (int, float)):
        line += f"　成交量 {volume:,.0f}"
    lines = [f"### {session}快照（nasdaq，截至 {as_of}）", "", line]
    if isinstance(high, (int, float)) and isinstance(low, (int, float)):
        lines.append(f"- 时段区间 {low:.2f} – {high:.2f}")
    if isinstance(previous, (int, float)):
        lines.append(f"- 前一收盘 {previous:.2f}")
    lines += [
        "",
        f"用法：这份快照只回答「{session}时段价格在哪、量能如何」，用于判断开盘缺口"
        "与入场时点。技术指标、均线、支撑阻力的计算仍以日线收盘为准——不得用"
        f"{session}价替换任何收盘价，也不得因为{session}涨跌就改变趋势判断。"
        "数据源只提供成交汇总，不提供逐笔与盘口。",
        "",
    ]
    return "\n".join(lines)


def snapshot_row_fields(snapshot: dict | None) -> dict:
    """The few scalars the decision row and the cockpit keep."""
    if not isinstance(snapshot, dict):
        return {}
    out: dict[str, Any] = {"status": snapshot.get("status"),
                           "slot": snapshot.get("slot")}
    for key in ("price", "change_pct", "volume", "previous_close"):
        value = snapshot.get(key)
        if isinstance(value, (int, float)):
            out[key] = value
    if snapshot.get("as_of"):
        out["as_of"] = str(snapshot["as_of"])
    if snapshot.get("error"):
        out["error"] = str(snapshot["error"])[:200]
    if snapshot.get("reason"):
        out["reason"] = str(snapshot["reason"])
    out["fetched_at"] = datetime.now().isoformat(timespec="seconds")
    return out
