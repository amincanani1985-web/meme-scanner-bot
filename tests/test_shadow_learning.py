import unittest

import shadow_learning as sl


class ShadowLearningTests(unittest.TestCase):
    def test_metrics_separates_raw_mean_from_outlier_resistant_mean(self):
        rows = [
            {"score": 75.0, "pnl_pct": 5.0, "timestamp": "1"},
            {"score": 75.0, "pnl_pct": -5.0, "timestamp": "2"},
            {"score": 75.0, "pnl_pct": 2021.0, "timestamp": "3"},
        ]
        result = sl.metrics(rows)
        self.assertEqual(result["n"], 3)
        self.assertEqual(result["outlier_count"], 1)
        self.assertAlmostEqual(result["robust_mean_pnl"], 0.0)
        self.assertGreater(result["mean_pnl"], 600.0)

    def test_round_trip_cost_can_turn_gross_profit_into_net_loss(self):
        rows = [
            {"score": 85.0, "pnl_pct": 3.0, "timestamp": "1"},
            {"score": 85.0, "pnl_pct": -1.0, "timestamp": "2"},
        ]
        result = sl.metrics(rows, round_trip_cost_pct=2.0)
        self.assertAlmostEqual(result["mean_pnl"], 1.0)
        self.assertAlmostEqual(result["net_mean_pnl"], -1.0)
        self.assertAlmostEqual(result["net_median_pnl"], -1.0)

    def test_analyzer_refuses_to_recommend_with_insufficient_history(self):
        rows = [
            {"score": 90.0, "pnl_pct": 10.0, "timestamp": f"{i:03d}"}
            for i in range(29)
        ]
        report = sl.analyze(rows)
        self.assertEqual(report["status"], "insufficient_history")
        self.assertEqual(report["rows"], 29)
        self.assertEqual(report["recommendations"], [])

    def test_out_of_sample_recommendation_requires_minimum_validation_samples(self):
        rows = []
        for i in range(70):
            score = 75.0 if i < 51 else 50.0
            pnl = 3.0 if i < 51 else -3.0
            rows.append({
                "timestamp": f"{i:03d}",
                "score": score,
                "pnl_pct": pnl,
                "reason": "shadow_max_hold_timeout",
            })
        report = sl.analyze(rows, min_validation_samples=8)
        self.assertEqual(report["status"], "no_validated_bucket")
        self.assertEqual(report["recommendations"], [])
        self.assertGreaterEqual(report["validation_rows"], 8)

    def test_profitable_gross_returns_do_not_pass_if_net_of_costs_is_negative(self):
        rows = [
            {"timestamp": f"{i:03d}", "score": 95.0, "pnl_pct": 1.0}
            for i in range(70)
        ]
        report = sl.analyze(rows, min_validation_samples=8, round_trip_cost_pct=2.0)
        self.assertEqual(report["status"], "no_validated_bucket")
        self.assertEqual(report["recommendations"], [])
        self.assertEqual(report["assumed_round_trip_cost_pct"], 2.0)
        self.assertLess(report["buckets"]["90-100"]["net_robust_mean_pnl"], 0.0)

    def test_recommendation_is_based_on_chronological_holdout_not_training_rows(self):
        rows = []
        for i in range(70):
            # Early data looks profitable, but the later holdout is consistently losing.
            pnl = 5.0 if i < 49 else -3.0
            rows.append({"timestamp": f"{i:03d}", "score": 75.0, "pnl_pct": pnl})
        report = sl.analyze(rows, min_validation_samples=8)
        self.assertEqual(report["status"], "no_validated_bucket")
        self.assertEqual(report["recommendations"], [])


if __name__ == "__main__":
    unittest.main()

    def test_wilson_lower_bound_is_conservative_for_small_samples(self):
        self.assertLess(sl.wilson_lower_bound(4, 8), 0.5)
        self.assertGreater(sl.wilson_lower_bound(8, 8), 0.5)

    def test_feature_associations_ignore_missing_and_nonfinite_values(self):
        rows = [
            {"pnl_pct": 5.0, "features": {"momentum_m5_pct": 1.0}},
            {"pnl_pct": 10.0, "features": {"momentum_m5_pct": 2.0}},
            {"pnl_pct": -5.0, "features": {"momentum_m5_pct": 3.0}},
            {"pnl_pct": 1.0, "features": {"momentum_m5_pct": float("nan")}},
            {"pnl_pct": 7.0, "features": {}},
        ]
        result = sl.feature_associations(rows)
        self.assertEqual(result["momentum_m5_pct"]["n"], 3)
        self.assertLess(result["momentum_m5_pct"]["correlation"], 0.0)

    def test_shadow_recommendation_requires_positive_confidence_lower_bound(self):
        rows = []
        # 8 validation wins at a barely profitable net return should not be enough
        # if the lower confidence bound on the win rate is weak.
        for i in range(70):
            score = 95.0
            pnl = 5.0 if i < 62 else -0.1
            rows.append({"timestamp": f"{i:03d}", "score": score,
                         "pnl_pct": pnl, "reason": "test", "features": {}})
        report = sl.analyze(rows, round_trip_cost_pct=2.0)
        bucket = report["buckets"]["90-100"]
        self.assertIn("net_win_rate_wilson_lower", bucket)
        self.assertFalse(bucket["eligible"])
