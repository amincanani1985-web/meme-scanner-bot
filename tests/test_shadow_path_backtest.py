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

    def test_baseline_net_performance_is_reported_before_candidate_sample_gate(self):
        record = path([
            (seconds, 1.0 if seconds == 0 else 1.2, 100_000)
            for seconds in range(0, 901, 100)
        ], benchmark_exit={
            "reason": "max_hold_timeout",
            "timestamp": 1600.0,
            "price_usd": 1.2,
            "liquidity_usd": 100_000.0,
            "after_slippage_pnl_pct": 17.6,
        })
        report = analyze_records([record], min_total_paths=50)
        self.assertEqual(report["status"], "insufficient_history")
        self.assertIsNone(report["selected_config"])
        self.assertEqual(report["baseline_performance"]["n"], 1)
        self.assertAlmostEqual(
            report["baseline_performance"]["mean_pnl_pct"], 16.6, places=6
        )
        self.assertEqual(report["baseline_performance"]["wins"], 1)

    def test_walk_forward_rejects_sparse_paths_even_when_the_horizon_is_long_enough(self):
        sparse = path([
            (0, 1.0, 100_000),
            (300, 0.99, 100_000),
            (600, 0.98, 100_000),
            (900, 0.97, 100_000),
        ])
        report = analyze_records([sparse], min_total_paths=1)
        self.assertEqual(report["usable_paths"], 0)
        self.assertEqual(report["rejected_or_censored_paths"], 1)
        self.assertEqual(report["status"], "insufficient_history")
        self.assertEqual(report["path_rejection_reasons"].get("duration_under_900_seconds", 0), 0)
        self.assertEqual(report["path_rejection_reasons"].get("fewer_than_9_observed_samples", 0), 1)
        self.assertEqual(report["path_rejection_reasons"].get("sample_gap_over_240_seconds", 0), 1)

    def test_walk_forward_accepts_observed_paths_with_maximum_gap_under_four_minutes(self):
        samples = (
            [(seconds, 1.0, 100_000) for seconds in range(0, 301, 30)]
            + [(seconds, 0.99, 100_000) for seconds in range(480, 901, 30)]
        )
        observed = path(samples)
        report = analyze_records([observed], min_total_paths=1)
        self.assertEqual(report["usable_paths"], 1)
        self.assertEqual(report["rejected_or_censored_paths"], 0)
        self.assertEqual(
            report["path_quality_policy"]["maximum_sample_gap_seconds"], 240.0
        )
        self.assertEqual(report["accepted_sample_gaps"]["maximum_seconds"], 180.0)
        self.assertEqual(report["accepted_sample_gaps"]["count"], 25)

    def test_walk_forward_accepts_a_dense_complete_observed_path(self):
        dense = path([
            (seconds, 1.0 - 0.0001 * seconds, 100_000)
            for seconds in range(0, 901, 30)
        ])
        report = analyze_records([dense], min_total_paths=1)
        self.assertEqual(report["usable_paths"], 1)
        self.assertEqual(report["rejected_or_censored_paths"], 0)

    def test_walk_forward_counts_only_one_complete_path_per_mint(self):
        first = path([
            (seconds, 1.0 if seconds == 0 else 1.2, 100_000)
            for seconds in range(0, 901, 100)
        ])
        repeated = path([
            (seconds, 1.0 if seconds == 0 else 0.9, 100_000)
            for seconds in range(0, 901, 100)
        ])
        repeated["entry_timestamp"] = 2_000.0
        repeated["price_history"] = [
            {**sample, "timestamp": sample["timestamp"] + 1_000.0}
            for sample in repeated["price_history"]
        ]
        repeated["observation_end_timestamp"] += 1_000.0
        report = analyze_records([first, repeated], min_total_paths=1)
        self.assertEqual(report["paths_received"], 2)
        self.assertEqual(report["quality_usable_path_records"], 2)
        self.assertEqual(report["repeated_mint_paths_excluded"], 1)
        self.assertEqual(report["usable_paths"], 1)
        self.assertEqual(report["baseline_performance"]["n"], 1)

    def test_predeclared_entry_filters_are_serializable_and_reject_missing_features(self):
        import shadow_path_backtest as backtest

        filters = backtest.candidate_entry_filters()
        names = [item["name"] for item in filters]
        self.assertGreaterEqual(len(filters), 5)
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(backtest.MIN_TRAIN_ROWS, 20)
        self.assertIn("score_at_least_40", names)
        self.assertIn("score_at_least_50", names)
        record = path([(0, 1.0, 100_000), (30, 0.99, 100_000)])
        record["score"] = 75.0
        record["entry_components"] = {
            "momentum_h1_pct": 10.0,
            "momentum_m5_pct": 2.0,
            "buy_sell_ratio": 1.5,
            "liquidity_usd": 100_000.0,
            "volume_h1_usd": 50_000.0,
            "market_cap_usd": 500_000.0,
            "liquidity_to_mcap_ratio": 0.2,
        }
        self.assertTrue(backtest.entry_filter_matches(record, filters[0]))
        self.assertTrue(all(isinstance(item, dict) for item in filters))
        record["entry_components"] = {}
        self.assertFalse(backtest.entry_filter_matches(record, next(
            item for item in filters if item.get("h1_min") is not None
        )))

    def test_joint_entry_exit_search_does_not_select_a_losing_training_rule(self):
        import shadow_path_backtest as backtest

        records = []
        for i in range(50):
            record = path([
                (seconds, 1.0 - 0.0001 * seconds, 100_000.0)
                for seconds in range(0, 901, 30)
            ])
            record["mint"] = f"unique-mint-{i}"
            record["score"] = 75.0
            record["entry_components"] = {
                "momentum_h1_pct": 10.0,
                "momentum_m5_pct": 2.0,
                "buy_sell_ratio": 1.5,
                "liquidity_usd": 100_000.0,
                "volume_h1_usd": 50_000.0,
                "market_cap_usd": 500_000.0,
                "liquidity_to_mcap_ratio": 0.2,
            }
            records.append(record)

        report = backtest.analyze_records(records, min_total_paths=50)
        self.assertEqual(report["train_rows"], 35)
        self.assertEqual(report["validation_rows"], 15)
        self.assertEqual(report["status"], "no_profitable_training_candidate")
        self.assertEqual(report["eligible_training_candidates"], [])
        self.assertIsNone(report["selected_config"])
        self.assertGreater(report["joint_candidate_configs"], 0)

    def test_empty_history_never_selects_or_approves_a_rule(self):
        report = analyze_records([], min_total_paths=50)
        self.assertEqual(report["status"], "insufficient_history")
        self.assertEqual(report["eligible_training_candidates"], [])
        self.assertIsNone(report["selected_config"])
        self.assertEqual(report["train_rows"], 0)
        self.assertEqual(report["validation_rows"], 0)


if __name__ == "__main__":
    unittest.main()
