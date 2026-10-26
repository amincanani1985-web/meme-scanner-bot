import unittest

from shadow_path_backtest import (
    BASELINE_CONFIG,
    analyze_prospective_cohorts,
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

    def test_memeswap_risk_filters_fail_closed_when_the_profile_is_missing(self):
        import shadow_path_backtest as backtest

        filters = {item["name"]: item for item in backtest.candidate_entry_filters()}
        record = path([(0, 1.0, 100_000), (30, 0.99, 100_000)])
        record["score"] = 75.0
        record["entry_components"] = {
            "memeswap_available": 1.0,
            "memeswap_price_age_seconds": 12.0,
            "memeswap_profile_age_seconds": 20.0,
            "memeswap_risk_data_available": 1.0,
            "memeswap_full_risk_screen_available": 1.0,
            "memeswap_gate_pass": 1.0,
            "memeswap_fake_score": 3.0,
            "memeswap_buy_tax_value": 0.0,
            "memeswap_sell_tax_value": 0.0,
            "memeswap_top1_pct": 9.0,
            "memeswap_top10_pct": 29.0,
            "memeswap_top20_pct": 55.0,
            "memeswap_honeypot": 0.0,
            "memeswap_mintable": 0.0,
            "memeswap_lp_locked_pct": 60.0,
        }
        self.assertTrue(backtest.entry_filter_matches(record, filters["memeswap_top10_at_most_35"]))
        self.assertTrue(backtest.entry_filter_matches(record, filters["memeswap_honeypot_false"]))
        self.assertTrue(backtest.entry_filter_matches(record, filters["memeswap_profile_fresh"]))
        record["entry_components"]["memeswap_risk_data_available"] = 0.0
        self.assertFalse(backtest.entry_filter_matches(record, filters["memeswap_profile_fresh"]))
        record["entry_components"]["memeswap_risk_data_available"] = 1.0
        record["entry_components"]["memeswap_full_risk_screen_available"] = 0.0
        self.assertFalse(backtest.entry_filter_matches(record, filters["memeswap_profile_fresh"]))

        record["entry_components"].pop("memeswap_top10_pct")
        self.assertFalse(backtest.entry_filter_matches(record, filters["memeswap_top10_at_most_35"]))
        record["entry_components"] = {}
        self.assertFalse(backtest.entry_filter_matches(record, filters["memeswap_honeypot_false"]))
        self.assertFalse(backtest.entry_filter_matches(record, filters["memeswap_profile_fresh"]))

    def test_memeswap_risk_filters_exclude_reported_honeypot_or_mintable_tokens(self):
        import shadow_path_backtest as backtest

        filters = {item["name"]: item for item in backtest.candidate_entry_filters()}
        record = path([(0, 1.0, 100_000), (30, 0.99, 100_000)])
        record["score"] = 75.0
        record["entry_components"] = {
            "memeswap_available": 1.0,
            "memeswap_profile_age_seconds": 15.0,
            "memeswap_honeypot": 1.0,
            "memeswap_mintable": 1.0,
        }
        self.assertFalse(backtest.entry_filter_matches(record, filters["memeswap_honeypot_false"]))
        self.assertFalse(backtest.entry_filter_matches(record, filters["memeswap_mintable_false"]))

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

    def test_insufficient_history_runs_training_only_search_but_never_reads_candidate_holdout(self):
        records = []
        for i in range(34):
            shift = i * 1_000.0
            record = path([
                (seconds, 1.0 - 0.0001 * seconds, 100_000.0)
                for seconds in range(0, 901, 30)
            ])
            record["mint"] = f"insufficient-sample-{i:03d}"
            record["entry_timestamp"] += shift
            record["observation_end_timestamp"] += shift
            for sample in record["price_history"]:
                sample["timestamp"] += shift
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

        report = analyze_records(records, min_total_paths=50)
        self.assertEqual(report["status"], "insufficient_history")
        self.assertEqual(report["usable_paths"], 34)
        self.assertEqual(report["train_rows"], 23)
        self.assertEqual(report["validation_rows"], 11)
        self.assertGreater(report["evaluated_candidate_configs"], 0)
        self.assertEqual(
            report["exploratory_status"],
            "training_only_search_complete_holdout_not_evaluated",
        )
        self.assertGreater(len(report["exploratory_training_candidates"]), 0)
        self.assertIsNone(report["selected_config"])
        self.assertIsNone(report["holdout"])

    def test_prospective_cohort_excludes_untagged_historical_paths(self):
        records = []
        for i in range(3):
            shift = i * 1_000.0
            record = path([
                (seconds, 1.0, 100_000.0)
                for seconds in (0, 112.5, 225, 337.5, 450, 562.5, 675, 787.5, 900)
            ])
            record["mint"] = f"prospective-test-{i}"
            record["entry_timestamp"] += shift
            record["observation_end_timestamp"] += shift
            for sample in record["price_history"]:
                sample["timestamp"] += shift
            record["entry_components"] = {
                "momentum_h1_pct": 15.0,
                "momentum_m5_pct": -1.0,
                "buy_sell_ratio": 1.5,
            }
            records.append(record)

        # Historical paths may look like the rule, but they have no entry-time tag.
        report = analyze_prospective_cohorts(records, min_total_paths=1)
        cohort = report["moderate_pullback_v1"]
        self.assertEqual(cohort["paths_received"], 0)
        self.assertEqual(cohort["usable_paths"], 0)
        self.assertEqual(cohort["status"], "insufficient_history")
        self.assertIsNone(cohort["selected_config"])

        # Only a path tagged when the current scanner version opened it joins the cohort.
        records[1]["prospective_entry_filters"] = ["moderate_pullback_v1"]
        report = analyze_prospective_cohorts(records, min_total_paths=1)
        cohort = report["moderate_pullback_v1"]
        self.assertEqual(cohort["paths_received"], 1)
        self.assertEqual(cohort["usable_paths"], 1)
        self.assertEqual(cohort["status"], "insufficient_history")
        self.assertIsNone(cohort["selected_config"])

    def test_prospective_cohort_registry_contains_all_eleven_preregistered_tags(self):
        import shadow_path_backtest as backtest

        expected = {
            "moderate_pullback_v1",
            "broad_pullback_v1",
            "liquid_pullback_v1",
            "tight_pullback_v1",
            "recovery_pullback_v1",
            "broad_moderate_momentum_v2",
            "volume_confirmed_pullback_v1",
            "steady_pressure_v1",
            "continuation_cooldown_v1",
            "weak_trend_recovery_v1",
            "risk_screened_pullback_v1",
            "score_band_35_50_v1",
            "score_band_50_60_v1",
            "score_band_60_70_v1",
            "score_band_70_80_v1",
            "score_band_80_100_v1",
        }
        self.assertEqual(set(backtest.PROSPECTIVE_ENTRY_TAGS), expected)
        reports = backtest.analyze_prospective_cohorts([], min_total_paths=1)
        self.assertEqual(set(reports), expected)
        for result in reports.values():
            self.assertEqual(result["status"], "insufficient_history")
            self.assertEqual(result["paths_received"], 0)
            self.assertIsNone(result["selected_config"])

    def test_rugcheck_entry_filters_fail_closed_when_report_is_missing_stale_or_risky(self):
        import shadow_path_backtest as backtest

        filters = {item["name"]: item for item in backtest.candidate_entry_filters()}
        record = path([(0, 1.0, 100_000), (300, 0.99, 100_000), (600, 0.98, 100_000),
                       (900, 0.97, 100_000)])
        record["score"] = 75.0
        safe = {
            "rugcheck_available": 1.0,
            "rugcheck_risk_data_available": 1.0,
            "rugcheck_full_risk_screen_available": 1.0,
            "rugcheck_report_age_seconds": 300.0,
            "rugcheck_mint_authority_active": 0.0,
            "rugcheck_freeze_authority_active": 0.0,
            "rugcheck_mint_close_authority_active": 0.0,
            "rugcheck_metadata_mutable": 0.0,
            "rugcheck_rugged": 0.0,
            "rugcheck_danger_risk_count": 0.0,
            "rugcheck_critical_risk_count": 0.0,
            "rugcheck_warning_risk_count": 0.0,
            "rugcheck_top1_pct": 6.0,
            "rugcheck_top10_pct": 20.0,
            "rugcheck_top20_pct": 34.0,
            "rugcheck_lp_locked_pct": 65.0,
            "rugcheck_holder_count": 1000.0,
        }
        record["entry_components"] = safe
        self.assertTrue(backtest.entry_filter_matches(record, filters["rugcheck_full_profile"]))
        self.assertTrue(backtest.entry_filter_matches(record, filters["rugcheck_no_danger_or_critical"]))
        self.assertTrue(backtest.entry_filter_matches(record, filters["rugcheck_low_concentration_locked_lp"]))
        self.assertTrue(backtest.entry_filter_matches(record, filters["rugcheck_no_warning_risk"]))
        for field, value in (
            ("rugcheck_full_risk_screen_available", 0.0),
            ("rugcheck_danger_risk_count", 1.0),
            ("rugcheck_rugged", 1.0),
            ("rugcheck_report_age_seconds", 25000.0),
        ):
            bad = dict(safe)
            bad[field] = value
            record["entry_components"] = bad
            self.assertFalse(backtest.entry_filter_matches(record, filters["rugcheck_no_danger_or_critical"]))
        missing = dict(safe)
        missing.pop("rugcheck_top20_pct")
        record["entry_components"] = missing
        self.assertFalse(backtest.entry_filter_matches(record, filters["rugcheck_low_concentration_locked_lp"]))

    def test_empty_history_never_selects_or_approves_a_rule(self):
        report = analyze_records([], min_total_paths=50)
        self.assertEqual(report["status"], "insufficient_history")
        self.assertEqual(report["eligible_training_candidates"], [])
        self.assertIsNone(report["selected_config"])
        self.assertEqual(report["train_rows"], 0)
        self.assertEqual(report["validation_rows"], 0)


if __name__ == "__main__":
    unittest.main()
