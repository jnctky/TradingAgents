"""Technical-indicator evidence panels: RSI, MACD -- the numbers the analyst cited.

One generic renderer draws a single panel: reference guides for RSI, a zero
line plus histogram bars for MACD. Compose panels side by side by wrapping two
renders in the caller (the report stacks them vertically).
"""

from __future__ import annotations

from tradingagents.charts._svg import (
    FS_LABEL,
    FS_TAG,
    axis_text,
    close_svg,
    fmt_num,
    month_ticks,
    nice_ticks,
    open_svg,
    polyline,
    scale_x,
)

W = 860
TOP, BOTTOM = 30, 150
PAD_L, PAD_R = 12, 64


def render_indicator_chart(dates: list[str], series: dict[str, list], title: str,
                           colors, guides: list[float] | None = None,
                           hist: list | None = None, force_range: tuple | None = None) -> str:
    """One indicator panel.

    ``series``: ``{legend_label: [values aligned with dates]}`` drawn as lines;
    ``hist``: histogram values (MACD histogram) drawn as bars around zero;
    ``guides``: horizontal reference lines (RSI 30/70);
    ``force_range``: ``(lo, hi)`` to pin the y-scale (RSI 0..100).
    """
    n = len(dates)
    if n == 0:
        return ""
    all_vals = [v for values in series.values() for v in values if isinstance(v, (int, float))]
    if hist:
        all_vals += [v for v in hist if isinstance(v, (int, float))]
    if not all_vals:
        return ""
    lo, hi = force_range or (min(all_vals), max(all_vals))
    span = (hi - lo) or 1.0
    if not force_range:
        pad = span * 0.12
        lo, hi = lo - pad, hi + pad

    def py(v: float) -> float:
        return BOTTOM - (v - lo) / (hi - lo) * (BOTTOM - TOP)

    def px(i: int) -> float:
        return scale_x(i, n, PAD_L + 8, W - PAD_R)

    parts = [open_svg(W, BOTTOM + 16)]
    parts.append(f'<rect x="0" y="0" width="{W}" height="{BOTTOM + 16}" fill="none"/>')

    for tick in (guides if guides else nice_ticks(lo, hi, 4)):
        y = py(tick)
        if TOP <= y <= BOTTOM:
            parts.append(f'<line x1="{PAD_L + 8}" y1="{y:.1f}" x2="{W - PAD_R}" y2="{y:.1f}" '
                         f'stroke="{colors["rule"]}" stroke-width="1"/>')
            parts.append(axis_text(W - PAD_R + 6, y + 3.5, fmt_num(tick), colors["muted"], "start"))

    zero_y = py(0) if lo < 0 < hi else None
    if zero_y is not None:
        parts.append(f'<line x1="{PAD_L + 8}" y1="{zero_y:.1f}" x2="{W - PAD_R}" y2="{zero_y:.1f}" '
                     f'stroke="{colors["muted"]}" stroke-width="1"/>')

    if hist:
        bar_w = max(1.0, (W - PAD_R - PAD_L - 8) / n * 0.55)
        for i, v in enumerate(hist):
            if not isinstance(v, (int, float)):
                continue
            y0 = zero_y if zero_y is not None else py(0)
            y1 = py(v)
            color = colors["up"] if v >= 0 else colors["down"]
            top, bottom = min(y0, y1), max(y0, y1)
            if bottom - top < 1.0:
                bottom = top + 1.0
            parts.append(f'<rect x="{px(i) - bar_w / 2:.2f}" y="{top:.2f}" width="{bar_w:.2f}" '
                         f'height="{bottom - top:.2f}" fill="{color}" opacity="0.5"/>')

    palette = [colors["accent"], colors["ma50"], colors["ma200"]]
    for k, (label, values) in enumerate(series.items()):
        pts = [(px(i), py(v)) for i, v in enumerate(values) if isinstance(v, (int, float))]
        parts.append(polyline(pts, palette[k % len(palette)], 1.5))

    # last-value tag per series
    lx = PAD_L
    parts.append(axis_text(lx, 18, title, colors["ink"], "start", FS_TAG))
    for k, (label, values) in enumerate(series.items()):
        usable = [v for v in values if isinstance(v, (int, float))]
        if not usable:
            continue
        lx += 130
        parts.append(f'<line x1="{lx - 16}" y1="14.5" x2="{lx - 4}" y2="14.5" '
                     f'stroke="{palette[k % len(palette)]}" stroke-width="2.5"/>')
        parts.append(axis_text(lx, 18, f"{label} {fmt_num(usable[-1])}", colors["muted"], "start"))

    for x, label in month_ticks(dates, PAD_L + 8, W - PAD_R):
        parts.append(axis_text(x, BOTTOM + 12, label, colors["muted"]))

    parts.append(close_svg())
    return "".join(parts)


def render_rsi(dates: list[str], values: list, colors) -> str:
    return render_indicator_chart(
        dates, {"RSI(14)": values}, "RSI (14-day)", colors,
        guides=(30.0, 70.0), force_range=(0.0, 100.0),
    )


def render_macd(dates: list[str], macd: list, signal: list, hist: list, colors) -> str:
    return render_indicator_chart(
        dates, {"MACD": macd, "signal": signal}, "MACD (12, 26, 9)", colors, hist=hist,
    )
