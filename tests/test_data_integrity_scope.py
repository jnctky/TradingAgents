"""The honesty of the 「数据完整性」 block (2026-11-11 user core principle).

The BNB rerun that prompted this file had a header table saying every dependency
was 正常 while the body listed StockTwits, Reddit and the live quote as missing.
Two causes, one rule: the summary may only claim what the trace actually saw, and
an agent may only speak for the data it fetched itself.
"""

import json

from tradingagents.agents import context as context_mod
from tradingagents.agents.analysts import sentiment_analyst as sentiment_mod
from tradingagents.dataflows import data_integrity
from tradingagents.dataflows.vendors.openbb import quote as quote_mod


class TestSentimentIsTraced:
    def test_a_failed_feed_is_recorded_as_failed(self, tmp_path, monkeypatch):
        trace = tmp_path / "trace.jsonl"
        monkeypatch.setenv("TA_TRACE_FILE", str(trace))

        text = sentiment_mod._fetch_traced(
            "stocktwits", "stocktwits",
            lambda *a, **k: "<stocktwits unavailable: HTTP 404>",
            "BNB",
        )

        assert "unavailable" in text
        record = json.loads(trace.read_text(encoding="utf-8").splitlines()[0])
        assert record["category"] == "sentiment_data"
        assert record["vendor"] == "stocktwits"
        assert record["ok"] is False
        assert "404" in record["error"]

    def test_a_served_feed_is_recorded_as_ok(self, tmp_path, monkeypatch):
        trace = tmp_path / "trace.jsonl"
        monkeypatch.setenv("TA_TRACE_FILE", str(trace))

        sentiment_mod._fetch_traced("reddit", "reddit", lambda *a, **k: "### Reddit\n\n12 posts", "BNB")

        record = json.loads(trace.read_text(encoding="utf-8").splitlines()[0])
        assert record["ok"] is True
        assert record["chars"] > 0
        assert "error" not in record


class TestIntegritySummaryShowsSentiment:
    def test_sentiment_appears_as_its_own_row(self, tmp_path):
        trace = tmp_path / "trace.jsonl"
        rows = [
            {"category": "core_stock_apis", "ok": True, "chars": 900, "ms": 10},
            {"category": "technical_indicators", "ok": True, "chars": 400, "ms": 10},
            {"category": "news_data", "ok": True, "chars": 800, "ms": 10},
            {"category": "sentiment_data", "ok": False, "chars": 0, "ms": 10,
             "vendor": "stocktwits", "error": "HTTP 404"},
        ]
        trace.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

        verdict = data_integrity.summarize_trace(str(trace))

        assert verdict["categories"]["sentiment_data"]["status"] == "failed"
        # A missing social feed is reported, but it does not invalidate the
        # price-based conclusions the way a dead price feed does.
        assert verdict["complete"] is True
        assert "sentiment_data" not in verdict["missing"]
        assert "情绪" in data_integrity.markdown(verdict)


class TestCryptoSkipsEquityOnlyLookups:
    def test_quote_crosscheck_does_not_ask_the_equity_route(self, monkeypatch):
        called: list[str] = []
        monkeypatch.setattr(quote_mod, "get_quote", lambda symbol: called.append(symbol))

        block = quote_mod.build_quote_crosscheck("BNB", "2026-11-10", 680.58)

        assert "Skipped" in block
        assert called == []

    def test_identity_does_not_fetch_a_profile_for_a_coin(self, monkeypatch):
        called: list[str] = []
        monkeypatch.setattr(context_mod, "get_company_profile", lambda t: called.append(t))

        identity = context_mod._identity("BNB")

        assert identity == {"quote_type": "CRYPTO"}
        assert called == []


class TestScopeOfTheIronRule:
    def test_agents_are_told_not_to_speak_for_other_analysts(self):
        text = context_mod.get_data_integrity_instruction()

        # The BNB fundamentals write-up announced "价格数据完全缺失" from a
        # toolset that never had a price tool. The instruction now forbids it.
        assert "只声明你自己这一轮实际调用的数据" in text
        assert "本次分析无价格数据" in text
