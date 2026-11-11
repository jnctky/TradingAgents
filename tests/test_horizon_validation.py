"""周期校验的方向（validate_row）。

为什么单独钉：目标价区间是**幅度**，不是符号。上一批按「+8%~+25%」判所有行，
于是每一条看跌结论（Underweight/Sell，目标价在现价下方）都被标成
「target_horizon_mismatch」——14/22 行误报，红点天天亮，等于没有红点。
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


def _row(rating: str, target: float | None, decision: str = "") -> dict:
    return {
        "ticker": "X", "status": "done", "rating": rating,
        "target_price": target, "confidence": 0.6, "decision": decision,
        "snapshot": {"last": LAST},
    }


@pytest.mark.unit
def test_a_bearish_target_below_spot_sits_inside_the_band():
    verdict = pf.validate_row(_row("Underweight", 80.0), "position")
    assert verdict["passed"] is True, verdict["flags"]


@pytest.mark.unit
def test_a_bearish_target_above_spot_is_the_mismatch():
    verdict = pf.validate_row(_row("Sell", 112.0), "position")
    assert verdict["passed"] is False
    assert any(flag.startswith("target_horizon_mismatch") for flag in verdict["flags"])


@pytest.mark.unit
def test_a_bullish_target_below_spot_is_the_mismatch():
    verdict = pf.validate_row(_row("Buy", 92.0), "position")
    assert verdict["passed"] is False
    assert any(flag.startswith("target_horizon_mismatch") for flag in verdict["flags"])


@pytest.mark.unit
def test_the_flag_states_the_band_in_the_rating_s_direction():
    verdict = pf.validate_row(_row("Underweight", 130.0), "position")
    flag = next(f for f in verdict["flags"] if f.startswith("target_horizon_mismatch"))
    # A reader must be able to tell which band was applied without opening code.
    assert "−25% to −8%" in flag


@pytest.mark.unit
def test_a_hold_carries_no_directional_band():
    # Hold means "no directional target": the band cannot say what is wrong.
    verdict = pf.validate_row(_row("Hold", 118.0), "position")
    assert not any(flag.startswith("target_horizon_mismatch") for flag in verdict["flags"])


@pytest.mark.unit
@pytest.mark.parametrize("code,rating,target", [
    ("ultrashort", "Buy", 104.0),
    ("swing", "Buy", 106.0),
    ("position", "Buy", 115.0),
    ("long", "Buy", 140.0),
])
def test_each_holding_period_keeps_its_own_band(code: str, rating: str, target: float):
    assert pf.validate_row(_row(rating, target), code)["passed"] is True
