"""The political backdrop: whitelist, de-duplication, thresholds, and dated odds.

Every test runs offline. The news feed and Polymarket are stubbed at the module
boundary, because what has to hold is the editing logic: which headline reaches
the report, how often it is counted, and what is said when there is nothing to
say.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from tradingagents.dataflows.vendors import politics as pol
from tradingagents.dataflows.vendors import polymarket
from tradingagents.dataflows.vendors.polymarket import PolymarketUnavailable

TRADE_DATE = "2026-11-05"


def _row(title, published="2026-11-04T20:00:00", source="", url=None, excerpt=""):
    return {
        "date": published,
        "title": title,
        "source": source,
        "url": "https://example.com/a" if url is None else url,
        "excerpt": excerpt,
    }


@pytest.fixture
def feed(monkeypatch):
    """Serve a fixed set of rows, whatever window is asked for."""
    rows: list[dict] = []

    def fake_get_json(path, label, params=None):
        return list(rows)

    monkeypatch.setattr(
        "tradingagents.dataflows.vendors.openbb.client.get_json", fake_get_json
    )
    return rows


@pytest.fixture
def odds_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(pol, "odds_dir", lambda: tmp_path)
    return tmp_path


# --------------------------------------------------------------------------- #
# whitelist
# --------------------------------------------------------------------------- #

def test_a_political_headline_lands_in_its_topic(feed):
    feed.append(_row("Stocks Surge as Democrats Sweep Congress"))
    result = pol.fetch_politics(TRADE_DATE)
    assert [t["key"] for t in result["topics"]] == ["congress"]
    assert result["topics"][0]["articles"][0]["title"] == "Stocks Surge as Democrats Sweep Congress"


def test_a_market_headline_without_political_words_is_dropped(feed):
    feed.append(_row("Chipmakers Slump on AI Capex Fears"))
    result = pol.fetch_politics(TRADE_DATE)
    assert result["topics"] == []
    assert result["available"] is False


def test_one_headline_is_counted_under_one_topic(feed):
    """Filed under both 'Congress' and 'Party control' it would read as two events."""
    feed.append(_row("Democrats Take Over Congress"))
    result = pol.fetch_politics(TRADE_DATE)
    assert sum(len(t["articles"]) for t in result["topics"]) == 1


def test_the_same_wrap_rewritten_twice_that_day_counts_once(feed):
    feed.append(_row("Stocks Surge as Democrats Sweep Congress", published="2026-11-04T18:00:00"))
    feed.append(_row("Stocks Sharply Higher as Democrats Retake Congress",
                     published="2026-11-04T21:00:00"))
    result = pol.fetch_politics(TRADE_DATE)
    assert len(result["topics"][0]["articles"]) == 1


def test_two_different_stories_on_one_day_both_survive(feed):
    feed.append(_row("Democrats Sweep Congress", published="2026-11-04T18:00:00"))
    feed.append(_row("Senate Passes Tariff Relief Bill", published="2026-11-04T21:00:00"))
    result = pol.fetch_politics(TRADE_DATE)
    titles = [a["title"] for t in result["topics"] for a in t["articles"]]
    assert len(titles) == 2


def test_an_article_without_a_url_is_not_evidence(feed):
    feed.append(_row("Democrats Sweep Congress", url=""))
    assert pol.fetch_politics(TRADE_DATE)["topics"] == []


def test_a_row_published_after_the_analysis_date_is_never_read(feed):
    feed.append(_row("Stocks Surge as Democrats Sweep Congress", published="2026-11-09T09:00:00"))
    assert pol.fetch_politics(TRADE_DATE)["topics"] == []


# --------------------------------------------------------------------------- #
# thresholds: silence is a result
# --------------------------------------------------------------------------- #

def test_one_stray_headline_is_not_a_backdrop(feed):
    feed.append(_row("Stocks Surge on Midterm Election Optimism"))
    result = pol.fetch_politics(TRADE_DATE)
    assert result["available"] is False
    assert "not a backdrop" in result["reason"]


def test_a_single_fiscal_deadline_headline_does_count(feed):
    """A shutdown is market-moving on the announcement, unlike a passing mention."""
    feed.append(_row("Government Shutdown Talks Collapse"))
    result = pol.fetch_politics(TRADE_DATE)
    assert result["available"] is True
    assert result["topics"][0]["key"] == "fiscal"


def test_two_ordinary_headlines_clear_the_threshold(feed):
    feed.append(_row("Democrats Sweep Congress", published="2026-11-04T18:00:00"))
    feed.append(_row("Stocks Rally as Crude Oil Sinks on US-Iran Hopes",
                     published="2026-11-02T18:00:00"))
    assert pol.fetch_politics(TRADE_DATE)["available"] is True


def test_no_headline_says_so_rather_than_staying_empty(feed):
    result = pol.fetch_politics(TRADE_DATE)
    assert result["available"] is False
    assert "no whitelisted political headline" in result["reason"]
    block = pol.politics_prompt_block(result, {"available": False, "reason": "none"})
    assert "none reported" in block


# --------------------------------------------------------------------------- #
# odds: written today, read tomorrow
# --------------------------------------------------------------------------- #

def _snapshot(day, markets=None, version=pol.ODDS_VERSION):
    payload = {
        "version": version,
        "as_of": f"{day}T13:05:00+00:00",
        "trade_date": str(day),
        "markets": markets if markets is not None else [
            {"question": "Democrats win the House?", "outcome": "Yes", "probability": 0.62,
             "volume_usd": 1_200_000, "resolves": "2026-11-30", "week_change_pp": 4.0, "slug": "x"},
        ],
        "errors": [],
    }
    (pol.odds_dir() / f"{day}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_no_snapshot_reports_no_odds_rather_than_guessing(odds_dir):
    odds = pol.read_odds(TRADE_DATE)
    assert odds["available"] is False
    assert "live odds are not served" in odds["reason"]
    assert odds["markets"] == []


def test_yesterdays_snapshot_is_read_and_its_age_reported(odds_dir):
    yesterday = date(2026, 11, 4)
    _snapshot(yesterday)
    odds = pol.read_odds(TRADE_DATE)
    assert odds["available"] is True
    assert odds["age_days"] == 1
    assert odds["markets"][0]["probability"] == 0.62


def test_a_snapshot_taken_after_the_analysis_date_is_never_read(odds_dir):
    """A backdated run must not see today's snapshot: it prices tomorrow's news."""
    _snapshot(date(2026, 11, 5))
    assert pol.read_odds("2026-10-20")["available"] is False


def test_odds_older_than_the_staleness_budget_are_not_served(odds_dir):
    _snapshot(date(2026, 11, 5) - timedelta(days=pol.MAX_ODDS_AGE_DAYS + 1))
    assert pol.read_odds(TRADE_DATE)["available"] is False


def test_an_older_schema_is_ignored(odds_dir):
    _snapshot(date(2026, 11, 4), version=pol.ODDS_VERSION + 1)
    assert pol.read_odds(TRADE_DATE)["available"] is False


def test_a_dead_api_writes_nothing(odds_dir, monkeypatch):
    def boom(*a, **k):
        raise PolymarketUnavailable("Tunnel connection failed: 502")

    monkeypatch.setattr(polymarket, "search_markets", boom)
    result = pol.record_odds(TRADE_DATE)
    assert result["written"] is False
    assert "502" in result["reason"]
    assert list(odds_dir.iterdir()) == []


def test_a_live_fetch_is_stored_under_the_fetch_date(odds_dir, monkeypatch):
    monkeypatch.setattr(
        polymarket, "search_markets",
        lambda topic, limit=None: [{"question": f"Q about {topic}", "outcome": "Yes",
                                    "probability": 0.5, "volume_usd": 100,
                                    "resolves": "2026-12-31", "week_change_pp": None,
                                    "slug": "s"}],
    )
    result = pol.record_odds(TRADE_DATE)
    assert result["written"] is True
    written = list(odds_dir.iterdir())
    assert len(written) == 1
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    # The four search topics overlap on one market, so it is stored once.
    assert len(payload["markets"]) == len(pol.ODDS_TOPICS)


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #

def _odds_available():
    return {"available": True, "age_days": 1, "as_of": "2026-11-04T13:05:00+00:00",
            "markets": [{"question": "Democrats win the House?", "outcome": "Yes",
                         "probability": 0.62, "volume_usd": 1_200_000,
                         "resolves": "2026-11-30", "week_change_pp": 4.0, "slug": "x"}]}


def test_odds_markdown_states_its_vintage(odds_dir):
    text = pol.odds_markdown(_odds_available())
    assert "Democrats win the House?" in text
    assert "62%" in text
    assert "+4.0pp" in text
    assert "read 1 day(s) later" in text


def test_absent_odds_say_no_odds_are_used(odds_dir):
    text = pol.odds_markdown({"available": False, "reason": "no snapshot", "markets": []})
    assert "Prediction-market odds: none" in text
    assert "No odds are used" in text


def test_the_prompt_forbids_a_probability_when_odds_are_absent():
    block = pol.politics_prompt_block(
        {"available": False, "reason": "quiet", "window": ["2026-10-22", TRADE_DATE],
         "source": "feed", "topics": [], "scanned": 0},
        {"available": False, "reason": "none"},
    )
    assert "do not state any probability" in block


def test_every_headline_in_the_report_carries_its_link(feed):
    feed.append(_row("Democrats Sweep Congress", published="2026-11-04T18:00:00"))
    feed.append(_row("Senate Passes Tariff Relief Bill", published="2026-11-04T21:00:00"))
    result = pol.fetch_politics(TRADE_DATE)
    text = pol.politics_markdown(result, {"available": False, "reason": "none", "markets": []})
    for topic in result["topics"]:
        for article in topic["articles"]:
            assert f"]({article['url']})" in text
    assert "not endorsed" in text
