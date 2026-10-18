import unittest

import whale_research as wr


class WhaleResearchTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
