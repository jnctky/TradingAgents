"""目标价必须有依据，依据必须能被读回来。

为什么单独钉：11-10 那批 8/22 只标的直接拒给目标价，理由是「没有估值依据」；
而 9/22 给出的目标价又和自己的评级打架。两件事是同一个缺口的正反面——系统
从来没有要求模型说明「这个数字量的是什么」，于是既无法核实，也无法在不给
估值的情况下使用图上已经存在的价位。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from tradingagents.agents.schemas import (
    PortfolioDecision,
    PortfolioRating,
    TargetBasis,
    render_pm_decision,
)

_SRC = Path(__file__).resolve().parents[1] / "my_tools" / "portfolio_run.py"
_spec = importlib.util.spec_from_file_location("portfolio_run_basis", _SRC)
pf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pf)


def _decision(**kwargs) -> PortfolioDecision:
    return PortfolioDecision(
        rating=PortfolioRating.BUY,
        executive_summary="Enter on a pullback.",
        investment_thesis="Prior high at 140 was the last supply shelf.",
        **kwargs,
    )


@pytest.mark.unit
def test_the_schema_carries_the_basis_beside_the_price():
    decision = _decision(price_target=140.0, target_basis=TargetBasis.STRUCTURAL)
    assert decision.target_basis is TargetBasis.STRUCTURAL


@pytest.mark.unit
def test_the_rendered_decision_names_the_basis():
    text = render_pm_decision(_decision(price_target=140.0,
                                        target_basis=TargetBasis.STRUCTURAL))
    assert "**Price Target**: 140.0" in text
    assert "**Target Basis**: structural" in text


@pytest.mark.unit
def test_a_decision_without_a_target_still_says_so():
    text = render_pm_decision(_decision())
    assert "**Price Target**: not provided" in text
    assert "**Target Basis**: not provided" in text


@pytest.mark.unit
def test_the_pipeline_reads_the_basis_back():
    text = render_pm_decision(_decision(price_target=140.0,
                                        target_basis=TargetBasis.CONSENSUS))
    assert pf.parse_target_basis(text) == "consensus"


@pytest.mark.unit
def test_an_unknown_basis_reads_as_absent_rather_than_as_a_guess():
    assert pf.parse_target_basis("**Target Basis**: vibes") is None
    assert pf.parse_target_basis("no basis line here") is None


@pytest.mark.unit
def test_the_digest_labels_the_price_with_its_basis():
    # A reader looking at "140（结构）" knows they are reading a chart level,
    # not a valuation someone calculated.
    assert pf._BASIS_ZH["structural"] == "结构"
