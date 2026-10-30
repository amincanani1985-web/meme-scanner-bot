import time
import tempfile
import unittest
from unittest.mock import Mock, patch

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
        record = {"history_complete": True}
        trades = [
            follower.parse_swap_transaction(swap("buy1", 100, "buy", 1000, 100), WALLET),
            follower.parse_swap_transaction(swap("sell1", 200, "sell", 1000, 120), WALLET),
        ]
        result = follower.apply_wallet_trades(record, trades, fee_reserve_pct=1.0)
        self.assertEqual(result["new_closed_trades"], 1)
        self.assertAlmostEqual(record["closed_trades"][0]["net_pnl_pct"], 19.0)
        self.assertEqual(record["metrics"]["closed_trades"], 1)
        self.assertEqual(record["metrics"]["wins"], 1)


    def test_partial_sells_are_one_completed_round_trip_not_many_trades(self):
        record = {"history_complete": True}
        buy = follower.parse_swap_transaction(
            swap("buy-partials", 100, "buy", 1000, 100), WALLET
        )
        follower.apply_wallet_trades(record, [buy], now=100)

        # Eight partial sells are still one open position, not eight completed
        # trades that can inflate a wallet's qualification sample.
        for index in range(8):
            partial = follower.parse_swap_transaction(
                swap(f"partial-{index}", 200 + index, "sell", 100, 15), WALLET
            )
            follower.apply_wallet_trades(record, [partial], now=300 + index)
            self.assertEqual(record["closed_trades"], [])
            self.assertEqual(record["metrics"]["closed_trades"], 0)
            self.assertAlmostEqual(
                record["positions"][MEME]["token_amount"], 900 - index * 100
            )

        final_sell = follower.parse_swap_transaction(
            swap("final-partial", 300, "sell", 200, 25), WALLET
        )
        result = follower.apply_wallet_trades(record, [final_sell], now=400)

        self.assertEqual(result["new_closed_trades"], 1)
        self.assertEqual(len(record["closed_trades"]), 1)
        self.assertEqual(record["metrics"]["closed_trades"], 1)
        self.assertAlmostEqual(record["closed_trades"][0]["net_pnl_pct"], 44.0)
        self.assertEqual(record["positions"], {})

    def test_wallet_trade_processing_is_idempotent_by_signature(self):
        record = {"history_complete": True}
        buy = follower.parse_swap_transaction(swap("buy1", 100, "buy", 1000, 100), WALLET)
        sell = follower.parse_swap_transaction(swap("sell1", 200, "sell", 1000, 110), WALLET)
        follower.apply_wallet_trades(record, [buy, sell])
        result = follower.apply_wallet_trades(record, [buy, sell])
        self.assertEqual(result["new_closed_trades"], 0)
        self.assertEqual(record["metrics"]["closed_trades"], 1)

    def test_late_historical_backfill_rebuilds_wallet_pnl_in_timestamp_order(self):
        old_buy = follower.parse_swap_transaction(
            swap("old-buy", 100, "buy", 1000, 50), WALLET
        )
        recent_buy = follower.parse_swap_transaction(
            swap("recent-buy", 200, "buy", 1000, 100), WALLET
        )
        sell = follower.parse_swap_transaction(
            swap("recent-sell", 300, "sell", 1000, 120), WALLET
        )
        final_sell = follower.parse_swap_transaction(
            swap("recent-sell-final", 301, "sell", 1000, 120), WALLET
        )

        # Simulate the scanner fetching a fresh page first, then backfilling an
        # older page on the next cycle. Results must match a full chronological replay.
        incremental = {"history_complete": False}
        follower.apply_wallet_trades(incremental, [recent_buy, sell, final_sell], now=400)
        self.assertEqual(incremental["closed_trades"], [])
        # The final short page establishes complete history; replay must now
        # include the delayed older buy in the cost basis of the later sale.
        incremental["history_complete"] = True
        follower.apply_wallet_trades(incremental, [old_buy], now=400)

        chronological = {"history_complete": True}
        follower.apply_wallet_trades(
            chronological, [old_buy, recent_buy, sell, final_sell], now=400
        )

        self.assertEqual(len(incremental["closed_trades"]), 1)
        self.assertAlmostEqual(incremental["closed_trades"][0]["net_pnl_pct"], 59.0)
        self.assertEqual(
            incremental["closed_trades"], chronological["closed_trades"]
        )
        self.assertEqual(
            incremental["positions"], chronological["positions"]
        )
        self.assertEqual(incremental["metrics"], chronological["metrics"])

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
            "closed_trades": 30, "wins": 18, "win_rate": 0.6,
            "profit_factor": 1.5, "mean_net_pnl_pct": 3.0,
        }))

    def test_wallet_qualification_accepts_strong_positive_expectancy_with_lower_win_rate(self):
        self.assertTrue(follower.wallet_qualifies({
            "closed_trades": 145, "wins": 62, "win_rate": 0.4276,
            "profit_factor": 1.3428, "mean_net_pnl_pct": 6.3932,
        }))

    def test_wallet_qualification_rejects_low_sample_even_with_strong_metrics(self):
        self.assertFalse(follower.wallet_qualifies({
            "closed_trades": 29, "wins": 13, "win_rate": 0.4483,
            "profit_factor": 1.8, "mean_net_pnl_pct": 4.0,
        }))


    def _run_copy_paper_with_signals(self, folder, buy_signals, **options):
        pair = {
            "priceUsd": "1.0",
            "liquidity": {"usd": 100000},
            "baseToken": {"symbol": "TEST"},
        }
        with patch.object(follower, "_copy_entry_allowed", return_value=(True, "ok")), \
             patch.object(follower.scanner, "fetch_dex_pair", return_value=pair):
            return follower.run_copy_paper_account(
                buy_signals,
                [],
                state_path=f"{folder}/state.json",
                ledger_path=f"{folder}/trades.csv",
                now=1030,
                **options,
            )

    def test_default_copy_mode_still_requires_two_distinct_wallets(self):
        signal = {
            "wallet": "qualified-wallet",
            "mint": MEME,
            "timestamp": 1000,
            "signature": "buy-one",
            "quote_symbol": "USDC",
            "leader_price_quote": 1.0,
        }
        with tempfile.TemporaryDirectory() as folder:
            report = self._run_copy_paper_with_signals(folder, [signal])
        self.assertEqual(report["opened_this_cycle"], 0)
        self.assertEqual(report["open_positions"], 0)

    def test_single_qualified_wallet_experiment_can_copy_one_leader(self):
        signal = {
            "wallet": "qualified-wallet",
            "mint": MEME,
            "timestamp": 1000,
            "signature": "buy-one",
            "quote_symbol": "USDC",
            "leader_price_quote": 1.0,
        }
        with tempfile.TemporaryDirectory() as folder:
            report = self._run_copy_paper_with_signals(
                folder, [signal], min_signal_wallets=1
            )
        self.assertEqual(report["opened_this_cycle"], 1)
        self.assertEqual(report["open_positions"], 1)

    def test_wallet_history_fetch_paginates_and_stops_on_short_page(self):
        first = Mock()
        first.raise_for_status.return_value = None
        first.json.return_value = [{"signature": "sig-3"}, {"signature": "sig-2"}]
        second = Mock()
        second.raise_for_status.return_value = None
        second.json.return_value = [{"signature": "sig-1"}]
        with patch.object(follower.requests, "get", side_effect=[first, second]) as get:
            txs = follower.fetch_wallet_transactions(WALLET, "test-key", limit=2, max_pages=3)
        self.assertEqual([tx["signature"] for tx in txs], ["sig-3", "sig-2", "sig-1"])
        self.assertEqual(get.call_count, 2)
        self.assertNotIn("before-signature", get.call_args_list[0].kwargs["params"])
        self.assertEqual(get.call_args_list[1].kwargs["params"]["before-signature"], "sig-2")

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

    def test_wallet_selection_finishes_started_backfills_before_new_wallets(self):
        watch = {"wallets": {
            "partial": {
                "confirmed_sell_pressure_events": 0,
                "observed_balance_drops": 0,
                "confirmed_buy_pressure_events": 0,
                "observed_balance_increases": 0,
                "tokens_seen": [],
            },
            "new-hot-wallet": {
                "confirmed_sell_pressure_events": 0,
                "observed_balance_drops": 0,
                "confirmed_buy_pressure_events": 100,
                "observed_balance_increases": 100,
                "tokens_seen": ["mint-a", "mint-b", "mint-c"],
            },
        }}
        research = {"wallet_cursor": 0, "wallets": {
            "partial": {
                "normalized_history_initialized": True,
                "history_cursor": "oldest-known-signature",
                "history_complete": False,
                "history_truncated": False,
                "backfill_visits": 3,
                "normalized_swaps": [{"signature": "known-swap"}],
                "metrics": {},
            },
        }}
        self.assertEqual(
            follower.select_wallets(watch, research, limit=1),
            ["partial"],
        )

    def test_rate_limit_response_exposes_retry_after_metadata(self):
        response = Mock()
        response.status_code = 429
        response.headers = {"Retry-After": "45"}
        response.raise_for_status.side_effect = follower.requests.HTTPError(
            "too many requests", response=response
        )
        with patch.object(follower.requests, "get", return_value=response):
            result = follower.fetch_wallet_transactions(
                WALLET, "test-key", return_metadata=True
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "http_429")
        self.assertEqual(result["retry_after_seconds"], 45.0)

    def test_copy_research_stops_after_http_429_and_persists_backoff(self):
        now = 1_793_000_000.0
        research = {"version": 1, "wallets": {}, "wallet_cursor": 0, "scan_count": 2}
        watch = {"wallets": {"wallet-a": {}, "wallet-b": {}, "wallet-c": {}}}
        persisted = {}
        failed_page = {
            "transactions": [], "last_signature": None, "reached_end": False,
            "ok": False, "pages_fetched": 0, "error": "http_429",
            "retry_after_seconds": 45.0,
        }

        def load_json(path, default=None):
            if path == "whale_watch_state.json":
                return watch
            if path == "whale_copy_state.json":
                return research
            return default or {}

        def atomic_json(path, value):
            persisted[path] = value

        with patch.object(follower.time, "time", return_value=now), \
             patch.object(follower, "_load_json", side_effect=load_json), \
             patch.object(follower, "_api_key", return_value="test-key"), \
             patch.object(follower, "select_wallets", return_value=["wallet-a", "wallet-b", "wallet-c"]), \
             patch.object(follower, "fetch_wallet_transactions", return_value=failed_page) as fetch, \
             patch.object(follower, "_atomic_json", side_effect=atomic_json), \
             patch.object(follower, "run_copy_paper_account", return_value={
                 "equity": 1000.0, "closed_trades": 0, "wins": 0, "net_pnl_usd": 0.0,
                 "open_positions": 0, "opened_this_cycle": 0, "closed_this_cycle": 0,
                 "entry_rejections": {}, "status": "paper_only",
             }):
            result = follower.run()

        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(result["status"], "helius_rate_limit_backoff")
        self.assertEqual(result["wallets_attempted"], 1)
        self.assertEqual(
            persisted["whale_copy_state.json"]["last_scan"]["wallets_attempted"], 1
        )
        self.assertAlmostEqual(persisted["whale_copy_state.json"]["api_backoff_until"], now + 45.0)

    def test_copy_research_skips_wallet_fetch_while_backoff_is_active(self):
        now = 1_793_000_000.0
        research = {
            "version": 1, "wallets": {}, "wallet_cursor": 0, "scan_count": 2,
            "api_backoff_until": now + 120.0,
        }
        watch = {"wallets": {"wallet-a": {}}}
        persisted = {}

        def load_json(path, default=None):
            if path == "whale_watch_state.json":
                return watch
            if path == "whale_copy_state.json":
                return research
            return default or {}

        with patch.object(follower.time, "time", return_value=now), \
             patch.object(follower, "_load_json", side_effect=load_json), \
             patch.object(follower, "_api_key", return_value="test-key"), \
             patch.object(follower, "select_wallets", return_value=["wallet-a"]), \
             patch.object(follower, "fetch_wallet_transactions") as fetch, \
             patch.object(follower, "_atomic_json", side_effect=lambda path, value: persisted.update({path: value})), \
             patch.object(follower, "run_copy_paper_account", return_value={
                 "equity": 1000.0, "closed_trades": 0, "wins": 0, "net_pnl_usd": 0.0,
                 "open_positions": 0, "opened_this_cycle": 0, "closed_this_cycle": 0,
                 "entry_rejections": {}, "status": "paper_only",
             }):
            result = follower.run()

        fetch.assert_not_called()
        self.assertEqual(result["status"], "helius_rate_limit_backoff")
        self.assertEqual(result["wallets_attempted"], 0)
        self.assertEqual(
            persisted["whale_copy_state.json"]["last_scan"]["wallets_attempted"], 0
        )

    def test_only_previously_profitable_wallets_emit_fresh_buy_signals(self):
        now = time.time()
        good_history = [{"net_pnl_pct": 10.0} for _ in range(24)] + [
            {"net_pnl_pct": -2.0} for _ in range(6)
        ]
        record = {
            "positions": {}, "seen_signatures": [], "closed_trades": good_history,
            "history_complete": True,
        }
        trade = follower.parse_swap_transaction(
            swap("fresh-good-buy", now - 30, "buy", 1000, 100), WALLET
        )
        result = follower.apply_wallet_trades(record, [trade], now=now)
        self.assertEqual(len(result["buy_signals"]), 1)
        self.assertEqual(result["buy_signals"][0]["wallet"], WALLET)

        poor_record = {
            "positions": {}, "seen_signatures": [],
            "closed_trades": [{"net_pnl_pct": -5.0} for _ in range(12)],
            "history_complete": True,
        }
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
