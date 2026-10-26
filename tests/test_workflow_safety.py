import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WorkflowSafetyTests(unittest.TestCase):
    def test_adaptive_update_uses_its_own_paper_account_observations(self):
        workflow = (ROOT / ".github/workflows/trading-cycle.yml").read_text(encoding="utf-8")
        expected = (
            "python learn_agent.py --state paper_account/state.json "
            "--observations paper_account/strategy_observations.csv --apply"
        )
        self.assertIn(expected, workflow)
        self.assertIn(
            "python strategy_backtest.py --observations paper_account/strategy_observations.csv",
            workflow,
        )
        self.assertIn(
            "python strategy_backtest.py --observations paper_account/whale_strategy_observations.csv",
            workflow,
        )
        self.assertIn('"paper_whale_strategy": paper_whale_strategy', workflow)
        self.assertIn('Path("dashboard/backtest-data.js").write_text', workflow)
        self.assertIn('"paper_strategy": paper_strategy', workflow)
        self.assertNotIn(
            "python learn_agent.py --state paper_account/state.json "
            "--observations paper_account/whale_strategy_observations.csv --apply",
            workflow,
        )
        self.assertNotIn(
            "python learn_agent.py --state state.json --observations strategy_observations.csv --apply",
            workflow,
        )

    def test_shadow_path_writer_and_backtest_use_only_the_isolated_paper_ledger(self):
        workflow = (ROOT / ".github/workflows/trading-cycle.yml").read_text(encoding="utf-8")
        self.assertIn('SHADOW_PRICE_PATH_LOG: "${{ github.workspace }}/paper_account/shadow_price_paths.jsonl"', workflow)
        self.assertIn(
            "python shadow_path_backtest.py --paths paper_account/shadow_price_paths.jsonl",
            workflow,
        )
        self.assertNotIn(
            "python shadow_path_backtest.py --paths shadow_price_paths.jsonl paper_account/shadow_price_paths.jsonl",
            workflow,
        )
        self.assertIn(
            "if [ -f paper_account/shadow_price_paths.jsonl ]; then "
            "git add paper_account/shadow_price_paths.jsonl; fi",
            workflow,
        )
        scanner_source = (ROOT / "meme_scanner_cycle.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.get("SHADOW_PRICE_PATH_LOG", "shadow_price_paths.jsonl")', scanner_source)
        self.assertIn('Path("paper_account/shadow_price_paths.jsonl")', scanner_source)

    def test_discovery_cycle_runs_in_the_same_isolated_paper_account_as_monitoring(self):
        workflow = (ROOT / ".github/workflows/trading-cycle.yml").read_text(encoding="utf-8")
        marker = "- name: Run one discovery and entry cycle"
        self.assertIn(marker, workflow)
        section = workflow.split(marker, 1)[1].split("# Recompute after the scan", 1)[0]
        self.assertIn("working-directory: paper_account", section)
        self.assertIn("run: python ../meme_scanner_cycle.py", section)
        self.assertNotIn("run: python meme_scanner_cycle.py", section)
        self.assertIn("python paper_position_monitor.py", workflow)
        self.assertIn("--state paper_account/state.json", workflow)
        self.assertIn("--observations paper_account/strategy_observations.csv", workflow)

    def test_exit_monitoring_samples_positions_for_four_minutes_without_rediscovery(self):
        workflow = (ROOT / ".github/workflows/trading-cycle.yml").read_text(encoding="utf-8")
        self.assertIn("- name: Run one discovery and entry cycle", workflow)
        self.assertIn("- name: Monitor existing paper positions with 30-second gaps", workflow)
        self.assertIn("for check in 1 2 3 4 5 6 7 8", workflow)
        self.assertIn('Monitor-only check $check/8', workflow)
        self.assertIn("python paper_position_monitor.py", workflow)
        self.assertIn("sleep 30", workflow)
        monitor_at = workflow.index("- name: Monitor existing paper positions")
        self.assertLess(monitor_at, workflow.index("- name: Run scanner tests 10 times"))
        self.assertLess(monitor_at, workflow.index("- name: Observe whale wallet behavior"))
        self.assertLess(monitor_at, workflow.index("- name: Run one discovery and entry cycle"))
        self.assertNotIn("for check in 1 2 3 4; do", workflow)
        self.assertNotIn("for cycle in 1 2 3", workflow)

    def test_trading_cycle_cannot_recursively_start_the_validation_loop(self):
        workflow = (ROOT / ".github/workflows/trading-cycle.yml").read_text(encoding="utf-8")
        self.assertNotIn("gh workflow run paper-profit-loop.yml", workflow)

    def test_validation_loop_is_manual_bounded_and_not_profit_peeking(self):
        workflow = (ROOT / ".github/workflows/paper-profit-loop.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("  push:", workflow)
        self.assertNotIn("if net > 0:", workflow)
        self.assertIn("for cycle in $(seq 1 10)", workflow)
        self.assertIn("Reached the 10-cycle validation limit", workflow)


if __name__ == "__main__":
    unittest.main()
