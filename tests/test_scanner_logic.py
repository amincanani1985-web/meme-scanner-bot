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
    def test_max_concurrent_positions_is_ten(self):
        self.assertEqual(scanner.MAX_CONCURRENT_POSITIONS, 10)

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

    def test_learning_changes_score_after_history(self):
        learning = {"trades": 10, "wins": 8, "buckets": {"80": {"n": 5, "wins": 4}}}
        score, _ = scanner.score_market_opportunity(pair(), learning)
        baseline, _ = scanner.score_market_opportunity(pair())
        self.assertGreater(score, baseline)


if __name__ == "__main__":
    unittest.main()
