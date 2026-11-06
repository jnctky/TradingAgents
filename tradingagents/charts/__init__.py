"""Report evidence charts, drawn as SVG.

Pure Python: no matplotlib, no plotly, no image files. Each renderer returns an
SVG string sized for the report page (the page is 46rem wide, so the charts use
a 860-unit viewBox and scale down cleanly). Colors follow the report palette --
when the chart is embedded in the HTML report, the strokes read the page's CSS
custom properties so dark mode works; the standalone ``charts/*.svg`` files
written next to the markdown report use fixed hex colors instead.

Labels inside the charts are English (dates, series names, values) to avoid a
font dependency; Chinese lives in the surrounding prose.
"""

from tradingagents.charts._svg import (
    CHART_COLORS,
    INLINE_COLORS,
    fmt_num,
    fmt_volume,
    safe_text,
)
from tradingagents.charts.indicator_svg import (
    render_indicator_chart,
    render_macd,
    render_rsi,
)
from tradingagents.charts.macro_svg import render_macro_grid
from tradingagents.charts.price_svg import render_price_chart

__all__ = [
    "INLINE_COLORS",
    "CHART_COLORS",
    "fmt_num",
    "fmt_volume",
    "safe_text",
    "render_price_chart",
    "render_indicator_chart",
    "render_rsi",
    "render_macd",
    "render_macro_grid",
]
