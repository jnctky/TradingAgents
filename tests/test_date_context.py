"""The per-date context file: one fetch a day, independent degradation, one cache.

The calendar and the political backdrop are fetched per analysis date, not per
ticker, so the caching rule is the whole point of the module — and a 20-ticker
batch that quietly refetched twenty times would still look correct in the report.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from tradingagents.dataflows import date_context as dc
from tradingagents.dataflows.date_window import get_current_date

TODAY = get_current_date()
PAST = "2026-10-20"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(dc, "context_dir", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def calls(monkeypatch):
    """Count the platform round-trips the builder makes."""
    tally = {"events": 0, "politics": 0, "odds_read": 0, "odds_recorded": 0}

    def fake_events(trade_date):
        tally["events"] += 1
        return {"trade_date": trade_date, "published": [], "upcoming": [],
                "window": [trade_date, trade_date]}

    def fake_politics(trade_date):
        tally["politics"] += 1
        return {"trade_date": trade_date, "window": [trade_date, trade_date],
                "source": "feed", "scanned": 0, "available": False,
                "reason": "quiet", "topics": []}

    monkeypatch.setattr(dc, "fetch_events", fake_events)
    monkeypatch.setattr(dc, "fetch_politics", fake_politics)
    monkeypatch.setattr(dc, "read_odds",
                        lambda d: tally.__setitem__("odds_read", tally["odds_read"] + 1)
                        or {"available": False, "reason": "none", "markets": []})
    monkeypatch.setattr(dc, "record_odds",
                        lambda d: tally.__setitem__("odds_recorded", tally["odds_recorded"] + 1)
                        or {"written": True})
    return tally


def test_the_second_call_reads_the_file(store, calls):
    first = dc.build_date_context(TODAY)
    second = dc.build_date_context(TODAY)
    assert calls["events"] == 1 and calls["politics"] == 1
    assert second["built_at"] == first["built_at"]
    assert (store / f"{TODAY}.json").exists()


def test_todays_context_expires_so_a_later_print_is_seen(store, calls):
    dc.build_date_context(TODAY)
    path = store / f"{TODAY}.json"
    old = time.time() - (dc.REFRESH_MINUTES + 1) * 60
    os.utime(path, (old, old))
    dc.build_date_context(TODAY)
    assert calls["events"] == 2


def test_a_past_dates_context_never_expires(store, calls):
    dc.build_date_context(PAST)
    path = store / f"{PAST}.json"
    old = time.time() - 90 * 24 * 3600
    os.utime(path, (old, old))
    dc.build_date_context(PAST)
    assert calls["events"] == 1


def test_refresh_rebuilds_on_demand(store, calls):
    dc.build_date_context(TODAY)
    dc.build_date_context(TODAY, refresh=True)
    assert calls["events"] == 2


def test_odds_are_only_written_for_the_current_date(store, calls):
    dc.build_date_context(TODAY)
    dc.build_date_context(PAST)
    assert calls["odds_recorded"] == 1


def test_a_dead_calendar_leaves_the_political_block_intact(store, calls, monkeypatch):
    def dead(trade_date):
        raise RuntimeError("calendar route returned 502")

    monkeypatch.setattr(dc, "fetch_events", dead)
    context = dc.build_date_context(TODAY)
    assert "502" in context["events"]["error"]
    markdown = dc.context_markdown(context)
    assert "Economic events" in markdown and "calendar was unavailable" in markdown
    assert "Political backdrop" in markdown
    assert "Do not guess what was released" in dc.prompt_block(context)


def test_a_dead_feed_leaves_the_calendar_intact(store, calls, monkeypatch):
    def dead(trade_date):
        raise RuntimeError("news feed returned 500")

    monkeypatch.setattr(dc, "fetch_politics", dead)
    context = dc.build_date_context(TODAY)
    assert "500" in context["politics"]["error"]
    markdown = dc.context_markdown(context)
    assert "news feed was unavailable" in markdown
    assert "Prediction-market odds" in markdown


def test_a_malformed_date_fails_before_any_fetch(store, calls):
    with pytest.raises(ValueError):
        dc.build_date_context("2026-13-40")
    assert calls["events"] == 0


def test_the_prompt_block_names_both_sections(store, calls):
    block = dc.prompt_block(dc.build_date_context(TODAY))
    assert "Economic events around the analysis date" in block
    assert "Political headlines" in block


def test_a_dead_calendar_reaches_the_trace_instead_of_only_the_prose(store, calls,
                                                                     tmp_path, monkeypatch):
    # The analyst body already said "日历不可用"; the integrity table said every
    # row 正常 because the builder swallowed the error without a trace record.
    trace = tmp_path / "trace.jsonl"
    monkeypatch.setenv("TA_TRACE_FILE", str(trace))

    def dead(_date):
        raise RuntimeError("economic calendar returned 422")

    monkeypatch.setattr(dc, "fetch_events", dead)
    monkeypatch.setattr(dc, "fetch_politics", lambda d: {
        "trade_date": d, "window": [d, d], "source": "feed", "scanned": 3,
        "available": True, "topics": [{"label": "Tariffs", "articles": [{"title": "x"}]}],
    })
    dc.build_date_context(TODAY)

    rows = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    by_category = {row["category"]: row for row in rows}
    assert by_category["events"]["ok"] is False
    assert "422" in by_category["events"]["error"]
    assert by_category["politics"]["ok"] is True


def test_a_zero_byte_political_feed_is_traced_as_a_failure(store, calls, tmp_path, monkeypatch):
    trace = tmp_path / "trace.jsonl"
    monkeypatch.setenv("TA_TRACE_FILE", str(trace))

    def empty(_date):
        return {"trade_date": _date, "available": False, "topics": [], "scanned": 0,
                "reason": "feed returned 0 bytes"}

    monkeypatch.setattr(dc, "fetch_politics", empty)
    dc.build_date_context(TODAY)

    rows = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    politics = next(row for row in rows if row["category"] == "politics")
    # An available=False payload is a miss even though no exception was raised.
    assert politics["ok"] is False
    assert "0 bytes" in politics["error"]
