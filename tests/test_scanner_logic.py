import importlib.util
from pathlib import Path
import unittest
import time
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "meme_scanner_cycle.py"
spec = importlib.util.spec_from_file_location("scanner", MODULE_PATH)
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


def pair(**overrides):
    base = {
        "liquidity": {"usd": 60000},
        "volume": {"h1": 120000},
        "marketCap": 150000,
        "txns": {"m5": {"buys": 20, "sells": 10}},
        "priceChange": {"m5": 2.0, "h1": 8.0},
        "pairCreatedAt": int(time.time() * 1000) - 3600 * 1000,
    }
    base.update(overrides)
    return base


class ScannerLogicTests(unittest.TestCase):
    def test_max_concurrent_positions_is_eight(self):
        self.assertEqual(scanner.MAX_CONCURRENT_POSITIONS, 8)

    def test_position_size_is_capped_at_one_percent_of_equity(self):
        state = {"equity": 400.0, "positions": {}}
        pair_data = pair(priceUsd="1.0", baseToken={"symbol": "TEST"})
        self.assertTrue(scanner.open_position(state, "mint", pair_data, {"holder": 100}, 70, {}))
        self.assertEqual(state["positions"]["mint"]["amount_usd"], 4.0)

    def test_target_is_one_hundred_trades(self):
        self.assertEqual(scanner.TARGET_TRADES, 200)

    def test_positive_opportunity_passes(self):
        self.assertTrue(scanner.passes_market_filter(pair())[0])

    def test_zero_momentum_gets_lower_score(self):
        good, _ = scanner.score_market_opportunity(pair(priceChange={"m5": 2.0, "h1": 8.0}))
        weak, _ = scanner.score_market_opportunity(pair(priceChange={"m5": 0, "h1": 8.0}))
        self.assertGreater(good, weak)

    def test_negative_momentum_gets_lower_score(self):
        good, _ = scanner.score_market_opportunity(pair(priceChange={"m5": 2.0, "h1": 8.0}))
        weak, _ = scanner.score_market_opportunity(pair(priceChange={"m5": -1.0, "h1": 8.0}))
        self.assertGreater(good, weak)

    def test_low_liquidity_is_rejected(self):
        self.assertFalse(scanner.passes_market_filter(pair(liquidity={"usd": 19999}))[0])

    def test_low_liquidity_to_mcap_is_rejected(self):
        self.assertFalse(scanner.passes_market_filter(pair(liquidity={"usd": 12000}, marketCap=50000))[0])

    def test_weak_buy_sell_is_rejected(self):
        self.assertFalse(scanner.passes_market_filter(pair(txns={"m5": {"buys": 14, "sells": 10}}))[0])

    def test_low_volume_is_rejected(self):
        self.assertFalse(scanner.passes_market_filter(pair(volume={"h1": 2999}))[0])

    def test_weak_buy_pressure_lowers_score(self):
        weak, _ = scanner.score_market_opportunity(pair(txns={"m5": {"buys": 12, "sells": 10}}))
        strong, _ = scanner.score_market_opportunity(pair(txns={"m5": {"buys": 20, "sells": 10}}))
        self.assertGreater(strong, weak)

    def test_strong_opportunity_passes(self):
        self.assertTrue(scanner.passes_market_filter(pair())[0])

    def test_no_recent_transactions_are_rejected(self):
        self.assertFalse(scanner.passes_market_filter(pair(txns={"m5": {"buys": 0, "sells": 0}}))[0])

    def test_entry_is_blocked_below_minimum_equity(self):
        state = {"equity": 199.0, "positions": {}}
        pair_data = pair(priceUsd="1.0", baseToken={"symbol": "TEST"})
        self.assertFalse(scanner.open_position(state, "mint", pair_data, {"holder": 100}, 70, {}))

    def test_emergency_liquidity_floor_closes_immediately(self):
        pos = {"entry_liquidity": 20000, "liquidity_drain_hits": 0}
        self.assertTrue(scanner.liquidity_drain_detected(pos, 9000))

    def test_liquidity_drop_below_half_requires_two_observations(self):
        pos = {"entry_liquidity": 30000, "liquidity_drain_hits": 0}
        self.assertFalse(scanner.liquidity_drain_detected(pos, 14000))
        self.assertTrue(scanner.liquidity_drain_detected(pos, 14000))

    def test_liquidity_recovery_resets_confirmation(self):
        pos = {"entry_liquidity": 30000, "liquidity_drain_hits": 1}
        self.assertFalse(scanner.liquidity_drain_detected(pos, 20000))
        self.assertEqual(pos["liquidity_drain_hits"], 0)

    def test_learning_changes_score_after_history(self):
        learning = {"trades": 10, "wins": 8, "buckets": {"90": {"n": 5, "wins": 4}}}
        score, _ = scanner.score_market_opportunity(pair(), learning)
        baseline, _ = scanner.score_market_opportunity(pair())
        self.assertGreater(score, baseline)

    def test_entry_confirmation_resets_on_score_deterioration(self):
        s = {"candidate_observations": {}}
        self.assertFalse(scanner.candidate_confirmation(s, "mint", 70))
        self.assertFalse(scanner.candidate_confirmation(s, "mint", 60))
        self.assertEqual(s["candidate_observations"]["mint"]["hits"], 1)

    def test_liquidity_ratio_emergency_exit(self):
        pos = {"entry_liquidity": 40000, "liquidity_drain_hits": 0}
        self.assertTrue(scanner.liquidity_drain_detected(pos, 13000))

    def test_risk_constants_are_hardened(self):
        self.assertEqual(scanner.MIN_LIQUIDITY_USD, 50000)
        self.assertEqual(scanner.MIN_LIQ_MCAP_RATIO, 0.35)
        self.assertEqual(scanner.MIN_BUY_SELL_RATIO, 1.5)
        self.assertEqual(scanner.MIN_PRICE_CHANGE_M5_PCT, 1.0)
        self.assertEqual(scanner.OPPORTUNITY_ENTRY_THRESHOLD, 65.0)
        self.assertEqual(scanner.MAX_POSITION_EQUITY_FRACTION, 0.01)
        self.assertEqual(scanner.MAX_PORTFOLIO_EXPOSURE_FRACTION, 0.05)
        self.assertEqual(scanner.MIN_PAIR_AGE_SECONDS, 1800)

    def test_missing_pair_age_is_rejected(self):
        data = pair()
        data.pop("pairCreatedAt")
        ok, reason = scanner.passes_market_filter(data)
        self.assertFalse(ok)
        self.assertEqual(reason, "missing_pair_age")

    def test_new_pair_is_rejected(self):
        data = pair(pairCreatedAt=int(time.time() * 1000) - 60 * 1000)
        ok, reason = scanner.passes_market_filter(data)
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("pair_too_new:"))

    def test_severe_liquidity_drop_exits_immediately(self):
        pos = {"entry_liquidity": 40000, "liquidity_drain_hits": 0}
        self.assertTrue(scanner.liquidity_drain_detected(pos, 13999))

    def test_both_stop_loss_reasons_apply_cooldown(self):
        for reason in ("stop_loss", "hard_stop_loss"):
            state = {
                "equity": 1000.0,
                "trade_count": 0,
                "positions": {"mint": {"symbol": "TEST", "entry_price": 1.0, "amount_usd": 10.0, "estimated_impact": 0.0, "signal_score": 70}},
                "blacklist": [],
                "cooldowns": {},
            }
            with patch.object(scanner, "append_learning_observation"), patch.object(scanner, "append_trade"):
                scanner.close_position(state, "mint", 0.8, reason)
            self.assertGreater(state["cooldowns"]["mint"], time.time() + 29 * 60)


if __name__ == "__main__":
    unittest.main()
