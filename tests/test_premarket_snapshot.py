"""The extended-session tape: an entry reference, never a substitute close.

User requirement (2026-11-12): the pre-open session needs pre-market data, not
a re-read of last night's close. The source is nasdaq's extended-trading row,
which is what these tests pin down:

- the numbers are parsed from the vendor's strings, not guessed;
- crypto answers "not applicable" instead of a fabricated pre-market price;
- a failed fetch says so, and the prompt text forbids using the prior close in
  its place (the data-integrity principle, applied to this new feed).
"""

from __future__ import annotations

import pytest

from tradingagents.dataflows.vendors import premarket


@pytest.mark.unit
def test_the_vendor_row_is_parsed_into_numbers():
    row = {"consolidated": "$349.49 +0.98 (+0.28%)", "volume": "541,193",
           "highPrice": "$349.78 (09:28:40 AM)", "lowPrice": "$345.55 (05:51:25 AM)"}
    data = {"previousInfo": " Market Close: $348.51",
            "lastUpdateInfo": ["Data last updated Nov 11, 2026 09:30 AM ET."]}
    parsed = premarket._parse_row(row, data, "pre", "AAPL")
    assert parsed["price"] == pytest.approx(349.49)
    assert parsed["change_pct"] == pytest.approx(0.28)
    assert parsed["volume"] == pytest.approx(541193)
    assert parsed["high"] == pytest.approx(349.78)
    assert parsed["low"] == pytest.approx(345.55)
    assert parsed["previous_close"] == pytest.approx(348.51)
    assert parsed["as_of"] == "Nov 11, 2026 09:30 AM ET"


@pytest.mark.unit
def test_a_negative_change_does_not_confuse_the_price():
    row = {"consolidated": "$928 -17.81 (-1.88%)", "volume": "1,000",
           "highPrice": "$946.00 (04:00:01 AM)", "lowPrice": "$925.00 (09:29:00 AM)"}
    parsed = premarket._parse_row(row, {"lastUpdateInfo": []}, "pre", "GS")
    assert parsed["price"] == pytest.approx(928.0)
    assert parsed["change_pct"] == pytest.approx(-1.88)


@pytest.mark.unit
def test_crypto_is_not_a_failure_it_is_not_applicable(monkeypatch):
    calls = []
    monkeypatch.setattr(premarket, "record_call", lambda **kw: calls.append(kw))
    snapshot = premarket.fetch_extended_session("BTC", asset_class="crypto", slot="pre")
    assert snapshot["status"] == "na"
    assert snapshot["reason"]
    assert calls and calls[0]["vendor"] == premarket.NOT_APPLICABLE_VENDOR


@pytest.mark.unit
def test_crypto_text_says_why_there_is_no_premarket():
    text = premarket.render_extended_session(
        {"status": "na", "slot": "pre", "reason": "加密资产 24 小时连续交易"})
    assert "不适用" in text
    assert "24 小时" in text


@pytest.mark.unit
def test_the_rendered_block_forbids_using_it_as_a_close():
    snapshot = {"status": "ok", "slot": "pre", "price": 349.49, "change_pct": 0.28,
                "volume": 541193, "high": 349.78, "low": 345.55,
                "previous_close": 348.51, "as_of": "Nov 11, 2026 09:30 AM ET"}
    text = premarket.render_extended_session(snapshot)
    assert "盘前快照" in text
    assert "349.49" in text and "+0.28%" in text
    assert "以日线收盘为准" in text
    assert "不得用" in text


@pytest.mark.unit
def test_a_failed_fetch_carries_the_unavailable_sentinel():
    text = premarket.render_extended_session(
        {"status": "failed", "slot": "pre", "error": "HTTP 403"})
    assert "DATA_UNAVAILABLE" in text
    assert "HTTP 403" in text
    assert "不得用前一交易日收盘价冒充" in text


@pytest.mark.unit
def test_the_row_fields_are_json_scalars_only():
    fields = premarket.snapshot_row_fields(
        {"status": "ok", "slot": "pre", "price": 1.5, "change_pct": 2.0,
         "volume": 3, "previous_close": 1.4, "as_of": "x", "raw": "{...}"})
    assert fields["price"] == 1.5 and fields["status"] == "ok"
    assert "raw" not in fields
    assert fields["fetched_at"]
