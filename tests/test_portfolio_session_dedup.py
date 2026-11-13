"""场次级去重（2026-11-13）。

AtLogOn 补偿触发上线后，每次登录都会唤起盘前、盘后两场任务。去重必须挡住
已经跑完的场次，同时**不能**挡住 aborted_preflight 的缺口——那正是要补的。

跑：.venv/Scripts/python.exe -m pytest tests/test_portfolio_session_dedup.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

MY_TOOLS = Path(__file__).resolve().parents[1] / "my_tools"
if str(MY_TOOLS) not in sys.path:
    sys.path.insert(0, str(MY_TOOLS))

import portfolio_run  # noqa: E402


@pytest.fixture()
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(portfolio_run, "STATE", tmp_path)
    return tmp_path


def write_decisions(state: Path, slot: str, results: dict) -> None:
    path = state / f"decisions-2026-11-13-position-{slot}.json"
    path.write_text(json.dumps({"date": "2026-11-13", "results": results}), encoding="utf-8")


def test_missing_file_is_not_done(state):
    assert portfolio_run.session_already_done("2026-11-13", "position", "pre") is None


def test_all_done_rows_count_as_done(state):
    write_decisions(state, "pre", {
        "NVDA": {"status": "done", "rating": "Buy"},
        "MU": {"status": "done", "rating": "Hold"},
    })
    assert portfolio_run.session_already_done("2026-11-13", "position", "pre") == 2


def test_partial_run_is_not_done(state):
    write_decisions(state, "pre", {
        "NVDA": {"status": "done"},
        "MU": {"status": "failed"},
    })
    assert portfolio_run.session_already_done("2026-11-13", "position", "pre") is None


def test_aborted_preflight_is_the_gap_to_fill(state):
    """数据校验拦下的批次写的是空 results——必须补跑，不能当成已完成。"""
    path = state / "decisions-2026-11-13-position-post.json"
    path.write_text(json.dumps({
        "date": "2026-11-13",
        "slot": "post",
        "status": "aborted_preflight",
        "results": {},
    }), encoding="utf-8")
    assert portfolio_run.session_already_done("2026-11-13", "position", "post") is None


def test_sessions_are_deduped_separately(state):
    write_decisions(state, "pre", {"NVDA": {"status": "done"}})
    assert portfolio_run.session_already_done("2026-11-13", "position", "pre") == 1
    assert portfolio_run.session_already_done("2026-11-13", "position", "post") is None
