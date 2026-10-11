import time
import unittest
from unittest.mock import patch

import meme_scanner_cycle as scanner


class HardenedImprovementTests(unittest.TestCase):
    def test_normalize_state_preserves_valid_values_and_repairs_bad_fields(self):
        state = scanner.normalize_state({
            "equity": "500", "positions": [], "blacklist": "oops", "cooldowns": {},
            "trade_count": "3", "scan_count": "7", "candidate_observations": {},
            "strategy": {"active_threshold": "82.5"}, "learning": {"trades": "2", "wins": "1"},
        })
        self.assertEqual(state["equity"], 500.0)
        self.assertEqual(state["trade_count"], 3)
        self.assertEqual(state["scan_count"], 7)
        self.assertEqual(state["strategy"]["active_threshold"], 82.5)
        self.assertEqual(state["learning"]["trades"], 2)
        self.assertEqual(state["blacklist"], [])

    def test_entry_slippage_is_applied(self):
        state = {"equity": 1000.0, "positions": {}, "blacklist": [], "cooldowns": {}}
        pair = {"priceUsd": "1.0", "liquidity": {"usd": 100000}, "baseToken": {"symbol": "TEST"}}
        self.assertTrue(scanner.open_position(state, "mint", pair, {}, 85, {}))
        self.assertAlmostEqual(state["positions"]["mint"]["entry_price"], 1.015)

    def test_stop_loss_sets_cooldown(self):
        state = {"equity": 1000.0, "trade_count": 0, "positions": {
            "mint": {"symbol": "TEST", "entry_price": 1.0, "amount_usd": 10.0, "signal_score": 85}
        }, "blacklist": [], "cooldowns": {}}
        with patch.object(scanner, "append_learning_observation"), patch.object(scanner, "append_trade"):
            scanner.close_position(state, "mint", 0.8, "hard_stop_loss")
        self.assertGreater(state["cooldowns"]["mint"], time.time())
        self.assertNotIn("mint", state["positions"])

    def test_liquidity_exit_blacklists_token(self):
        state = {"equity": 1000.0, "trade_count": 0, "positions": {
            "mint": {"symbol": "TEST", "entry_price": 1.0, "amount_usd": 10.0, "signal_score": 85}
        }, "blacklist": [], "cooldowns": {}}
        with patch.object(scanner, "append_learning_observation"), patch.object(scanner, "append_trade"):
            scanner.close_position(state, "mint", 0.9, "liquidity_drained")
        self.assertIn("mint", state["blacklist"])

    def test_missing_price_data_does_not_reuse_stale_peak(self):
        state = {"equity": 1000.0, "trade_count": 0, "positions": {
            "mint": {"symbol": "TEST", "entry_price": 1.0, "peak_price": 1.2, "amount_usd": 10.0,
                     "signal_score": 85, "entry_time": 0.0}
        }, "blacklist": [], "cooldowns": {}}
        with patch.object(scanner, "fetch_dex_pair", return_value=None), \
             patch.object(scanner, "append_learning_observation"), patch.object(scanner, "append_trade"):
            scanner.manage_open_positions(state)
        self.assertNotIn("mint", state["positions"])
        self.assertEqual(state["equity"], 990.0)

    def test_legacy_strategy_migrates_to_hardened_threshold(self):
        state = {"strategy": {"active_threshold": 60.0, "rollback_threshold": 60.0,
                              "candidate_threshold": None, "status": "waiting_for_history"}}
        scanner.normalize_legacy_strategy_baseline(state)
        self.assertEqual(state["strategy"]["active_threshold"], 80.0)
        self.assertEqual(state["strategy"]["rollback_threshold"], 80.0)

    def test_market_filter_rejects_missing_pair_age(self):
        pair = {"liquidity": {"usd": 100000}, "volume": {"h1": 50000},
                "marketCap": 250000, "txns": {"m5": {"buys": 30, "sells": 10}},
                "priceChange": {"m5": 15, "h1": 10}}
        self.assertEqual(scanner.passes_market_filter(pair), (False, "missing_pair_age"))

    def test_market_filter_accepts_good_pair(self):
        pair = {"pairCreatedAt": int(time.time() * 1000) - 7200000,
                "liquidity": {"usd": 100000}, "volume": {"h1": 50000},
                "marketCap": 250000, "txns": {"m5": {"buys": 30, "sells": 10}},
                "priceChange": {"m5": 15, "h1": 10}}
        self.assertTrue(scanner.passes_market_filter(pair)[0])

    def test_liquidity_drain_requires_confirmation_only_for_catastrophic_drop(self):
        pos = {"entry_liquidity": 100000, "liquidity_drain_hits": 0}
        self.assertFalse(scanner.liquidity_drain_detected(pos, 40000))
        self.assertTrue(scanner.liquidity_drain_detected(pos, 40000))


if __name__ == "__main__":
    unittest.main()
