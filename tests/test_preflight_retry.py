"""数据源预检的重试语义（2026-11-13 用户反馈）。

用户原话：数据源异常几乎每次都会出现，但手动刷新几次基本就能恢复——这说明
以往的一次超时被判成「源挂了」。规则改为：抖动型失败（超时/连接重置/断流）
重试一次；定论型失败（连接被拒绝、空载荷、vendor 哨兵）不重试。

跑：.venv/Scripts/python.exe -m pytest tests/test_preflight_retry.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

MY_TOOLS = Path(__file__).resolve().parents[1] / "my_tools"
if str(MY_TOOLS) not in sys.path:
    sys.path.insert(0, str(MY_TOOLS))

import preflight  # noqa: E402


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """重试间隔不该让测试真的等 1.5 秒。"""
    monkeypatch.setattr(preflight.time, "sleep", lambda _s: None)


def test_transient_timeout_is_retried_and_can_recover():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("read timed out")
        return "close,120.5\n"

    ok, error, attempts = preflight._probe(flaky)
    assert ok is True
    assert error is None
    # 第一次失败 + 一次重试成功：这正是「手动刷新就好」的那种抖动。
    assert attempts == 2
    assert calls["n"] == 2


def test_refused_connection_is_a_verdict_not_a_wobble():
    calls = {"n": 0}

    def refused():
        calls["n"] += 1
        raise ConnectionRefusedError("[WinError 10061] 连接被拒绝")

    ok, error, attempts = preflight._probe(refused)
    assert ok is False
    assert "10061" in (error or "")
    # 代理没开是确定的事实，重试只是把同一句话再等一遍。
    assert attempts == 1
    assert calls["n"] == 1


def test_empty_payload_is_not_retried():
    calls = {"n": 0}

    def empty():
        calls["n"] += 1
        return "   "

    ok, error, attempts = preflight._probe(empty)
    assert ok is False
    assert error == "empty payload"
    assert attempts == 1
    assert calls["n"] == 1


def test_vendor_sentinel_is_not_retried():
    calls = {"n": 0}

    def sentinel():
        calls["n"] += 1
        return "DATA_UNAVAILABLE: vendor down"

    ok, error, attempts = preflight._probe(sentinel)
    assert ok is False
    assert error == "vendor sentinel"
    assert attempts == 1


def test_persistent_timeout_reports_failure_after_both_attempts():
    calls = {"n": 0}

    def always_timeout():
        calls["n"] += 1
        raise TimeoutError("ReadTimeout")

    ok, error, attempts = preflight._probe(always_timeout)
    assert ok is False
    assert "ReadTimeout" in (error or "")
    assert attempts == preflight.PROBE_ATTEMPTS
    assert calls["n"] == preflight.PROBE_ATTEMPTS


def test_first_try_success_costs_one_attempt():
    ok, error, attempts = preflight._probe(lambda: "ok")
    assert (ok, error, attempts) == (True, None, 1)


def test_retryable_pattern_matches_wobbles_only():
    assert preflight._retryable("TimeoutError: read timed out")
    assert preflight._retryable("[WinError 10060] 由于连接方在一段时间后没有正确答复")
    assert preflight._retryable("ConnectionResetError(10054)")
    assert not preflight._retryable("[WinError 10061] 连接被拒绝")
    assert not preflight._retryable("empty payload")
    assert not preflight._retryable(None)


def test_run_preflight_records_attempts_per_source(tmp_path, monkeypatch):
    """落盘的 preflight-latest.json 必须带 attempts，驾驶舱靠它说「复测通过」。"""
    monkeypatch.setattr(preflight, "STATE", tmp_path)
    monkeypatch.setattr(preflight, "LATEST", tmp_path / "preflight-latest.json")

    def fake_units(trade_date):
        return {unit: (True, None, 1) for unit in preflight.UNIT_LABELS}

    monkeypatch.setattr(preflight, "_probe_units", fake_units)
    payload = preflight.run_preflight("2026-11-13")

    assert payload["ok"] is True
    assert all(s["attempts"] == 1 for s in payload["sources"])
    on_disk = (tmp_path / "preflight-latest.json").read_text(encoding="utf-8")
    assert '"attempts"' in on_disk


def test_every_probe_helper_returns_a_three_tuple(monkeypatch):
    """run_preflight 解包 (ok, error, attempts) 三元组——任何一个 _probe_*
    助手还按旧的二元组返回，整批预检就会崩成 exit 3（2026-11-13 真实批次
    踩过：_probe_prediction 改漏了）。这里把每个助手都真实调用一遍。"""
    monkeypatch.setattr(preflight, "_polymarket_call", lambda *_a: "ok")
    monkeypatch.setattr(
        "tradingagents.dataflows.vendors.polymarket.get_prediction_markets",
        lambda *_a, **_k: "ok",
    )
    start5, _ = preflight._window("2026-11-13", 5)
    # 与 _probe_units 相同的实参形状：签名漂移同样会炸掉整批预检。
    calls = [
        (preflight._probe_prediction, ("2026-11-13",)),
        (preflight._probe_macro, ("2026-11-13",)),
        (preflight._probe_reddit, (start5, "2026-11-13")),
        (preflight._probe_stocktwits, (start5, "2026-11-13")),
    ]
    for fn, args in calls:
        result = fn(*args)
        assert isinstance(result, tuple) and len(result) == 3, f"{fn.__name__} returned {result!r}"
