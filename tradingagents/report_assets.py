"""The evidence pack: what the analysis actually read, in checkable form.

A finished run leaves two traces behind: the structured provenance JSONL (every
vendor call the router served) and the agents' report texts that cite figures.
Neither is easy for a reader to verify against — the trace is raw JSONL, the
citations are prose. This module assembles both into an *evidence pack*:

- ``price``   the OHLCV rows (same vendor chain, same as-of cutoff) + the
              indicator values (stockstats, the snapshot's engine) → charts
- ``macro``   four FRED series with the same vintage pinning the macro tool
              used → chart + a re-check link per series
- ``news``    article URLs the news vendors returned, extracted from the trace
- ``trace``   the per-call provenance summary (tool, vendor, args, status)

Everything here is best-effort and read-only: a failed block is recorded in
``evidence['errors']`` and skipped, never fatal. Nothing calls the LLM.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

PRICE_WINDOW_ROWS = 130          # ~6 months of trading days
MACRO_LOOKBACK_DAYS = 180
MAX_NEWS_LINKS = 8

# The macro panel: what a reader needs to judge rate/dollar/curve context.
MACRO_SERIES = (
    ("DTWEXBGS", "Dollar index (trade-weighted)"),
    ("DGS10", "10-year Treasury yield"),
    ("T10Y2Y", "2s10s yield-curve spread"),
    ("DFF", "Fed funds effective rate"),
)

_URL_RE = re.compile(r"https?://[^\s)>\]\"']+")


def build_evidence(ticker: str, trade_date: str, trace_path=None) -> dict | None:
    """Assemble the evidence pack for a finished run; ``None`` if nothing usable.

    ``trace_path`` defaults to the batch runner's
    ``{state}/data_sources/{date}/{ticker}.jsonl`` layout via ``TA_TRACE_FILE``.
    """
    evidence: dict = {"ticker": ticker, "trade_date": trade_date, "errors": []}
    blocks = (
        ("price", _price_block, (ticker, trade_date)),
        ("macro", _macro_block, (trade_date,)),
        ("trace", _trace_block, (trace_path, ticker, trade_date)),
    )
    filled = 0
    for key, builder, args in blocks:
        try:
            block = builder(*args)
        except Exception as exc:  # noqa: BLE001 - evidence must never break a report
            evidence["errors"].append(f"{key}: {type(exc).__name__}: {exc}")
            continue
        if block:
            evidence[key] = block
            filled += 1
    if key := "trace":
        # News links come out of the trace block; attach them at top level so
        # the report section can list them even when the charts are skipped.
        links = (evidence.get("trace") or {}).get("news_links") or []
        if links:
            evidence["news_links"] = links
    if filled == 0:
        return None
    return evidence


# ---------------------------------------------------------------- price block

def _price_block(ticker: str, trade_date: str) -> dict | None:
    import pandas as pd
    from stockstats import wrap

    from tradingagents.dataflows.vendors.ohlcv import load_ohlcv

    df = load_ohlcv(ticker, trade_date, fill_gaps=False)
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date"])
    df = df[df["Date"] <= pd.to_datetime(trade_date)].sort_values("Date").reset_index(drop=True)
    if df.empty:
        return None

    stock = wrap(df.copy())
    indicator_cols = ("rsi", "macd", "macds", "macdh", "boll_ub", "boll_lb",
                      "close_50_sma", "close_200_sma", "atr")
    for col in indicator_cols:
        stock[col]          # trigger stockstats lazily

    window = df.tail(PRICE_WINDOW_ROWS)
    offset = len(df) - len(window)
    dates = [d.strftime("%Y-%m-%d") for d in window["Date"]]

    def col(name: str) -> list:
        series = stock[name].iloc[offset:]
        return [None if pd.isna(v) else float(v) for v in series]

    rows = []
    for _, row in window.iterrows():
        rows.append({
            "date": row["Date"].strftime("%Y-%m-%d"),
            "open": _num(row.get("Open")),
            "high": _num(row.get("High")),
            "low": _num(row.get("Low")),
            "close": _num(row.get("Close")),
            "volume": _num(row.get("Volume")),
        })

    latest = rows[-1] if rows else {}
    indicators = {name: _round(col(name)[-1]) for name in indicator_cols}
    return {
        "symbol": ticker,
        "analysis_date": trade_date,
        "latest": latest,
        "window_start": dates[0] if dates else "",
        "rows": rows,
        "dates": dates,
        "bands": {"upper": col("boll_ub"), "lower": col("boll_lb")},
        "series": {
            "rsi": col("rsi"), "macd": col("macd"), "macds": col("macds"),
            "macdh": col("macdh"), "ma50": col("close_50_sma"),
            "ma200": col("close_200_sma"),
        },
        "indicators": indicators,
        "source": "load_ohlcv() — the same vendor chain and as-of cutoff the analysts' tools used",
    }


# ---------------------------------------------------------------- macro block

def _macro_block(trade_date: str) -> list | None:
    """Four FRED series, same engine the macro tool used.

    Preferred: the FRED vendor with its vintage pinning (realtime bounds set to
    the analysis date, #1275) when ``FRED_API_KEY`` is configured. Fallback:
    the local OpenBB platform's keyless ``economy/fred_series`` route — same
    series IDs, but the latest published revisions, which is stated in the
    ``vintage`` flag the report surfaces.
    """
    from tradingagents.dataflows.vendors.openbb.client import get_json

    end = datetime.strptime(trade_date, "%Y-%m-%d")
    start = (end - timedelta(days=MACRO_LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    out: list = []
    missing: list[str] = []
    try:
        from tradingagents.dataflows.vendors.fred import (
            _fred_today,
            _request,
            _resolve_series_id,
        )

        pit = min(trade_date, _fred_today())
        realtime = {"realtime_start": pit, "realtime_end": pit}
        for series_id, title in MACRO_SERIES:
            observations = _request(
                "series/observations",
                {
                    "series_id": _resolve_series_id(series_id),
                    "observation_start": start,
                    "observation_end": trade_date,
                    "sort_order": "asc",
                    **realtime,
                },
            ).get("observations", [])
            points = [
                (obs["date"], float(obs["value"]))
                for obs in observations
                if obs.get("value") not in (None, ".", "")
                and _is_number(obs.get("value"))
            ]
            if len(points) >= 2:
                out.append({"id": series_id, "title": title, "points": points,
                            "vintage": True,
                            "url": f"https://fred.stlouisfed.org/series/{series_id}"})
            else:
                missing.append(series_id)
    except Exception:                       # noqa: BLE001 - no key / network: use the platform
        missing = [series_id for series_id, _t in MACRO_SERIES]

    if missing:
        rows = get_json(
            "/api/v1/economy/fred_series", "FRED series",
            params={"provider": "fred", "symbol": ",".join(missing),
                    "start_date": start, "end_date": trade_date},
        ) or []
        for series_id, title in MACRO_SERIES:
            if series_id not in missing:
                continue
            points = [
                (str(row.get("date"))[:10], float(row[series_id]))
                for row in rows
                if row.get(series_id) is not None and _is_number(row.get(series_id))
            ]
            if len(points) >= 2:
                out.append({"id": series_id, "title": title, "points": points,
                            "vintage": False,
                            "url": f"https://fred.stlouisfed.org/series/{series_id}"})

    return out or None


def _is_number(value) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


# ---------------------------------------------------------------- trace block

def _trace_block(trace_path, ticker: str, trade_date: str) -> dict | None:
    path = _resolve_trace_path(trace_path, ticker, trade_date)
    if path is None or not path.exists():
        return None
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except ValueError:
            continue
        records.append({
            "tool": raw.get("tool"),
            "vendor": raw.get("vendor"),
            "args": raw.get("argsDisplay"),
            "ok": raw.get("ok", False),
            "ms": raw.get("ms"),
            "error": raw.get("error"),
            "sample": raw.get("sample"),
        })
    if not records:
        return None

    news_links: list[str] = []
    for record in records:
        if "news" not in (record.get("tool") or ""):
            continue
        for url in _URL_RE.findall(record.get("sample") or ""):
            url = url.rstrip(".,;")
            if url not in news_links:
                news_links.append(url)
            if len(news_links) >= MAX_NEWS_LINKS:
                break
        if len(news_links) >= MAX_NEWS_LINKS:
            break

    for record in records:
        record.pop("sample", None)          # keep evidence.json lean
    return {"path": str(path), "calls": records, "news_links": news_links}


def _resolve_trace_path(trace_path, ticker: str, trade_date: str):
    import os

    if trace_path:
        return Path(trace_path)
    explicit = os.environ.get("TA_TRACE_FILE")
    if explicit:
        return Path(explicit)
    root = os.environ.get("TA_TRACE_DIR")
    if root:
        return Path(root) / trade_date / f"{ticker}.jsonl"
    return Path.home() / ".tradingagents" / "logs" / "data_sources" / trade_date / f"{ticker}.jsonl"


# ---------------------------------------------------------------- renderers

def evidence_charts(evidence: dict, inline: bool = True) -> list[dict]:
    """The run's charts as ``[{'name','title','svg'}]``; blocks that failed are
    simply absent. ``inline=True`` colors follow the report page's CSS vars."""
    from tradingagents.charts import (
        CHART_COLORS,
        INLINE_COLORS,
        render_macd,
        render_price_chart,
        render_rsi,
    )

    colors = INLINE_COLORS if inline else CHART_COLORS
    charts: list[dict] = []
    price = evidence.get("price")
    if price and price.get("rows"):
        symbol = price.get("symbol") or evidence.get("ticker", "")
        date = price.get("analysis_date") or evidence.get("trade_date", "")
        charts.append({
            "name": "price",
            "title": f"{symbol.upper()} daily candles, {price.get('window_start', '')} → {date}",
            "svg": render_price_chart(price["rows"], symbol, date, colors, price.get("bands")),
        })
        series = price.get("series") or {}
        dates = price.get("dates") or [r.get("date", "") for r in price["rows"]]
        if series.get("rsi"):
            charts.append({"name": "rsi", "title": "RSI (14-day)",
                           "svg": render_rsi(dates, series["rsi"], colors)})
        if series.get("macd"):
            charts.append({"name": "macd", "title": "MACD (12, 26, 9)",
                           "svg": render_macd(dates, series["macd"], series.get("macds"),
                                              series.get("macdh"), colors)})
    if evidence.get("macro"):
        from tradingagents.charts import render_macro_grid
        charts.append({"name": "macro", "title": "Macro context (FRED)",
                       "svg": render_macro_grid(evidence["macro"], colors)})
    return charts


def write_evidence_files(save_path, evidence: dict) -> list[Path]:
    """Write ``charts/*.svg`` (fixed colors) + ``evidence.json`` next to the report."""
    save_path = Path(save_path)
    charts_dir = save_path / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for chart in evidence_charts(evidence, inline=False):
        target = charts_dir / f"{chart['name']}.svg"
        target.write_text(chart["svg"], encoding="utf-8")
        written.append(target)
    (save_path / "evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return written


def evidence_markdown(evidence: dict) -> str:
    """The "0. Evidence & charts" section for ``complete_report.md``."""
    from tradingagents.charts import fmt_num

    lines = ["## 0. Evidence & charts", "",
             "_What this analysis actually read. The charts are drawn from the same "
             "price rows the analysts' tools served during the run — nothing here is "
             "re-fetched after the fact._", ""]

    price = evidence.get("price") or {}
    if price.get("latest"):
        latest = price["latest"]
        lines += [
            "### Price baseline", "",
            f"- Latest verified row used by the agents: **{latest.get('date')} "
            f"close {fmt_num(latest.get('close'))}** "
            f"(open {fmt_num(latest.get('open'))}, high {fmt_num(latest.get('high'))}, "
            f"low {fmt_num(latest.get('low'))}, volume {fmt_volume_text(latest.get('volume'))})",
            f"- Chart window: {price.get('window_start', '')} → {latest.get('date')} "
            f"({len(price.get('rows') or [])} trading rows, as-of cutoff {price.get('analysis_date')})",
            f"- Source: {price.get('source')}", "",
            "### Charts", "",
        ]
        captions = {"price": "6-month daily candles with MA50 / MA200, Bollinger bands and volume",
                    "rsi": "RSI(14) — readings above 70 / below 30 mark stretched momentum",
                    "macd": "MACD(12, 26, 9) — histogram crosses signal the momentum turn",
                    "macro": "Macro context over the past 6 months (FRED, vintage-pinned to the analysis date)"}
        for chart in evidence_charts(evidence, inline=False):
            lines.append(f"![{chart['title']}](charts/{chart['name']}.svg)")
            lines.append("")
            lines.append(f"<small>*{captions.get(chart['name'], chart['title'])}*</small>")
            lines.append("")

    if evidence.get("price", {}).get("indicators"):
        indicators = evidence["price"]["indicators"]
        lines += ["### Indicator readings (latest verified row)", "",
                  "| Indicator | Value |", "|---|---:|"]
        for name, value in indicators.items():
            lines.append(f"| {name} | {fmt_num(value) if value is not None else 'N/A'} |")
        lines.append("")

    if evidence.get("macro"):
        lines += ["### Macro readings (FRED)", "",
                  "| Series | Latest | Window change | Re-check |", "|---|---:|---:|---|"]
        for item in evidence["macro"]:
            points = item["points"]
            first, last = points[0][1], points[-1][1]
            delta = last - first
            sign = "+" if delta > 0 else ""
            digits = 2 if abs(last) >= 5 else 2
            lines.append(f"| {item['title']} (`{item['id']}`) | {fmt_num(last, digits)} | "
                         f"{sign}{fmt_num(delta, digits)} over {len(points)} obs | "
                         f"[FRED]({item['url']}) |")
        if all(item.get("vintage") for item in evidence["macro"]):
            lines.append("")
            lines.append("_All series are vintage-pinned to the analysis date: the values "
                         "known at that date, not later revisions._")
        elif any(item.get("vintage") for item in evidence["macro"]):
            lines.append("")
            lines.append("_Series marked with revisions serve the latest published values "
                         "(no FRED key configured for them)._")
        else:
            lines.append("")
            lines.append("_These series serve the latest published values — no FRED key "
                         "is configured, so a point-in-time vintage could not be pinned._")
        lines.append("")

    links = evidence.get("news_links") or []
    if links:
        lines += ["### Article links from the news calls", ""]
        lines += [f"- <{url}>" for url in links]
        lines.append("")

    calls = (evidence.get("trace") or {}).get("calls") or []
    if calls:
        ok = sum(1 for c in calls if c.get("ok"))
        lines += ["### Data calls the agents made", "",
                  f"{len(calls)} vendor calls ({ok} answered). The full payload samples live in "
                  "`evidence.json` and the provenance trace.", "",
                  "| # | Tool | Vendor | Args | Status |", "|---|---|---|---|---|"]
        for i, call in enumerate(calls, 1):
            status = "ok" if call.get("ok") else f"FAILED: {call.get('error') or 'no data'}"
            lines.append(f"| {i} | {call.get('tool')} | {call.get('vendor') or '—'} | "
                         f"{_cell(call.get('args'))} | {status} |")
        lines.append("")

    if evidence.get("errors"):
        lines += ["### Evidence gaps", ""]
        lines += [f"- {error}" for error in evidence["errors"]]
        lines.append("")

    return "\n".join(lines)


def evidence_html(evidence: dict) -> str:
    """The evidence section body for the HTML report page (inline SVG charts)."""
    from html import escape

    from tradingagents.charts import fmt_num

    parts = []
    charts = evidence_charts(evidence, inline=True)
    for chart in charts:
        parts.append(f'<figure class="chart">{chart["svg"]}'
                     f"<figcaption>{escape(chart['title'])}</figcaption></figure>")

    price = evidence.get("price") or {}
    if price.get("latest"):
        latest = price["latest"]
        parts.append(
            f'<p class="evidence-note">Price baseline: latest verified row <strong>{escape(str(latest.get("date")))} '
            f'close {fmt_num(latest.get("close"))}</strong> · window {escape(str(price.get("window_start", "")))} → '
            f'{escape(str(latest.get("date")))} ({len(price.get("rows") or [])} rows, as-of {escape(str(price.get("analysis_date")))})'
            "</p>"
        )

    if evidence.get("price", {}).get("indicators"):
        rows = "".join(
            f"<tr><td>{escape(name)}</td><td>{fmt_num(value) if value is not None else 'N/A'}</td></tr>"
            for name, value in evidence["price"]["indicators"].items()
        )
        parts.append(f'<div class="table"><table><thead><tr><th>Indicator</th><th>Latest</th></tr></thead>'
                     f"<tbody>{rows}</tbody></table></div>")

    if evidence.get("macro"):
        rows = []
        for item in evidence["macro"]:
            points = item["points"]
            delta = points[-1][1] - points[0][1]
            sign = "+" if delta > 0 else ""
            digits = 2 if abs(points[-1][1]) >= 5 else 2
            rows.append(
                f"<tr><td>{escape(item['title'])} (<code>{escape(item['id'])}</code>)</td>"
                f"<td>{fmt_num(points[-1][1], digits)}</td><td>{sign}{fmt_num(delta, digits)}</td>"
                f'<td><a href="{escape(item["url"])}" rel="noopener">FRED</a></td></tr>'
            )
        parts.append('<div class="table"><table><thead><tr><th>Series (FRED)</th><th>Latest</th>'
                     "<th>Window change</th><th>Re-check</th></tr></thead>"
                     f"<tbody>{''.join(rows)}</tbody></table></div>")
        if not all(item.get("vintage") for item in evidence["macro"]):
            parts.append('<p class="evidence-note">Some or all of these series serve the '
                         "latest published revisions (no FRED key configured to pin a "
                         "point-in-time vintage).</p>")

    links = evidence.get("news_links") or []
    if links:
        items = "".join(f'<li><a href="{escape(url)}" rel="noopener">{escape(url)}</a></li>' for url in links)
        parts.append(f'<ul class="news-links">{items}</ul>')

    calls = (evidence.get("trace") or {}).get("calls") or []
    if calls:
        ok = sum(1 for c in calls if c.get("ok"))
        rows = []
        for i, call in enumerate(calls, 1):
            status = ("ok" if call.get("ok")
                      else f'<span class="failed">FAILED: {escape(call.get("error") or "no data")}</span>')
            rows.append(f"<tr><td>{i}</td><td>{escape(str(call.get('tool')))}</td>"
                        f"<td>{escape(str(call.get('vendor') or '—'))}</td>"
                        f"<td>{escape(str(call.get('args') or ''))}</td><td>{status}</td></tr>")
        parts.append(f'<p class="evidence-note">{len(calls)} vendor calls ({ok} answered). '
                     "Full payload samples: <code>evidence.json</code> / provenance trace.</p>")
        parts.append('<div class="table"><table><thead><tr><th>#</th><th>Tool</th><th>Vendor</th>'
                     "<th>Args</th><th>Status</th></tr></thead>"
                     f"<tbody>{''.join(rows)}</tbody></table></div>")

    if evidence.get("errors"):
        items = "".join(f"<li>{escape(error)}</li>" for error in evidence["errors"])
        parts.append(f'<ul class="evidence-gaps">{items}</ul>')

    return "".join(parts)


# ---------------------------------------------------------------- helpers

def _num(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value != value:          # NaN
        return None
    return value


def _round(value):
    return None if value is None else round(value, 4)


def _cell(text) -> str:
    text = str(text or "")
    return text if len(text) <= 60 else text[:57] + "…"


def fmt_volume_text(value) -> str:
    if value is None:
        return "N/A"
    for factor, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= factor:
            return f"{value / factor:.1f}{unit}"
    return f"{value:,.0f}"


def macro_section_markdown(final_state: dict) -> str:
    """The macro section body for a run: the narrative, then the readings behind it.

    Both halves come from the state the macro node settled, so the report can
    never show a narrative built on different numbers than the table below it.
    Empty when the backdrop could not be built — the caller omits the section
    rather than printing an empty heading.
    """
    import re

    from tradingagents.dataflows.vendors.macro_context import snapshot_markdown

    report = (final_state.get("macro_report") or "").strip()
    snapshot = final_state.get("macro_snapshot") or {}
    blocks = []
    if report:
        # The narrative's headings sit under the section heading, never beside
        # it: a model that opens with "## Policy" would otherwise split the
        # report into sibling sections.
        def demote(match):
            return "#" * min(len(match.group(1)) + 1, 6) + " "

        blocks.append(re.sub(r"^(#{1,5}) ", demote, report, flags=re.M))
    if snapshot.get("readings"):
        blocks.append(snapshot_markdown(snapshot))
    return "\n\n".join(blocks)
