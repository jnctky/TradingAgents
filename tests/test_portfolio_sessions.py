"""Two sessions a day, and a re-run that does not erase the first answer.

User requirement (2026-11-12): every trading day runs at least twice — before
the open and after the close — the two reports stand on their own, and either
session's timing is the user's to set. On top of that, any single ticker can be
re-run by hand and that run must stay viewable.

That makes two identifiers part of every piece of state: the *slot*
(pre/post) and the *version* (v1, v2, … inside one slot). Before this, one
file per (date, horizon) held one row per ticker, so the second run of a day
either overwrote the first or was skipped by the idempotency rule.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "my_tools" / "portfolio_run.py"
_spec = importlib.util.spec_from_file_location("portfolio_run_sessions", _SRC)
pf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pf)


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "STATE", tmp_path)
    monkeypatch.setattr(pf, "WORKSPACE", tmp_path / "portfolio")
    return tmp_path


def _row(ticker: str = "AAPL", **extra) -> dict:
    row = {
        "ticker": ticker, "status": "done", "asset_class": "equity",
        "rating": "Underweight", "target_price": 325.33, "target_basis": "structural",
        "confidence": 0.56, "decision": "**Rating**: Underweight\n**Price Target**: 325.33",
        "snapshot": {"last": 337.27}, "reasons": ["例行巡检"],
        "data_integrity": {"complete": True, "price_ok": True, "missing": [],
                           "categories": {}},
        "started_at": "2026-11-11T22:30:00", "finished_at": "2026-11-11T22:41:00",
    }
    row.update(extra)
    return row


# --- 场次：两场各写各的文件 ------------------------------------------------

@pytest.mark.unit
def test_the_two_sessions_never_share_a_file():
    pre = pf.decisions_path("2026-11-11", "position", "pre")
    post = pf.decisions_path("2026-11-11", "position", "post")
    assert pre != post
    assert pre.name == "decisions-2026-11-11-position-pre.json"
    assert post.name == "decisions-2026-11-11-position-post.json"


@pytest.mark.unit
def test_a_pre_slot_never_borrows_the_close_report(state):
    """A pre-market page must not show the close run's verdicts.

    Showing them would be worse than an empty page: the reader would think the
    pre-open run had looked at the pre-market when it had not run at all.
    """
    (state / "decisions-2026-11-11-position.json").write_text('{"results": {}}', encoding="utf-8")
    resolved = pf.resolve_decisions_path("2026-11-11", "position", "pre")
    assert resolved.name == "decisions-2026-11-11-position-pre.json"
    assert not resolved.exists()


@pytest.mark.unit
def test_the_close_slot_still_reads_files_written_before_sessions_existed(state):
    legacy = state / "decisions-2026-11-11-position.json"
    legacy.write_text('{"results": {}}', encoding="utf-8")
    assert pf.resolve_decisions_path("2026-11-11", "position", "post") == legacy
    assert pf.resolve_decisions_path("2026-11-11", "position", "pre") != legacy


@pytest.mark.unit
def test_the_two_sessions_get_two_reports(state):
    rows = {"AAPL": _row()}
    pre = pf.write_report("2026-11-11", rows, ["market"], "position", "pre")
    post = pf.write_report("2026-11-11", rows, ["market"], "position", "post")
    assert pre != post
    assert pre.name.endswith("-pre.md") and post.name.endswith("-post.md")


@pytest.mark.unit
def test_the_pre_report_states_both_dates():
    """报告日期与数据日是两回事（2026-11-12 口径修正）。

    pre 场的报告日期 = 它服务的那次开盘（11-12），数据日 = 上一交易日
    （11-11）。只写一个日期，读者会把「昨晚的复述」当成「今早的判断」。
    """
    assert pf.data_through("2026-11-12", "pre") == "2026-11-11"
    assert pf.data_through("2026-11-12", "post") == "2026-11-12"
    # 周一开盘的数据日是周五：周末回退，永不落在周六周日。
    assert pf.data_through("2026-11-16", "pre") == "2026-11-13"
    assert pf.data_through("2026-11-15", "pre") == "2026-11-13"


@pytest.mark.unit
def test_report_date_keys_pre_to_the_open_it_serves():
    """pre 场的报告日期跟着「它服务的开盘」走，而不是跟着上一收盘走。

    11-12 美东早上跑的盘前，必须编在 11-12 的页上；旧的「最近收盘日」
    会把它推回 11-11——比 11-11 的盘后还晚 14 小时，看起来像时间倒流。
    """
    # 周五 06:30 北京（= 周四 17:30 美东）：盘后复盘周四。
    fri_morning = datetime(2026, 11, 13, 6, 30)
    # 周五 20:30 北京（= 周五 07:30 美东）：盘前服务周五开盘。
    fri_evening = datetime(2026, 11, 13, 20, 30)
    assert pf.us_session_date_at(fri_morning) == "2026-11-12"
    assert pf.report_date_at("pre", fri_evening) == "2026-11-13"
    assert pf.report_date_at("post", fri_morning) == "2026-11-12"
    # 周六早上 06:30 北京（= 周五 17:30 美东）：盘后复盘周五——这就是
    # 「周五的盘后在周六早上跑」的机制，不再拖到周一。
    sat_morning = datetime(2026, 11, 14, 6, 30)
    assert pf.report_date_at("post", sat_morning) == "2026-11-13"
    # 周日 20:30 北京：crypto 全年无休，pre 页照常成立（数据日回退周五）。
    sun_evening = datetime(2026, 11, 15, 20, 30)
    assert pf.report_date_at("pre", sun_evening) == "2026-11-15"
    assert pf.data_through("2026-11-15", "pre") == "2026-11-13"


@pytest.mark.unit
def test_the_header_names_the_session(state):
    path = pf.write_report("2026-11-12", {"AAPL": _row()}, ["market"], "position", "pre")
    header = path.read_text(encoding="utf-8").splitlines()[2]
    assert "盘前" in header
    assert "2026-11-12" in header          # 报告日期 = 服务的开盘
    assert "2026-11-11" in header          # 数据日 = 上一交易日


# --- 版本：重跑追加，不覆盖 ------------------------------------------------

@pytest.mark.unit
def test_a_rerun_keeps_the_previous_verdict_as_a_version():
    first = pf.record_version(_row(origin="scheduled"), origin="scheduled", slot="post")
    assert first["version"] == 1
    assert len(first["versions"]) == 1

    second = pf.record_version(
        {**first, "rating": "Overweight", "target_price": 410.0,
         "finished_at": "2026-11-11T23:41:00"},
        origin="manual", slot="post")
    assert second["version"] == 2
    assert [v["rating"] for v in second["versions"]] == ["Underweight", "Overweight"]
    assert second["versions"][1]["origin"] == "manual"
    # 顶层字段仍是当前版，老读者（日报、审计、驾驶舱）不受影响
    assert second["rating"] == "Overweight"


@pytest.mark.unit
def test_the_verdict_about_to_be_overwritten_enters_the_history_once():
    """``_ensure_versioned`` is called before every spawn, so it must be idempotent."""
    row = pf.record_version(_row(), origin="scheduled", slot="post")
    once = pf._ensure_versioned(dict(row))
    twice = pf._ensure_versioned(once)
    assert len(twice["versions"]) == 1
    assert twice["versions"][0]["rating"] == "Underweight"


@pytest.mark.unit
def test_a_failed_rerun_falls_back_to_the_previous_verdict():
    """A 400 on a hand-triggered re-run must not blank out a usable analysis."""
    previous = pf.record_version(_row(), origin="scheduled", slot="post")
    merged = {"ticker": "AAPL", "status": "failed", "rating": None,
              "target_price": None, "decision": "", "error": "400 INVALID_REQUEST"}
    kept = pf._keep_previous_verdict(merged, previous)
    assert kept["status"] == "failed"
    assert kept["rating"] == "Underweight"
    assert kept["target_price"] == 325.33
    assert kept["kept_version"] == 1
    assert kept["error"] == "400 INVALID_REQUEST"


@pytest.mark.unit
def test_the_report_counts_versions_and_names_the_origin(state):
    row = pf.record_version(_row(), origin="scheduled", slot="post")
    row = pf.record_version(
        {**row, "rating": "Hold", "target_price": None,
         "finished_at": "2026-11-11T23:41:00"},
        origin="manual", slot="post")
    text = pf.write_report("2026-11-11", {"AAPL": row}, ["market"],
                           "position", "post").read_text(encoding="utf-8")
    assert "| 版本 |" in text
    assert "v2 手动" in text
    assert "版本记录（同场次重跑）" in text
    assert "Underweight" in text and "Hold" in text


@pytest.mark.unit
def test_the_report_states_whether_the_extended_session_snapshot_was_read(state):
    """数据完整性：盘前快照不参与指标计算，但「有没有取到」必须写在开头。

    用户原则（2026-11-11）：取数失败是技术问题，绝不用替代数据凑结论；同时
    报告必须逐项列清每项依赖数据的状态。扩展时段快照就是盘前场的一项依赖，
    只在各标的条目里留一句等于没写。
    """
    ok = _row("AAPL", extended_session={
        "status": "ok", "slot": "pre", "price": 349.49, "change_pct": 0.28,
        "as_of": "Nov 11, 2026 09:30 AM ET",
    })
    na = _row("BTC", asset_class="crypto", extended_session={
        "status": "na", "slot": "pre", "reason": "加密资产 24 小时连续交易，无盘前/盘后时段",
    })
    bad = _row("MU", extended_session={
        "status": "failed", "slot": "pre", "error": "DATA_UNAVAILABLE",
    })
    text = pf.write_report("2026-11-11", {"AAPL": ok, "BTC": na, "MU": bad},
                           ["market"], "position", "pre").read_text(encoding="utf-8")

    head = text.split("## 决策表")[0]
    assert "盘前快照：取得 1 只" in head
    assert "不适用 1 只（BTC）" in head
    # 取不到要醒目，并且明说没有用前收盘顶替。
    assert "未取得 1 只（MU）" in head
    assert "未用前收盘替代" in head
    assert "仅入场参考" not in head  # 这句属于各标的条目
    assert "只作入场参考" in head
    # 一次，不是两次：同一段曾在编辑中重复插入，读者会以为有两套快照来源。
    assert head.count("盘前快照：取得") == 1


@pytest.mark.unit
def test_the_report_states_the_index_futures_tape_once(monkeypatch, state):
    """指数期货是市场级依赖：四个合约取到几个、缺了谁，都要写在开头。

    缺的合约不得用昨结算价顶上（数据完整性原则），所以「未取得」必须点名。
    """
    monkeypatch.setattr(pf, "read_market_tape", lambda *a, **k: {
        "status": "partial", "slot": "pre", "fetched_at": "2026-11-12T11:42:15",
        "rows": [
            {"symbol": "ES", "label": "标普 500 期货", "status": "ok",
             "price": 7617.0, "change_pct": -0.29, "as_of": "2026-11-11 22:30 ET"},
            {"symbol": "NQ", "label": "纳指 100 期货", "status": "ok",
             "price": 28254.75, "change_pct": -0.77, "as_of": "2026-11-11 22:30 ET"},
            {"symbol": "YM", "label": "道指期货", "status": "failed",
             "error": "HTTP 403"},
        ]})
    text = pf.write_report("2026-11-11", {"AAPL": _row()}, ["market"],
                           "position", "pre").read_text(encoding="utf-8")
    head = text.split("## 决策表")[0]
    assert "指数期货" in head
    assert "ES 7617.00（-0.29%）" in head
    assert "NQ 28254.75（-0.77%）" in head
    assert "未取得：YM" in head
    assert head.count("指数期货（整批取一次") == 1


@pytest.mark.unit
def test_a_dead_tape_is_reported_as_missing_not_as_flat(monkeypatch, state):
    monkeypatch.setattr(pf, "read_market_tape", lambda *a, **k: {
        "status": "failed", "slot": "pre",
        "rows": [{"symbol": "ES", "label": "标普 500 期货", "status": "failed",
                  "error": "HTTP 403"}]})
    text = pf.write_report("2026-11-11", {"AAPL": _row()}, ["market"],
                           "position", "pre").read_text(encoding="utf-8")
    head = text.split("## 决策表")[0]
    assert "指数期货：**未取得**" in head
    assert "未用任何替代数据" in head
