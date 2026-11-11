"""Global news is the same for every ticker in a batch, so it is fetched once.

Why this is pinned: a 22-name batch asked the nasdaq basket route 22 times for
the identical macro feed, and the provider starts answering 0-byte bodies long
before that. Those empties made ``news_data`` ``partial`` on 19 of 21 rows,
which marked the whole row 数据不完整 and hid its target price — a real
analysis losing its numbers to a caching bug.
"""

from __future__ import annotations

import json

import pytest

from tradingagents.dataflows.vendors.openbb import news


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(news, "get_config",
                        lambda: {"data_cache_dir": str(tmp_path / "cache"),
                                 "global_news_lookback_days": 7,
                                 "global_news_article_limit": 10})
    return tmp_path / "cache" / "global_news"


def _rows():
    return [{"date": "2026-11-10T12:00:00", "title": "Fed holds rates",
             "url": "https://example.com/a", "symbol": "SPY", "text": "body"}]


def test_the_second_ticker_reuses_the_first_fetch(cache, monkeypatch):
    calls = []

    def fake_get_json(path, what, params=None, **_kwargs):
        calls.append(params)
        return _rows()

    monkeypatch.setattr(news, "get_json", fake_get_json)
    first = news.get_global_news_openbb("2026-11-10", 7, 10)
    second = news.get_global_news_openbb("2026-11-10", 7, 10)

    assert len(calls) == 1
    assert first == second


def test_a_different_window_is_a_different_cache_entry(cache, monkeypatch):
    calls = []
    monkeypatch.setattr(news, "get_json",
                        lambda path, what, params=None, **kw: (calls.append(1), _rows())[1])
    news.get_global_news_openbb("2026-11-10", 7, 10)
    news.get_global_news_openbb("2026-11-10", 3, 10)
    assert len(calls) == 2


def test_an_empty_answer_is_not_pinned_to_the_date(cache, monkeypatch):
    # Caching "no news" would keep a later run blind after the feed recovered.
    state = {"rows": [], "calls": 0}

    def fake(path, what, params=None, **_kwargs):
        state["calls"] += 1
        return state["rows"]

    monkeypatch.setattr(news, "get_json", fake)
    news.get_global_news_openbb("2026-11-10", 7, 10)
    assert not (cache / "2026-11-10_7_10.json").exists()
    state["rows"] = _rows()
    news.get_global_news_openbb("2026-11-10", 7, 10)
    assert state["calls"] == 2
    assert (cache / "2026-11-10_7_10.json").exists()


def test_a_payload_from_another_version_is_ignored(cache, monkeypatch):
    cache.mkdir(parents=True)
    (cache / "2026-11-10_7_10.json").write_text(
        json.dumps({"version": news.CACHE_VERSION + 1, "text": "stale"}), encoding="utf-8")
    calls = []
    monkeypatch.setattr(news, "get_json",
                        lambda path, what, params=None, **kw: (calls.append(1), _rows())[1])
    out = news.get_global_news_openbb("2026-11-10", 7, 10)
    assert calls and "stale" not in out
