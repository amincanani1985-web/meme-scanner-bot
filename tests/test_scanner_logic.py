import importlib.util
from pathlib import Path
import unittest

MODULE_PATH = Path(__file__).resolve().parents[1] / "meme_scanner_cycle.py"
spec = importlib.util.spec_from_file_location("scanner", MODULE_PATH)
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


def pair(**overrides):
    base = {
        "liquidity": {"usd": 20000},
        "volume": {"h1": 10000},
        "marketCap": 50000,
        "txns": {"m5": {"buys": 20, "sells": 10}},
        "priceChange": {"m5": 2.0, "h1": 8.0},
    }
    base.update(overrides)
    return base


class ScannerLogicTests(unittest.TestCase):
    def test_max_concurrent_positions_is_eight(self):
        self.assertEqual(scanner.MAX_CONCURRENT_POSITIONS, 8)

    def test_position_size_is_capped_at_five_percent_of_equity(self):
        state = {"equity": 400.0, "positions": {}}
        pair_data = pair(priceUsd="1.0", baseToken={"symbol": "TEST"})
        self.assertTrue(scanner.open_position(state, "mint", pair_data, {"holder": 100}, 70, {}))
        self.assertEqual(state["positions"]["mint"]["amount_usd"], 20.0)

    def test_target_is_one_hundred_trades(self):
        self.assertEqual(scanner.TARGET_TRADES, 100)

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
        self.assertFalse(scanner.passes_market_filter(pair(liquidity={"usd": 1999}))[0])

    def test_low_liquidity_to_mcap_is_rejected(self):
        self.assertFalse(scanner.passes_market_filter(pair(liquidity={"usd": 12000}, marketCap=100000))[0])

    def test_weak_buy_sell_is_rejected(self):
        self.assertFalse(scanner.passes_market_filter(pair(txns={"m5": {"buys": 10, "sells": 10}}))[0])

    def test_low_volume_is_rejected(self):
        self.assertFalse(scanner.passes_market_filter(pair(volume={"h1": 499}))[0])

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

    def test_liquidity_drop_requires_confirmation(self):
        pos = {"entry_liquidity": 20000, "liquidity_drain_hits": 0}
        self.assertFalse(scanner.liquidity_drain_detected(pos, 9000))
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
        learning = {"trades": 10, "wins": 8, "buckets": {"80": {"n": 5, "wins": 4}}}
        score, _ = scanner.score_market_opportunity(pair(), learning)
        baseline, _ = scanner.score_market_opportunity(pair())
        self.assertGreater(score, baseline)


if __name__ == "__main__":
    unittest.main()
