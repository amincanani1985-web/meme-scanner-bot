#!/usr/bin/env python3
"""Run deterministic tests and offline strategy research reports."""
import subprocess
import sys
from pathlib import Path


def run(command, label):
    print(f"\n=== {label} ===", flush=True)
    result = subprocess.run(command, check=False)
    if result.returncode:
        print(f"FAILED: {label} (exit {result.returncode})", flush=True)
        return False
    print(f"PASSED: {label}", flush=True)
    return True


def main():
    py = sys.executable
    checks = [
        run([py, "-m", "py_compile", "memeswap_research.py",
             "tests/test_memeswap_research.py", "meme_scanner_cycle.py",
             "learn_agent.py", "strategy_backtest.py", "shadow_path_backtest.py",
             "scanner_simulator.py", "tests/test_scanner_simulator.py",
             "whale_behavior_lab.py", "tests/test_whale_behavior_lab.py",
             "market_data_connectors.py", "tests/test_market_data_connectors.py",
             "whale_follow_backtest.py", "tests/test_whale_follow_backtest.py",
             "whale_shadow_capture.py", "tests/test_whale_shadow_capture.py",
             "tests/test_scanner_logic.py", "tests/test_adaptive.py",
             "tests/test_v17_improvements.py", "tests/test_shadow_path_backtest.py"],
            "Python syntax"),
        run([py, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"],
            "Unit tests"),
        run([py, "strategy_backtest.py"], "Entry-strategy walk-forward report"),
        run([py, "scanner_simulator.py"], "Observed-path portfolio replay"),
    ]
    print(f"\nSummary: {sum(checks)}/{len(checks)} checks passed.")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
