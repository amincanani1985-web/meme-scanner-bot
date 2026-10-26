import unittest

from shadow_path_backtest import (
    BASELINE_CONFIG,
    analyze_records,
    baseline_consistency,
    simulate_exit,
)


def path(prices, entry_liquidity=100_000.0, benchmark_exit=None):
    samples = []
    for seconds, price, liquidity in prices:
        samples.append({
            "timestamp": 1_000.0 + seconds,
            "price_usd": price,
            "liquidity_usd": liquidity,
        })
    result = {
        "schema_version": 1,
        "mint": "test-mint",
        "symbol": "TEST",
        "score": 75.0,
        "entry_timestamp": 1_000.0,
        "entry_price": 1.0,
        "entry_liquidity_usd": entry_liquidity,
        "entry_components": {},
        "price_history": samples,
        "observation_end_timestamp": samples[-1]["timestamp"],
        "observed_duration_seconds": samples[-1]["timestamp"] - 1_000.0,
    }
    if benchmark_exit is not None:
        result["benchmark_exit"] = benchmark_exit
    return result


class ShadowPathBacktestTests(unittest.TestCase):
    def test_hard_stop_uses_observed_quote_and_charges_exit_slippage_and_fees(self):
        record = path([(0, 1.0, 100_000), (30, 0.89, 100_000), (60, 0.95, 100_000)])
        result = simulate_exit(record, {
            **BASELINE_CONFIG,
            "hard_stop_pct": 0.10,
            "trailing_arm_pct": 0.08,
            "trailing_stop_pct": 0.04,
            "max_hold_seconds": 600,
        })
        self.assertEqual(result["status"], "closed")
        self.assertEqual(result["reason"], "hard_stop_loss")
        self.assertEqual(result["exit_timestamp"], 1_030.0)
        self.assertAlmostEqual(result["net_pnl_pct"], -13.78, places=6)

    def test_trailing_stop_uses_peak_then_observed_pullback(self):
        record = path([
            (0, 1.0, 100_000),
            (30, 1.10, 100_000),
            (60, 1.05, 100_000),
            (90, 1.02, 100_000),
        ])
        result = simulate_exit(record, {
            **BASELINE_CONFIG,
            "hard_stop_pct": 0.10,
            "trailing_arm_pct": 0.08,
            "trailing_stop_pct": 0.04,
            "max_hold_seconds": 600,
        })
        self.assertEqual(result["status"], "closed")
        self.assertEqual(result["reason"], "trailing_stop")
        self.assertEqual(result["exit_timestamp"], 1_060.0)
        self.assertGreater(result["net_pnl_pct"], 0)

    def test_liquidity_drain_between_25_and_50_percent_exits_on_first_observation(self):
        record = path([(0, 1.0, 100_000), (30, 0.99, 70_000)], entry_liquidity=100_000)
        result = simulate_exit(record, {
            **BASELINE_CONFIG,
            "hard_stop_pct": 0.10,
            "trailing_arm_pct": 0.08,
            "trailing_stop_pct": 0.04,
            "max_hold_seconds": 600,
        })
        self.assertEqual(result["reason"], "liquidity_drained")
        self.assertEqual(result["exit_timestamp"], 1_030.0)

    def test_incomplete_observed_path_is_censored_not_fabricated_as_exit(self):
        record = path([(0, 1.0, 100_000), (300, 1.01, 100_000)])
        result = simulate_exit(record, {
            **BASELINE_CONFIG,
            "hard_stop_pct": 0.10,
            "trailing_arm_pct": 0.08,
            "trailing_stop_pct": 0.04,
            "max_hold_seconds": 600,
        })
        self.assertEqual(result["status"], "unresolved")
        self.assertIsNone(result["net_pnl_pct"])
        self.assertIsNone(result["reason"])

    def test_baseline_replay_matches_persisted_benchmark_outcome(self):
        benchmark = {
            "reason": "hard_stop_loss",
            "timestamp": 1_030.0,
            "price_usd": 0.89,
            "liquidity_usd": 100_000.0,
            "after_slippage_pnl_pct": -12.78,
        }
        record = path(
            [(0, 1.0, 100_000), (30, 0.89, 100_000), (900, 0.90, 100_000)],
            benchmark_exit=benchmark,
        )
        report = baseline_consistency([record])
        self.assertEqual(report["checked"], 1)
        self.assertEqual(report["matches"], 1)
        self.assertEqual(report["mismatches"], 0)

    def test_empty_history_never_selects_or_approves_a_rule(self):
        report = analyze_records([], min_total_paths=50)
        self.assertEqual(report["status"], "insufficient_history")
        self.assertEqual(report["eligible_training_candidates"], [])
        self.assertIsNone(report["selected_config"])
        self.assertEqual(report["train_rows"], 0)
        self.assertEqual(report["validation_rows"], 0)


if __name__ == "__main__":
    unittest.main()
