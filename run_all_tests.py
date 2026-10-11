#!/usr/bin/env python3
"""Run the repository's deterministic tests and offline strategy report."""
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
    root = Path(__file__).resolve().parent
    py = sys.executable
    checks = [
        run([py, "-m", "py_compile", "meme_scanner_cycle.py", "learn_agent.py",
             "strategy_backtest.py", "tests/test_scanner_logic.py",
             "tests/test_adaptive.py", "tests/test_v17_improvements.py"],
            "Python syntax"),
        run([py, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"],
            "Unit tests"),
        run([py, "strategy_backtest.py"], "Offline walk-forward report"),
    ]
    print(f"\nSummary: {sum(checks)}/{len(checks)} checks passed.")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
