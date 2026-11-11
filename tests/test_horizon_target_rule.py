"""目标价是「走势自然终点」，不是「失效线」。

为什么单独钉：2026-11-11 全量的 21 个结构性目标全部落在中线档位区间
（−8%~−25%）之外，中位 −6.2%、最小 −3.2%。逐条看原文，取的是
AAPL 的 50SMA 防线、GS 的「跌破判多头失效」线——那是止损该待的地方。
一个 1-3 个月的判断把目标定在它自己会认输的价位，读者拿到的不是
目标，是反向的警示。

规则由四档共用一份常量，避免改一档漏三档。
"""

from __future__ import annotations

import pytest

from tradingagents import horizon


@pytest.mark.unit
@pytest.mark.parametrize("code", sorted(horizon.HORIZONS))
def test_every_horizon_separates_target_from_invalidation(code):
    rules = horizon.HORIZONS[code].rules
    assert horizon.TARGET_TERMINUS in rules, code


@pytest.mark.unit
@pytest.mark.parametrize("code", sorted(horizon.HORIZONS))
def test_the_terminus_rule_sits_with_the_target_rule_not_the_stop(code):
    """顺序即语义：它读起来必须是目标价的补充，而不是止损的补充。"""
    rules = list(horizon.HORIZONS[code].rules)
    index = rules.index(horizon.TARGET_TERMINUS)
    target_rule = rules[index - 1].lower()
    assert "target" in target_rule, rules[index - 1]
    assert "stop" in rules[index + 1].lower(), rules[index + 1]


@pytest.mark.unit
def test_the_prompt_actually_carries_the_rule():
    text = horizon.instruction("position")
    assert "where the move ends" in text
    assert "belongs in the stop" in text
    # 图上没有更远的位就照实说空间不够，别为凑带编价位（2026-11-11 B）。
    assert "the room is X%" in text
    assert "fabrication" in text
