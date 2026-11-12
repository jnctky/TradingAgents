"""Index futures: the market-wide tape, and what it is not allowed to be.

User requirement (2026-11-12): the pre-open session reads "pre-market quotes /
futures". The quotes are per symbol and pinned in test_premarket_snapshot.py;
this file pins the market-wide half — four index contracts fetched once per
batch, rendered for every analyst, and never used as a symbol's price.

The failure tests are the point of the module: a missing contract must arrive as
a missing contract, not as yesterday's settle wearing a live label.
"""

from __future__ import annotations

import pytest

from tradingagents.dataflows.vendors import futures


@pytest.mark.unit
def test_a_contract_row_is_parsed_from_the_meta_block():
    meta = {"regularMarketPrice": 7617.0, "previousClose": 7640.5,
            "regularMarketChangePercent": -0.294, "regularMarketVolume": 72128,
            "regularMarketTime": 1794454240, "gmtoffset": -18000,
            "shortName": "E-Mini S&P 500 Dec 26"}
    row = futures._row("ES", "ES=F", "标普 500 期货", meta, "")
    assert row["status"] == "ok"
    assert row["price"] == pytest.approx(7617.0)
    assert row["change_pct"] == pytest.approx(-0.294)
    assert row["previous_close"] == pytest.approx(7640.5)
    assert row["change"] == pytest.approx(-23.5)
    # Yahoo reports EST/EDT itself; 03:30Z minus 5h is 22:30 on the prior ET day.
    assert row["as_of"] == "2026-11-11 22:30 ET"


@pytest.mark.unit
def test_a_missing_change_is_derived_from_the_prior_settle():
    meta = {"regularMarketPrice": 100.0, "previousClose": 98.0}
    row = futures._row("NQ", "NQ=F", "纳指 100 期货", meta, "")
    assert row["change_pct"] == pytest.approx(2.0408, rel=1e-3)


@pytest.mark.unit
def test_a_contract_without_a_price_is_a_failure_not_a_zero():
    row = futures._row("YM", "YM=F", "道指期货", {"previousClose": 52692.0}, "")
    assert row["status"] == "failed"
    assert "price" not in row


@pytest.mark.unit
def test_the_tape_reports_partial_coverage_instead_of_hiding_it(monkeypatch):
    answers = {"ES=F": ({"regularMarketPrice": 7600.0, "previousClose": 7640.5,
                         "regularMarketChangePercent": -0.53,
                         "regularMarketTime": 1794454240, "gmtoffset": -18000}, ""),
               "NQ=F": (None, "Timeout: timed out"),
               "YM=F": ({"regularMarketPrice": 52692.0, "previousClose": 52578.0,
                         "regularMarketChangePercent": 0.22,
                         "regularMarketTime": 1794454240, "gmtoffset": -18000}, ""),
               "RTY=F": ({"regularMarketPrice": 2858.4, "previousClose": 2853.7,
                          "regularMarketChangePercent": 0.16,
                          "regularMarketTime": 1794454240, "gmtoffset": -18000}, "")}
    monkeypatch.setattr(futures, "_fetch_one", lambda symbol, attempts=2: answers[symbol])
    monkeypatch.setattr(futures, "record_call", lambda **kw: None)

    tape = futures.fetch_index_futures("pre")
    assert tape["status"] == "partial"
    missing = [r["symbol"] for r in tape["rows"] if r["status"] != "ok"]
    assert missing == ["NQ"]

    text = futures.render_index_futures(tape)
    assert "未取得" in text and "NQ" in text
    assert "7617" not in text                      # the dead contract is not filled
    assert "7600.00" in text and "-0.53%" in text


@pytest.mark.unit
def test_a_dead_feed_carries_the_unavailable_sentinel(monkeypatch):
    monkeypatch.setattr(futures, "_fetch_one",
                        lambda symbol, attempts=2: (None, "HTTP 403"))
    monkeypatch.setattr(futures, "record_call", lambda **kw: None)

    tape = futures.fetch_index_futures("pre")
    assert tape["status"] == "failed"
    text = futures.render_index_futures(tape)
    assert "DATA_UNAVAILABLE" in text
    assert "不得用昨日收盘" in text


@pytest.mark.unit
def test_the_rendered_block_says_it_is_context_not_a_price():
    tape = {"status": "ok", "slot": "pre", "rows": [
        {"symbol": "NQ", "label": "纳指 100 期货", "status": "ok",
         "price": 28254.75, "change_pct": -0.77, "as_of": "2026-11-11 22:30 ET"}]}
    text = futures.render_index_futures(tape)
    assert "指数期货" in text and "28254.75" in text and "-0.77%" in text
    assert "不是这只标的的价格" in text
    assert "以它的日线" in text


@pytest.mark.unit
def test_the_post_slot_heading_speaks_about_the_next_open():
    tape = {"status": "ok", "slot": "post", "rows": [
        {"symbol": "ES", "label": "标普 500 期货", "status": "ok",
         "price": 7619.0, "change_pct": -0.28, "as_of": "2026-11-11 22:32 ET"}]}
    text = futures.render_index_futures(tape)
    assert "收盘后隔夜" in text
    assert "下一场开盘前" in text


@pytest.mark.unit
def test_the_kept_fields_are_scalars_and_name_what_is_missing():
    tape = {"status": "partial", "slot": "pre", "fetched_at": "2026-11-12T11:42:15",
            "rows": [{"symbol": "ES", "label": "标普 500 期货", "status": "ok",
                      "price": 7617.0, "change_pct": -0.29, "as_of": "2026-11-11 22:30 ET"},
                     {"symbol": "NQ", "label": "纳指 100 期货", "status": "failed",
                      "error": "HTTP 403"}]}
    fields = futures.tape_row_fields(tape)
    assert fields["status"] == "partial"
    assert fields["rows"][0]["symbol"] == "ES"
    assert fields["missing"] == ["NQ"]
    assert "error" not in fields


@pytest.mark.unit
def test_no_tape_renders_nothing_rather_than_a_hedge():
    assert futures.render_index_futures(None) == ""
    assert futures.render_index_futures({"status": "failed", "rows": []}) == ""
