"""The price evidence chart: 6M daily candles, MA50/MA200, volume, analysis-date mark.

The chart is drawn from the exact OHLCV rows the analysts' verification
snapshot used (same vendor chain, same as-of cutoff), so "what the model saw"
and "what the reader sees" are the same numbers.
"""

from __future__ import annotations

from tradingagents.charts._svg import (
    FS_LABEL,
    FS_TAG,
    FONT,
    axis_text,
    close_svg,
    fmt_num,
    fmt_volume,
    month_ticks,
    nice_ticks,
    open_svg,
    polyline,
    safe_text,
    scale_x,
)

W = 860
TOP = 34            # legend row height above the plot
PRICE_TOP, PRICE_BOTTOM = 40, 216
VOL_TOP, VOL_BOTTOM = 248, 326
BOTTOM = 356        # room for month labels under the volume panel
PAD_L, PAD_R = 12, 64

_UP_THRESHOLD_BARS = 40


def render_price_chart(rows, symbol: str, analysis_date: str, colors,
                       bands: dict | None = None) -> str:
    """One daily OHLCV panel + volume strip.

    ``rows``: ascending ``{'date','open','high','low','close','volume'}`` dicts,
    already cut to the analysis date. ``bands``: optional
    ``{'upper': [...], 'lower': [...]}`` (Bollinger) drawn as dashed guides.
    Rows with an unusable close are skipped but keep their date position, so a
    data gap reads as a gap, not a shortened timeline.
    """
    n = len(rows)
    if n == 0:
        return ""

    closes = [r.get("close") for r in rows]
    usable = [c for c in closes if isinstance(c, (int, float))]
    if not usable:
        return ""

    inner_left, inner_right = PAD_L + 8, W - PAD_R
    lo = min(usable)
    hi = max(usable)
    for series_name in ("upper", "lower"):
        vals = [v for v in (bands or {}).get(series_name) or [] if isinstance(v, (int, float))]
        if vals:
            lo = min(lo, min(vals))
            hi = max(hi, max(vals))
    span = (hi - lo) or 1.0
    pad = span * 0.06
    lo, hi = lo - pad, hi + pad

    def py(value: float) -> float:
        return PRICE_BOTTOM - (value - lo) / (hi - lo) * (PRICE_BOTTOM - PRICE_TOP)

    def px(i: int) -> float:
        return scale_x(i, n, inner_left, inner_right)

    parts = [open_svg(W, BOTTOM)]
    parts.append(f'<rect x="0" y="0" width="{W}" height="{BOTTOM}" fill="none"/>')

    # --- price grid + right axis ---
    for tick in nice_ticks(lo, hi, 5):
        y = py(tick)
        parts.append(f'<line x1="{inner_left}" y1="{y:.1f}" x2="{inner_right}" y2="{y:.1f}" '
                     f'stroke="{colors["rule"]}" stroke-width="1"/>')
        parts.append(axis_text(W - PAD_R + 6, y + 3.5, fmt_num(tick), colors["muted"], "start"))

    # --- analysis-date marker (the cutoff the run was pinned to) ---
    last_x = px(n - 1)
    parts.append(f'<line x1="{last_x:.1f}" y1="{PRICE_TOP - 8}" x2="{last_x:.1f}" y2="{VOL_BOTTOM}" '
                 f'stroke="{colors["mark"]}" stroke-width="1" stroke-dasharray="3,3" opacity="0.75"/>')
    parts.append(axis_text(last_x - 4, PRICE_TOP - 14, f"analysis date {analysis_date}",
                           colors["mark"], "end", FS_LABEL))

    # --- Bollinger band guides ---
    if bands:
        for name, dash in (("upper", "4,3"), ("lower", "4,3")):
            pts = [(px(i), py(v)) for i, v in enumerate(bands.get(name) or [])
                   if isinstance(v, (int, float)) and 0 <= i < n]
            parts.append(polyline(pts, colors["band"], 1.0, dash))

    # --- candles ---
    cw = max(1.0, min(6.0, (inner_right - inner_left) / n * 0.55))
    thin = n > _UP_THRESHOLD_BARS
    for i, row in enumerate(rows):
        close = row.get("close")
        if not isinstance(close, (int, float)):
            continue
        o = row.get("open")
        h = row.get("high") if isinstance(row.get("high"), (int, float)) else close
        l = row.get("low") if isinstance(row.get("low"), (int, float)) else close
        o = close if not isinstance(o, (int, float)) else o
        up = close >= o
        color = colors["up"] if up else colors["down"]
        x = px(i)
        if thin:
            # At 6M density a thin open-close tick reads better than a candle.
            parts.append(f'<line x1="{x:.2f}" y1="{py(h):.2f}" x2="{x:.2f}" y2="{py(l):.2f}" '
                         f'stroke="{color}" stroke-width="1"/>')
        else:
            body_top, body_bottom = py(max(o, close)), py(min(o, close))
            if body_bottom - body_top < 1.0:
                body_bottom = body_top + 1.0
            parts.append(f'<line x1="{x:.2f}" y1="{py(h):.2f}" x2="{x:.2f}" y2="{py(l):.2f}" '
                         f'stroke="{color}" stroke-width="1"/>')
            parts.append(f'<rect x="{x - cw / 2:.2f}" y="{body_top:.2f}" width="{cw:.2f}" '
                         f'height="{body_bottom - body_top:.2f}" fill="{color}"/>')

    # --- moving averages over the same closes ---
    parts.append(polyline([(px(i), py(v)) for i, v in enumerate(_rolling_mean(closes, 50))
                           if v is not None], colors["ma50"], 1.5))
    parts.append(polyline([(px(i), py(v)) for i, v in enumerate(_rolling_mean(closes, 200))
                           if v is not None], colors["ma200"], 1.5))

    # --- last close tag on the axis ---
    last_close = usable[-1]
    last_y = py(last_close)
    tag = fmt_num(last_close)
    tag_w = 7.0 * (len(tag) + 1)
    parts.append(f'<rect x="{W - PAD_R + 2:.1f}" y="{last_y - 8:.1f}" width="{tag_w:.0f}" height="16" '
                 f'rx="2" fill="{colors["accent"]}"/>')
    parts.append(f'<text x="{W - PAD_R + 6:.1f}" y="{last_y + 3.5:.1f}" fill="#ffffff" '
                 f'font-size="{FS_TAG}" font-weight="600" {FONT}>{safe_text(tag)}</text>')

    # --- legend ---
    last_date = rows[-1].get("date", "")
    parts.append(f'<text x="{PAD_L}" y="22" fill="{colors["ink"]}" font-size="{FS_TAG}" '
                 f'font-weight="600" {FONT}>{safe_text(f"{symbol.upper()} daily · close {fmt_num(last_close)} ({last_date})")}</text>')
    legend = [
        (colors["ma50"], "MA50"),
        (colors["ma200"], "MA200"),
    ]
    lx = PAD_L + 250
    for color, label in legend:
        parts.append(f'<line x1="{lx}" y1="18" x2="{lx + 16}" y2="18" stroke="{color}" stroke-width="2.5"/>')
        parts.append(axis_text(lx + 20, 21.5, label, colors["muted"], "start"))
        lx += 68
    if bands:
        parts.append(f'<line x1="{lx}" y1="18" x2="{lx + 16}" y2="18" stroke="{colors["band"]}" '
                     f'stroke-width="1.2" stroke-dasharray="4,3"/>')
        parts.append(axis_text(lx + 20, 21.5, "Bollinger (20, 2)", colors["muted"], "start"))
        lx += 110

    # --- volume strip ---
    volumes = [r.get("volume") if isinstance(r.get("volume"), (int, float)) else 0 for r in rows]
    vmax = max(volumes) or 1.0

    def vy(v: float) -> float:
        return VOL_BOTTOM - (v / vmax) * (VOL_BOTTOM - VOL_TOP)

    for i, (row, v) in enumerate(zip(rows, volumes)):
        close = row.get("close")
        o = row.get("open")
        if not isinstance(close, (int, float)) or not isinstance(o, (int, float)):
            continue
        color = colors["up"] if close >= o else colors["down"]
        parts.append(f'<rect x="{px(i) - cw / 2:.2f}" y="{vy(v):.2f}" width="{cw:.2f}" '
                     f'height="{VOL_BOTTOM - vy(v):.2f}" fill="{color}" opacity="0.55"/>')
    parts.append(axis_text(W - PAD_R + 6, VOL_TOP + 8, fmt_volume(vmax), colors["muted"], "start"))
    parts.append(axis_text(PAD_L, VOL_TOP - 6, "Volume", colors["muted"], "start"))

    # --- month ticks ---
    for x, label in month_ticks([r.get("date", "") for r in rows], inner_left, inner_right):
        parts.append(axis_text(x, BOTTOM - 4, label, colors["muted"]))

    parts.append(close_svg())
    return "".join(parts)


def _rolling_mean(values: list, window: int) -> list:
    """Simple moving average aligned with the input; None until the window fills."""
    out: list = []
    total = 0.0
    count = 0
    for i, v in enumerate(values):
        if isinstance(v, (int, float)):
            total += v
            count += 1
        if i >= window:
            old = values[i - window]
            if isinstance(old, (int, float)):
                total -= old
                count -= 1
        out.append(total / count if count >= window else None)
    return out
