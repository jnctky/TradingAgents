"""The market-wide macro backdrop, built once per analysis date and shared by every ticker.

The per-ticker agents each answer "is *this* company worth owning". Nothing in
the pipeline answered "what kind of market is this being asked in" — so every
ticker re-derived the rate/dollar/volatility backdrop from its own tool calls, or
skipped it. This module builds that backdrop once per date.

Two properties matter more than the numbers:

* **Same source as the tools.** The readings come from the same keyless platform
  route the ``macro_data`` tool serves, so a reader who re-checks a number gets
  the same one. Where a series is only published with a lag (Treasury yields
  print a day after the equity close), the observation date travels with the
  value instead of being presented as "today".
* **Labels show their basis.** The regime tags below are fixed rules over the
  readings, not a model's judgement. Each tag carries the numbers that produced
  it, so a reader can disagree with the rule rather than having to trust it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SNAPSHOT_VERSION = 1

# Lookback per request: enough for a 20-session move on daily series, a month on
# weekly claims, and 13 months on the monthly price indexes (a YoY read needs the
# same month a year earlier, which 365 days does not reach).
DAILY_WINDOW_DAYS = 120
MONTHLY_WINDOW_DAYS = 1100

# id -> (label, FRED series, transform, decimals)
# transform: "none" (as published), "yoy" (% vs same month last year), "mom"
# (change vs previous month, levels only).
_READINGS: dict[str, tuple[str, str, str, int]] = {
    "dxy": ("US dollar broad index", "DTWEXBGS", "none", 2),
    "us2y": ("2-year Treasury yield", "DGS2", "none", 2),
    "us10y": ("10-year Treasury yield", "DGS10", "none", 2),
    "us30y": ("30-year Treasury yield", "DGS30", "none", 2),
    "curve_2s10s": ("10y-2y spread", "T10Y2Y", "none", 2),
    "real_10y": ("10-year real yield (TIPS)", "DFII10", "none", 2),
    "breakeven_10y": ("10-year breakeven inflation", "T10YIE", "none", 2),
    "fed_funds": ("Effective fed funds rate", "DFF", "none", 2),
    "vix": ("VIX", "VIXCLS", "none", 2),
    "claims": ("Initial jobless claims", "ICSA", "none", 0),
    "core_pce_yoy": ("Core PCE inflation (YoY)", "PCEPILFE", "yoy", 2),
    "payrolls_chg": ("Nonfarm payrolls (monthly change)", "PAYEMS", "mom", 0),
    "cpi_yoy": ("CPI inflation (YoY)", "CPIAUCSL", "yoy", 2),
    "unemployment": ("Unemployment rate", "UNRATE", "none", 1),
}

# Series published monthly or quarterly: they need the long window, and a
# 20-session move is meaningless for them.
_MONTHLY_IDS = {"core_pce_yoy", "payrolls_chg", "cpi_yoy", "unemployment"}


# --------------------------------------------------------------------------- #
# fetching
# --------------------------------------------------------------------------- #

def _month_index(date: str) -> int:
    return int(date[:4]) * 12 + int(date[5:7])


def _yoy(points: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Percent change against the same month a year earlier (month-keyed)."""
    by_month: dict[str, float] = {}
    for date, value in points:
        by_month.setdefault(date[:7], value)
    out = []
    for date, value in points:
        base = by_month.get(f"{int(date[:4]) - 1}-{date[5:7]}")
        if base:
            out.append((date, (value / base - 1) * 100))
    return out


def _mom(points: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Change vs the previous observation, only for consecutive months."""
    return [
        (date, value - prev_value)
        for (prev_date, prev_value), (date, value) in zip(points, points[1:])
        if _month_index(date) - _month_index(prev_date) == 1
    ]


def _fetch_block(series_ids: list[str], start: str, end: str) -> dict[str, list[tuple[str, float]]]:
    """One platform request for a block of series; {series id: [(date, value)]}."""
    from tradingagents.dataflows.vendors.openbb.client import assert_rows, get_json

    rows = get_json(
        "/api/v1/economy/fred_series",
        "FRED macro block",
        params={"provider": "fred", "symbol": ",".join(series_ids),
                "start_date": start, "end_date": end},
    )
    assert_rows("macro", rows, "macro snapshot")
    out: dict[str, list[tuple[str, float]]] = {sid: [] for sid in series_ids}
    for row in rows:
        date = str(row.get("date"))[:10]
        for sid in series_ids:
            value = row.get(sid)
            if value is not None:
                out[sid].append((date, float(value)))
    return out


def fetch_readings(trade_date: str) -> list[dict[str, Any]]:
    """Every reading with its latest value, date, and window change.

    The two blocks are requested separately so the daily series are not dragged
    back three years by the monthly ones. A series with no observation in its
    window is reported as unavailable rather than dropped: an absent reading is
    information the caller needs, not a formatting detail.
    """
    end = datetime.strptime(trade_date, "%Y-%m-%d")
    daily_start = (end - timedelta(days=DAILY_WINDOW_DAYS)).strftime("%Y-%m-%d")
    monthly_start = (end - timedelta(days=MONTHLY_WINDOW_DAYS)).strftime("%Y-%m-%d")

    daily_ids = [sid for key, (_l, sid, _t, _d) in _READINGS.items() if key not in _MONTHLY_IDS]
    monthly_ids = [sid for key, (_l, sid, _t, _d) in _READINGS.items() if key in _MONTHLY_IDS]

    blocks = _fetch_block(daily_ids, daily_start, trade_date)
    blocks.update(_fetch_block(monthly_ids, monthly_start, trade_date))

    readings = []
    for key, (label, series_id, transform, decimals) in _READINGS.items():
        points = blocks.get(series_id) or []
        points = _yoy(points) if transform == "yoy" else _mom(points) if transform == "mom" else points
        # The platform serves the latest published value; a run dated before a
        # release must not read a later print as if it were known on that date.
        points = [p for p in points if p[0] <= trade_date]
        if not points:
            readings.append({"id": key, "label": label, "series": series_id,
                             "available": False, "reason": "no observation in window"})
            continue
        first_date, first_value = points[0]
        last_date, last_value = points[-1]
        prev = points[-2][1] if len(points) > 1 else None
        # A 120-day window describes the backdrop; it cannot tell you the market
        # just moved. `recent_change` is one month of prints: 20 sessions on daily
        # series, the previous observation on monthly ones (20 monthly points
        # would be almost two years).
        window = 2 if key in _MONTHLY_IDS else 21
        recent = points[-window] if len(points) >= window else (points[-2] if len(points) > 1 else None)
        readings.append({
            "id": key,
            "label": label,
            "series": series_id,
            "available": True,
            "value": round(last_value, decimals),
            "date": last_date,
            "prev": round(prev, decimals) if prev is not None else None,
            "prev_date": points[-2][0] if len(points) > 1 else None,
            "recent_change": round(last_value - recent[1], decimals) if recent else None,
            "recent_start": recent[0] if recent else None,
            "window_start": first_date,
            "window_change": round(last_value - first_value, decimals),
            "window_change_pct": (round((last_value / first_value - 1) * 100, 2)
                                  if first_value and transform == "none" else None),
            "transform": transform,
            "source_url": f"https://fred.stlouisfed.org/series/{series_id}",
            "observations": len(points),
        })
    return readings


# --------------------------------------------------------------------------- #
# regime: fixed rules over the readings, each carrying its own basis
# --------------------------------------------------------------------------- #

def _by_id(readings: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["id"]: r for r in readings if r.get("available")}


def derive_regime(readings: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Rule-based labels for the backdrop; every tag names the numbers behind it."""
    r = _by_id(readings)
    regime: dict[str, dict[str, Any]] = {}

    def put(key: str, tag: str, basis: str) -> None:
        regime[key] = {"tag": tag, "basis": basis}

    vix = r.get("vix")
    if vix:
        level = "risk_off" if vix["value"] >= 22 else "risk_on" if vix["value"] <= 15 else "neutral"
        # A calm level that has just jumped still tells you the market changed.
        recent = vix.get("recent_change")
        if level == "risk_on" and recent is not None and recent >= 3:
            level = "neutral"
        if level == "neutral" and recent is not None and recent >= 5:
            level = "risk_off"
        basis = f"VIX {vix['value']:.2f} on {vix['date']}"
        if recent is not None:
            basis += f", {recent:+.2f} since {vix['recent_start']}"
        put("risk_appetite", level, basis)
    else:
        put("risk_appetite", "unknown", "VIX unavailable")

    for key, label, threshold, unit, reading_id in (
        ("rates_direction", "10Y", 0.10, "pp", "us10y"),
        ("dollar_direction", "DXY", 0.5, "%", "dxy"),
    ):
        item = r.get(reading_id)
        if not item:
            put(key, "unknown", f"{label} unavailable")
            continue
        move = item.get("recent_change")
        if move is None:
            put(key, "unknown", f"{label} has a single observation")
            continue
        if unit == "%":
            move = round((item["value"] / (item["value"] - move) - 1) * 100, 2) if item["value"] - move else None
        if move is None:
            put(key, "unknown", f"{label} move could not be computed")
        elif abs(move) < threshold:
            put(key, "flat", f"{label} {item['value']:.2f} on {item['date']}, {move:+.2f}{unit} since {item['recent_start']}")
        elif move > 0:
            put(key, "rising" if unit == "pp" else "strengthening",
                f"{label} {item['value']:.2f} on {item['date']}, {move:+.2f}{unit} since {item['recent_start']}")
        else:
            put(key, "falling" if unit == "pp" else "weakening",
                f"{label} {item['value']:.2f} on {item['date']}, {move:+.2f}{unit} since {item['recent_start']}")

    curve = r.get("curve_2s10s")
    if curve:
        spread = curve["value"]
        shape = ("inverted" if spread < 0 else "flat" if spread < 0.25
                 else "steep" if spread >= 1.0 else "normal")
        recent = curve.get("recent_change")
        put("curve_shape", shape,
            f"10y-2y {spread:+.2f}pp on {curve['date']}"
            + (f", {recent:+.2f}pp since {curve['recent_start']}" if recent is not None else ""))
    else:
        put("curve_shape", "unknown", "curve unavailable")

    fed = r.get("fed_funds")
    if fed:
        move = fed.get("recent_change") or 0.0
        stance = ("easing" if move <= -0.05 else "tightening" if move >= 0.05 else "on_hold")
        put("fed_stance", stance,
            f"effective rate {fed['value']:.2f}% on {fed['date']}"
            + (f", {move:+.2f}pp since {fed['recent_start']}" if fed.get("recent_change") is not None else ""))
    else:
        put("fed_stance", "unknown", "policy rate unavailable")

    core = r.get("core_pce_yoy")
    if core and core.get("recent_change") is not None:
        move = core["recent_change"]
        trend = ("cooling" if move <= -0.10 else "reaccelerating" if move >= 0.10 else "sticky")
        put("inflation_trend", trend,
            f"core PCE {core['value']:.2f}% YoY ({core['date']}), "
            f"{move:+.2f}pp since {core['recent_start']}")
    else:
        put("inflation_trend", "unknown", "core PCE unavailable")

    return regime


# --------------------------------------------------------------------------- #
# snapshot: cached per analysis date
# --------------------------------------------------------------------------- #

def snapshot_dir() -> Path:
    """Where snapshots live: ``macro_context_dir`` if set, else the data cache."""
    from tradingagents.dataflows.config import get_config

    configured = get_config().get("macro_context_dir")
    if configured:
        return Path(configured)
    cache = get_config().get("data_cache_dir") or os.path.join(
        os.path.expanduser("~"), ".tradingagents", "cache")
    return Path(cache) / "macro_context"


def cache_path(trade_date: str) -> Path:
    return snapshot_dir() / f"{trade_date}.json"


def model_signature() -> str:
    """The model config a cached narrative was written under."""
    from tradingagents.dataflows.config import get_config

    cfg = get_config()
    return f"{cfg.get('llm_provider')}/{cfg.get('deep_think_llm')}/{cfg.get('output_language')}"


def load_snapshot(trade_date: str) -> dict[str, Any] | None:
    """The cached snapshot for a date, or None when absent/unreadable."""
    path = cache_path(trade_date)
    if not path.exists():
        return None
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Ignoring unreadable macro snapshot %s: %s", path, exc)
        return None
    if snapshot.get("version") != SNAPSHOT_VERSION:
        logger.info("Macro snapshot %s is version %s, rebuilding", path, snapshot.get("version"))
        return None
    return snapshot


def save_snapshot(snapshot: dict[str, Any]) -> Path:
    path = cache_path(snapshot["trade_date"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)          # atomic: a half-written snapshot must never be read back
    return path


def _data_signature(readings: list[dict[str, Any]]) -> str:
    """Fingerprint of the readings, so a corrected value invalidates the narrative."""
    payload = json.dumps(
        [[r["id"], r.get("value"), r.get("date")] for r in readings], sort_keys=True
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def build_snapshot(trade_date: str, *, refresh: bool = False) -> dict[str, Any]:
    """Readings + regime for a date, from cache when the data has not moved.

    ``refresh`` (or ``TA_MACRO_REFRESH=1``) re-fetches even when a snapshot
    exists — the escape hatch for a mid-session data correction.
    """
    force = refresh or os.getenv("TA_MACRO_REFRESH") == "1"
    cached = None if force else load_snapshot(trade_date)
    if cached:
        return cached

    readings = fetch_readings(trade_date)
    snapshot = {
        "version": SNAPSHOT_VERSION,
        "trade_date": trade_date,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "model_signature": model_signature(),
        "data_signature": _data_signature(readings),
        "readings": readings,
        "regime": derive_regime(readings),
        "report": None,
        "report_model": None,
    }
    save_snapshot(snapshot)
    logger.info("Built macro snapshot for %s (%d readings)", trade_date,
                sum(1 for r in readings if r.get("available")))
    return snapshot


def remember_report(trade_date: str, report: str) -> dict[str, Any] | None:
    """Store the narrative next to its readings, so the next ticker reuses it."""
    snapshot = load_snapshot(trade_date)
    if not snapshot:
        return None
    snapshot["report"] = report
    snapshot["report_model"] = model_signature()
    save_snapshot(snapshot)
    return snapshot


def narrative_is_reusable(snapshot: dict[str, Any]) -> bool:
    """True when this snapshot already holds a narrative for the current model."""
    report = snapshot.get("report")
    return bool(report and snapshot.get("report_model") == model_signature())


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #

def _fmt(reading: dict[str, Any]) -> str:
    decimals = _READINGS[reading["id"]][3]
    value = reading["value"]
    if decimals == 0:
        return f"{value:,.0f}"
    return f"{value:,.{decimals}f}"


def _fmt_change(reading: dict[str, Any], key: str) -> str:
    change = reading.get(key)
    if change is None:
        return "—"
    decimals = _READINGS[reading["id"]][3]
    return f"{change:+,.0f}" if decimals == 0 else f"{change:+,.{decimals}f}"


def snapshot_markdown(snapshot: dict[str, Any]) -> str:
    """The macro block as a report section body (no heading — the caller adds it)."""
    trade_date = snapshot["trade_date"]
    available = [r for r in snapshot["readings"] if r.get("available")]
    missing = [r for r in snapshot["readings"] if not r.get("available")]

    lines = [
        f"Readings served as of **{trade_date}**. Each row carries its own observation date: "
        "Treasury yields and the dollar index publish a session after the equity close, so a "
        "reading dated the prior day is the latest one that existed, not a stale lookup.",
        "",
        f"### Regime — rules over the readings below, not a model judgement ({len(snapshot['regime'])} tags)",
        "",
        "| Dimension | Label | Basis |",
        "|---|---|---|",
    ]
    for key, tag in snapshot["regime"].items():
        lines.append(f"| {key.replace('_', ' ')} | **{tag['tag']}** | {tag['basis']} |")

    lines += [
        "",
        "### Readings",
        "",
        "| Reading | Latest | As of | 1M change | Since | Re-check |",
        "|---|---:|---|---:|---|---|",
    ]
    for reading in available:
        lines.append(
            f"| {reading['label']} | {_fmt(reading)} | {reading['date']} | "
            f"{_fmt_change(reading, 'recent_change')} | {reading.get('recent_start') or '—'} | "
            f"[{reading['series']}]({reading['source_url']}) |"
        )
    if missing:
        lines += ["", "Unavailable this run: "
                  + ", ".join(f"{r['label']} ({r['reason']})" for r in missing) + "."]

    lines += ["", "_Regime labels are fixed thresholds applied to the readings — each row states "
              "its own basis so the rule, not just the label, can be checked._", ""]
    return "\n".join(lines)


def snapshot_prompt_context(snapshot: dict[str, Any]) -> str:
    """The backdrop as prompt text: same numbers, none of the report formatting."""
    available = {r["id"]: r for r in snapshot["readings"] if r.get("available")}
    regime = snapshot["regime"]

    lines = [f"Market backdrop for {snapshot['trade_date']} (FRED series; every number is re-checkable in the report's macro section):"]
    for key, tag in regime.items():
        lines.append(f"- {key.replace('_', ' ')}: {tag['tag']} — {tag['basis']}")

    # Every available reading, not just the headline four: the analyst is asked
    # what the rate path implies, and that needs 2Y/30Y, not only 10Y.
    market = [f"{r['label']} {_fmt(r)} ({r['date']})"
              for key, r in available.items() if key not in _MONTHLY_IDS]
    macro = [f"{r['label']} {_fmt(r)} ({r['date']})"
             for key, r in available.items() if key in _MONTHLY_IDS]
    if market:
        lines.append("- rates, dollar, volatility: " + " · ".join(market))
    if macro:
        lines.append("- inflation and labour: " + " · ".join(macro))
    lines.append(
        "Treat this as the environment your ticker trades in — rates, the dollar, "
        "volatility and the policy path are already priced into its move. Do not restate "
        "these numbers as ticker-specific findings, and do not claim the macro backdrop "
        "changed the ticker's numbers unless a tool result shows it."
    )
    return "\n".join(lines)
