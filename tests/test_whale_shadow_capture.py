import json
import tempfile
import unittest
from pathlib import Path

import whale_shadow_capture as capture


def buy(wallet, mint, timestamp, signature):
    return {"wallet": wallet, "mint": mint, "timestamp": timestamp, "side": "buy",
            "token_amount": 100, "quote_amount": 1, "quote_symbol": "SOL",
            "signature": signature}


def quote(price=1.0):
    return {"priceUsd": str(price), "liquidity": {"usd": 100000}, "baseToken": {"symbol": "TEST"}}


class WhaleShadowCaptureTests(unittest.TestCase):
    def fixture(self, state):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        (root / "paper_account").mkdir()
        (root / "whale_copy_state.json").write_text(json.dumps(state), encoding="utf-8")
        self.addCleanup(tmp.cleanup)
        return root

    def test_recent_buy_consensus_counts_distinct_wallets_only(self):
        state = {"wallets": {
            "a": {"normalized_swaps": [buy("a", "mint-a", 980, "a1"), buy("a", "mint-a", 990, "a2")]},
            "b": {"normalized_swaps": [buy("b", "mint-a", 995, "b1")]},
            "c": {"normalized_swaps": [buy("c", "old", 10, "c1")]},
        }}
        signals, rejected = capture.recent_buy_signals(state, now=1000)
        self.assertEqual(signals["mint-a"]["wallet_count"], 2)
        self.assertEqual(signals["mint-a"]["signal_mode"], "two_wallet_consensus")
        self.assertNotIn("old", signals)
        self.assertEqual(rejected, 0)

    def test_monitor_list_is_bounded_and_deduplicated(self):
        root = self.fixture({"wallets": {"a": {"normalized_swaps": [
            buy("a", f"mint-{i}", 990 + i, f"s{i}") for i in range(8)
        ]}}})
        (root / "paper_account" / capture.STATE_FILENAME).write_text(json.dumps({
            "version": 1, "active": {"active": {}}, "completed_mints": ["mint-7"]
        }), encoding="utf-8")
        mints = capture.monitored_mints(root, now=1000, max_new_mints=3)
        self.assertEqual(mints[0], "active")
        self.assertEqual(len(mints), 4)
        self.assertEqual(len(set(mints)), len(mints))
        self.assertNotIn("mint-7", mints)

    def test_entry_starts_at_observed_quote_not_historical_swap_time(self):
        root = self.fixture({"wallets": {"a": {"normalized_swaps": [buy("a", "mint-a", 1000, "sig")]}}})
        result = capture.sample(root, lambda mint: quote(2), now=1030)
        state = json.loads((root / "paper_account" / capture.STATE_FILENAME).read_text())
        row = state["active"]["mint-a"]
        self.assertEqual(row["signal_timestamp"], 1000)
        self.assertEqual(row["entry_timestamp"], 1030)
        self.assertEqual(row["price_history"][0]["price_usd"], 2)
        self.assertEqual(result["new_paths_started"], 1)
        self.assertFalse(result["live_trading_authorized"])

    def test_invalid_quote_does_not_start_a_path(self):
        root = self.fixture({"wallets": {"a": {"normalized_swaps": [buy("a", "mint-a", 1000, "sig")]}}})
        result = capture.sample(root, lambda mint: quote(0), now=1030)
        state = json.loads((root / "paper_account" / capture.STATE_FILENAME).read_text())
        self.assertEqual(state.get("active", {}), {})
        self.assertEqual(result["quotes_failed"], 1)

    def test_same_timestamp_does_not_duplicate_quote_samples(self):
        root = self.fixture({"wallets": {"a": {"normalized_swaps": [buy("a", "mint-a", 1000, "sig")]}}})
        capture.sample(root, lambda mint: quote(1), now=1030)
        capture.sample(root, lambda mint: quote(1.1), now=1030)
        capture.sample(root, lambda mint: quote(1.2), now=1060)
        state = json.loads((root / "paper_account" / capture.STATE_FILENAME).read_text())
        history = state["active"]["mint-a"]["price_history"]
        self.assertEqual([p["timestamp"] for p in history], [1030, 1060])
        self.assertEqual(history[-1]["price_usd"], 1.2)

    def test_path_is_saved_once_after_900_seconds_of_observed_quotes(self):
        root = self.fixture({"wallets": {"a": {"normalized_swaps": [buy("a", "mint-a", 1000, "sig")]}}})
        for seconds in range(0, 901, 30):
            capture.sample(root, lambda mint, s=seconds: quote(1 + s / 9000), now=1000 + seconds)
        log = root / "paper_account" / capture.PATHS_FILENAME
        rows = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]["price_history"]), 31)
        self.assertEqual(rows[0]["price_history"][-1]["timestamp"], 1900)
        self.assertFalse(rows[0]["live_trading_authorized"])
        capture.sample(root, lambda mint: quote(9), now=1930)
        self.assertEqual(len(log.read_text().splitlines()), 1)

    def test_bad_swap_rows_are_rejected(self):
        state = {"wallets": {"a": {"normalized_swaps": [
            buy("a", "mint-a", 990, "good"),
            {**buy("a", "mint-b", 991, "bad-side"), "side": "transfer"},
            {**buy("a", "mint-c", 992, "bad-quote"), "quote_symbol": "UNKNOWN"},
            {**buy("a", "mint-d", 993, "zero"), "quote_amount": 0},
        ]}}}
        signals, rejected = capture.recent_buy_signals(state, now=1000)
        self.assertEqual(set(signals), {"mint-a"})
        self.assertGreaterEqual(rejected, 3)


if __name__ == "__main__":
    unittest.main()
