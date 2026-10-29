#!/usr/bin/env python3
"""Persistent paper-only memecoin worker for Railway.

Runs one scanner process at a time, polls exits frequently, and periodically
runs offline walk-forward reports. It never signs or submits blockchain trades.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

LOG = logging.getLogger("paper_worker")


def _run_json_command(command: list[str], *, cwd: Path, timeout: int = 180) -> dict:
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = completed.stdout.strip()
    if not output:
        return {}
    try:
        value = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"command did not emit valid JSON ({Path(command[1]).name}): {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"expected a JSON object from {Path(command[1]).name}")
    return value


def monitor_once(root_dir: str | Path) -> dict:
    """Update current paper positions only."""
    from paper_position_monitor import run

    return run(Path(root_dir))


def scan_once(root_dir: str | Path) -> None:
    """Run exactly one existing scanner cycle in the isolated paper directory."""
    root = Path(root_dir).resolve()
    paper_dir = root / "paper_account"
    paper_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.setdefault("SHADOW_PRICE_PATH_LOG", str(paper_dir / "shadow_price_paths.jsonl"))
    env.setdefault("MEMESWAP_RESEARCH_ENABLED", "1")
    env.setdefault("PAPER_REQUIRE_WHALE_ACCUMULATION", "1")
    completed = subprocess.run(
        [sys.executable, "-u", str(root / "meme_scanner_cycle.py")],
        cwd=str(paper_dir),
        env=env,
        check=True,
        timeout=240,
    )
    LOG.info("paper scan finished with exit_code=%d", completed.returncode)


def backtest_once(root_dir: str | Path) -> dict:
    """Run read-only chronological backtests and atomically persist their reports."""
    root = Path(root_dir).resolve()
    paper_dir = root / "paper_account"
    paper_dir.mkdir(parents=True, exist_ok=True)
    reports = (
        (
            "exit_path_backtest_latest.json",
            [
                sys.executable,
                str(root / "shadow_path_backtest.py"),
                "--paths",
                str(paper_dir / "shadow_price_paths.jsonl"),
            ],
        ),
        (
            "entry_strategy_backtest_latest.json",
            [
                sys.executable,
                str(root / "strategy_backtest.py"),
                "--observations",
                str(paper_dir / "strategy_observations.csv"),
            ],
        ),
    )
    summary: dict[str, dict] = {}
    for filename, command in reports:
        report = _run_json_command(command, cwd=root, timeout=180)
        destination = paper_dir / filename
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(
            json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(destination)
        status = report.get("status", "unknown")
        summary[filename] = report
        LOG.info("backtest report=%s status=%s", filename, status)
    return summary


def run_loop(
    root_dir: str | Path,
    poll_interval: float = 15,
    scan_interval: float = 300,
    backtest_interval: float = 900,
    monitor_fn: Optional[Callable[[str | Path], object]] = None,
    scan_fn: Optional[Callable[[str | Path], object]] = None,
    backtest_fn: Optional[Callable[[str | Path], object]] = None,
    clock: Optional[Callable[[], float]] = None,
    sleep: Optional[Callable[[float], None]] = None,
    stop_event: Optional[threading.Event] = None,
    max_iterations: Optional[int] = None,
) -> None:
    """Run tasks sequentially so the scanner and monitor never race on state.json."""
    for name, interval in (
        ("poll_interval", poll_interval),
        ("scan_interval", scan_interval),
        ("backtest_interval", backtest_interval),
    ):
        if not isinstance(interval, (int, float)) or interval <= 0:
            raise ValueError(f"{name} must be a positive number")
    root = Path(root_dir).resolve()
    if not (root / "meme_scanner_cycle.py").is_file():
        raise FileNotFoundError(f"scanner entry point not found in {root}")
    (root / "paper_account").mkdir(parents=True, exist_ok=True)

    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    stop_event = stop_event or threading.Event()
    monitor_fn = monitor_fn or monitor_once
    scan_fn = scan_fn or scan_once
    backtest_fn = backtest_fn or backtest_once

    next_scan = 0.0
    next_backtest = 0.0
    iterations = 0
    while not stop_event.is_set() and (max_iterations is None or iterations < max_iterations):
        now = clock()
        if now >= next_scan:
            try:
                scan_fn(root)
            except Exception:
                LOG.exception("paper scan failed; worker will keep monitoring")
            finally:
                next_scan = clock() + scan_interval

        # A single writer is intentional: both operations mutate paper state.
        try:
            result = monitor_fn(root)
            if isinstance(result, dict):
                LOG.info(
                    "monitor mode=%s equity=%s closed=%s positions=%s risk=%s",
                    result.get("mode", "unknown"),
                    result.get("equity", "n/a"),
                    result.get("closed_trades", "n/a"),
                    result.get("open_positions", "n/a"),
                    result.get("risk_status", "n/a"),
                )
            else:
                LOG.info("position monitor completed")
        except Exception:
            LOG.exception("position monitor failed")

        now = clock()
        if now >= next_backtest:
            try:
                report = backtest_fn(root)
                if isinstance(report, dict):
                    for name, result in report.items():
                        status = result.get("status", "unknown") if isinstance(result, dict) else "unknown"
                        LOG.info("backtest completed report=%s status=%s", name, status)
            except Exception:
                LOG.exception("walk-forward backtest failed; worker will keep running")
            finally:
                next_backtest = clock() + backtest_interval

        iterations += 1
        if stop_event.is_set() or (max_iterations is not None and iterations >= max_iterations):
            break
        sleep(poll_interval)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--poll-seconds", type=float, default=15)
    parser.add_argument("--scan-seconds", type=float, default=300)
    parser.add_argument("--backtest-seconds", type=float, default=900)
    args = parser.parse_args()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    stop_event = threading.Event()

    def request_stop(signum, _frame):
        LOG.info("received signal %s; stopping after current operation", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        run_loop(
            root_dir=args.root_dir,
            poll_interval=args.poll_seconds,
            scan_interval=args.scan_seconds,
            backtest_interval=args.backtest_seconds,
            stop_event=stop_event,
        )
    except Exception:
        LOG.exception("paper worker could not start")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
