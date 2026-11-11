"""数据完整性汇总（2026-11-11 用户核心原则）的行为测试。

原则：技术性取数失败只是技术问题——必须如实呈现为「数据不完整」，
禁止用替代数据/替代方式（事件触发止损、账户级回撤线等）补齐价格类结论。
本模块是那个原则的机器实现，这里的测试钉住它的判定。
"""

import json

from tradingagents.dataflows.data_integrity import (
    failed_price_categories,
    header_line,
    markdown,
    summarize_trace,
)


def _trace(tmp_path, records):
    path = tmp_path / "trace.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return path


def _rec(category, ok=True, chars=500):
    return {"category": category, "ms": 10, "chars": chars if ok else 0,
            "ok": ok, "tool": "x", "vendor": "v", "chain": ["v"]}


def test_all_ok_is_complete(tmp_path):
    records = [_rec("core_stock_apis"), _rec("technical_indicators"), _rec("news_data")]
    verdict = summarize_trace(_trace(tmp_path, records))
    assert verdict["complete"] is True
    assert verdict["missing"] == []
    assert "数据完整" in markdown(verdict)
    assert header_line(verdict) is None


def test_zero_byte_200s_are_failures_not_ok(tmp_path):
    # The BNB day: the price pipeline answered 200 with 0-byte bodies. Those
    # are failures — counting them as ok would hide the blackout.
    records = [_rec("core_stock_apis", ok=True, chars=0)]
    verdict = summarize_trace(_trace(tmp_path, records))
    assert verdict["categories"]["core_stock_apis"]["status"] == "failed"
    assert verdict["price_ok"] is False


def test_price_failure_bars_priced_advice(tmp_path):
    records = [_rec("core_stock_apis", ok=False), _rec("technical_indicators", ok=False),
               _rec("news_data"), _rec("macro_data")]
    verdict = summarize_trace(_trace(tmp_path, records))
    assert verdict["complete"] is False
    assert set(failed_price_categories(verdict)) == {"core_stock_apis", "technical_indicators"}
    text = markdown(verdict)
    assert "数据不完整" in text
    assert "事件触发" in text  # the banned substitutions are named as banned
    assert "无法给出" in text
    header = header_line(verdict)
    assert header and "不完整" in header


def test_partial_news_is_a_gap_too(tmp_path):
    records = [_rec("core_stock_apis"), _rec("technical_indicators"),
               _rec("news_data"), _rec("news_data", ok=False)]
    verdict = summarize_trace(_trace(tmp_path, records))
    assert verdict["categories"]["news_data"]["status"] == "partial"
    assert verdict["complete"] is False


def test_crypto_without_fundamentals_tools_is_not_a_gap(tmp_path):
    # A crypto run never attempted fundamentals: nothing missing there.
    records = [_rec("core_stock_apis"), _rec("technical_indicators"), _rec("news_data")]
    verdict = summarize_trace(_trace(tmp_path, records))
    assert verdict["complete"] is True
    assert "fundamental_data" not in verdict["missing"]


def test_attempted_but_failed_fundamentals_is_a_gap(tmp_path):
    records = [_rec("core_stock_apis"), _rec("technical_indicators"),
               _rec("news_data"), _rec("fundamental_data", ok=False)]
    verdict = summarize_trace(_trace(tmp_path, records))
    assert verdict["complete"] is False
    assert "fundamental_data" in verdict["missing"]


def test_withheld_prediction_markets_is_design_not_outage(tmp_path):
    records = [_rec("core_stock_apis"), _rec("technical_indicators"), _rec("news_data"),
               {"category": "prediction_markets", "ms": 0, "chars": 200, "ok": True,
                "tool": "get_prediction_markets", "vendor": "polymarket", "chain": [],
                "sample": "DATA_UNAVAILABLE: prediction-market odds are withheld for "
                          "2026-11-10 — Polymarket serves only live odds."}]
    verdict = summarize_trace(_trace(tmp_path, records))
    # Design answer, not an outage: core completeness stands.
    assert verdict["complete"] is True
    assert verdict["categories"]["prediction_markets"]["status"] == "withheld"


def test_a_refusal_sentinel_is_never_counted_as_a_success(tmp_path):
    # The failure this pins: the vendor answered 200 with "DATA_UNAVAILABLE ...",
    # the trace counted it ok, and the table claimed 预测市场 正常 while the
    # analyst body said the odds were withheld. Both surfaces must agree.
    records = [_rec("core_stock_apis"), _rec("technical_indicators"), _rec("news_data"),
               {"category": "prediction_markets", "ms": 0, "chars": 200, "ok": True,
                "tool": "get_prediction_markets", "vendor": "polymarket", "chain": [],
                "sample": "DATA_UNAVAILABLE: Polymarket could not be reached."}]
    verdict = summarize_trace(_trace(tmp_path, records))
    bucket = verdict["categories"]["prediction_markets"]
    assert bucket["ok"] == 0 and bucket["withheld"] == 1
    assert bucket["status"] == "withheld"
    assert "按设计不提供" in markdown(verdict)


def test_the_conclusion_names_optional_gaps_instead_of_claiming_a_clean_run(tmp_path):
    # Sentiment feeds die far more often than prices. "数据完整" next to a 失败
    # row read as a contradiction, so the conclusion has to carry both facts.
    records = [_rec("core_stock_apis"), _rec("technical_indicators"), _rec("news_data"),
               _rec("sentiment_data", ok=False)]
    verdict = summarize_trace(_trace(tmp_path, records))
    assert verdict["complete"] is True
    text = markdown(verdict)
    assert "核心数据（行情、技术指标、新闻）完整" in text
    assert "情绪（StockTwits / Reddit） 失败" in text


def test_calendar_and_politics_are_traced_so_the_table_cannot_stay_silent(tmp_path):
    # Both are built outside the router (date_context); before they were traced
    # the body could say "日历不可用" while every table row read 正常.
    records = [_rec("core_stock_apis"), _rec("technical_indicators"), _rec("news_data"),
               _rec("events", ok=False), _rec("politics", ok=False)]
    verdict = summarize_trace(_trace(tmp_path, records))
    assert verdict["categories"]["events"]["status"] == "failed"
    assert verdict["categories"]["politics"]["status"] == "failed"
    text = markdown(verdict)
    assert "经济日历 失败" in text and "政治头条 失败" in text
    # Soft data never blocks a priced conclusion.
    assert verdict["complete"] is True


def test_missing_trace_file_returns_none(tmp_path):
    assert summarize_trace(tmp_path / "nope.jsonl") is None
    assert summarize_trace(None) is None


def test_crypto_fundamentals_answered_not_applicable_is_not_a_failure(tmp_path):
    # The router answers crypto fundamentals with a "this does not apply"
    # sentinel instead of calling an equity vendor. That must read as 不适用,
    # not as a fetch failure — otherwise every coin report carries a phantom
    # "数据不完整".
    records = [_rec("core_stock_apis"), _rec("technical_indicators"), _rec("news_data"),
               {"category": "fundamental_data", "ms": 0, "chars": 260, "ok": True,
                "tool": "get_fundamentals", "vendor": "not_applicable",
                "chain": ["not_applicable"]}]
    verdict = summarize_trace(_trace(tmp_path, records))

    assert verdict["complete"] is True
    assert verdict["categories"]["fundamental_data"]["status"] == "na"
    assert "fundamental_data" not in verdict["missing"]
    assert "不适用" in markdown(verdict)
