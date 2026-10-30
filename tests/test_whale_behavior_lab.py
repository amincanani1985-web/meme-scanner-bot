import unittest

import whale_behavior_lab as lab


def swap(wallet, mint, timestamp, side, quote_amount=1.0, quote_symbol="SOL", signature=None):
    return {
        "wallet": wallet,
        "mint": mint,
        "timestamp": timestamp,
        "side": side,
        "token_amount": 100.0,
        "quote_amount": quote_amount,
        "quote_symbol": quote_symbol,
        "signature": signature or f"{wallet}-{mint}-{timestamp}-{side}",
    }


class WhaleBehaviorLabTests(unittest.TestCase):
    def test_high_expectancy_wallet_is_consistently_qualified(self):
        record = {"history_complete": True, "history_truncated": False}
        metrics = {
            "closed_trades": 145, "win_rate": 0.4276,
            "profit_factor": 1.3428, "mean_net_pnl_pct": 6.3932,
        }
        self.assertTrue(lab._copy_paper_qualified(record, metrics))

    def test_high_expectancy_wallet_still_needs_minimum_history(self):
        record = {"history_complete": True, "history_truncated": False}
        metrics = {
            "closed_trades": 29, "win_rate": 0.4483,
            "profit_factor": 1.8, "mean_net_pnl_pct": 4.0,
        }
        self.assertFalse(lab._copy_paper_qualified(record, metrics))
    def test_classifies_accumulation_and_distribution_as_observed_patterns(self):
        state = {"wallets": {
            "accumulator": {"history_complete": False, "normalized_swaps": [
                swap("accumulator", "mint-a", 100, "buy"),
                swap("accumulator", "mint-a", 160, "buy"),
                swap("accumulator", "mint-a", 220, "buy"),
                swap("accumulator", "mint-b", 280, "buy"),
            ]},
            "distributor": {"history_complete": True, "normalized_swaps": [
                swap("distributor", "mint-a", 110, "sell"),
                swap("distributor", "mint-a", 170, "sell"),
                swap("distributor", "mint-c", 230, "buy"),
                swap("distributor", "mint-c", 290, "sell"),
            ]},
        }}

        report = lab.analyze_behavior(state, now=300)
        profiles = {row["wallet"]: row for row in report["wallet_profiles"]}

        self.assertEqual(profiles["accumulator"]["observed_style"], "accumulator")
        self.assertEqual(profiles["distributor"]["observed_style"], "distributor")
        self.assertEqual(profiles["accumulator"]["buy_count"], 4)
        self.assertEqual(profiles["distributor"]["sell_count"], 3)
        self.assertTrue(report["research_only"])
        self.assertFalse(report["live_trading_authorized"])

    def test_flags_short_window_multi_wallet_buy_consensus_and_crowding(self):
        state = {"wallets": {
            "wallet-a": {"history_complete": True, "normalized_swaps": [
                swap("wallet-a", "mint-x", 1000, "buy", 2.0)]},
            "wallet-b": {"history_complete": True, "normalized_swaps": [
                swap("wallet-b", "mint-x", 1120, "buy", 3.0)]},
            "wallet-c": {"history_complete": True, "normalized_swaps": [
                swap("wallet-c", "mint-x", 1290, "buy", 1.5)]},
        }}

        report = lab.analyze_behavior(state, now=1300)
        token = next(row for row in report["token_flows"] if row["mint"] == "mint-x")

        self.assertEqual(token["latest_window_unique_buyers"], 3)
        self.assertEqual(token["latest_window_unique_sellers"], 0)
        self.assertEqual(token["flow_label"], "multi_wallet_buy_consensus")
        self.assertTrue(token["crowding_risk"])
        self.assertFalse(token["trade_signal"])

    def test_records_observed_leader_and_follower_timing_without_claiming_market_wide_leadership(self):
        state = {"wallets": {
            "first-seen": {"history_complete": True, "normalized_swaps": [
                swap("first-seen", "mint-y", 100, "buy")]},
            "later-a": {"history_complete": True, "normalized_swaps": [
                swap("later-a", "mint-y", 145, "buy")]},
            "later-b": {"history_complete": True, "normalized_swaps": [
                swap("later-b", "mint-y", 190, "buy")]},
        }}

        report = lab.analyze_behavior(state, now=200)
        profiles = {row["wallet"]: row for row in report["wallet_profiles"]}

        self.assertEqual(profiles["first-seen"]["observed_leader_count"], 1)
        self.assertEqual(profiles["later-a"]["observed_follower_count"], 1)
        self.assertEqual(profiles["later-b"]["observed_follower_count"], 1)
        self.assertEqual(report["leadership_scope"], "first observed among tracked wallet histories only")

    def test_incomplete_or_small_history_stays_low_confidence_and_cannot_authorize_copying(self):
        state = {"wallets": {"partial": {
            "history_complete": False,
            "normalized_swaps": [swap("partial", "mint-a", 100 + i * 10, "buy" if i < 4 else "sell")
                                 for i in range(6)],
            "metrics": {"closed_trades": 100, "win_rate": 1.0,
                        "profit_factor": 99.0, "mean_net_pnl_pct": 50.0},
        }}}

        report = lab.analyze_behavior(state, now=200)
        profile = report["wallet_profiles"][0]

        self.assertEqual(profile["confidence"], "low")
        self.assertFalse(profile["history_complete"])
        self.assertFalse(profile["copy_paper_qualified"])
        self.assertFalse(report["live_trading_authorized"])

    def test_repeated_same_token_timing_overlap_is_not_called_coordination(self):
        state = {"wallets": {
            "wallet-a": {"history_complete": True, "normalized_swaps": [
                swap("wallet-a", "mint-a", 100, "buy"),
                swap("wallet-a", "mint-b", 1000, "buy")]},
            "wallet-b": {"history_complete": True, "normalized_swaps": [
                swap("wallet-b", "mint-a", 140, "buy"),
                swap("wallet-b", "mint-b", 1040, "buy")]},
        }}

        report = lab.analyze_behavior(state, now=1100)
        edge = next(row for row in report["wallet_relationships"]
                    if row["earlier_observed_wallet"] == "wallet-a"
                    and row["later_observed_wallet"] == "wallet-b")

        self.assertEqual(edge["shared_token_count"], 2)
        self.assertEqual(edge["relationship_label"], "repeated_timing_overlap")
        self.assertFalse(edge["coordination_proven"])
        self.assertEqual(report["statistics"]["repeated_timing_overlap_pairs"], 1)

    def test_short_holding_pattern_is_descriptive_not_a_copy_instruction(self):
        state = {"wallets": {"wallet-fast": {"history_complete": True, "normalized_swaps": [
            swap("wallet-fast", "mint-a", 100, "buy"),
            swap("wallet-fast", "mint-a", 115, "sell"),
            swap("wallet-fast", "mint-b", 200, "buy"),
            swap("wallet-fast", "mint-b", 225, "sell"),
            swap("wallet-fast", "mint-c", 300, "buy"),
            swap("wallet-fast", "mint-c", 320, "sell"),
        ]}}}

        profile = lab.analyze_behavior(state, now=330)["wallet_profiles"][0]

        self.assertEqual(profile["median_observed_hold_seconds"], 20.0)
        self.assertIn("short_horizon_trade_pattern", profile["behavior_tags"])
        self.assertFalse(profile["copy_paper_qualified"])

    def test_duplicate_signature_does_not_hide_a_different_wallet_side_of_a_swap(self):
        state = {"wallets": {
            "wallet-a": {"history_complete": True, "normalized_swaps": [
                swap("wallet-a", "mint-a", 100, "buy", signature="shared-signature")]},
            "wallet-b": {"history_complete": True, "normalized_swaps": [
                swap("wallet-b", "mint-a", 100, "buy", signature="shared-signature")]},
        }}

        report = lab.analyze_behavior(state, now=110)

        self.assertEqual(report["statistics"]["swap_events_used"], 2)
        token = next(row for row in report["token_flows"] if row["mint"] == "mint-a")
        self.assertEqual(token["latest_window_unique_buyers"], 2)

    def test_balance_drops_are_reported_separately_from_swap_sells(self):
        swaps = {"wallets": {"wallet-a": {"normalized_swaps": [
            swap("wallet-a", "mint-a", 100, "buy")]}}}
        balances = {"wallets": {"wallet-a": {
            "observed_balance_drops": 4,
            "confirmed_sell_pressure_events": 3,
            "observed_balance_increases": 2,
            "confirmed_buy_pressure_events": 1,
            "tokens_seen": ["mint-a", "mint-b"],
        }}}

        profile = lab.analyze_behavior(swaps, balances, now=110)["wallet_profiles"][0]

        self.assertEqual(profile["sell_count"], 0)
        self.assertEqual(profile["holder_balance_signals"]["confirmed_sell_pressure_events"], 3)
        self.assertEqual(
            profile["holder_balance_signals"]["evidence_type"],
            "balance_change_plus_market_context_not_verified_swap",
        )

    def test_quote_notional_is_kept_separate_by_currency_and_bad_events_are_ignored(self):
        state = {"wallets": {"wallet-a": {"history_complete": True, "normalized_swaps": [
            swap("wallet-a", "mint-a", 100, "buy", 2.0, "SOL"),
            swap("wallet-a", "mint-a", 110, "sell", 50.0, "USDC"),
            swap("wallet-a", "mint-a", 120, "buy", 0.0, "SOL"),
            {"wallet": "wallet-a", "mint": "mint-a", "timestamp": 130,
             "side": "buy", "token_amount": 1, "quote_amount": 2, "quote_symbol": "BAD"},
        ]}}}

        report = lab.analyze_behavior(state, now=140)
        profile = report["wallet_profiles"][0]

        self.assertEqual(report["statistics"]["swap_events_used"], 2)
        self.assertEqual(profile["buy_notional_by_quote"], {"SOL": 2.0})
        self.assertEqual(profile["sell_notional_by_quote"], {"USDC": 50.0})


if __name__ == "__main__":
    unittest.main()
