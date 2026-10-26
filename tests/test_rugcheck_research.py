import os
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import rugcheck_research as research


MINT = "42nvgmxdtEyHLD1qJUeTyexenLpCGoZsLfQPQnsdpump"


def report_payload(mint=MINT):
    return {
        "mint": mint,
        "detectedAt": "2026-10-26T15:11:13.635210507Z",
        "token": {
            "mintAuthority": None,
            "freezeAuthority": None,
            "supply": "990422497383508",
            "decimals": 6,
            "isInitialized": True,
        },
        "token_extensions": {
            "mintCloseAuthority": None,
            "tokenMetadata": {"authority": None},
            "transferFeeConfig": None,
        },
        "tokenMeta": {"name": "Fart coin", "symbol": "Fartcoin", "mutable": False},
        "topHolders": [
            {"pct": 6.0, "insider": False},
            {"pct": 2.0, "insider": False},
            {"pct": 1.0, "insider": True},
            {"pct": 0.5, "insider": False},
            {"pct": 0.4, "insider": False},
            {"pct": 0.3, "insider": False},
            {"pct": 0.2, "insider": False},
            {"pct": 0.1, "insider": False},
            {"pct": 0.1, "insider": False},
            {"pct": 0.1, "insider": False},
            {"pct": 0.08, "insider": False},
            {"pct": 0.07, "insider": False},
            {"pct": 0.01, "insider": False},
            {"pct": 0.01, "insider": False},
            {"pct": 0.01, "insider": False},
            {"pct": 0.01, "insider": False},
            {"pct": 0.01, "insider": False},
            {"pct": 0.01, "insider": False},
            {"pct": 0.01, "insider": False},
            {"pct": 0.01, "insider": False},
        ],
        "risks": [
            {"name": "Copycat token", "level": "warn", "score": 2000},
            {"name": "Example severe issue", "level": "danger", "score": 100},
        ],
        "score": 2100,
        "score_normalised": 29,
        "totalMarketLiquidity": 65000.0,
        "totalHolders": 6649,
        "rugged": False,
        "markets": [
            {"lp": {"lpLockedPct": 20.0, "lpLockedUSD": 1000.0}},
            {"lp": {"lpLockedPct": 100.0, "lpLockedUSD": 64000.0}},
        ],
    }


class RugCheckResearchTests(unittest.TestCase):
    def test_normalizes_public_full_report_without_turning_unknowns_into_safe_zeros(self):
        now = datetime(2026, 10, 26, 15, 12, 0, tzinfo=timezone.utc).timestamp()
        result = research.normalize_token_report(report_payload(), MINT, now=now)
        self.assertEqual(result["rugcheck_available"], 1.0)
        self.assertEqual(result["rugcheck_risk_data_available"], 1.0)
        self.assertEqual(result["rugcheck_full_risk_screen_available"], 1.0)
        self.assertEqual(result["rugcheck_mint_authority_active"], 0.0)
        self.assertEqual(result["rugcheck_freeze_authority_active"], 0.0)
        self.assertEqual(result["rugcheck_mint_close_authority_active"], 0.0)
        self.assertAlmostEqual(result["rugcheck_top1_pct"], 6.0)
        self.assertAlmostEqual(result["rugcheck_top10_pct"], 10.7)
        self.assertAlmostEqual(result["rugcheck_top20_pct"], 10.93)
        self.assertAlmostEqual(result["rugcheck_insider_pct"], 1.0)
        self.assertAlmostEqual(result["rugcheck_lp_locked_pct"], 100.0)
        self.assertAlmostEqual(result["rugcheck_lp_locked_usd"], 64000.0)
        self.assertEqual(result["rugcheck_warning_risk_count"], 1.0)
        self.assertEqual(result["rugcheck_danger_risk_count"], 1.0)
        self.assertEqual(result["rugcheck_critical_risk_count"], 0.0)
        self.assertEqual(result["rugcheck_rugged"], 0.0)
        self.assertEqual(result["rugcheck_score_normalised"], 29.0)
        self.assertGreaterEqual(result["rugcheck_report_age_seconds"], 0.0)

    def test_basic_or_incomplete_report_is_available_but_not_full_risk_screen(self):
        payload = {
            "mint": MINT,
            "token": {"mintAuthority": None, "freezeAuthority": None},
            "risks": [],
            "score": 10,
            "rugged": False,
        }
        result = research.normalize_token_report(payload, MINT, now=1_798_000_000.0)
        self.assertIsNotNone(result)
        self.assertEqual(result["rugcheck_available"], 1.0)
        self.assertEqual(result["rugcheck_risk_data_available"], 1.0)
        self.assertEqual(result["rugcheck_full_risk_screen_available"], 0.0)
        self.assertIsNone(result["rugcheck_top1_pct"])
        self.assertIsNone(result["rugcheck_top10_pct"])
        self.assertIsNone(result["rugcheck_lp_locked_pct"])
        self.assertIsNone(result["rugcheck_report_age_seconds"])

    def test_wrong_mint_and_invalid_payload_are_rejected(self):
        self.assertIsNone(research.normalize_token_report(report_payload("wrong-mint"), MINT))
        self.assertIsNone(research.normalize_token_report([], MINT))
        self.assertIsNone(research.normalize_token_report({"mint": MINT}, "different-mint"))

    def test_disabled_research_never_makes_a_network_request(self):
        with patch.dict(os.environ, {"RUGCHECK_RESEARCH_ENABLED": "0"}), \
             patch.object(research.requests, "get", side_effect=AssertionError("disabled research must not request")) as get:
            self.assertIsNone(research.fetch_token_report(MINT))
            get.assert_not_called()

    def test_enabled_research_uses_read_only_report_endpoint_and_bounded_timeout(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = report_payload()
        with patch.dict(os.environ, {"RUGCHECK_RESEARCH_ENABLED": "1"}), \
             patch.object(research.requests, "get", return_value=response) as get:
            result = research.fetch_token_report(MINT, timeout=100)
        self.assertEqual(get.call_count, 1)
        args, kwargs = get.call_args
        self.assertEqual(args[0], research.RUGCHECK_REPORT_URL.format(mint=MINT))
        self.assertLessEqual(kwargs["timeout"], 8)
        self.assertEqual(kwargs["headers"]["Accept"], "application/json")
        self.assertEqual(result["rugcheck_available"], 1.0)

    def test_provider_failure_fails_closed_without_crashing(self):
        with patch.dict(os.environ, {"RUGCHECK_RESEARCH_ENABLED": "1"}), \
             patch.object(research.requests, "get", side_effect=research.requests.Timeout("slow")):
            self.assertIsNone(research.fetch_token_report(MINT))

    def test_only_solana_mint_report_is_requested_not_transaction_execution(self):
        self.assertEqual(research.RUGCHECK_REPORT_URL, "https://api.rugcheck.xyz/v1/tokens/{mint}/report")
        self.assertTrue(research.is_enabled())


if __name__ == "__main__":
    unittest.main()
