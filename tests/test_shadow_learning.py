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
            rows.append({
                "timestamp": f"{i:03d}",
                "score": 75.0,
                "pnl_pct": 3.0 if i % 3 else -1.0,
                "reason": "shadow_max_hold_timeout",
            })
        report = sl.analyze(rows, min_validation_samples=8)
        self.assertEqual(report["status"], "no_validated_bucket")
        self.assertEqual(report["recommendations"], [])
        self.assertGreaterEqual(report["validation_rows"], 8)

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
