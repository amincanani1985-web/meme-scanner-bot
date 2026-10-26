import time
import unittest

import whale_copy_paper as follower


WALLET = "WhaleWallet111111111111111111111111111111"
MEME = "MemeMint1111111111111111111111111111111111"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def swap(signature, timestamp, side, token_amount, quote_amount, quote_mint=USDC):
    if side == "buy":
        token_inputs = [{"mint": quote_mint, "tokenAmount": quote_amount, "userAccount": WALLET}]
        token_outputs = [{"mint": MEME, "tokenAmount": token_amount, "userAccount": WALLET}]
    else:
        token_inputs = [{"mint": MEME, "tokenAmount": token_amount, "userAccount": WALLET}]
        token_outputs = [{"mint": quote_mint, "tokenAmount": quote_amount, "userAccount": WALLET}]
    return {
        "signature": signature,
        "timestamp": timestamp,
        "type": "SWAP",
        "transactionError": None,
        "events": {"swap": {"tokenInputs": token_inputs, "tokenOutputs": token_outputs}},
    }


class WhaleCopyPaperTests(unittest.TestCase):
    def test_parse_swap_uses_wallet_side_and_quote_amount(self):
        tx = swap("sig-buy", 1_800_000_000, "buy", 1000, 100)
        parsed = follower.parse_swap_transaction(tx, WALLET)
        self.assertEqual(parsed["side"], "buy")
        self.assertEqual(parsed["mint"], MEME)
        self.assertEqual(parsed["token_amount"], 1000.0)
        self.assertEqual(parsed["quote_amount"], 100.0)
        self.assertEqual(parsed["quote_symbol"], "USDC")

    def test_parse_native_sol_swap_converts_lamports(self):
        tx = {
            "signature": "sig-sol",
            "timestamp": 1_800_000_000,
            "type": "SWAP",
            "transactionError": None,
            "events": {"swap": {
                "nativeInput": {"amount": "1500000000"},
                "tokenOutputs": [{
                    "mint": MEME,
                    "rawTokenAmount": {"tokenAmount": "2500000", "decimals": 6},
                    "userAccount": WALLET,
                }],
            }},
        }
        parsed = follower.parse_swap_transaction(tx, WALLET)
        self.assertEqual(parsed["side"], "buy")
        self.assertAlmostEqual(parsed["quote_amount"], 1.5)
        self.assertEqual(parsed["quote_symbol"], "SOL")
        self.assertAlmostEqual(parsed["token_amount"], 2.5)

    def test_non_swap_failed_and_unrelated_quote_transfers_are_ignored(self):
        tx = swap("sig-transfer", 1_800_000_000, "buy", 1000, 100)
        tx["type"] = "TRANSFER"
        self.assertIsNone(follower.parse_swap_transaction(tx, WALLET))
        tx["type"] = "SWAP"
        tx["transactionError"] = {"InstructionError": [1, "Custom"]}
        self.assertIsNone(follower.parse_swap_transaction(tx, WALLET))
        tx = swap("sig-stable-stable", 1_800_000_000, "buy", 100, 100, quote_mint=USDC)
        tx["events"]["swap"]["tokenOutputs"][0]["mint"] = USDC
        self.assertIsNone(follower.parse_swap_transaction(tx, WALLET))

    def test_wallet_ledger_calculates_realized_pnl_after_cost_reserve(self):
        record = {}
        trades = [
            follower.parse_swap_transaction(swap("buy1", 100, "buy", 1000, 100), WALLET),
            follower.parse_swap_transaction(swap("sell1", 200, "sell", 1000, 120), WALLET),
        ]
        result = follower.apply_wallet_trades(record, trades, fee_reserve_pct=1.0)
        self.assertEqual(result["new_closed_trades"], 1)
        self.assertAlmostEqual(record["closed_trades"][0]["net_pnl_pct"], 19.0)
        self.assertEqual(record["metrics"]["closed_trades"], 1)
        self.assertEqual(record["metrics"]["wins"], 1)

    def test_wallet_trade_processing_is_idempotent_by_signature(self):
        record = {}
        buy = follower.parse_swap_transaction(swap("buy1", 100, "buy", 1000, 100), WALLET)
        sell = follower.parse_swap_transaction(swap("sell1", 200, "sell", 1000, 110), WALLET)
        follower.apply_wallet_trades(record, [buy, sell])
        result = follower.apply_wallet_trades(record, [buy, sell])
        self.assertEqual(result["new_closed_trades"], 0)
        self.assertEqual(record["metrics"]["closed_trades"], 1)

    def test_wallet_qualification_rejects_small_or_unprofitable_samples(self):
        self.assertFalse(follower.wallet_qualifies({
            "closed_trades": 7, "wins": 7, "win_rate": 1.0,
            "profit_factor": 99.0, "mean_net_pnl_pct": 20.0,
        }))
        self.assertFalse(follower.wallet_qualifies({
            "closed_trades": 10, "wins": 8, "win_rate": 0.8,
            "profit_factor": 0.8, "mean_net_pnl_pct": 2.0,
        }))
        self.assertTrue(follower.wallet_qualifies({
            "closed_trades": 10, "wins": 6, "win_rate": 0.6,
            "profit_factor": 1.5, "mean_net_pnl_pct": 3.0,
        }))

    def test_wallet_selection_prioritizes_accumulation_over_sell_drops(self):
        watch = {"wallets": {
            "seller": {
                "confirmed_sell_pressure_events": 9,
                "observed_balance_drops": 12,
                "confirmed_buy_pressure_events": 0,
                "observed_balance_increases": 0,
                "tokens_seen": ["mint-a"],
            },
            "accumulator": {
                "confirmed_sell_pressure_events": 0,
                "observed_balance_drops": 0,
                "confirmed_buy_pressure_events": 4,
                "observed_balance_increases": 6,
                "tokens_seen": ["mint-a", "mint-b"],
            },
        }}
        selected = follower.select_wallets(watch, {"wallets": {}}, limit=1)
        self.assertEqual(selected, ["accumulator"])

    def test_only_previously_profitable_wallets_emit_fresh_buy_signals(self):
        now = time.time()
        good_history = [{"net_pnl_pct": 10.0} for _ in range(6)] + [
            {"net_pnl_pct": -2.0}, {"net_pnl_pct": -2.0}
        ]
        record = {"positions": {}, "seen_signatures": [], "closed_trades": good_history}
        trade = follower.parse_swap_transaction(
            swap("fresh-good-buy", now - 30, "buy", 1000, 100), WALLET
        )
        result = follower.apply_wallet_trades(record, [trade], now=now)
        self.assertEqual(len(result["buy_signals"]), 1)
        self.assertEqual(result["buy_signals"][0]["wallet"], WALLET)

        poor_record = {"positions": {}, "seen_signatures": [],
                       "closed_trades": [{"net_pnl_pct": -5.0} for _ in range(12)]}
        poor_trade = follower.parse_swap_transaction(
            swap("fresh-bad-buy", now - 30, "buy", 1000, 100), WALLET
        )
        poor_result = follower.apply_wallet_trades(poor_record, [poor_trade], now=now)
        self.assertEqual(poor_result["buy_signals"], [])

    def test_expired_copy_signal_is_never_eligible(self):
        now = time.time()
        self.assertTrue(follower.signal_is_fresh(now - 30, now=now, max_age_seconds=60))
        self.assertFalse(follower.signal_is_fresh(now - 61, now=now, max_age_seconds=60))
        self.assertFalse(follower.signal_is_fresh(now + 1, now=now, max_age_seconds=60))


if __name__ == "__main__":
    unittest.main()
