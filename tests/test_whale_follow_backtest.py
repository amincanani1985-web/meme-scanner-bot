import unittest

import whale_follow_backtest as backtest


def swap(wallet, mint, timestamp, side, token_amount=100.0, quote_amount=1.0,
         quote_symbol="SOL", signature=None):
    return {
        "wallet": wallet,
        "mint": mint,
        "timestamp": float(timestamp),
        "side": side,
        "token_amount": token_amount,
        "quote_amount": quote_amount,
        "quote_symbol": quote_symbol,
        "signature": signature or f"{wallet}-{mint}-{timestamp}-{side}",
    }


def quote_path(mint="mint-a", start=1000.0, count=31, step=30.0,
               prices=None):
    history = []
    for index in range(count):
        price = prices[index] if prices and index < len(prices) else 1.0 + index * 0.01
        history.append({
            "timestamp": start + index * step,
            "price_usd": price,
            "liquidity_usd": 100_000.0,
        })
    return {
        "schema_version": 1,
        "mint": mint,
        "symbol": "TEST",
        "entry_timestamp": start,
        "entry_price": history[0]["price_usd"],
        "entry_liquidity_usd": history[0]["liquidity_usd"],
        "observation_end_timestamp": history[-1]["timestamp"],
        "observed_duration_seconds": history[-1]["timestamp"] - start,
        "price_history": history,
        "score": 70.0,
    }


class WhaleFollowBacktestTests(unittest.TestCase):
    def test_entry_uses_first_recorded_quote_after_copy_delay_without_interpolation(self):
        path = quote_path()
        signal = {"wallet": "wallet-a", "mint": "mint-a", "timestamp": 1000.0, "side": "buy"}

        entry, reason = backtest.prepare_entry_path(path, signal, delay_seconds=45)

        self.assertIsNone(reason)
        self.assertEqual(entry["entry_timestamp"], 1060.0)
        self.assertAlmostEqual(entry["entry_price"], 1.02)
        self.assertEqual(entry["price_history"][0]["timestamp"], 1060.0)
        self.assertEqual(entry["entry_price"], entry["price_history"][0]["price_usd"])

    def test_multi_wallet_consensus_requires_two_distinct_wallets(self):
        path = quote_path()
        same_wallet_state = {"wallets": {"wallet-a": {
            "history_complete": True,
            "normalized_swaps": [
                swap("wallet-a", "mint-a", 1000, "buy", signature="a-1"),
                swap("wallet-a", "mint-a", 1100, "buy", signature="a-2"),
            ],
        }}}
        two_wallet_state = {"wallets": {
            "wallet-a": {"history_complete": True, "normalized_swaps": [
                swap("wallet-a", "mint-a", 1000, "buy", signature="a-1")]},
            "wallet-b": {"history_complete": True, "normalized_swaps": [
                swap("wallet-b", "mint-a", 1150, "buy", signature="b-1")]},
        }}

        self.assertIsNone(backtest.select_signal_for_path(path, same_wallet_state, "two_wallet_consensus"))
        signal = backtest.select_signal_for_path(path, two_wallet_state, "two_wallet_consensus")
        self.assertIsNotNone(signal)
        self.assertEqual(signal["wallet_count"], 2)
        self.assertEqual(signal["timestamp"], 1150.0)

    def test_signal_too_old_for_the_recorded_path_is_rejected(self):
        path = quote_path(start=2000.0)
        state = {"wallets": {"wallet-a": {"history_complete": True, "normalized_swaps": [
            swap("wallet-a", "mint-a", 1000.0, "buy")]}}}

        self.assertIsNone(backtest.select_signal_for_path(path, state, "all_tracked_buys"))

    def test_past_realized_wallet_metrics_are_used_not_future_performance(self):
        swaps = []
        for i in range(8):
            mint = f"closed-mint-{i}"
            t = 100.0 + i * 100.0
            swaps.append(swap("wallet-a", mint, t, "buy", 100.0, 10.0, "USDC", f"b-{i}"))
            swaps.append(swap("wallet-a", mint, t + 20.0, "sell", 100.0, 12.0, "USDC", f"s-{i}"))
        swaps.append(swap("wallet-a", "mint-a", 2000.0, "buy", signature="future-signal"))

        state = {"wallets": {"wallet-a": {
            "history_complete": True,
            "history_truncated": False,
            "normalized_swaps": swaps,
        }}}
        path = quote_path(start=2000.0)

        signal = backtest.select_signal_for_path(path, state, "qualified_wallet_buy")

        self.assertIsNotNone(signal)
        self.assertEqual(signal["wallet"], "wallet-a")
        self.assertGreaterEqual(signal["past_closed_trades"], 8)
        self.assertGreater(signal["past_mean_net_pnl_pct"], 0)

        # A future closed-trade summary cannot qualify a wallet with incomplete history.
        state["wallets"]["wallet-a"]["history_complete"] = False
        self.assertIsNone(backtest.select_signal_for_path(path, state, "qualified_wallet_buy"))

    def test_multiple_wallets_in_one_transaction_are_not_deduplicated_globally(self):
        state = {"wallets": {
            "wallet-a": {"normalized_swaps": [
                swap("wallet-a", "mint-a", 1000.0, "buy", signature="shared-tx")]},
            "wallet-b": {"normalized_swaps": [
                swap("wallet-b", "mint-a", 1000.0, "buy", signature="shared-tx")]},
        }}

        events, rejected = backtest.normalize_swap_state(state)

        self.assertEqual(rejected, 0)
        self.assertEqual(len(events), 2)
        self.assertEqual({event["wallet"] for event in events}, {"wallet-a", "wallet-b"})

    def test_small_dataset_is_reported_as_insufficient_not_profitable(self):
        paths = [quote_path(f"mint-{i}", start=1000 + i * 2000) for i in range(4)]
        state = {"wallets": {f"wallet-{i}": {"history_complete": True, "normalized_swaps": [
            swap(f"wallet-{i}", f"mint-{i}", 1000 + i * 2000, "buy")] }
            for i in range(4)}}

        report = backtest.analyze_follow(paths, state)

        self.assertIn(report["status"], {"insufficient_history", "no_profitable_training_candidate"})
        self.assertIsNone(report["selected_candidate"])
        self.assertFalse(report["live_trading_authorized"])
        self.assertTrue(report["research_only"])
        self.assertTrue(all(not row["validated"] for row in report["candidates"]))


if __name__ == "__main__":
    unittest.main()
