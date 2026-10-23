import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WorkflowSafetyTests(unittest.TestCase):
    def test_adaptive_update_targets_only_the_isolated_paper_account(self):
        workflow = (ROOT / ".github/workflows/trading-cycle.yml").read_text(encoding="utf-8")
        expected = (
            "python learn_agent.py --state paper_account/state.json "
            "--observations paper_account/strategy_observations.csv --apply"
        )
        self.assertIn(expected, workflow)
        self.assertNotIn(
            "python learn_agent.py --state state.json --observations strategy_observations.csv --apply",
            workflow,
        )

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
