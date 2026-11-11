"""portfolio_run 的两个时间预算：单只（各自起跑计时）+ 整批墙钟上限。

背景（11-05 实盘）：20 只串行、单只实测约 4.8 分钟，旧实现把所有标的的
截止时刻在批跑开始就算成同一个 `start + 90min`，于是队尾的 SPCX/TSLA
一个跑到 4 分 50 秒被杀、一个启动 5 秒就被杀，日志还写「超过 90 分钟未完成」。

这里锁三件事：
1. 单只超时从该只自己的启动时刻算（晚起跑的标的不会秒被杀）；
2. 整批到达墙钟上限后，正在跑的被打断、还没开始的标 timeout「未开始执行」；
3. 两条闸的原因文案能区分，不再互相冒充。
"""

from __future__ import annotations

import importlib.util
import json
from types import SimpleNamespace

import pytest

_spec = importlib.util.spec_from_file_location(
    "portfolio_run",
    r"D:\work\trade\TradingAgents\my_tools\portfolio_run.py",
)
pf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pf)


class _StuckProcess:
    """永远活着的子进程替身：只能靠超时闸结束。"""

    def __init__(self, target=None, args=None, daemon=False):
        self.target = target
        self.args = args

    def start(self):
        pass

    def is_alive(self):
        return True

    def join(self, timeout=None):
        pass

    def terminate(self):
        pass

    def kill(self):
        pass


def _pick(ticker: str) -> dict:
    return {"ticker": ticker, "reasons": ["例行巡检"], "score": 0, "snapshot": {}}


def _fake_clock(monkeypatch, start: float = 1000.0, step: float = 60.0):
    """把 time 换成可控时钟：每次轮询 sleep(s) 让时间前进 step 秒。"""
    clock = {"now": start}
    monkeypatch.setattr(pf, "time", SimpleNamespace(
        time=lambda: clock["now"],
        sleep=lambda _s: clock.__setitem__("now", clock["now"] + step),
    ))
    return clock


@pytest.fixture()
def stuck_env(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "STATE", tmp_path)
    monkeypatch.setattr(pf, "mp", SimpleNamespace(
        get_context=lambda name: SimpleNamespace(Process=_StuckProcess)))
    return tmp_path


# ---------------------------------------------------------------- 纯函数口径

def test_kill_reason_measures_from_own_start():
    batch_start = 1000.0
    batch_deadline = batch_start + 200 * 60
    # 排队排了 150 分钟才轮到它，自己才跑了 1 分钟 → 不该杀（旧实现会杀）
    late_start = batch_start + 150 * 60
    assert pf._kill_reason(late_start, late_start + 60, 15, batch_deadline, 200) is None
    # 自己跑满 16 分钟 → 单只闸
    why = pf._kill_reason(late_start, late_start + 16 * 60, 15, batch_deadline, 200)
    assert why is not None and "单只运行超过 15 分钟" in why and "本次已跑 16.0 分钟" in why


def test_kill_reason_batch_cap_is_separate():
    start = 1000.0
    batch_deadline = start + 200 * 60
    # 单只才跑 3 分钟，但整批已越过 200 分钟 → 整批闸的原因
    late_start = batch_deadline - 120
    why = pf._kill_reason(late_start, batch_deadline + 60, 15, batch_deadline, 200)
    assert why is not None and "整批超过 200 分钟总预算" in why
    assert "单只" not in why


# ------------------------------------------------------------ 调度循环行为

def test_tail_ticker_gets_full_window_of_its_own(stuck_env, monkeypatch):
    """两只串行、单只上限 2 分钟：第二只必须也拿到完整的 2 分钟。"""
    _fake_clock(monkeypatch, step=60.0)
    out = pf.run_batch([_pick("NVDA"), _pick("TSLA")], "2026-11-05", ["market"],
                       concurrency=1, timeout_min=2, resume=False,
                       total_timeout_min=100)

    for ticker in ("NVDA", "TSLA"):
        assert out[ticker]["status"] == "timeout"
        assert "单只运行超过 2 分钟" in out[ticker]["error"]
        # 两只的 elapsed 都约 180s；旧实现里第二只会是 0~60s
        assert out[ticker]["elapsed_sec"] >= 120
    saved = json.loads((stuck_env / "decisions-2026-11-05-position.json").read_text(encoding="utf-8"))
    assert saved["results"]["TSLA"]["status"] == "timeout"


def test_batch_cap_stops_launching_and_marks_rest(stuck_env, monkeypatch):
    """整批上限 2 分钟、单只上限 10 分钟：第三只根本不会启动。"""
    _fake_clock(monkeypatch, step=60.0)
    out = pf.run_batch([_pick("NVDA"), _pick("MU"), _pick("GS")], "2026-11-05",
                       ["market"], concurrency=1, timeout_min=10, resume=False,
                       total_timeout_min=2)

    assert out["NVDA"]["status"] == "timeout"
    assert "整批超过 2 分钟总预算" in out["NVDA"]["error"]
    for ticker in ("MU", "GS"):
        assert out[ticker]["status"] == "timeout"
        assert "未开始执行" in out[ticker]["error"]
        assert out[ticker]["elapsed_sec"] == 0
