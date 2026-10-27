import unittest

import whale_event_study as study


def swap(wallet, mint, timestamp, side, signature, quote_symbol="SOL", quote_amount=1.0):
    return {
        "wallet": wallet, "mint": mint, "timestamp": float(timestamp), "side": side,
        "signature": signature, "token_amount": 100.0,
        "quote_amount": quote_amount, "quote_symbol": quote_symbol,
    }


def path(mint="mint-a", start=1000.0, prices=None):
    if prices is None:
        prices = [1.0 + index * 0.01 for index in range(31)]
    history = [
        {"timestamp": start + index * 30.0, "price_usd": price, "liquidity_usd": 100000.0}
        for index, price in enumerate(prices)
    ]
    return {
        "schema_version": 1, "mint": mint, "symbol": "TEST",
        "entry_timestamp": start, "entry_price": history[0]["price_usd"],
        "entry_liquidity_usd": history[0]["liquidity_usd"],
        "observation_end_timestamp": history[-1]["timestamp"],
        "observed_duration_seconds": history[-1]["timestamp"] - start,
        "price_history": history,
    }


class WhaleEventStudyTests(unittest.TestCase):
    def test_response_uses_next_real_quote_and_never_interpolates(self):
        source = {
            "wallets": {"wallet-a": {"normalized_swaps": [
                swap("wallet-a", "mint-a", 1045, "buy", "buy-1")
            ]}}
        }
        result = study.analyze_event_response([path()], source, delays_seconds=(0,), horizons_seconds=(30, 60))
        observations = result["observations"]
        single = next(row for row in observations if row["signal_class"] == "single_wallet")
        self.assertEqual(single["signal_timestamp"], 1045)
        self.assertEqual(single["entry_timestamp"], 1060)
        self.assertEqual(single["entry_price_usd"], 1.02)
        self.assertEqual(single["horizon_responses"][0]["horizon_seconds"], 30)
        self.assertEqual(single["horizon_responses"][0]["observed_quote_timestamp"], 1090)
        self.assertAlmostEqual(single["horizon_responses"][0]["raw_return_pct"], (1.03 / 1.02 - 1) * 100, places=6)

    def test_distinct_wallet_consensus_and_quote_currencies_are_separated(self):
        source = {
            "wallets": {
                "wallet-a": {"normalized_swaps": [
                    swap("wallet-a", "mint-a", 1000, "buy", "a-buy", "SOL", 2.0)]},
                "wallet-b": {"normalized_swaps": [
                    swap("wallet-b", "mint-a", 1120, "buy", "b-buy", "USDC", 50.0)]},
            }
        }
        result = study.analyze_event_response([path()], source, delays_seconds=(0,), horizons_seconds=(30,))
        consensus = next(row for row in result["observations"] if row["signal_class"] == "multi_wallet_consensus")
        self.assertEqual(consensus["signal_timestamp"], 1120)
        self.assertEqual(consensus["signal_wallets"], ["wallet-a", "wallet-b"])
        self.assertEqual(consensus["quote_notional_by_currency"], {"SOL": 2.0, "USDC": 50.0})
        self.assertFalse(result["live_trading_authorized"])

    def test_sell_response_uses_inverse_directional_return(self):
        source = {"wallets": {"wallet-a": {"normalized_swaps": [
            swap("wallet-a", "mint-a", 1000, "sell", "sell-1")
        ]}}}
        prices = [1.0 - index * 0.005 for index in range(31)]
        result = study.analyze_event_response([path(prices=prices)], source, delays_seconds=(0,), horizons_seconds=(60,))
        event = next(row for row in result["observations"] if row["side"] == "sell")
        response = event["horizon_responses"][0]
        self.assertLess(response["raw_return_pct"], 0)
        self.assertGreater(response["directional_return_pct"], 0)

    def test_sparse_invalid_paths_are_rejected_and_do_not_create_events(self):
        bad = path()
        bad["price_history"] = bad["price_history"][:5]
        source = {"wallets": {"a": {"normalized_swaps": [
            swap("a", "mint-a", 1000, "buy", "s1")
        ]}}}
        result = study.analyze_event_response([bad], source, delays_seconds=(0,), horizons_seconds=(30,))
        self.assertEqual(result["usable_paths"], 0)
        self.assertEqual(result["observations"], [])
        self.assertEqual(result["status"], "insufficient_history")

    def test_train_and_holdout_are_split_by_path_time_and_never_select_a_strategy(self):
        paths = [path(f"mint-{i}", start=1000 + i * 2000) for i in range(10)]
        wallets = {}
        for i in range(10):
            wallets[f"wallet-{i}"] = {"normalized_swaps": [
                swap(f"wallet-{i}", f"mint-{i}", 1000 + i * 2000, "buy", f"buy-{i}")
            ]}
        result = study.analyze_event_response(
            paths, {"wallets": wallets}, delays_seconds=(0,), horizons_seconds=(30,),
            min_train_paths=6, min_holdout_paths=3,
        )
        self.assertEqual(result["train_paths"], 7)
        self.assertEqual(result["holdout_paths"], 3)
        self.assertFalse(result["strategy_selected"])
        self.assertFalse(result["live_trading_authorized"])
        self.assertEqual(result["status"], "descriptive_only")
        self.assertTrue(all(row["partition"] in {"train", "holdout"} for row in result["observations"]))

    def test_no_horizon_quote_within_sampling_tolerance_is_censored(self):
        source = {"wallets": {"wallet-a": {"normalized_swaps": [
            swap("wallet-a", "mint-a", 1000, "buy", "buy-1")
        ]}}}
        result = study.analyze_event_response(
            [path()], source, delays_seconds=(120,), horizons_seconds=(900,)
        )
        row = next(x for x in result["observations"] if x["signal_class"] == "single_wallet")
        self.assertEqual(row["entry_timestamp"], 1120)
        self.assertEqual(row["horizon_responses"][0]["status"], "censored_no_observed_quote")
        self.assertIsNone(row["horizon_responses"][0]["raw_return_pct"])


if __name__ == "__main__":
    unittest.main()
