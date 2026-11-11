"""Data integrity for a run, aggregated from the vendor-router trace.

Why this exists (2026-11-11, user-mandated core principle): a technical
fetch failure is a technical problem, not a trading signal and not something
to route around. Investment needs exact data — a day whose price pipeline
returned zero bytes must say "数据不完整" loudly, never present an
alternative method (event-triggered stops, account-level drawdown caps,
substitute indicators) as if it were the missing data.

The router already records every data call as one JSONL line
(``data_sources/<date>/<ticker>.jsonl`` via ``TA_TRACE_FILE``). This module
turns that trace into a per-category verdict any surface can render:

- the portfolio row (``data_integrity`` / ``data_complete`` fields),
- the report's opening "数据完整性" section (``markdown()`` / ``header_line()``),
- the validator (a row whose price data died must not carry priced advice).

Status per category:
  ok         every call succeeded with a non-empty payload
  partial    some calls succeeded, some failed or came back empty
  failed     no call produced usable data
  withheld   the vendor deliberately serves nothing (e.g. Polymarket live-only)
  na         the question does not apply (crypto has no issuer statements)

``complete`` is True only when every *required* category is ``ok``. Prices
(``core_stock_apis`` + ``technical_indicators``) and news are required:
a rating built without them is exactly the substitution this module exists
to prevent. Macro and prediction markets are enrichment (see
``router.OPTIONAL_CATEGORIES``); fundamentals stay required for equities
and are not expected for crypto (the crypto pipeline has no fundamentals
tools, so an all-failed fundamentals block on a coin is not a data gap).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: Router categories a *priced, actionable* conclusion may never lack.
#: Crypto runs legitimately have no fundamentals tools, so fundamentals are
#: only required when the trace shows the pipeline actually attempted them
#: for an equity (see :func:`summarize_trace`).
PRICE_CATEGORIES = ("core_stock_apis", "technical_indicators")
REQUIRED_CATEGORIES = ("core_stock_apis", "technical_indicators", "news_data")

#: Reader-facing labels, keyed for the report and the cockpit row.
CATEGORY_LABELS: dict[str, str] = {
    "core_stock_apis": "行情价格",
    "technical_indicators": "技术指标",
    "news_data": "新闻",
    "fundamental_data": "基本面",
    "macro_data": "宏观",
    "prediction_markets": "预测市场",
    # Read straight by the sentiment analyst (not through the router); without
    # these rows the header could claim "完整" while the body listed both feeds
    # as missing.
    "sentiment_data": "情绪（StockTwits / Reddit）",
}

STATUS_LABELS = {
    "ok": "正常",
    "partial": "部分可用",
    "failed": "失败",
    "withheld": "按设计不提供",
    "na": "不适用",
}

#: Vendor name the router records when it answers a question that cannot exist
#: for the instrument (crypto fundamentals). Distinct from a failure: nothing
#: was unreachable, there is simply no such statement to fetch.
NOT_APPLICABLE_VENDOR = "not_applicable"


def summarize_trace(trace_path: str | Path | None) -> dict[str, Any] | None:
    """Aggregate one run's trace into the integrity verdict. ``None`` without a trace.

    A zero-byte payload counts as a failure, not a success: the BNB day that
    prompted this module returned six 200-responses of 0 bytes, and counting
    those as "ok" would have hidden the blackout the report had to declare.
    """
    if not trace_path:
        return None
    path = Path(trace_path)
    if not path.exists():
        return None

    per: dict[str, dict[str, int]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        category = str(record.get("category") or "")
        if not category:
            continue
        bucket = per.setdefault(category, {"ok": 0, "fail": 0, "zero_bytes": 0, "na": 0, "ms": 0})
        bucket["ms"] += int(record.get("ms") or 0)
        if record.get("vendor") == NOT_APPLICABLE_VENDOR:
            bucket["na"] += 1
        elif record.get("ok"):
            if (record.get("chars") or 0) == 0:
                bucket["zero_bytes"] += 1
            else:
                bucket["ok"] += 1
        else:
            bucket["fail"] += 1

    categories: dict[str, Any] = {}
    for category, bucket in per.items():
        attempted = bucket["ok"] + bucket["fail"] + bucket["zero_bytes"]
        if bucket["ok"] and not bucket["fail"] and not bucket["zero_bytes"]:
            status = "ok"
        elif bucket["ok"]:
            status = "partial"
        elif attempted == 0 and bucket["na"]:
            # Every call was answered with "this does not apply" — a coin has no
            # issuer statements. Reporting that as a fetch failure would put a
            # phantom "数据不完整" on every crypto report.
            status = "na"
        elif category == "prediction_markets" and attempted == bucket["fail"]:
            # The live-odds vendor deliberately withholds historical vintages;
            # its sentinel text is a design answer, not an outage.
            status = "withheld"
        else:
            status = "failed"
        categories[category] = {**bucket, "status": status}

    # A category the run never attempted is a gap too (the analyst never even
    # asked), but only for categories the router knows about. Fundamentals are
    # required only when the run actually attempted them: a crypto pipeline
    # with no fundamentals tools legitimately never calls them, while an
    # equity run whose fundamentals feed died has a real gap.
    attempted = set(categories)
    required = [c for c in REQUIRED_CATEGORIES]
    if categories.get("fundamental_data", {}).get("status") not in (None, "na"):
        required.append("fundamental_data")
    complete = all(categories.get(c, {}).get("status") == "ok" for c in required)

    return {
        "categories": categories,
        "complete": complete,
        "price_ok": all(categories.get(c, {}).get("status") == "ok" for c in PRICE_CATEGORIES),
        "missing": [c for c in required
                    if categories.get(c, {}).get("status") not in ("ok",)],
        "unattempted": [c for c in required if c not in attempted],
    }


def failed_price_categories(integrity: dict[str, Any] | None) -> list[str]:
    """Price categories that did not come back ``ok`` — the ones that bar priced advice."""
    if not integrity:
        return []
    return [c for c in PRICE_CATEGORIES
            if (integrity.get("categories") or {}).get(c, {}).get("status") != "ok"]


def header_line(integrity: dict[str, Any] | None) -> str | None:
    """One header line for ``complete_report.md``; ``None`` when everything read."""
    if not integrity or integrity.get("complete"):
        return None
    parts = []
    for category, bucket in (integrity.get("categories") or {}).items():
        if bucket.get("status") != "ok":
            label = CATEGORY_LABELS.get(category, category)
            parts.append(f"{label} {STATUS_LABELS.get(bucket['status'], bucket['status'])}")
    text = "、".join(parts) if parts else "部分数据源异常"
    return f"- 数据完整性: **不完整**（{text}）——缺失数据不参与结论，替代方式一律不采用"


def markdown(integrity: dict[str, Any] | None) -> str:
    """The report's opening "数据完整性" section (user-mandated, 2026-11-11).

    Lists every dependency the trace recorded with its verdict, then states
    the consequences for the report's conclusions. Written by the pipeline,
    not by a model, so it cannot be argued away or softened downstream.
    """
    lines = ["## 数据完整性", "",
             "_以下逐项列出本次分析依赖的每项数据是否正常，由取数轨迹（vendor trace）"
             "自动汇总，非模型自述。数据不完整时：缺失项不参与结论；禁止以任何替代数据、"
             "替代指标或替代规则（如事件触发止损、账户级回撤线）替代缺失的价格数据给出"
             "入场价、止损价或目标价；无法得出的结论在正文中明确写明「无法给出」。_", ""]

    categories = (integrity or {}).get("categories") or {}
    if not categories:
        return "\n".join(lines + ["- 取数轨迹缺失：无法核实本次分析读取了哪些数据。"
                                  "按数据完整性原则，本报告的全部结论视为未经数据校验。", ""])

    lines += ["| 数据依赖 | 状态 | 成功 | 失败 | 0字节 |", "|---|---|---:|---:|---:|"]
    for category, bucket in sorted(categories.items()):
        label = CATEGORY_LABELS.get(category, category)
        status = STATUS_LABELS.get(bucket.get("status"), bucket.get("status"))
        lines.append(f"| {label} | {status} | {bucket.get('ok', 0)} | "
                     f"{bucket.get('fail', 0)} | {bucket.get('zero_bytes', 0)} |")
    lines.append("")

    price_failed = failed_price_categories(integrity)
    if integrity.get("complete"):
        lines += ["**结论：本次分析依赖的数据完整。**", ""]
    else:
        missing = "、".join(CATEGORY_LABELS.get(c, c) for c in integrity.get("missing") or [])
        lines += [f"**结论：本次分析数据不完整**（缺失：{missing}）。", ""]
        if price_failed:
            names = "、".join(CATEGORY_LABELS.get(c, c) for c in price_failed)
            lines += [
                f"- 价格类数据（{names}）未能取得：本报告**不得**给出入场价、止损价、"
                "目标价或任何基于价格结构的仓位点位；正文中出现的此类数字若非来自"
                "明确标注的其他来源，一律视为无效。",
                "- 止损/入场安排不得以「事件触发」「账户级回撤线」「波动率估计」等替代"
                "方式补齐——替代不是数据。正确的做法是明确写明「数据缺失，无法给出」。",
            ]
    return "\n".join(lines) + "\n"
