"""Macro evidence grid: four small panels -- dollar, yields, curve, policy rate.

Each panel is the same FRED series (same vintage pinning) the macro tool served
the analyst, as a sparkline with the latest reading and the change over the
window. Panel titles carry the FRED series ID so a reader can open
fred.stlouisfed.org and re-check the number.
"""

from __future__ import annotations

from tradingagents.charts._svg import (
    FS_LABEL,
    FS_TAG,
    axis_text,
    close_svg,
    fmt_num,
    open_svg,
    polyline,
)

W, H = 860, 300
COL_W = W // 2
PANEL_H = H // 2
PAD = 16


def render_macro_grid(series: list[dict], colors) -> str:
    """2x2 grid of sparklines.

    ``series``: ``[{'id','title','points': [(date, value), ...]}, ...]`` -- up to
    four are drawn; fewer render as fewer panels (never placeholders).
    """
    parts = [open_svg(W, H)]
    parts.append(f'<rect x="0" y="0" width="{W}" height="{H}" fill="none"/>')
    for idx, item in enumerate(series[:4]):
        col, row = idx % 2, idx // 2
        x0 = col * COL_W
        y0 = row * PANEL_H
        parts.append(_panel(x0, y0, item, colors, last=idx == len(series[:4]) - 1))
    parts.append(close_svg())
    return "".join(parts)


def _panel(x0: int, y0: int, item: dict, colors, last: bool) -> str:
    points = [(d, v) for d, v in item.get("points") or [] if isinstance(v, (int, float))]
    x0 += PAD
    y0 += 8
    plot_left, plot_right = x0, x0 + COL_W - PAD * 2
    title_y = y0 + 12
    plot_top, plot_bottom = title_y + 12, y0 + PANEL_H - 40

    parts = [f'<g>']
    if not last:
        parts.append(f'<line x1="{x0 - PAD}" y1="{y0}" x2="{x0 - PAD}" y2="{y0 + PANEL_H - 8}" '
                     f'stroke="{colors["rule"]}" stroke-width="1"/>')

    title = f'{item.get("title", "")} ({item.get("id", "")})'
    parts.append(f'<text x="{x0}" y="{title_y}" fill="{colors["ink"]}" font-size="{FS_TAG}" '
                 f'font-weight="600">{_esc(title)}</text>')

    if len(points) < 2:
        parts.append(f'<text x="{x0}" y="{plot_top + 20}" fill="{colors["muted"]}" '
                     f'font-size="{FS_LABEL}">no observations</text>')
        parts.append('</g>')
        return "".join(parts)

    values = [v for _d, v in points]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    pad = span * 0.18
    lo, hi = lo - pad, hi + pad

    def px(i: int) -> float:
        return plot_left + i * (plot_right - plot_left) / (len(points) - 1)

    def py(v: float) -> float:
        return plot_bottom - (v - lo) / (hi - lo) * (plot_bottom - plot_top)

    pts = [(px(i), py(v)) for i, (_d, v) in enumerate(points)]
    parts.append(polyline(pts, colors["accent"], 1.6))
    # dot on the latest reading
    parts.append(f'<circle cx="{pts[-1][0]:.1f}" cy="{pts[-1][1]:.1f}" r="2.6" '
                 f'fill="{colors["accent"]}"/>')

    first, lastv = values[0], values[-1]
    delta = lastv - first
    arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
    sign = "+" if delta > 0 else ""
    digits = 2 if abs(lastv) >= 5 else 2
    last_label = f"{fmt_num(lastv, digits)} {arrow} {sign}{fmt_num(delta, digits)} over {len(points)} obs"
    parts.append(f'<text x="{plot_right}" y="{title_y}" fill="{colors["muted"]}" font-size="{FS_LABEL}" '
                 f'text-anchor="end">{_esc(last_label)}</text>')

    # min/max guides as quiet text instead of gridlines
    parts.append(f'<text x="{plot_left}" y="{plot_bottom + 13}" fill="{colors["muted"]}" '
                 f'font-size="{FS_LABEL}" text-anchor="start">{_esc(points[0][0])}</text>')
    parts.append(f'<text x="{plot_right}" y="{plot_bottom + 13}" fill="{colors["muted"]}" '
                 f'font-size="{FS_LABEL}" text-anchor="end">{_esc(points[-1][0])}</text>')
    parts.append('</g>')
    return "".join(parts)


def _esc(value: str) -> str:
    from html import escape
    return escape(str(value))
