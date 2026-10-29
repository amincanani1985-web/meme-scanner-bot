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
