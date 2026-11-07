"""StockTwits fetch: transport resilience (#1024), fingerprint fallback, and
crypto symbol mapping (#1113).

StockTwits lists crypto under ``<BASE>.X`` (Yahoo's ``BTC-USD`` 404s), and any
transport error must degrade to a placeholder rather than raise. The endpoint is
behind Cloudflare, which refuses some TLS fingerprints while allowing others, so
the transport walks a profile list instead of failing on the first 403.
"""

from __future__ import annotations

import http.client
import json
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from tradingagents.dataflows.vendors import stocktwits


class _Resp:
    def __init__(self, payload=None, status=200):
        self.status_code = status
        self.content = json.dumps(payload or {}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.content


@patch.object(stocktwits, "_cffi", None)
@pytest.mark.unit
class TestStockTwitsResilience:
    @pytest.mark.parametrize(
        "exc",
        [
            http.client.IncompleteRead(b""),
            HTTPError("url", 503, "down", {}, None),
            TimeoutError("slow"),
        ],
    )
    def test_transport_errors_return_placeholder(self, exc):
        class _Broken(_Resp):
            def read(self_inner):
                raise exc

        with patch.object(stocktwits, "urlopen", return_value=_Broken()):
            out = stocktwits.fetch_stocktwits_messages("NVDA")
        assert "unavailable" in out.lower()
        assert out.startswith("<stocktwits unavailable")


@patch.object(stocktwits, "_cffi", None)
@pytest.mark.unit
class TestStockTwitsCryptoSymbols:
    @pytest.mark.parametrize(
        ("ticker", "expected"),
        [
            ("BTC-USD", "BTC.X"),
            ("eth-usd", "ETH.X"),
            ("SOL-USD", "SOL.X"),
            ("BTCUSD", "BTC.X"),      # undashed broker form
            ("BTC-USDT", "BTC.X"),    # stablecoin quote
            ("AMD", "AMD"),
            ("BRK-B", "BRK-B"),       # dashed class share: untouched
            ("GOLD", "GOLD"),         # real equity (aliases elsewhere): untouched here
            ("XYZ-USD", "XYZ-USD"),   # unknown base: not treated as crypto
        ],
    )
    def test_symbol_mapping(self, ticker, expected):
        assert stocktwits._stocktwits_symbol(ticker) == expected

    def test_crypto_pair_requests_dot_x_endpoint(self):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            raise TimeoutError("stop after capturing the URL")

        with patch.object(stocktwits, "urlopen", side_effect=fake_urlopen):
            stocktwits.fetch_stocktwits_messages("BTC-USD")
        assert "/symbol/BTC.X.json" in seen["url"]


def _payload(*bodies):
    return {"messages": [
        {"body": b, "created_at": "2026-01-09T15:00:00Z", "user": {"username": "u"},
         "entities": {"sentiment": {"basic": "Bullish"}}}
        for b in bodies
    ]}


def _stream(*bodies):
    return _Resp(_payload(*bodies))


def _drop_spam(texts):
    return [not t.startswith("SPAM") for t in texts], "Screened: note"


@pytest.mark.unit
class TestStockTwitsScreening:
    def test_screened_out_messages_leave_the_block_and_its_counts(self):
        with patch.object(stocktwits, "_fetch_payload", return_value=_payload("SPAM", "long NVDA")):
            out = stocktwits.fetch_stocktwits_messages("NVDA", screen=_drop_spam)
        assert out.startswith("Screened: note")
        assert "long NVDA" in out and "SPAM" not in out
        assert "Total: 1 most-recent" in out

    def test_all_screened_out_is_not_called_empty(self):
        with patch.object(stocktwits, "_fetch_payload", return_value=_payload("SPAM")):
            out = stocktwits.fetch_stocktwits_messages("NVDA", screen=_drop_spam)
        assert "none of the 1 StockTwits messages is about $NVDA" in out


@patch.object(stocktwits, "_cffi", None)
@pytest.mark.unit
def test_html_entities_in_message_bodies_are_decoded():
    with patch.object(stocktwits, "urlopen", return_value=_stream("S&amp;P wasn&#39;t up")):
        out = stocktwits.fetch_stocktwits_messages("NVDA")
    assert "S&P wasn't up" in out


def _fake_cffi(statuses):
    """A stand-in for curl_cffi that answers with ``statuses`` in order.

    Records every (profile, proxies) pair it was asked for so the tests can
    assert on the walk order.
    """
    calls = []
    remaining = list(statuses)
    payload = {"messages": [{"body": "hi", "created_at": "2026-01-09T15:00:00Z",
                             "user": {"username": "u"}, "entities": {}}]}

    class _Client:
        @staticmethod
        def get(url, impersonate=None, timeout=None, headers=None, proxies=None):
            calls.append({"profile": impersonate, "proxies": proxies})
            status = remaining.pop(0) if remaining else 200
            return _Resp(payload, status=status)

    return _Client, calls


@pytest.mark.unit
class TestStockTwitsFingerprints:
    @pytest.fixture(autouse=True)
    def _reset_profile(self):
        stocktwits._working_profile = None
        yield
        stocktwits._working_profile = None

    def test_a_challenged_profile_falls_through_to_the_next(self):
        client, calls = _fake_cffi([403, 200])
        with patch.object(stocktwits, "_cffi", client):
            out = stocktwits.fetch_stocktwits_messages("NVDA")
        assert [c["profile"] for c in calls] == list(stocktwits._FINGERPRINTS[:2])
        assert "unavailable" not in out

    def test_the_profile_that_worked_is_tried_first_next_time(self):
        client, calls = _fake_cffi([403, 403, 200])
        with patch.object(stocktwits, "_cffi", client):
            stocktwits.fetch_stocktwits_messages("NVDA")
            stocktwits.fetch_stocktwits_messages("AMD")
        # The second fetch opens with the third profile — the one that answered.
        assert calls[3]["profile"] == stocktwits._FINGERPRINTS[2]

    def test_every_profile_refused_reports_the_status_not_a_blank(self):
        client, _ = _fake_cffi([403, 403, 403])
        with patch.object(stocktwits, "_cffi", client):
            out = stocktwits.fetch_stocktwits_messages("NVDA")
        assert out == "<stocktwits unavailable: blocked (403)>"

    def test_a_404_stops_the_walk_instead_of_burning_profiles(self):
        client, calls = _fake_cffi([404])
        with patch.object(stocktwits, "_cffi", client):
            out = stocktwits.fetch_stocktwits_messages("NOSUCHSYMBOL")
        assert len(calls) == 1
        assert out == "<stocktwits unavailable: HTTP 404>"

    def test_the_configured_proxy_reaches_the_transport(self):
        client, calls = _fake_cffi([200])
        with patch.object(stocktwits, "_cffi", client):
            stocktwits.fetch_stocktwits_messages("NVDA")
        assert calls[0]["proxies"] == stocktwits.requests_proxies()

    def test_a_transport_exception_becomes_a_placeholder_not_a_crash(self):
        class _Dead:
            @staticmethod
            def get(*a, **kw):
                raise RuntimeError("proxy refused CONNECT")

        with patch.object(stocktwits, "_cffi", _Dead):
            out = stocktwits.fetch_stocktwits_messages("NVDA")
        assert out == "<stocktwits unavailable: RuntimeError>"
