"""portfolio_run 的 --force 重新生成路径（驾驶舱行内 Regenerate 依赖）。

run_batch 的真实子进程用假 Process 替身（不跑模型）；断言三件事：
1. resume 且不带 force：done 标的被跳过，不会重新尝试；
2. resume + force：done 标的被强制重跑，且 decisions 文件里**其它标的
   的记录原样保留**（这是「不能抹掉同日其余 22 只」的回归护栏）；
3. append_history(replace_tickers)：被重跑标的当日旧记录被替换，
   其它日期/其它标的的记录不动。
"""

from __future__ import annotations

import importlib.util
import json
import time as _time
from types import SimpleNamespace

import pytest

_spec = importlib.util.spec_from_file_location(
    "portfolio_run",
    r"D:\work\trade\TradingAgents\my_tools\portfolio_run.py",
)
pf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pf)


class _FakeProcess:
    """秒死的子进程替身：start 后立即结束，永远产不出结果文件。"""

    def __init__(self, target=None, args=None, daemon=False):
        self.target = target
        self.args = args

    def start(self):
        pass

    def is_alive(self):
        return False

    def join(self, timeout=None):
        pass

    def terminate(self):
        pass

    def kill(self):
        pass


@pytest.fixture()
def pipeline_env(tmp_path, monkeypatch):
    """STATE 指向 tmp；spawn 换成假进程；sleep 摘掉（省 5s 轮询等待）。"""
    monkeypatch.setattr(pf, "STATE", tmp_path)
    monkeypatch.setattr(pf, "mp", SimpleNamespace(
        get_context=lambda name: SimpleNamespace(Process=_FakeProcess)))
    monkeypatch.setattr(pf, "time", SimpleNamespace(time=_time.time, sleep=lambda s: None))
    return tmp_path


def _pick(ticker: str) -> dict:
    return {"ticker": ticker, "reasons": ["手动指定"], "score": 0, "snapshot": {"last": 1.0}}


def test_resume_without_force_skips_done(pipeline_env):
    state = pipeline_env
    (state / "decisions-2026-11-05-position-post.json").write_text(json.dumps({
        "date": "2026-11-05",
        "results": {"NVDA": {"ticker": "NVDA", "status": "done", "rating": "buy"}},
    }, ensure_ascii=False), encoding="utf-8")

    results = pf.run_batch([_pick("NVDA")], "2026-11-05", ["market"],
                           1, 90, resume=True, force=False)
    assert results["NVDA"]["status"] == "done"


def test_force_reruns_done_and_preserves_others(pipeline_env):
    state = pipeline_env
    decisions = state / "decisions-2026-11-05-position-post.json"
    decisions.write_text(json.dumps({
        "date": "2026-11-05",
        "results": {
            "NVDA": {"ticker": "NVDA", "status": "done", "rating": "buy"},
            "MU": {"ticker": "MU", "status": "done", "rating": "hold",
                   "target_price": 100, "report_dir": "X"},
        },
    }, ensure_ascii=False), encoding="utf-8")

    out = pf.run_batch([_pick("NVDA")], "2026-11-05", ["market"],
                       1, 90, resume=True, force=True)

    # NVDA 被强制重跑：假子进程无产出 → failed（证明确实尝试了）
    assert out["NVDA"]["status"] == "failed"
    # 关键护栏：MU 的记录原样保留，没有被 --only 抹掉
    saved = json.loads(decisions.read_text(encoding="utf-8"))
    assert saved["results"]["MU"]["status"] == "done"
    assert saved["results"]["MU"]["report_dir"] == "X"


def test_force_refreshes_snapshot(pipeline_env):
    state = pipeline_env
    decisions = state / "decisions-2026-11-05-position-post.json"
    decisions.write_text(json.dumps({
        "date": "2026-11-05",
        "results": {
            "NVDA": {"ticker": "NVDA", "status": "done",
                     "snapshot": {"last": 100.0}},
        },
    }, ensure_ascii=False), encoding="utf-8")

    fresh_pick = {"ticker": "NVDA", "reasons": ["手动指定"], "score": 0,
                  "snapshot": {"last": 180.5, "pct": 2.1}}
    pf.run_batch([fresh_pick], "2026-11-05", ["market"], 1, 90,
                 resume=True, force=True)

    saved = json.loads(decisions.read_text(encoding="utf-8"))
    assert saved["results"]["NVDA"]["snapshot"]["last"] == 180.5


def test_append_history_replaces_rerun_tickers(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "STATE", tmp_path)
    hist = tmp_path / "history.jsonl"
    hist.write_text(
        json.dumps({"date": "2026-11-05", "ticker": "NVDA", "rating": "buy"}) + "\n"
        + json.dumps({"date": "2026-11-05", "ticker": "MU", "rating": "hold"}) + "\n"
        + json.dumps({"date": "2026-11-04", "ticker": "NVDA", "rating": "sell"}) + "\n",
        encoding="utf-8")

    pf.append_history(
        "2026-11-05",
        {"NVDA": {"ticker": "NVDA", "status": "done", "rating": "hold",
                  "target_price": 200, "confidence": 0.7}},
        replace_tickers={"NVDA"})

    rows = [json.loads(line) for line in hist.read_text(encoding="utf-8").splitlines()]
    nvda_today = [r for r in rows if r["date"] == "2026-11-05" and r["ticker"] == "NVDA"]
    assert len(nvda_today) == 1
    assert nvda_today[0]["rating"] == "hold"
    assert nvda_today[0]["target_price"] == 200
    # 其它标的、其它日期的记录不动
    assert any(r["date"] == "2026-11-05" and r["ticker"] == "MU" for r in rows)
    assert any(r["date"] == "2026-11-04" and r["ticker"] == "NVDA" for r in rows)


def test_append_history_without_replace_keeps_first(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "STATE", tmp_path)
    hist = tmp_path / "history.jsonl"
    hist.write_text(
        json.dumps({"date": "2026-11-05", "ticker": "NVDA", "rating": "buy"}) + "\n",
        encoding="utf-8")

    pf.append_history(
        "2026-11-05",
        {"NVDA": {"ticker": "NVDA", "status": "done", "rating": "sell"}})

    rows = [json.loads(line) for line in hist.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["rating"] == "buy"
