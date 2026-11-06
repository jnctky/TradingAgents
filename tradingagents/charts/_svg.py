"""Shared SVG drawing helpers for the evidence charts.

Nothing clever: string building, linear scaling, "nice" axis ticks. Every
renderer here must stay dependency-free -- the evidence charts are produced
wherever the report is written, including headless batch runs on machines
without an image stack.

``INLINE_COLORS`` values are CSS custom properties of the report page, so an
embedded chart follows the report's light/dark theme. ``CHART_COLORS`` are the
fixed hex fallbacks for the standalone ``charts/*.svg`` files (no page CSS
around them there).
"""

from __future__ import annotations

from html import escape

# The report page's palette (see report_html._CSS). Charts embedded inline
# inherit it; `--up`/`--down` also flip in dark mode.
INLINE_COLORS = {
    "ink": "var(--ink)",
    "muted": "var(--muted)",
    "rule": "var(--rule)",
    "wash": "var(--wash)",
    "up": "var(--up)",
    "down": "var(--down)",
    "accent": "var(--accent)",
    "ma50": "#c47f17",
    "ma200": "#7a5bd6",
    "band": "var(--muted)",
    "mark": "var(--accent)",
}

CHART_COLORS = {
    "ink": "#1c2126",
    "muted": "#5d6670",
    "rule": "#e2e5e8",
    "wash": "#f2f4f5",
    "up": "#087a59",
    "down": "#b4372f",
    "accent": "#087a59",
    "ma50": "#c47f17",
    "ma200": "#7a5bd6",
    "band": "#5d6670",
    "mark": "#087a59",
}

# Chart text sizes: slightly smaller than body text, sans-serif to match the
# report's figure/table typography.
FONT = 'font-family="system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif"'
FS_LABEL = 11   # axis ticks, legends
FS_TAG = 12     # headings inside the chart, the closing-price tag


def safe_text(value) -> str:
    """Escape a label for XML text content."""
    return escape(str(value))


def fmt_num(value, digits: int = 2) -> str:
    """A compact number for axis labels and tags: 121.67, 4.90, 1.2M."""
    if value is None:
        return "N/A"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return str(value)
    if digits == 0:
        return f"{value:,.0f}"
    return f"{value:,.{digits}f}"


def fmt_volume(value) -> str:
    """Volume axis shorthand: 42.1M, 3.5B."""
    if value is None:
        return "N/A"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return str(value)
    for factor, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= factor:
            return f"{value / factor:.1f}{unit}"
    return f"{value:,.0f}"


def scale_x(i: int, n: int, left: float, right: float) -> float:
    """Index -> pixel, across the plot area. n<=1 pins the single point."""
    if n <= 1:
        return (left + right) / 2
    return left + i * (right - left) / (n - 1)


def nice_ticks(lo: float, hi: float, count: int = 5) -> list[float]:
    """Rounded tick values covering [lo, hi]; never raises on degenerate input."""
    if hi <= lo:
        hi = lo + 1.0
    span = hi - lo
    step0 = span / max(1, count)
    mag = 10 ** _floor_log10(step0)
    step = mag
    for mult in (1, 2, 2.5, 5, 10):
        if mult * mag >= step0:
            step = mult * mag
            break
    start = -(-lo // step) * step  # ceil
    ticks = []
    value = start
    while value <= hi + step * 1e-9:
        ticks.append(round(value, 10))
        value += step
    return ticks or [lo, hi]


def _floor_log10(value: float) -> int:
    n = 0
    while value < 1 and n > -12:
        value *= 10
        n -= 1
    while value >= 10 and n < 12:
        value /= 10
        n += 1
    return n


def polyline(points: list[tuple[float, float]], color: str, width: float = 1.6,
             dash: str | None = None) -> str:
    """A stroke-only path through the points; empty/short input is tolerated."""
    if len(points) < 2:
        return ""
    coords = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    return (f'<polyline fill="none" stroke="{color}" stroke-width="{width}"'
            f' stroke-linejoin="round" stroke-linecap="round"{dash_attr} points="{coords}"/>')


def month_ticks(dates: list[str], left: float, right: float, max_ticks: int = 7) -> list[tuple[float, str]]:
    """(x, label) for the first trading row of each month, thinned to max_ticks."""
    if not dates:
        return []
    marks: list[tuple[float, str]] = []
    seen = None
    for i, raw in enumerate(dates):
        month = (raw or "")[:7]
        if month and month != seen:
            seen = month
            marks.append((scale_x(i, len(dates), left, right), month))
    if len(marks) > max_ticks:
        stride = -(-len(marks) // max_ticks)
        marks = marks[::stride]
    # "2026-06" -> "Jun"
    names = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
             "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    try:
        out = [(x, names[int(label[5:7]) - 1]) for x, label in marks]
    except (ValueError, IndexError):
        out = [(x, label) for x, label in marks]
    return out


def axis_text(x: float, y: float, label: str, color: str, anchor: str = "middle",
              size: int = FS_LABEL) -> str:
    return (f'<text x="{x:.1f}" y="{y:.1f}" fill="{color}" font-size="{size}"'
            f' text-anchor="{anchor}" {FONT}>{safe_text(label)}</text>')


def open_svg(width: int, height: int) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
            f'width="100%" style="display:block" role="img">')


def close_svg() -> str:
    return "</svg>"
