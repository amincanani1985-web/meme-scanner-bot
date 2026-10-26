import unittest
from unittest.mock import Mock, patch

import requests

import memeswap_research as research


MINT = "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump"


def profile_payload(mint=MINT, chain="solana", now=2_000.0):
    return {
        "chain": chain,
        "token": mint,
        "row": {
            "chain": chain,
            "address": mint,
            "priceTs": int((now - 12) * 1000),
            "top5": 26.5,
            "lpLockedPct": 65.0,
            "fake": {"score": 3},
            "security": {
                "honeypot": "0",
                "mintable": "1",
                "buyTax": "0",
                "sellTax": "1",
            },
        },
        "updatedAt": int((now - 18) * 1000),
        "profile": {
            "updatedAt": int((now - 20) * 1000),
            "holders": {
                "top1": 9.2,
                "top10": 28.8,
                "top20": 54.5,
            },
            "lpLockedPct": 60.0,
        },
    }


class MemeSwapResearchTests(unittest.TestCase):
    def test_normalizes_only_reported_solana_risk_fields_and_source_ages(self):
        result = research.normalize_token_profile(profile_payload(), MINT, now=2_000.0)

        self.assertIsNotNone(result)
        self.assertEqual(result["memeswap_available"], 1.0)
        self.assertEqual(result["memeswap_top1_pct"], 9.2)
        self.assertEqual(result["memeswap_top5_pct"], 26.5)
        self.assertEqual(result["memeswap_top10_pct"], 28.8)
        self.assertEqual(result["memeswap_top20_pct"], 54.5)
        self.assertEqual(result["memeswap_lp_locked_pct"], 60.0)
        self.assertEqual(result["memeswap_fake_score"], 3.0)
        self.assertEqual(result["memeswap_honeypot"], 0.0)
        self.assertEqual(result["memeswap_mintable"], 1.0)
        self.assertEqual(result["memeswap_buy_tax_value"], 0.0)
        self.assertEqual(result["memeswap_sell_tax_value"], 1.0)
        self.assertEqual(result["memeswap_price_age_seconds"], 12.0)
        self.assertEqual(result["memeswap_profile_age_seconds"], 20.0)
        self.assertEqual(result["memeswap_risk_data_available"], 1.0)
        self.assertEqual(result["memeswap_full_risk_screen_available"], 1.0)

    def test_missing_risk_fields_remain_unknown_instead_of_becoming_zero(self):
        payload = {
            "chain": "solana",
            "token": MINT,
            "row": {"chain": "solana", "address": MINT},
            "profile": {"holders": {}},
        }

        result = research.normalize_token_profile(payload, MINT, now=2_000.0)

        self.assertIsNotNone(result)
        self.assertEqual(result["memeswap_available"], 1.0)
        self.assertIsNone(result["memeswap_top10_pct"])
        self.assertIsNone(result["memeswap_honeypot"])
        self.assertIsNone(result["memeswap_price_age_seconds"])
        self.assertIsNone(result["memeswap_profile_age_seconds"])
        self.assertEqual(result["memeswap_risk_data_available"], 0.0)
        self.assertEqual(result["memeswap_full_risk_screen_available"], 0.0)

    def test_basic_lookup_row_is_not_mistaken_for_security_risk_data(self):
        payload = {
            "chain": "solana",
            "token": MINT,
            "row": {
                "chain": "solana",
                "address": MINT,
                "gatePass": False,
            },
            "lookup": "chain+dexscreener",
        }
        result = research.normalize_token_profile(payload, MINT, now=2_000.0)
        self.assertIsNotNone(result)
        self.assertEqual(result["memeswap_available"], 1.0)
        self.assertEqual(result["memeswap_gate_pass"], 0.0)
        self.assertEqual(result["memeswap_risk_data_available"], 0.0)
        self.assertEqual(result["memeswap_full_risk_screen_available"], 0.0)

    def test_rejects_wrong_mint_or_non_solana_payload(self):
        self.assertIsNone(
            research.normalize_token_profile(profile_payload(mint="different-mint"), MINT, now=2_000.0)
        )
        self.assertIsNone(
            research.normalize_token_profile(profile_payload(chain="ethereum"), MINT, now=2_000.0)
        )

    def test_disabled_enrichment_never_makes_a_network_request(self):
        with patch.dict("os.environ", {"MEMESWAP_RESEARCH_ENABLED": "0"}), \
             patch.object(research.requests, "get") as get:
            self.assertIsNone(research.fetch_token_profile(MINT))
        get.assert_not_called()

    def test_enabled_enrichment_uses_the_read_only_profile_endpoint_and_bounds_timeout(self):
        response = Mock()
        response.json.return_value = profile_payload()
        response.raise_for_status.return_value = None
        with patch.dict("os.environ", {"MEMESWAP_RESEARCH_ENABLED": "1"}), \
             patch.object(research.requests, "get", return_value=response) as get:
            result = research.fetch_token_profile(MINT, now=2_000.0)

        get.assert_called_once()
        args, kwargs = get.call_args
        self.assertEqual(args[0], f"https://memeswap.fun/api/token/solana/{MINT}")
        self.assertLessEqual(kwargs["timeout"], 3.0)
        self.assertEqual(result["memeswap_top10_pct"], 28.8)

    def test_provider_error_fails_closed_without_crashing_the_scanner(self):
        with patch.dict("os.environ", {"MEMESWAP_RESEARCH_ENABLED": "1"}), \
             patch.object(research.requests, "get", side_effect=requests.Timeout("slow")):
            self.assertIsNone(research.fetch_token_profile(MINT))


if __name__ == "__main__":
    unittest.main()
