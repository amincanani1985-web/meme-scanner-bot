import unittest
import token_quality_screen as screen


def pair(**overrides):
    row = {
        "mint": "mint-a", "token_name": "Example", "token_symbol": "EX",
        "pair_address": "pair-a", "pair_url": "https://dexscreener.com/solana/pair-a",
        "source": "dexscreener", "price_usd": 0.001, "liquidity_usd": 50_000,
        "market_cap_usd": 500_000, "volume_h6_usd": 100_000, "volume_h24_usd": 250_000,
        "price_change_m5_pct": 2, "price_change_h1_pct": 10,
        "price_change_h6_pct": 15, "price_change_h24_pct": 25,
        "txns_h6": {"buys": 800, "sells": 600}, "boosts_active": 0,
        "observed_at": "2026-10-27T10:00:00Z",
    }
    row.update(overrides)
    return row


class TokenQualityScreenTests(unittest.TestCase):
    def test_large_boost_does_not_clear_low_liquidity_severe_drawdown(self):
        result = screen.screen_pair(pair(
            liquidity_usd=2_600, market_cap_usd=100_000,
            price_change_h24_pct=-95.2, volume_h24_usd=2_590_000, boosts_active=500,
        ))
        flags = {flag["code"] for flag in result["flags"]}
        self.assertIn("liquidity_below_floor", flags)
        self.assertIn("very_thin_pool", flags)
        self.assertIn("severe_24h_drawdown", flags)
        self.assertIn("promotion_not_quality_evidence", flags)
        self.assertGreaterEqual(result["risk_score"], 60)
        self.assertFalse(result["eligible_for_entry"])
        self.assertFalse(result["live_trading_authorized"])

    def test_sell_dominant_window_is_flagged_when_sample_is_large_enough(self):
        result = screen.screen_pair(pair(txns_h6={"buys": 3_260, "sells": 33_097}))
        self.assertIn("sell_pressure_h6", {flag["code"] for flag in result["flags"]})
        self.assertGreaterEqual(result["risk_score"], 20)

    def test_extreme_turnover_and_short_term_spike_are_flagged(self):
        result = screen.screen_pair(pair(
            volume_h24_usd=1_500_000, liquidity_usd=30_000,
            price_change_h1_pct=60, price_change_m5_pct=22,
        ))
        flags = {flag["code"] for flag in result["flags"]}
        self.assertIn("extreme_24h_turnover", flags)
        self.assertIn("extreme_short_term_momentum", flags)
        h6 = screen.screen_pair(pair(price_change_m5_pct=0, price_change_h1_pct=0, price_change_h6_pct=75))
        self.assertIn("extreme_short_term_momentum", {flag["code"] for flag in h6["flags"]})

    def test_non_finite_values_are_not_used_as_market_evidence(self):
        result = screen.screen_pair(pair(liquidity_usd=float("nan"), price_usd="inf", price_change_h24_pct="NaN"))
        self.assertEqual(result["risk_level"], "insufficient_data")
        self.assertIn("missing_liquidity_data", {flag["code"] for flag in result["flags"]})
        self.assertIn("missing_or_invalid_price", {flag["code"] for flag in result["flags"]})

    def test_report_deduplicates_pairs_and_never_outputs_trade_signal(self):
        report = screen.screen_report({
            "generated_at": "2026-10-27T10:00:00Z",
            "provider_status": [{"provider": "dexscreener", "status": "ok"}],
            "records": [pair(), pair(), {**pair(pair_address="pair-b"), "liquidity_usd": 2_000}],
        })
        self.assertEqual(report["pairs_screened"], 2)
        self.assertFalse(report["trade_signal"])
        self.assertFalse(report["live_trading_authorized"])
        self.assertTrue(all(item["eligible_for_entry"] is False for item in report["pairs"]))

    def test_unconfigured_or_empty_source_is_not_reported_as_safe(self):
        report = screen.screen_report({"records": [], "provider_status": [{"provider": "dexscreener", "status": "error"}]})
        self.assertEqual(report["status"], "no_dexscreener_records")
        self.assertEqual(report["pairs_screened"], 0)
        self.assertEqual(report["risk_level_counts"]["insufficient_data"], 0)


if __name__ == "__main__":
    unittest.main()
