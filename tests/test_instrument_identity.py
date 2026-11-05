"""Tests for deterministic instrument-identity resolution (#814) and the
context-anchored message placeholder (#888)."""

import unittest
from unittest.mock import patch

import pytest

from tradingagents.agents.context import (
    _identity,
    build_instrument_context,
    get_instrument_context_from_state,
    resolve_instrument_identity,
)


@pytest.mark.unit
class ResolveInstrumentIdentityTests(unittest.TestCase):
    """The resolver reads the akshare sidecar profile feed first and the OpenBB
    platform second; both are patched here so a case never reaches the network."""

    def setUp(self):
        _identity.cache_clear()

    def _patch_sidecar(self, **kwargs):
        return patch("tradingagents.agents.context.get_company_profile", **kwargs)

    def _patch_openbb(self, **kwargs):
        return patch("tradingagents.dataflows.vendors.openbb.get_company_profile", **kwargs)

    def test_resolves_company_metadata_from_the_profile_feed(self):
        with self._patch_sidecar() as mock:
            mock.return_value = {
                "longName": "TOTO LTD.",
                "shortName": "TOTO",
                "sector": "Industrials",
                "industry": "Building Products & Equipment",
                "exchange": "PNK",
                "quoteType": "EQUITY",
            }
            identity = resolve_instrument_identity("totdy")
        mock.assert_called_once_with("totdy")
        self.assertEqual(identity["company_name"], "TOTO LTD.")
        self.assertEqual(identity["sector"], "Industrials")
        self.assertEqual(identity["industry"], "Building Products & Equipment")
        self.assertEqual(identity["exchange"], "PNK")

    def test_falls_back_to_short_name(self):
        with self._patch_sidecar() as mock:
            mock.return_value = {"shortName": "TOTO", "sector": "Industrials"}
            identity = resolve_instrument_identity("TOTDY")
        self.assertEqual(identity["company_name"], "TOTO")

    def test_skips_placeholder_values(self):
        with self._patch_sidecar() as mock:
            mock.return_value = {"longName": "  ", "sector": "None", "industry": "n/a"}
            identity = resolve_instrument_identity("TOTDY")
        self.assertEqual(identity, {})

    def test_fails_open_on_exception(self):
        with self._patch_sidecar(side_effect=RuntimeError("rate limited")), \
                self._patch_openbb(side_effect=RuntimeError("rate limited")):
            self.assertEqual(resolve_instrument_identity("TOTDY"), {})

    def test_a_failed_lookup_is_asked_again(self):
        # A long process (a backtest) must not run every later cell without an
        # identity because one request failed.
        with self._patch_sidecar() as mock, self._patch_openbb(side_effect=RuntimeError("down")):
            mock.side_effect = TimeoutError()
            self.assertEqual(resolve_instrument_identity("TOTDY"), {})
            mock.side_effect = None
            mock.return_value = {"longName": "TOTO LTD."}
            self.assertEqual(resolve_instrument_identity("TOTDY")["company_name"], "TOTO LTD.")
        self.assertEqual(mock.call_count, 2)

    def test_result_is_cached(self):
        with self._patch_sidecar() as mock:
            mock.return_value = {"longName": "TOTO LTD."}
            first = resolve_instrument_identity("TOTDY")
            second = resolve_instrument_identity("TOTDY")
        mock.assert_called_once()  # second call served from cache
        self.assertEqual(first, second)


@pytest.mark.unit
class BuildInstrumentContextTests(unittest.TestCase):
    def test_mentions_exact_symbol_without_identity(self):
        context = build_instrument_context("7203.T")
        self.assertIn("7203.T", context)
        self.assertIn("exchange suffix", context)
        self.assertNotIn("Resolved identity", context)

    def test_injects_resolved_identity(self):
        context = build_instrument_context(
            "TOTDY", "stock",
            {
                "company_name": "TOTO LTD.",
                "sector": "Industrials",
                "industry": "Building Products & Equipment",
                "exchange": "PNK",
            },
        )
        self.assertIn("Company: TOTO LTD.", context)
        self.assertIn("Industrials / Building Products & Equipment", context)
        self.assertIn("Exchange: PNK", context)
        self.assertIn("Do not substitute a different company", context)

    def test_crypto_uses_name_label_and_keeps_hint(self):
        context = build_instrument_context(
            "BTC-USD", "crypto", {"company_name": "Bitcoin USD"}
        )
        self.assertIn("Name: Bitcoin USD", context)
        self.assertIn("crypto asset rather than a company", context)


@pytest.mark.unit
class GetInstrumentContextFromStateTests(unittest.TestCase):
    def test_prefers_precomputed_context(self):
        state = {"company_of_interest": "TOTDY", "instrument_context": "PRECOMPUTED"}
        self.assertEqual(get_instrument_context_from_state(state), "PRECOMPUTED")

    def test_fallback_is_network_free_ticker_only(self):
        # No instrument_context and no yfinance call — must not hit the network.
        with patch("tradingagents.dataflows.vendors.yahoo.market.yf.Ticker") as mock:
            context = get_instrument_context_from_state(
                {"company_of_interest": "NVDA", "asset_type": "stock"}
            )
        mock.assert_not_called()
        self.assertIn("NVDA", context)

    def test_fallback_respects_asset_type(self):
        context = get_instrument_context_from_state(
            {"company_of_interest": "BTC-USD", "asset_type": "crypto"}
        )
        self.assertIn("crypto asset", context)


if __name__ == "__main__":
    unittest.main()
