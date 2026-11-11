"""周期校验的三层判定（validate_row）。

为什么单独钉：目标价区间是**幅度**，不是符号。上一批按「+8%~+25%」判所有行，
于是每一条看跌结论（Underweight/Sell，目标价在现价下方）都被标成
「target_horizon_mismatch」——14/22 行误报，红点天天亮，等于没有红点。

第二层（2026-11-11 用户拍板）是依据：结构性目标（前高、阻力、测量移动）测的
是图上已有的位置，不该被估值带判死；但方向永不豁免——Underweight 配一个高于
现价的目标价，无论依据是什么都自相矛盾。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "my_tools" / "portfolio_run.py"
_spec = importlib.util.spec_from_file_location("portfolio_run_horizon", _SRC)
pf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pf)

#: 2026-11-10 batch's actual shape: one row, one position-horizon call.
LAST = 100.0


def _row(rating: str, target: float | None, decision: str = "",
         basis: str | None = None) -> dict:
    return {
        "ticker": "X", "status": "done", "rating": rating,
        "target_price": target, "target_basis": basis, "confidence": 0.6,
        "decision": decision, "snapshot": {"last": LAST},
    }


def _flags(verdict: dict, prefix: str) -> list[str]:
    return [flag for flag in verdict["flags"] if flag.startswith(prefix)]


# --- layer 1: the band is a magnitude, in the rating's direction -------------

@pytest.mark.unit
def test_a_bearish_target_below_spot_sits_inside_the_band():
    verdict = pf.validate_row(_row("Underweight", 80.0), "position")
    assert verdict["passed"] is True, verdict["flags"]


@pytest.mark.unit
def test_a_bearish_target_above_spot_is_the_mismatch():
    verdict = pf.validate_row(_row("Sell", 112.0), "position")
    assert verdict["passed"] is False
    assert _flags(verdict, "target_direction_mismatch")


@pytest.mark.unit
def test_a_bullish_target_below_spot_is_the_mismatch():
    verdict = pf.validate_row(_row("Buy", 92.0), "position")
    assert verdict["passed"] is False
    assert _flags(verdict, "target_direction_mismatch")


@pytest.mark.unit
def test_the_flag_states_the_band_in_the_rating_s_direction():
    verdict = pf.validate_row(_row("Underweight", 130.0, basis="valuation"), "position")
    flag = next(f for f in verdict["flags"] if f.startswith("target_"))
    # A reader must be able to tell which band was applied without opening code.
    assert "−25% to −8%" in flag


@pytest.mark.unit
@pytest.mark.parametrize("code,rating,target", [
    ("ultrashort", "Buy", 104.0),
    ("swing", "Buy", 106.0),
    ("position", "Buy", 115.0),
    ("long", "Buy", 140.0),
])
def test_each_holding_period_keeps_its_own_band(code: str, rating: str, target: float):
    assert pf.validate_row(_row(rating, target), code)["passed"] is True


# --- layer 2: a structural target is exempt from the band, not from direction -

@pytest.mark.unit
def test_a_structural_target_may_sit_outside_the_band():
    # A prior high 40% up is a real level, not a wrong holding period.
    verdict = pf.validate_row(_row("Buy", 140.0, basis="structural"), "position")
    assert verdict["passed"] is True, verdict["flags"]


@pytest.mark.unit
def test_a_structural_label_does_not_excuse_the_wrong_direction():
    # The 11-10 batch's worst row: Underweight with a target 57.5% ABOVE spot.
    verdict = pf.validate_row(_row("Underweight", 157.5, basis="structural"), "position")
    assert verdict["passed"] is False
    assert _flags(verdict, "target_direction_mismatch")


@pytest.mark.unit
def test_a_structural_target_far_beyond_any_chart_level_is_flagged():
    # Otherwise "structural" becomes a blanket excuse for any far-away number.
    verdict = pf.validate_row(_row("Buy", 200.0, basis="structural"), "position")
    assert verdict["passed"] is False
    assert _flags(verdict, "target_structural_outlier")


@pytest.mark.unit
def test_a_valuation_target_outside_the_band_is_still_a_mismatch():
    verdict = pf.validate_row(_row("Buy", 140.0, basis="valuation"), "position")
    assert verdict["passed"] is False
    assert _flags(verdict, "target_horizon_mismatch")


# --- Hold is an answer, not a gap -------------------------------------------

@pytest.mark.unit
def test_a_hold_needs_no_target_at_all():
    # The whole point of restoring Hold: a balanced case must be able to say so
    # without inventing a level.
    verdict = pf.validate_row(_row("Hold", None), "position")
    assert verdict["passed"] is True, verdict["flags"]


@pytest.mark.unit
def test_a_hold_with_a_target_inside_the_band_passes():
    verdict = pf.validate_row(_row("Hold", 118.0), "position")
    assert verdict["passed"] is True, verdict["flags"]


@pytest.mark.unit
def test_a_hold_with_a_target_beyond_its_own_band_is_flagged():
    # Hold has no direction, but a +40% target is still not a three-month Hold.
    verdict = pf.validate_row(_row("Hold", 140.0), "position")
    assert verdict["passed"] is False
    assert _flags(verdict, "target_horizon_mismatch")


# --- layer 3: a number with no basis ---------------------------------------

@pytest.mark.unit
def test_none_basis_with_a_price_is_a_contradiction():
    verdict = pf.validate_row(_row("Buy", 110.0, basis="none"), "position")
    assert verdict["passed"] is False
    assert _flags(verdict, "target_basis_conflict")


@pytest.mark.unit
def test_a_row_from_before_the_field_existed_is_not_punished():
    # Every 11-10 row predates **Target Basis**; re-validating them must not
    # turn the whole batch red over a field their text never carried.
    verdict = pf.validate_row(_row("Buy", 115.0), "position")
    assert verdict["passed"] is True, verdict["flags"]
