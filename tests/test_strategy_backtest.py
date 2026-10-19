import tempfile
import unittest
from pathlib import Path

import strategy_backtest as backtest


class StrategyBacktestTests(unittest.TestCase):
    def test_insufficient_history_is_not_reported_as_validated(self):
        report = backtest.run([])
        self.assertEqual(report["status"], "insufficient_history")
        self.assertEqual(report["required_rows"], 30)

    def test_invalid_and_unscored_rows_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.csv"
            path.write_text(
                "timestamp,score,pnl_pct,reason\n"
                "2026-01-01,85,2,ok\n"
                "2026-01-02,,3,missing_score\n"
                "2026-01-03,NaN,1,nonfinite\n"
                "2026-01-04,101,1,out_of_range\n",
                encoding="utf-8",
            )
            rows = backtest.load(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["score"], 85.0)

    def test_history_without_enough_samples_per_threshold_is_not_validated(self):
        rows = [{"timestamp": f"{i:03d}", "score": 50.0, "pnl_pct": 5.0} for i in range(30)]
        report = backtest.run(rows)
        self.assertEqual(report["status"], "insufficient_threshold_samples")
        self.assertEqual(report["eligible_thresholds"], [])

    def test_valid_threshold_requires_minimum_out_of_sample_count(self):
        rows = [{"timestamp": f"{i:03d}", "score": 85.0, "pnl_pct": 1.0} for i in range(30)]
        report = backtest.run(rows)
        self.assertEqual(report["status"], "ok")
        self.assertIn(85.0, report["eligible_thresholds"])
        self.assertEqual(report["thresholds"]["85.0"]["validation"]["n"], 9)



    def test_gross_profit_below_assumed_cost_is_not_validated(self):
        rows = [
            {"timestamp": f"{i:03d}", "score": 85.0, "pnl_pct": 1.0}
            for i in range(30)
        ]
        report = backtest.run(rows)
        self.assertNotIn(85.0, report["eligible_thresholds"])
        self.assertEqual(report["status"], "no_validated_threshold")
        metrics = report["thresholds"]["85.0"]["validation"]
        self.assertGreater(metrics["avg_pnl"], 0.0)
        self.assertLess(metrics["net_avg_pnl"], 0.0)



if __name__ == "__main__":
    unittest.main()
