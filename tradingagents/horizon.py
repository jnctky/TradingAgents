"""Trading horizon: what period a run is answering for.

A rating means nothing until the period it applies to is fixed. The same tape
reads bullish for six months and bearish for six weeks, and the report used to
let the model choose per ticker — the 2026-11-09 batch came back with horizons
from "2-6 weeks" to "6-12 months" on one page, and target prices four percent
away under a six-month label. Nothing was wrong with any single sentence; what
was wrong is that they could not be compared, and no reader could tell which
question had been answered.

So the horizon is an input, not an output. It is chosen once per batch, carried
on the run state, and it fixes four things that have to move together:

* the price window the analysts read (60 to 500 sessions),
* which indicators carry weight (a 10 EMA has no business deciding a 12-month
  call, and a 200 SMA is noise for a three-day trade),
* the band a target price must fall in, and how a stop is placed,
* how far ahead an event may be cited as a reason.

The bands are deliberately wide. They are a guard against a call that contradicts
its own label, not a claim about what any instrument will do.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class Horizon:
    """One trading period, with everything a run needs to stay inside it."""

    code: str
    label_zh: str
    label_en: str
    #: How long a position from this run is meant to be held, as shown to a reader.
    hold_zh: str
    #: The same period in English, for the prompt block (prompts stay English
    #: even when the report is rendered in another language).
    hold_en: str
    #: The same period in calendar days, for checking what a decision claims.
    hold_lo_days: int
    hold_hi_days: int
    #: Sessions of daily bars the analysts are told to read.
    window_days: int
    #: Allowed target-price move from the reference price, as fractions (0.08 = +8%).
    target_lo: float
    target_hi: float
    #: Human phrasing of the two rules above, for prompts and validation messages.
    target_text: str
    stop_text: str
    #: How far ahead a cited catalyst may sit, in days.
    event_window_days: int
    #: Percent of the book a single position may risk at its stop.
    risk_budget_pct: float
    #: Analyst set that can carry this horizon honestly (quick omits fundamentals).
    min_profile: str
    #: Prompt rules. Concrete and checkable, not adjectives.
    rules: tuple[str, ...]
    #: Phrases that contradict the horizon and must not be used as the case.
    forbid: tuple[str, ...]


HORIZONS: dict[str, Horizon] = {
    "ultrashort": Horizon(
        code="ultrashort",
        label_zh="超短",
        label_en="Ultra-short",
        hold_zh="隔夜至 5 个交易日",
        hold_en="one night to five trading sessions",
        hold_lo_days=1,
        hold_hi_days=7,
        window_days=60,
        target_lo=0.02,
        target_hi=0.06,
        target_text="+2% to +6%",
        stop_text="1x ATR from entry, on the losing side",
        event_window_days=5,
        risk_budget_pct=0.5,
        min_profile="quick",
        rules=(
            "Answer for a hold of one night to five sessions. A thesis that needs a quarter to play out is not this trade.",
            "Read a 60-session window. Weight the 10 EMA, ATR, volume expansion and gap behaviour; do not let the 200 SMA decide direction.",
            "A target price must sit 2%-6% from the reference price. A target further away than that belongs to a longer horizon and must not be given here.",
            "Place the stop 1x ATR from entry on the losing side, as an absolute price. Size the position so a stop-out costs at most 0.5% of the book.",
            "Fundamentals may only disqualify a trade (fraud, going concern, imminent dilution). They are not a reason to enter one this week.",
            "Cite only events inside the next 5 trading days. Earnings three weeks out cannot be the case for a trade closed this week.",
            "Note explicitly that prices come from daily bars, so no intraday level is claimed.",
        ),
        forbid=("1-3 months", "6-12 months", "next quarter", "long-term"),
    ),
    "swing": Horizon(
        code="swing",
        label_zh="波段",
        label_en="Swing",
        hold_zh="2 周至 2 个月",
        hold_en="two weeks to two months",
        hold_lo_days=14,
        hold_hi_days=60,
        window_days=120,
        target_lo=0.03,
        target_hi=0.10,
        target_text="+3% to +10%",
        stop_text="1.5x ATR or below the 20 SMA, whichever is closer",
        event_window_days=28,
        risk_budget_pct=0.5,
        min_profile="quick",
        rules=(
            "Answer for a hold of two weeks to two months. Frame the call as a swing position, not an investment and not a day trade.",
            "Read a 120-session window. Weight the 20 SMA, 10 EMA, MACD, RSI, ATR and Bollinger position; use the 200 SMA only as a regime filter, never as the reason to enter.",
            "A target price must sit 3%-10% from the reference price, and must be stated with its basis (a measured move, a resistance level, or a multiple).",
            "Place the stop at 1.5x ATR or below the 20 SMA, whichever is closer, as an absolute price. Size so a stop-out costs at most 0.5% of the book.",
            "Fundamentals are a risk filter here: they can veto a trade, not carry one. Do not argue a two-month call from a five-year cash-flow story.",
            "Cite only events inside the next four weeks; anything later is context, not a catalyst.",
        ),
        forbid=("6-12 months", "next year", "multi-year", "1-3 months as the holding period"),
    ),
    "position": Horizon(
        code="position",
        label_zh="中线",
        label_en="Position",
        hold_zh="1 至 3 个月",
        hold_en="one to three months",
        hold_lo_days=30,
        hold_hi_days=90,
        window_days=250,
        target_lo=0.08,
        target_hi=0.25,
        target_text="+8% to +25%",
        stop_text="2x ATR or below the 50 SMA, whichever is closer (or a structural low)",
        event_window_days=90,
        risk_budget_pct=1.0,
        min_profile="deep",
        rules=(
            "Answer for a hold of one to three months (one earnings cycle). This is the default horizon: act like a position trader, not a scalper.",
            "Read a 250-session window. Weight the 50 and 200 SMA, the weekly trend, relative strength against the index, and one valuation anchor; a 10 EMA crossover alone is not a thesis.",
            "A target price must sit 8%-25% from the reference price, and must be stated with its basis (multiple, DCF, measured move, or a documented consensus).",
            "Place the stop at 2x ATR or below the 50 SMA, whichever is closer, as an absolute price. Size so a stop-out costs at most 1% of the book.",
            "Fundamentals and the next earnings report carry real weight: say what the quarter must show for the call to hold.",
            "Cite only events inside the current or next earnings window. A catalyst beyond that is a reason to revisit, not a reason to act.",
        ),
        forbid=("overnight", "next week", "intraday", "within days"),
    ),
    "long": Horizon(
        code="long",
        label_zh="长线",
        label_en="Long-term",
        hold_zh="6 至 18 个月",
        hold_en="six to eighteen months",
        hold_lo_days=180,
        hold_hi_days=548,
        window_days=500,
        target_lo=0.20,
        target_hi=0.45,
        target_text="+20% to +45%",
        stop_text="a structural low, or a -20% drawdown from entry, whichever is tighter in risk terms",
        event_window_days=180,
        risk_budget_pct=2.0,
        min_profile="deep",
        rules=(
            "Answer for a hold of six to eighteen months. Short-term price noise is not evidence for or against this call.",
            "Read a 500-session window plus the weekly trend. Weight the 200 SMA as a regime line, valuation, cash flow and returns on capital; short-window indicators may time an entry but cannot set the direction.",
            "A target price must sit 20%-45% from the reference price, and must be stated with its basis (DCF, a normalised multiple, or a documented long-run growth case).",
            "Place the stop at a structural low or -20% from entry, whichever is tighter in risk terms, as an absolute price. Size so a stop-out costs at most 2% of the book.",
            "State what must be true in the business over two quarters, and what would falsify it. A single week's move changes nothing here.",
            "Cite only events inside the next two quarters. A CPI print next week may be worth a sentence, never the thesis.",
        ),
        forbid=("overnight", "next week", "2-6 weeks", "day trade"),
    ),
}

#: What an unlabelled run (every file written before horizons existed) is
#: closest to. Used for comparison and for reading old reports, never written
#: onto an old file: guessing a period would invent a constraint that run
#: never had.
DEFAULT_HORIZON = "position"

LABELS = {code: h.label_zh for code, h in HORIZONS.items()}


def resolve(value: str | None, default: str = DEFAULT_HORIZON) -> str:
    """Return a known horizon code, falling back to ``default``.

    Unknown input raises rather than silently landing on the default: a typo in
    a scheduling config must not produce a batch that looks like it ran the
    period somebody asked for.
    """
    if value is None or str(value).strip() == "":
        return default
    code = str(value).strip().lower()
    if code not in HORIZONS:
        raise ValueError(
            f"unknown horizon {value!r}; expected one of {', '.join(HORIZONS)}")
    return code


def get(code: str) -> Horizon:
    return HORIZONS[resolve(code)]


def get_from_state(state: Mapping[str, Any]) -> Horizon:
    """The horizon this run is answering for; the default when unmarked."""
    return get(str(state.get("horizon") or DEFAULT_HORIZON))


def target_band(code: str) -> tuple[float, float]:
    h = get(code)
    return h.target_lo, h.target_hi


def render_hold(code: str) -> str:
    """The holding period as it should appear in a decision's Time Horizon line."""
    return get(code).hold_zh


def instruction(code: str, reference_price: float | None = None) -> str:
    """The prompt block that pins a run to its horizon.

    Injected into every agent that can move the conclusion, because a horizon
    enforced only at the Portfolio Manager would leave four analysts arguing a
    different question and the manager guessing which answer was meant.

    Analysts run before any price is known to the prompt, so without a
    ``reference_price`` the band is anchored to the analysis-time price — the
    same price the validator later measures against.
    """
    h = get(code)
    anchor = (f"the reference price ({reference_price:g})" if reference_price
              else "the price at the time of analysis")
    rules = "\n".join(f"- {rule}" for rule in h.rules)
    return (
        f"\n\n## Trading horizon: {h.label_en} ({h.hold_en} / {h.hold_zh})\n\n"
        f"Every conclusion in this run answers for a holding period of {h.hold_en}. "
        f"Target-price band: {h.target_text} from {anchor}. "
        f"Stop rule: {h.stop_text}. Risk budget: {h.risk_budget_pct}% of the book per position."
        f"\n\n{rules}\n"
        "- If the evidence only supports a call on a different period, say so explicitly"
        " and do not relabel it into this one.\n"
    )
