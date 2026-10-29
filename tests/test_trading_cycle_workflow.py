import unittest
from pathlib import Path


class TradingCycleWorkflowTests(unittest.TestCase):
    def test_paper_entry_step_runs_only_the_isolated_paper_cycle(self):
        workflow_path = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "trading-cycle.yml"
        )
        workflow = workflow_path.read_text(encoding="utf-8")

        start = workflow.index("- name: Run one discovery and entry cycle")
        end = workflow.index("\n      - name:", start + 1)
        step = workflow[start:end]

        self.assertIn("run_isolated_paper_cycle", step)
        self.assertNotIn("python ../meme_scanner_cycle.py", step)
        self.assertNotIn("working-directory: paper_account", step)
        self.assertEqual(step.count("run_isolated_paper_cycle"), 1)


if __name__ == "__main__":
    unittest.main()
