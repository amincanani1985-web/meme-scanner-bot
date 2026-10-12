import time
import unittest
from unittest.mock import patch

import meme_scanner_cycle as scanner


def pair(**overrides):
    data = {
        "pairCreatedAt": int(time.time() * 1000) - 2 * 60 * 60 * 1000,
        "liquidity": {"usd": 100000},
        "volume": {"h1": 50000},
        "marketCap": 250000,
        "txns": {"m5": {"buys": 30, "sells": 10}},
        "priceChange": {"m5": 15.0, "h1": 10.0},
        "priceUsd": "1.0",
        "baseToken": {"symbol": "TEST"},
    }
    for key, value in overrides.items():
        data[key] = value
    return data


class ScannerLogicTests(unittest.TestCase):
    def test_hardened_risk_constants(self):
        self.assertEqual(scanner.MAX_CONCURRENT_POSITIONS, 4)
        self.assertEqual(scanner.OPPORTUNITY_ENTRY_THRESHOLD, 80.0)
        self.assertEqual(scanner.MAX_POSITION_EQUITY_FRACTION, 0.005)
        self.assertEqual(scanner.MAX_PORTFOLIO_EXPOSURE_FRACTION, 0.02)

    def test_good_pair_passes_current_market_filters(self):
        self.assertEqual(scanner.passes_market_filter(pair()), (True, "ok"))

    def test_pair_missing_age_is_rejected(self):
        data = pair()
        data.pop("pairCreatedAt")
        self.assertEqual(scanner.passes_market_filter(data), (False, "missing_pair_age"))

    def test_new_pair_is_rejected(self):
        result, reason = scanner.passes_market_filter(
            pair(pairCreatedAt=int(time.time() * 1000) - 60_000)
        )
        self.assertFalse(result)
        self.assertTrue(reason.startswith("pair_too_new:"))

    def test_low_liquidity_volume_ratio_and_buy_pressure_are_rejected(self):
        self.assertFalse(scanner.passes_market_filter(pair(liquidity={"usd": 99999}))[0])
        self.assertFalse(scanner.passes_market_filter(pair(volume={"h1": 19999}))[0])
        self.assertFalse(scanner.passes_market_filter(pair(marketCap=300000))[0])
        self.assertFalse(scanner.passes_market_filter(pair(txns={"m5": {"buys": 19, "sells": 10}}))[0])

    def test_negative_hourly_momentum_is_rejected(self):
        self.assertFalse(scanner.passes_market_filter(pair(priceChange={"m5": 15, "h1": -0.1}))[0])

    def test_score_rewards_stronger_momentum_and_buy_pressure(self):
        baseline, _ = scanner.score_market_opportunity(pair())
        weaker_momentum, _ = scanner.score_market_opportunity(
            pair(priceChange={"m5": 2.0, "h1": 10.0})
        )
        weaker_buying, _ = scanner.score_market_opportunity(
            pair(txns={"m5": {"buys": 20, "sells": 10}})
        )
        self.assertGreater(baseline, weaker_momentum)
        self.assertGreater(baseline, weaker_buying)

    def test_learning_bonus_only_applies_with_positive_history(self):
        baseline, _ = scanner.score_market_opportunity(pair())
        learned, components = scanner.score_market_opportunity(
            pair(), {"trades": 10, "wins": 8}
        )
        self.assertGreater(learned, baseline)
        self.assertEqual(components["learning_bonus"], 10.0)

    def test_candidate_confirmation_requires_three_consecutive_hits(self):
        state = {"strategy": {"active_threshold": 80}, "candidate_observations": {}}
        self.assertFalse(scanner.candidate_confirmation(state, "mint", 85))
        self.assertFalse(scanner.candidate_confirmation(state, "mint", 86))
        self.assertTrue(scanner.candidate_confirmation(state, "mint", 87))

    def test_candidate_confirmation_restarts_after_score_drop(self):
        state = {"strategy": {"active_threshold": 80}, "candidate_observations": {}}
        scanner.candidate_confirmation(state, "mint", 85)
        scanner.candidate_confirmation(state, "mint", 86)
        self.assertFalse(scanner.candidate_confirmation(state, "mint", 70))
        self.assertEqual(state["candidate_observations"]["mint"]["hits"], 0)

    def test_open_position_applies_slippage_and_position_caps(self):
        state = {"equity": 1000.0, "positions": {}, "blacklist": [], "cooldowns": {}}
        self.assertTrue(scanner.open_position(state, "mint", pair(), {}, 85, {}))
        position = state["positions"]["mint"]
        self.assertAlmostEqual(position["entry_price"], 1.015)
        self.assertAlmostEqual(position["amount_usd"], 5.0)
        for i in range(scanner.MAX_CONCURRENT_POSITIONS - 1):
            state["positions"][f"mint{i}"] = dict(position)
        self.assertFalse(scanner.open_position(state, "another", pair(), {}, 85, {}))

    def test_small_equity_can_still_open_risk_capped_position(self):
        state = {"equity": 400.0, "positions": {}, "blacklist": [], "cooldowns": {}}
        self.assertTrue(scanner.open_position(state, "small", pair(), {}, 85, {}))
        self.assertAlmostEqual(state["positions"]["small"]["amount_usd"], 2.0)

    def test_open_position_blocks_blacklist_cooldown_and_invalid_price(self):
        state = {"equity": 1000.0, "positions": {}, "blacklist": ["bad"], "cooldowns": {}}
        self.assertFalse(scanner.open_position(state, "bad", pair(), {}, 90, {}))
        state["blacklist"] = []
        state["cooldowns"] = {"cool": time.time() + 60}
        self.assertFalse(scanner.open_position(state, "cool", pair(), {}, 90, {}))
        self.assertFalse(scanner.open_position(state, "zero", pair(priceUsd="0"), {}, 90, {}))

    def test_liquidity_drain_between_25_and_50_percent_exits_immediately(self):
        pos = {"entry_liquidity": 100000}
        self.assertTrue(scanner.liquidity_drain_detected(pos, 70000))

    def test_catastrophic_liquidity_drain_requires_two_hits(self):
        pos = {"entry_liquidity": 100000, "liquidity_drain_hits": 0}
        self.assertFalse(scanner.liquidity_drain_detected(pos, 40000))
        self.assertTrue(scanner.liquidity_drain_detected(pos, 40000))

    def test_liquidity_recovery_resets_confirmation(self):
        pos = {"entry_liquidity": 100000, "liquidity_drain_hits": 1}
        self.assertFalse(scanner.liquidity_drain_detected(pos, 80000))
        self.assertEqual(pos["liquidity_drain_hits"], 0)

    def test_close_position_applies_exit_slippage_updates_learning_and_cooldown(self):
        state = {
            "equity": 1000.0, "trade_count": 0, "positions": {
                "mint": {"symbol": "TEST", "entry_price": 1.015, "amount_usd": 5.0, "signal_score": 85}
            }, "blacklist": [], "cooldowns": {},
        }
        with patch.object(scanner, "append_trade") as trade, patch.object(scanner, "append_learning_observation") as learning:
            scanner.close_position(state, "mint", 0.8, "hard_stop_loss")
        self.assertNotIn("mint", state["positions"])
        self.assertEqual(state["trade_count"], 1)
        self.assertLess(state["equity"], 1000.0)
        self.assertIn("mint", state["cooldowns"])
        trade.assert_called_once()
        learning.assert_called_once()

    def test_manage_positions_uses_live_price_for_hard_stop(self):
        state = {
            "equity": 1000.0, "trade_count": 0, "blacklist": [], "cooldowns": {},
            "positions": {"mint": {"symbol": "TEST", "entry_price": 1.0, "peak_price": 1.0,
                "entry_time": time.time(), "entry_liquidity": 100000, "amount_usd": 10.0,
                "signal_score": 85, "trailing_armed": False}},
        }
        live_pair = {"priceUsd": "0.85", "liquidity": {"usd": 100000}}
        with patch.object(scanner, "fetch_dex_pair", return_value=live_pair), \
             patch.object(scanner, "append_trade"), patch.object(scanner, "append_learning_observation"):
            scanner.manage_open_positions(state)
        self.assertNotIn("mint", state["positions"])
        self.assertEqual(state["trade_count"], 1)

    def test_run_cycle_opens_only_after_three_confirmations(self):
        state = scanner.normalize_state({})
        with patch.object(scanner, "helius_configured", return_value=False), \
             patch.object(scanner, "fetch_candidate_mints", return_value=["mint"]), \
             patch.object(scanner, "fetch_dex_pair", return_value=pair()), \
             patch.object(scanner, "save_state"), patch.object(scanner, "log"):
            first = scanner.run_cycle(state)
            second = scanner.run_cycle(state)
            third = scanner.run_cycle(state)
        self.assertEqual(first["open_positions"], 0)
        self.assertEqual(second["open_positions"], 0)
        self.assertEqual(third["open_positions"], 1)
        self.assertIn("mint", state["positions"])
        self.assertTrue(state.get("last_run"))

    def test_confirmed_whale_dump_closes_open_position(self):
        state = {
            "equity": 1000.0, "trade_count": 0, "blacklist": [], "cooldowns": {},
            "whale_snapshots": {"mint": {"owner-a": 1000.0}},
            "positions": {"mint": {"symbol": "TEST", "entry_price": 1.0, "peak_price": 1.0,
                "entry_time": time.time(), "entry_liquidity": 100000, "amount_usd": 10.0,
                "signal_score": 85, "trailing_armed": False,
                "whale_balances": {"owner-a": 1000.0}}},
        }
        live_pair = {"priceUsd": "0.98", "liquidity": {"usd": 100000},
                     "txns": {"m5": {"buys": 5, "sells": 15}},
                     "priceChange": {"m5": -1.0}}
        with patch.object(scanner, "helius_configured", return_value=True), \
             patch.object(scanner, "fetch_whale_balances", return_value={"owner-a": 700.0}), \
             patch.object(scanner, "fetch_dex_pair", return_value=live_pair), \
             patch.object(scanner, "append_trade"), patch.object(scanner, "append_learning_observation"), \
             patch.object(scanner, "log"):
            scanner.manage_open_positions(state)
        self.assertNotIn("mint", state["positions"])
        self.assertIn("mint", state["blacklist"])
        self.assertEqual(state["trade_count"], 1)

    def test_malformed_persisted_position_does_not_abort_scan(self):
        state = {
            "equity": 1000.0, "trade_count": 0, "blacklist": [], "cooldowns": {},
            "positions": {"broken": {"entry_price": "bad", "amount_usd": 10.0, "entry_time": "bad"}},
        }
        with patch.object(scanner, "fetch_dex_pair", return_value=None), patch.object(scanner, "log"):
            scanner.manage_open_positions(state)
        self.assertNotIn("broken", state["positions"])

    def test_missing_price_for_old_position_forces_conservative_close(self):
        state = {
            "equity": 1000.0, "trade_count": 0, "blacklist": [], "cooldowns": {},
            "positions": {"mint": {"symbol": "TEST", "entry_price": 1.0, "peak_price": 1.0,
                "entry_time": 0.0, "entry_liquidity": 100000, "amount_usd": 10.0,
                "signal_score": 85, "trailing_armed": False}},
        }
        with patch.object(scanner, "fetch_dex_pair", return_value=None), \
             patch.object(scanner, "append_trade"), patch.object(scanner, "append_learning_observation"):
            scanner.manage_open_positions(state)
        self.assertNotIn("mint", state["positions"])
        self.assertIn("mint", state["blacklist"])
        self.assertEqual(state["equity"], 990.0)

    def test_trade_target_disables_new_entries(self):
        state = scanner.normalize_state({"trade_count": scanner.TARGET_TRADES})
        with patch.object(scanner, "manage_open_positions"), patch.object(scanner, "save_state"), \
             patch.object(scanner, "fetch_candidate_mints") as fetch_candidates, patch.object(scanner, "log"):
            result = scanner.run_cycle(state)
        self.assertTrue(result["target_reached"])
        fetch_candidates.assert_not_called()

    def test_learning_bonus_requires_minimum_sample(self):
        baseline, _ = scanner.score_market_opportunity(pair())
        tiny_sample, components = scanner.score_market_opportunity(pair(), {"trades": 1, "wins": 1})
        self.assertEqual(tiny_sample, baseline)
        self.assertNotIn("learning_bonus", components)

    def test_normalize_state_repairs_invalid_types(self):
        fixed = scanner.normalize_state({
            "equity": "bad", "positions": [], "blacklist": "oops", "cooldowns": "nope",
            "trade_count": "abc", "scan_count": "7", "candidate_observations": "bad",
            "strategy": "invalid", "learning": None,
        })
        self.assertEqual(fixed["equity"], 1000.0)
        self.assertIsInstance(fixed["positions"], dict)
        self.assertEqual(fixed["blacklist"], [])
        self.assertEqual(fixed["scan_count"], 7)
        self.assertIsInstance(fixed["strategy"], dict)
        self.assertEqual(fixed["learning"]["trades"], 0)


if __name__ == "__main__":
    unittest.main()
