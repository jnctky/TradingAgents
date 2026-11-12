"""Index futures: the market-wide tape that a single symbol's quote cannot show.

Why this exists (2026-11-12): the pre-open slot answers "what is the tape doing
ahead of the bell". A symbol's own extended-session row says *that* it is quoted
lower, never *why*; with "/NQ −0.8%" in front of them the analysts can read a
market-wide move as a market-wide move instead of hunting for company news that
would explain it. The reverse mistake is just as expensive: a name that is flat
pre-market in a −1% tape is outperforming, and nobody notices without the index.

Four contracts: /ES, /NQ, /YM, /RTY — the four the press quotes, so a reader can
match a headline to a row.

The same three rules as the extended-session snapshot apply:

1. **Context, not a price.** The tape may inform "gap risk / risk appetite"; it
   never replaces a close in any indicator, and it never implies that a symbol
   will open at the same percentage.
2. **A failed fetch is a failure.** ``DATA_UNAVAILABLE`` with the reason. The
   previous settle is never dressed up as "now" — a stale number under a live
   label is exactly the substitution this project forbids.
3. **The reading carries its own clock.** Futures trade nearly around the clock,
   so "last" is only meaningful with the timestamp Yahoo stamps on it; that
   timestamp travels into the row and into the report.

Why no VIX: this feed does not carry a VIX future (``VIX=F`` is a 404), and cash
VIX only prints during the cash session, so a "pre-open VIX" would be yesterday's
close wearing today's label. The macro block already prints VIX from FRED *with
its observation date*, which is the honest version of the same number.

Source: Yahoo's chart endpoint, the keyless route ``yfinance`` itself uses. From
the mainland it answers only through the local proxy, so the dial comes from
``_net.sentiment_proxy()`` — the same one StockTwits and Reddit use.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from tradingagents.dataflows.data_integrity import UNAVAILABLE_PREFIX
from tradingagents.dataflows.trace import record_call
from tradingagents.dataflows.vendors._net import requests_proxies

logger = logging.getLogger(__name__)

VENDOR = "yahoo"
CATEGORY = "futures"

#: (short name, Yahoo symbol, 中文名). Order is the order they print in.
CONTRACTS: tuple[tuple[str, str, str], ...] = (
    ("ES", "ES=F", "标普 500 期货"),
    ("NQ", "NQ=F", "纳指 100 期货"),
    ("YM", "YM=F", "道指期货"),
    ("RTY", "RTY=F", "罗素 2000 期货"),
)

_HOST = "https://query1.finance.yahoo.com"

_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://finance.yahoo.com/",
}

_SESSION_LABEL = {"pre": "盘前氛围", "post": "收盘后隔夜"}


def _session_of(slot: str) -> str:
    return "pre" if str(slot).lower() == "pre" else "post"


def _stamp(epoch: Any, offset: Any) -> str:
    """Yahoo's epoch + exchange offset → ``2026-11-11 22:30 ET``.

    Built from the offset Yahoo reports instead of a tz database lookup: the
    contracts are CME, the offset already accounts for EST/EDT, and no tzdata
    file is guaranteed to exist on a Windows box.
    """
    if not isinstance(epoch, (int, float)):
        return ""
    try:
        shift = int(offset) if isinstance(offset, (int, float)) else 0
        return (datetime.fromtimestamp(float(epoch), tz=timezone.utc)
                + timedelta(seconds=shift)).strftime("%Y-%m-%d %H:%M ET")
    except (OverflowError, OSError, ValueError):
        return ""


def _fetch_one(symbol: str, attempts: int = 2) -> tuple[dict | None, str]:
    """One contract's meta, or the reason it could not be read.

    Retried once: four requests over one proxy drop one now and then, and a
    missing row in the tape is a real loss of context (the reader compares the
    four). A 404 is not retried — that is an answer, not a hiccup.
    """
    url = f"{_HOST}/v8/finance/chart/{symbol}?interval=5m&range=1d"
    last_error = ""
    for attempt in range(attempts):
        if attempt:
            time.sleep(1.5)
        try:
            from curl_cffi import requests as curl_requests

            response = curl_requests.get(url, impersonate="chrome124", timeout=20,
                                         headers=_HEADERS,
                                         proxies=requests_proxies())
            if response.status_code == 404:
                return None, "HTTP 404（该合约不存在）"
            if response.status_code != 200:
                last_error = f"HTTP {response.status_code}"
                continue
            results = ((response.json() or {}).get("chart") or {}).get("result") or []
            if not results:
                last_error = "数据源未返回结果"
                continue
            return results[0].get("meta") or {}, ""
        except Exception as exc:                  # noqa: BLE001 - never break a run
            # curl's own message is three lines of advice; the first clause is
            # the part a reader needs in a table cell.
            last_error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:80]}"
            logger.warning("[futures] %s 第 %d 次失败：%s", symbol, attempt + 1, last_error)
    return None, last_error or "未知错误"


def _row(name: str, symbol: str, label: str, meta: dict | None,
         error: str) -> dict[str, Any]:
    base: dict[str, Any] = {"symbol": name, "yahoo": symbol, "label": label,
                            "status": "ok"}
    if not meta:
        return {**base, "status": "failed", "error": error or "数据源未返回"}

    price = meta.get("regularMarketPrice")
    previous = meta.get("previousClose")
    change_pct = meta.get("regularMarketChangePercent")
    if not isinstance(price, (int, float)):
        return {**base, "status": "failed", "error": "无最新价"}
    if not isinstance(change_pct, (int, float)) and isinstance(previous, (int, float)) and previous:
        change_pct = (price / previous - 1) * 100
    out = {**base,
           "contract": str(meta.get("shortName") or ""),
           "price": float(price),
           "change_pct": float(change_pct) if isinstance(change_pct, (int, float)) else None,
           "as_of": _stamp(meta.get("regularMarketTime"), meta.get("gmtoffset"))}
    if isinstance(previous, (int, float)):
        out["previous_close"] = float(previous)
        out["change"] = float(price) - float(previous)
    for key, meta_key in (("volume", "regularMarketVolume"),
                          ("day_high", "regularMarketDayHigh"),
                          ("day_low", "regularMarketDayLow")):
        value = meta.get(meta_key)
        if isinstance(value, (int, float)):
            out[key] = float(value)
    return out


def fetch_index_futures(slot: str = "pre") -> dict:
    """All four contracts for this session. Never raises; never fills a gap.

    ``status`` is ``ok`` (every contract answered), ``partial`` (some did — the
    missing ones are named), or ``failed`` (none did).
    """
    session = _session_of(slot)
    started = time.perf_counter()
    rows = []
    for name, symbol, label in CONTRACTS:
        meta, error = _fetch_one(symbol)
        rows.append(_row(name, symbol, label, meta, error))
        state = rows[-1]["status"]
        if state != "ok":
            logger.warning("[futures] %s %s 失败：%s", name, session, rows[-1].get("error"))

    ok = [r for r in rows if r["status"] == "ok"]
    status = "ok" if len(ok) == len(rows) else ("partial" if ok else "failed")
    tape = {"status": status, "slot": session, "rows": rows,
            "fetched_at": datetime.now().isoformat(timespec="seconds")}
    # One trace line per contract is noise; the batch asks once, so one line
    # that shows what every contract answered is the readable version.
    record_call(
        method="get_index_futures", category=CATEGORY, vendor=VENDOR, chain=[VENDOR],
        args=(session,), kwargs={"contracts": [c[0] for c in CONTRACTS]},
        ms=(time.perf_counter() - started) * 1000,
        result=json.dumps({"status": status,
                           "rows": [{k: r.get(k) for k in ("symbol", "price", "change_pct", "status")}
                                    for r in rows]}, ensure_ascii=False),
    )
    return tape


def render_index_futures(tape: dict | None) -> str:
    """Markdown block for the analyst prompt.

    The usage note is not decoration: handed four negative numbers and nothing
    else, an analyst writes "the market is weak so this name is weak", which is
    a different claim from "this name is holding up in a weak tape".
    """
    if not isinstance(tape, dict) or not tape.get("rows"):
        return ""
    session = _session_of(tape.get("slot"))
    heading = _SESSION_LABEL[session]
    rows = tape.get("rows") or []
    ok = [r for r in rows if r.get("status") == "ok"]

    if not ok:
        reasons = "；".join(f"{r.get('symbol')}（{r.get('error') or '未返回'}）" for r in rows)
        return (f"### 指数期货（{heading}）\n\n{UNAVAILABLE_PREFIX}: 指数期货未取得"
                f"（{reasons}）。本次分析不含指数期货读数，不得用昨日收盘或任何"
                "替代数据推断开盘方向与风险偏好。\n")

    as_of = next((r.get("as_of") for r in ok if r.get("as_of")), "时间未标注")
    lines = [f"### 指数期货（{heading}，yahoo，截至 {as_of}）", ""]
    for row in rows:
        if row.get("status") != "ok":
            lines.append(f"- {row.get('label')}（{row.get('symbol')}）：**未取得**"
                         f"（{row.get('error') or '数据源未返回'}）")
            continue
        change = row.get("change_pct")
        text = f"- {row.get('label')}（{row.get('symbol')}）{row['price']:.2f}"
        if isinstance(change, (int, float)):
            text += f"　{change:+.2f}%"
        if row.get("volume"):
            text += f"　成交 {row['volume']:,.0f}"
        lines.append(text)
    when_text = "开盘前" if session == "pre" else "下一场开盘前"
    lines += [
        "",
        f"用法：这四个合约只回答「{when_text}整个市场在涨还是在跌、风险偏好在不在」，"
        "用来判断这只标的的开盘环境与缺口风险。它们不是这只标的的价格，"
        "不得据此推算它的开盘价；标的自身的结论仍以它的日线、指标与本场快照为准。"
        "若标的跌幅明显小于指数，说明它在弱市中相对抗跌，这一点值得写进理由。",
        "",
    ]
    return "\n".join(lines)


def tape_row_fields(tape: dict | None) -> dict:
    """The few scalars the decision file and the cockpit keep."""
    if not isinstance(tape, dict):
        return {}
    rows = []
    for row in tape.get("rows") or []:
        if row.get("status") != "ok":
            continue
        rows.append({k: row[k] for k in
                     ("symbol", "label", "price", "change_pct", "as_of")
                     if k in row})
    out: dict[str, Any] = {"status": tape.get("status") or "failed",
                           "slot": tape.get("slot"),
                           "fetched_at": tape.get("fetched_at")}
    if rows:
        out["rows"] = rows
    missing = [r.get("symbol") for r in tape.get("rows") or [] if r.get("status") != "ok"]
    if missing:
        out["missing"] = missing
    return out
