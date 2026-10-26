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

    def test_memeswap_enrichment_is_read_only_optional_and_in_the_test_gate(self):
        workflow = (ROOT / ".github/workflows/trading-cycle.yml").read_text(encoding="utf-8")
        self.assertIn('MEMESWAP_RESEARCH_ENABLED: "1"', workflow)
        ci_workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        compile_step = next(
            line for line in ci_workflow.splitlines()
            if "python -m py_compile" in line and "meme_scanner_cycle.py" in line
        )
        self.assertIn("memeswap_research.py", compile_step)
        self.assertIn("tests/test_memeswap_research.py", compile_step)

        scanner_source = (ROOT / "meme_scanner_cycle.py").read_text(encoding="utf-8")
        self.assertIn("MAX_MEMESWAP_RESEARCH_PROFILES_PER_CYCLE", scanner_source)
        self.assertIn("fetch_memeswap_token_profile", scanner_source)
        self.assertIn("scan_shadow_candidates", scanner_source)
        live_open = scanner_source[scanner_source.index("def open_position("):
            scanner_source.index("def close_position(")
        ]
        self.assertNotIn("fetch_memeswap_token_profile", live_open)
        self.assertNotIn("MEMESWAP_API_KEY", scanner_source)

    def test_existing_and_new_paper_positions_are_monitored_in_the_same_cycle(self):
        workflow = (ROOT / ".github/workflows/trading-cycle.yml").read_text(encoding="utf-8")
        initial = workflow.index("- name: Monitor existing paper positions with 30-second gaps")
        scan = workflow.index("- name: Run one discovery and entry cycle")
        post_entry = workflow.index("- name: Monitor newly opened paper positions immediately")
        refresh = workflow.index("- name: Refresh learning reports after the latest outcomes")
        self.assertLess(initial, workflow.index("- name: Run scanner tests 10 times"))
        self.assertLess(initial, scan)
        self.assertLess(scan, post_entry)
        self.assertLess(post_entry, refresh)
        # Shift monitoring time from before candidate discovery to the fresh
        # positions opened by that discovery. This keeps the total polling
        # budget nearly constant but reduces the first unobserved entry gap.
        self.assertIn("for check in 1 2; do", workflow)
        self.assertIn('Monitor-only check $check/2', workflow)
        self.assertIn("for check in 1 2 3 4 5 6 7 8; do", workflow)
        self.assertIn('Immediate post-entry check $check/8', workflow)
        self.assertNotIn("for check in 1 2 3 4 5 6; do", workflow)
        self.assertIn("python paper_position_monitor.py", workflow)
        self.assertIn("sleep 30", workflow)
        self.assertNotIn("for cycle in 1 2 3", workflow)

    def test_pull_request_runs_cannot_block_the_main_paper_cycle(self):
        workflow = (ROOT / ".github/workflows/trading-cycle.yml").read_text(encoding="utf-8")
        self.assertIn(
            "group: trading-cycle-${{ github.event_name == 'pull_request' && format('pr-{0}', github.event.pull_request.number) || github.ref }}",
            workflow,
        )
        self.assertIn("cancel-in-progress: false", workflow)

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
