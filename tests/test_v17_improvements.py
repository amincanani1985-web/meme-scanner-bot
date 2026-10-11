import importlib.util
import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "meme_scanner_cycle.py"
spec = importlib.util.spec_from_file_location("scanner", MODULE_PATH)
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


class V17ImprovementTests(unittest.TestCase):
    def test_normalize_state_recovers_from_corrupted_state(self):
        broken = {"equity": "bad", "positions": [], "blacklist": "oops", "cooldowns": "nope", "trade_count": "abc", "candidate_observations": "bad", "strategy": "str"}
        fixed = scanner.normalize_state(broken)
        self.assertIsInstance(fixed["positions"], dict)
        self.assertIsInstance(fixed["blacklist"], list)
        self.assertIsInstance(fixed["cooldowns"], dict)
        self.assertIsInstance(fixed["candidate_observations"], dict)
        self.assertIsInstance(fixed["strategy"], dict)
        self.assertEqual(fixed["equity"], scanner.STARTING_EQUITY)

    def test_open_position_uses_realistic_slippage(self):
        state = {"equity": 1000.0, "positions": {}, "blacklist": [], "cooldowns": {}, "trade_count": 0}
        pair = {"priceUsd": "1.0", "liquidity": {"usd": 100000}, "baseToken": {"symbol": "TEST"}}
        ok = scanner.open_position(state, "mint123", pair, {"addr": 100}, 70, {})
        self.assertTrue(ok)
        pos = state["positions"]["mint123"]
        self.assertGreater(pos["entry_price"], 1.0)
        self.assertAlmostEqual(pos["entry_price"], 1.015, places=3)

    def test_close_position_for_stop_loss_applies_cooldown(self):
        state = {
            "equity": 1000.0,
            "trade_count": 0,
            "positions": {
                "mint": {
                    "symbol": "TEST",
                    "entry_price": 1.0,
                    "amount_usd": 10.0,
                    "estimated_impact": 0.0,
                    "signal_score": 70,
                }
            },
            "blacklist": [],
            "cooldowns": {},
        }
        with patch.object(scanner, "append_learning_observation"), patch.object(scanner, "append_trade"):
            scanner.close_position(state, "mint", 0.8, "stop_loss")
        self.assertIn("mint", state["cooldowns"])
        self.assertGreater(state["cooldowns"]["mint"], time.time())
        self.assertNotIn("mint", state["positions"])

    def test_close_position_for_liquidity_drained_blacklists_permanently(self):
        state = {
            "equity": 1000.0,
            "trade_count": 0,
            "positions": {
                "mint": {
                    "symbol": "TEST",
                    "entry_price": 1.0,
                    "amount_usd": 10.0,
                    "estimated_impact": 0.0,
                    "signal_score": 70,
                }
            },
            "blacklist": [],
            "cooldowns": {},
        }
        with patch.object(scanner, "append_learning_observation"), patch.object(scanner, "append_trade"):
            scanner.close_position(state, "mint", 0.9, "liquidity_drained")
        self.assertIn("mint", state["blacklist"])

    def test_manage_open_positions_closes_after_missing_price_data(self):
        state = {
            "equity": 1000.0,
            "trade_count": 0,
            "positions": {
                "mint": {
                    "symbol": "TEST",
                    "entry_price": 1.0,
                    "amount_usd": 10.0,
                    "estimated_impact": 0.0,
                    "signal_score": 70,
                    "entry_time": time.time() - (2 * scanner.MAX_HOLD_SECONDS + 1),
                }
            },
            "blacklist": [],
            "cooldowns": {},
        }
        with patch.object(scanner, "fetch_dex_pair", return_value=None), \
             patch.object(scanner, "append_learning_observation"), \
             patch.object(scanner, "append_trade"):
            scanner.manage_open_positions(state)
        self.assertNotIn("mint", state["positions"])
        self.assertIn("mint", state["blacklist"])
        self.assertEqual(state["trade_count"], 1)

    def test_candidate_confirmation_restarts_when_score_drops(self):
        state = {"candidate_observations": {}}
        self.assertFalse(scanner.candidate_confirmation(state, "mint", 70))
        self.assertFalse(scanner.candidate_confirmation(state, "mint", 60))
        self.assertEqual(state["candidate_observations"]["mint"]["hits"], 1)

    def test_liquidity_drain_detected_requires_two_hits(self):
        pos = {"entry_liquidity": 30000, "liquidity_drain_hits": 0}
        self.assertFalse(scanner.liquidity_drain_detected(pos, 14000))
        self.assertTrue(scanner.liquidity_drain_detected(pos, 14000))

    def test_legacy_strategy_migration_rewrites_60_to_65(self):
        state = {"strategy": {"active_threshold": 60.0, "rollback_threshold": 60.0, "candidate_threshold": None, "status": "waiting_for_history"}}
        scanner.normalize_legacy_strategy_baseline(state)
        self.assertEqual(state["strategy"]["active_threshold"], 65.0)
        self.assertEqual(state["strategy"]["rollback_threshold"], 65.0)

    def test_passes_market_filter_rejects_missing_pair_age(self):
        pair = {"liquidity": {"usd": 60000}, "volume": {"h1": 120000}, "marketCap": 150000, "txns": {"m5": {"buys": 20, "sells": 10}}, "priceChange": {"m5": 2.0, "h1": 8.0}}
        ok, reason = scanner.passes_market_filter(pair)
        self.assertFalse(ok)
        self.assertEqual(reason, "missing_pair_age")

    def test_passes_market_filter_accepts_good_pair(self):
        pair = {
            "liquidity": {"usd": 60000},
            "volume": {"h1": 120000},
            "marketCap": 150000,
            "txns": {"m5": {"buys": 20, "sells": 10}},
            "priceChange": {"m5": 2.0, "h1": 8.0},
            "pairCreatedAt": int(time.time() * 1000) - 3600 * 1000,
        }
        ok, _ = scanner.passes_market_filter(pair)
        self.assertTrue(ok)

    def test_normalize_state_keeps_numeric_trade_count_integers(self):
        state = {"equity": "500", "positions": {}, "blacklist": [], "cooldowns": {}, "trade_count": "3", "scan_count": "7", "candidate_observations": {}, "strategy": {}}
        fixed = scanner.normalize_state(state)
        self.assertEqual(fixed["equity"], 500.0)
        self.assertEqual(fixed["trade_count"], 3)
        self.assertEqual(fixed["scan_count"], 7)


if __name__ == "__main__":
    unittest.main()
