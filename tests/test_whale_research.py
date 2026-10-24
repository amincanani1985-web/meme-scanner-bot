import os
import tempfile
import time
import unittest
from unittest.mock import patch

import whale_research as wr


class WhaleResearchTests(unittest.TestCase):
    def test_research_uses_batched_pair_fetch_for_candidate_universe(self):
        pair = {
            "chainId": "solana",
            "pairCreatedAt": int(time.time() * 1000) - 7_200_000,
            "priceUsd": "1.0",
            "liquidity": {"usd": 100_000},
            "volume": {"h1": 50_000},
            "marketCap": 250_000,
            "txns": {"m5": {"buys": 30, "sells": 10}},
            "priceChange": {"m5": 3.0, "h1": 10.0},
            "baseToken": {"symbol": "TEST"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            previous_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with patch.object(wr, "helius_configured", return_value=True), \
                     patch.object(wr.scanner, "fetch_candidate_mints", return_value=["mint-a"]), \
                     patch.object(wr.scanner, "fetch_dex_pairs", return_value={"mint-a": pair}) as batch_fetch, \
                     patch.object(wr.scanner, "fetch_dex_pair", side_effect=AssertionError("per-token fetch should not run")), \
                     patch.object(wr, "fetch_whale_balances", return_value={"owner-a": 1000.0}):
                    report = wr.run(state_path="whale_watch_state.json")
                batch_fetch.assert_called_once_with(["mint-a"])
                self.assertEqual(report["snapshots_updated"], 1)
            finally:
                os.chdir(previous_cwd)

    def test_large_balance_drop_is_confirmed_only_with_market_sell_pressure(self):
        previous = {"wallet-a": 1000.0, "wallet-b": 500.0}
        current = {"wallet-a": 700.0, "wallet-b": 450.0}
        pair = {"txns": {"m5": {"buys": 5, "sells": 12}},
                "priceChange": {"m5": -2.0}}
        state = {}
        events = wr.record_balance_changes(state, "mint-a", previous, current, pair,
                                           timestamp="2026-01-01T00:00:00Z")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["owner"], "wallet-a")
        self.assertTrue(events[0]["market_confirmed"])
        self.assertEqual(state["wallets"]["wallet-a"]["confirmed_sell_pressure_events"], 1)

    def test_balance_drop_without_market_confirmation_is_not_called_a_dump(self):
        state = {}
        events = wr.record_balance_changes(
            state, "mint-a", {"wallet-a": 1000.0}, {"wallet-a": 700.0},
            {"txns": {"m5": {"buys": 20, "sells": 5}},
             "priceChange": {"m5": 3.0}},
            timestamp="2026-01-01T00:00:00Z",
        )
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0]["market_confirmed"])
        self.assertEqual(state["wallets"]["wallet-a"]["observed_balance_drops"], 1)
        self.assertEqual(state["wallets"]["wallet-a"]["confirmed_sell_pressure_events"], 0)

    def test_wallet_history_accumulates_across_tokens_without_claiming_profitability(self):
        state = {}
        for mint in ("mint-a", "mint-b"):
            wr.record_balance_changes(
                state, mint, {"wallet-a": 1000.0}, {"wallet-a": 700.0},
                {"txns": {"m5": {"buys": 1, "sells": 3}},
                 "priceChange": {"m5": -1.0}},
                timestamp="2026-01-01T00:00:00Z",
            )
        wallet = state["wallets"]["wallet-a"]
        self.assertEqual(wallet["observed_balance_drops"], 2)
        self.assertEqual(wallet["confirmed_sell_pressure_events"], 2)
        self.assertEqual(len(wallet["tokens_seen"]), 2)
        self.assertNotIn("profit", wallet)

    def test_large_balance_increase_is_recorded_as_accumulation_only_with_buy_pressure(self):
        state = {}
        events = wr.record_balance_increases(
            state, "mint-a", {"wallet-a": 1000.0, "wallet-b": 500.0},
            {"wallet-a": 1300.0, "wallet-b": 550.0},
            {"txns": {"m5": {"buys": 20, "sells": 5}},
             "priceChange": {"m5": 3.0}},
            timestamp="2026-01-01T00:00:00Z",
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["owner"], "wallet-a")
        self.assertEqual(events[0]["increase_pct"], 30.0)
        self.assertTrue(events[0]["market_confirmed"])
        self.assertEqual(state["wallets"]["wallet-a"]["confirmed_buy_pressure_events"], 1)

    def test_balance_increase_without_buy_pressure_is_not_a_confirmed_accumulation(self):
        state = {}
        events = wr.record_balance_increases(
            state, "mint-a", {"wallet-a": 1000.0}, {"wallet-a": 1300.0},
            {"txns": {"m5": {"buys": 3, "sells": 8}},
             "priceChange": {"m5": -1.0}},
            timestamp="2026-01-01T00:00:00Z",
        )
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0]["market_confirmed"])
        self.assertEqual(state["wallets"]["wallet-a"]["observed_balance_increases"], 1)
        self.assertEqual(state["wallets"]["wallet-a"]["confirmed_buy_pressure_events"], 0)


if __name__ == "__main__":
    unittest.main()
