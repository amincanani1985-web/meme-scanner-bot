import unittest

from paper_worker import run_loop


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class PaperWorkerScheduleTests(unittest.TestCase):
    def test_monitor_is_polled_frequently_while_scan_and_backtest_are_throttled(self):
        clock = FakeClock()
        events = []

        def scan(_root):
            events.append(("scan", clock.monotonic()))

        def monitor(_root):
            events.append(("monitor", clock.monotonic()))

        def backtest(_root):
            events.append(("backtest", clock.monotonic()))

        run_loop(
            root_dir=".",
            poll_interval=10,
            scan_interval=30,
            backtest_interval=20,
            monitor_fn=monitor,
            scan_fn=scan,
            backtest_fn=backtest,
            clock=clock.monotonic,
            sleep=clock.sleep,
            max_iterations=7,
        )

        self.assertEqual([t for name, t in events if name == "scan"], [0, 30, 60])
        self.assertEqual([t for name, t in events if name == "backtest"], [0, 20, 40, 60])
        self.assertEqual(
            [t for name, t in events if name == "monitor"],
            [0, 10, 20, 30, 40, 50, 60],
        )

    def test_scan_runs_only_the_isolated_paper_cycle_from_repo_root(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        import paper_worker

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "meme_scanner_cycle.py").write_text("# test entry point\\n", encoding="utf-8")

            with patch.object(paper_worker.subprocess, "run") as subprocess_run:
                paper_worker.scan_once(root)

            args, kwargs = subprocess_run.call_args
            command = args[0]
            self.assertEqual(kwargs["cwd"], str(root))
            self.assertIn("-c", command)
            self.assertIn("run_isolated_paper_cycle", command[-1])
            self.assertNotEqual(kwargs["cwd"], str(root / "paper_account"))

    def test_monitor_continues_after_a_scan_failure(self):
        clock = FakeClock()
        monitored = []

        def fail_scan(_root):
            raise RuntimeError("simulated scan failure")

        run_loop(
            root_dir=".",
            poll_interval=1,
            scan_interval=30,
            backtest_interval=60,
            monitor_fn=lambda _root: monitored.append(clock.monotonic()),
            scan_fn=fail_scan,
            backtest_fn=lambda _root: None,
            clock=clock.monotonic,
            sleep=clock.sleep,
            max_iterations=1,
        )

        self.assertEqual(monitored, [0])

    def test_invalid_intervals_are_rejected(self):
        with self.assertRaises(ValueError):
            run_loop(root_dir=".", poll_interval=0)
        with self.assertRaises(ValueError):
            run_loop(root_dir=".", scan_interval=-1)
        with self.assertRaises(ValueError):
            run_loop(root_dir=".", backtest_interval=0)


if __name__ == "__main__":
    unittest.main()
