"""日报（daily-<date>-<horizon>.md）的可读性契约。

两条都来自 2026-11-10 全量复盘：
1. 头部只印「+8% to +25%」，而看跌标的目标价天然在现价下方——读者拿它对比，
   会把正常的看跌目标价看成越界。幅度必须带方向。
2. 22 只全绿时还占 22 行表格，等于用 22 行说「没事」；没消息本身就该是消息。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "my_tools" / "portfolio_run.py"
_spec = importlib.util.spec_from_file_location("portfolio_run_digest", _SRC)
pf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pf)


def _row(ticker: str, complete: bool = True, **extra) -> dict:
    row = {
        "ticker": ticker, "status": "done", "asset_class": "equity",
        "rating": "Underweight", "target_price": 88.0, "confidence": 0.7,
        "decision": "**Rating**: Underweight", "snapshot": {"last": 100.0},
        "reasons": ["例行巡检"], "data_complete": complete,
        "data_integrity": {
            "complete": complete,
            "price_ok": True,
            "missing": [] if complete else ["news_data"],
            "categories": {
                "core_stock_apis": {"ok": 1, "fail": 0, "zero_bytes": 0, "status": "ok"},
                "news_data": {"ok": 3, "fail": 1, "zero_bytes": 0,
                              "status": "ok" if complete else "partial"},
            },
        },
    }
    row.update(extra)
    return row


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "WORKSPACE", tmp_path)
    return tmp_path


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.mark.unit
def test_the_header_states_the_band_in_both_directions(workspace):
    path = pf.write_report("2026-11-10", {"AAA": _row("AAA")}, ["market"], "position")
    header = _read(path).splitlines()[2]
    assert "看涨" in header and "看跌" in header
    assert "−25%" in header and "−8%" in header


@pytest.mark.unit
def test_an_all_clean_batch_says_so_in_one_line(workspace):
    rows = {t: _row(t) for t in ("AAA", "BBB", "CCC")}
    text = _read(pf.write_report("2026-11-10", rows, ["market"], "position"))
    section = text.split("## 数据完整性")[1].split("## 决策表")[0]
    assert "全部已完成标的的依赖数据完整" in section
    # No table: three green rows carry no information.
    assert "| 标的 | 数据完整性" not in section


@pytest.mark.unit
def test_an_incomplete_row_is_named_with_its_missing_domain(workspace):
    rows = {"AAA": _row("AAA"), "BBB": _row("BBB", complete=False)}
    text = _read(pf.write_report("2026-11-10", rows, ["market"], "position"))
    section = text.split("## 数据完整性")[1].split("## 决策表")[0]
    assert "BBB" in section
    assert "新闻" in section
    assert "| BBB | **不完整** | 新闻部分可用 |" in section
