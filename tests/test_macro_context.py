"""The shared macro backdrop: transforms, regime rules, caching, and degradation.

Everything here runs offline — the platform fetch is stubbed at the block level,
so these tests describe the logic that has to hold whatever the data says.
"""

from __future__ import annotations

import json

import pytest

from tradingagents.dataflows.vendors import macro_context as mc


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(mc, "snapshot_dir", lambda: tmp_path)
    return tmp_path


def _stub(monkeypatch, blocks: dict[str, list[tuple[str, float]]]):
    """Serve the given per-series points regardless of the requested window."""

    def fake_fetch(series_ids, start, end):
        return {sid: list(blocks.get(sid, [])) for sid in series_ids}

    monkeypatch.setattr(mc, "_fetch_block", fake_fetch)


def _daily(series: str, values: list[float], start_day: int = 1) -> list[tuple[str, float]]:
    return [(f"2026-10-{start_day + i:02d}", v) for i, v in enumerate(values)]


# --------------------------------------------------------------------------- #
# transforms
# --------------------------------------------------------------------------- #

def test_yoy_keys_on_the_month_not_the_exact_date():
    """A late release moves the day; the year-over-year still has to match."""
    points = [("2025-09-01", 100.0), ("2026-08-01", 101.0), ("2026-09-02", 110.0)]
    assert mc._yoy(points) == [("2026-09-02", pytest.approx(10.0))]


def test_yoy_drops_months_with_no_year_earlier_point():
    """One year of history is not enough for the first observation."""
    assert mc._yoy([("2026-09-01", 100.0)]) == []


def test_mom_ignores_a_gap_that_is_not_one_month():
    """A two-month gap must not be reported as a one-month change."""
    points = [("2026-05-01", 100.0), ("2026-07-01", 130.0)]
    assert mc._mom(points) == []


def test_mom_reports_consecutive_months():
    points = [("2026-08-01", 100.0), ("2026-09-01", 130.0)]
    assert mc._mom(points) == [("2026-09-01", pytest.approx(30.0))]


# --------------------------------------------------------------------------- #
# readings
# --------------------------------------------------------------------------- #

def test_readings_drop_observations_after_the_analysis_date(monkeypatch):
    """A run dated 11-05 must not read a print published on 11-06."""
    _stub(monkeypatch, {"DGS10": [("2026-11-04", 4.80), ("2026-11-06", 9.99)]})
    readings = {r["id"]: r for r in mc.fetch_readings("2026-11-05")}
    assert readings["us10y"]["value"] == 4.80
    assert readings["us10y"]["date"] == "2026-11-04"


def test_a_series_with_no_observation_is_reported_not_dropped(monkeypatch):
    """An absent reading is information; silently omitting the row hides it."""
    _stub(monkeypatch, {})
    readings = mc.fetch_readings("2026-11-05")
    assert len(readings) == len(mc._READINGS)
    assert all(r["available"] is False for r in readings)
    assert {"id", "label", "series", "reason"} <= set(readings[0])


def test_monthly_series_compare_against_the_previous_month(monkeypatch):
    """20 points back on a monthly series would be almost two years."""
    _stub(monkeypatch, {"PCEPILFE": [
        ("2025-08-01", 100.0), ("2025-09-01", 100.0),
        ("2026-08-01", 130.0), ("2026-09-01", 132.0),
    ]})
    core = {r["id"]: r for r in mc.fetch_readings("2026-11-05")}["core_pce_yoy"]
    assert core["value"] == pytest.approx(32.0)          # YoY vs 2025-09
    assert core["recent_change"] == pytest.approx(2.0)   # vs 2026-08, not 20 points back


# --------------------------------------------------------------------------- #
# regime rules
# --------------------------------------------------------------------------- #

def _reading(id_, value, recent=None, start=None, date="2026-11-04"):
    return {"id": id_, "label": id_, "series": id_, "available": True, "value": value,
            "date": date, "recent_change": recent, "recent_start": start or "2026-10-06",
            "window_change": recent or 0.0, "window_start": "2026-07-01", "prev": value}


def test_elevated_vix_reads_risk_off():
    regime = mc.derive_regime([_reading("vix", 26.0, -1.0)])
    assert regime["risk_appetite"]["tag"] == "risk_off"
    assert "26.00" in regime["risk_appetite"]["basis"]


def test_a_calm_vix_that_just_jumped_is_not_risk_on():
    """A level of 13 says calm; a 6-point jump says that calm just ended."""
    regime = mc.derive_regime([_reading("vix", 13.0, 6.0)])
    assert regime["risk_appetite"]["tag"] == "risk_off"


def test_an_inverted_curve_is_named_inverted():
    regime = mc.derive_regime([_reading("curve_2s10s", -0.35, -0.05)])
    assert regime["curve_shape"]["tag"] == "inverted"
    assert "-0.35" in regime["curve_shape"]["basis"]


def test_a_falling_policy_rate_reads_easing():
    regime = mc.derive_regime([_reading("fed_funds", 3.88, -0.25)])
    assert regime["fed_stance"]["tag"] == "easing"


def test_missing_readings_produce_unknown_not_a_guess():
    regime = mc.derive_regime([])
    assert {v["tag"] for v in regime.values()} == {"unknown"}


# --------------------------------------------------------------------------- #
# snapshot caching
# --------------------------------------------------------------------------- #

def test_a_second_build_reads_the_snapshot_instead_of_refetching(cache_dir, monkeypatch):
    calls = []

    def fake_fetch(series_ids, start, end):
        calls.append(1)
        return {sid: _daily(sid, [1.0, 2.0]) for sid in series_ids}

    monkeypatch.setattr(mc, "_fetch_block", fake_fetch)

    first = mc.build_snapshot("2026-11-05")
    second = mc.build_snapshot("2026-11-05")

    assert len(calls) == 2                      # one request per block, only the first time
    assert first["data_signature"] == second["data_signature"]
    assert (cache_dir / "2026-11-05.json").exists()


def test_refresh_refetches_even_when_a_snapshot_exists(cache_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(mc, "_fetch_block", lambda ids, s, e: (calls.append(1), {i: [] for i in ids})[1])

    mc.build_snapshot("2026-11-05")
    before = len(calls)
    mc.build_snapshot("2026-11-05", refresh=True)

    assert len(calls) > before


def test_a_corrupt_snapshot_is_rebuilt_not_trusted(cache_dir, monkeypatch):
    (cache_dir / "2026-11-05.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(mc, "_fetch_block", lambda ids, s, e: {i: [] for i in ids})

    snapshot = mc.build_snapshot("2026-11-05")

    assert snapshot["trade_date"] == "2026-11-05"
    assert json.loads((cache_dir / "2026-11-05.json").read_text(encoding="utf-8"))["version"] == mc.SNAPSHOT_VERSION


def test_a_narrative_is_reused_only_under_the_same_model(cache_dir, monkeypatch):
    monkeypatch.setattr(mc, "_fetch_block", lambda ids, s, e: {i: [] for i in ids})
    mc.build_snapshot("2026-11-05")

    assert mc.remember_report("2026-11-05", "the backdrop")["report"] == "the backdrop"
    assert mc.narrative_is_reusable(mc.load_snapshot("2026-11-05"))

    monkeypatch.setattr(mc, "model_signature", lambda: "other-provider/other-model/English")
    assert mc.narrative_is_reusable(mc.load_snapshot("2026-11-05")) is False


# --------------------------------------------------------------------------- #
# rendering and the node
# --------------------------------------------------------------------------- #

def test_the_section_pushes_narrative_headings_below_the_section(cache_dir, monkeypatch):
    monkeypatch.setattr(mc, "_fetch_block", lambda ids, s, e: {i: [] for i in ids})
    from tradingagents.report_assets import macro_section_markdown

    markdown = macro_section_markdown({
        "macro_report": "## Policy\n\nRates fell.\n\n### What would flip it\n\nA hot print.",
        "macro_snapshot": {},
    })

    assert markdown.startswith("### Policy")
    assert "#### What would flip it" in markdown


def test_no_backdrop_means_no_section(cache_dir, monkeypatch):
    """An empty heading is worse than an absent one."""
    from tradingagents.report_assets import macro_section_markdown

    assert macro_section_markdown({"macro_report": "", "macro_snapshot": {}}) == ""


def test_the_node_skips_the_model_when_a_narrative_is_cached(cache_dir, monkeypatch):
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from tradingagents.agents.analysts.macro_analyst import create_macro_analyst

    monkeypatch.setattr(mc, "_fetch_block",
                        lambda ids, s, e: {i: _daily(i, [1.0, 2.0]) for i in ids})

    calls = []

    class Counting(FakeListChatModel):
        def invoke(self, *args, **kwargs):
            calls.append(1)
            return super().invoke(*args, **kwargs)

    node = create_macro_analyst(Counting(responses=["the backdrop"] * 4))

    first = node({"trade_date": "2026-11-05"})
    second = node({"trade_date": "2026-11-05"})

    assert len(calls) == 1                      # the batch pays for one narrative, not twenty
    assert first["macro_report"] == second["macro_report"] == "the backdrop"
    assert second["macro_context"]


def test_the_node_survives_a_dead_platform(monkeypatch, cache_dir):
    """A missing backdrop must not stop a 20-ticker batch at ticker one."""
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from tradingagents.agents.analysts.macro_analyst import create_macro_analyst

    def boom(series_ids, start, end):
        raise RuntimeError("platform down")

    monkeypatch.setattr(mc, "_fetch_block", boom)
    node = create_macro_analyst(FakeListChatModel(responses=["unused"]))

    out = node({"trade_date": "2026-11-05"})

    assert out["macro_report"] == ""
    assert out["macro_snapshot"] == {}
    assert out["macro_context"] == ""


# --------------------------------------------------------------------------- #
# the openbb macro aliases the backdrop rebuilt
# --------------------------------------------------------------------------- #

def test_the_fred_route_keeps_published_units(monkeypatch):
    """FRED already prints 4.90 for a 4.90% yield; rescaling would show 490%."""
    from tradingagents.dataflows.vendors.openbb import macro as om

    monkeypatch.setattr(om, "get_json", lambda *a, **k: [
        {"date": "2026-11-03", "DGS2": 4.80}, {"date": "2026-11-04", "DGS2": 4.90},
    ])
    out = om.get_macro_data("2y_treasury", "2026-11-05")

    assert "**Latest:** 4.90 (2026-11-04)" in out


def test_core_pce_is_served_now(monkeypatch):
    """It used to be FRED-key-only and returned an error on the keyless path."""
    from tradingagents.dataflows.vendors.openbb import macro as om

    rows = [{"date": "2025-09-01", "PCEPILFE": 100.0}, {"date": "2026-09-01", "PCEPILFE": 103.0}]
    monkeypatch.setattr(om, "get_json", lambda *a, **k: rows)
    out = om.get_macro_data("core_pce", "2026-11-05")

    assert "**Latest:** 3.00" in out
    assert "FRED_API_KEY" not in out
