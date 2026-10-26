import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scanner_simulator as simulator


def record(mint, timestamp):
    return {
        "mint": mint,
        "entry_timestamp": timestamp,
        "entry_price": 1.0,
        "entry_liquidity_usd": 100_000,
        "score": 70,
        "entry_components": {},
        "observed_duration_seconds": 900,
        "price_history": [
            {"timestamp": timestamp + i * 100, "price_usd": 1.0, "liquidity_usd": 100_000}
            for i in range(10)
        ],
    }


class ScannerSimulatorTests(unittest.TestCase):
    def test_loader_deduplicates_mints_and_rejects_bad_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paths.jsonl"
            path.write_text(
                json.dumps(record("mint-a", 1000)) + "\n"
                + json.dumps(record("mint-a", 2000)) + "\n"
                + "{not-json\n",
                encoding="utf-8",
            )
            records, rejected = simulator.load_paths([str(path)])
        self.assertEqual([row["mint"] for row in records], ["mint-a"])
        self.assertEqual(rejected, 1)

    def test_portfolio_does_not_double_spend_overlapping_positions(self):
        rows = [record("first", 1000), record("overlap", 1100), record("later", 3000)]

        def fake_exit(row, config):
            return {
                "status": "closed",
                "reason": "max_hold_timeout",
                "exit_timestamp": row["entry_timestamp"] + 1500,
                "net_pnl_pct": 10.0,
            }

        with patch.object(simulator, "simulate_exit", side_effect=fake_exit):
            report = simulator.portfolio_replay(
                rows, {"name": "all_research_candidates"}, {}, 1000.0, 10.0
            )
        self.assertEqual(report["trades"], 2)
        self.assertEqual(report["skipped_while_capital_busy"], 1)
        self.assertEqual(report["max_concurrent_positions"], 1)
        self.assertAlmostEqual(report["ending_equity"], 1020.1)
        self.assertAlmostEqual(report["return_pct"], 2.01)

    def test_empty_or_too_small_history_never_claims_profitability(self):
        report = simulator.analyze_portfolio([])
        self.assertEqual(report["status"], "insufficient_history")
        self.assertIsNone(report["selected_entry_filter"])
        self.assertIsNone(report["selected_exit_config"])
        self.assertIsNone(report["holdout_portfolio"])

    def test_portfolio_loss_reduces_equity_by_allocated_fraction(self):
        rows = [record("loser", 1000)]

        def fake_exit(row, config):
            return {
                "status": "closed",
                "reason": "hard_stop_loss",
                "exit_timestamp": row["entry_timestamp"] + 100,
                "net_pnl_pct": -20.0,
            }

        with patch.object(simulator, "simulate_exit", side_effect=fake_exit):
            report = simulator.portfolio_replay(
                rows, {"name": "all_research_candidates"}, {}, 1000.0, 10.0
            )
        self.assertAlmostEqual(report["ending_equity"], 980.0)
        self.assertAlmostEqual(report["return_pct"], -2.0)
        self.assertAlmostEqual(report["max_drawdown_pct"], 2.0)


if __name__ == "__main__":
    unittest.main()
