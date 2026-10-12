import csv
import time
import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import meme_scanner_cycle as scanner


def pair(**overrides):
    data = {
        "pairCreatedAt": int(time.time() * 1000) - 2 * 60 * 60 * 1000,
        "liquidity": {"usd": 100000},
        "volume": {"h1": 50000},
        "marketCap": 250000,
        "txns": {"m5": {"buys": 30, "sells": 10}},
        "priceChange": {"m5": 15.0, "h1": 10.0},
        "priceUsd": "1.0",
        "baseToken": {"symbol": "TEST"},
    }
    for key, value in overrides.items():
        data[key] = value
    return data


class ScannerLogicTests(unittest.TestCase):
    def test_drawdown_uses_account_peak_not_only_initial_balance(self):
        state = {"equity": 800.0, "initial_equity": 1000.0, "peak_equity": 1100.0}
        risk = scanner.update_risk_control(state)
        self.assertAlmostEqual(risk["drawdown_pct"], 27.27, places=2)
        self.assertEqual(risk["status"], "halted")
        self.assertAlmostEqual(risk["loss_from_initial_pct"], 20.0, places=2)

    def test_trade_ledger_reconciles_learning_totals_without_fabricating_scores(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trades_log.csv"
            path.write_text(
                "timestamp,mint,symbol,entry_price,exit_price,pnl_pct,pnl_usd,equity_after,reason\n"
                "2026-01-01T00:00:00Z,a,A,1,1.1,10,10,1010,max_hold\n"
                "2026-01-02T00:00:00Z,b,B,1,0.9,-10,-10,1000,stop_loss\n"
                "2026-01-03T00:00:00Z,c,C,1,1.2,20,20,1020,max_hold\n",
                encoding="utf-8",
            )
            state = {"initial_equity": 1000.0, "equity": 1020.0, "trade_count": 1,
                     "learning": {"trades": 1, "wins": 0, "buckets": {"80": {"n": 2}}}}
            scanner.reconcile_learning_from_trade_log(state, path)
        self.assertEqual(state["trade_count"], 3)
        self.assertEqual(state["learning"]["trades"], 3)
        self.assertEqual(state["learning"]["wins"], 2)
        self.assertEqual(state["learning"]["bucketed_trades"], 2)
        self.assertEqual(state["learning"]["unbucketed_trades"], 1)
        self.assertEqual(state["peak_equity"], 1020.0)

    def test_missing_legacy_signal_score_is_not_written_as_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "strategy_observations.csv"
            with patch.object(scanner, "Path", return_value=path):
                scanner.append_learning_observation(None, -5.0, "legacy_position")
            with path.open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["score"], "")
            self.assertEqual(row["pnl_pct"], "-5.0")

    def test_hardened_risk_constants(self):
        self.assertEqual(scanner.MAX_CONCURRENT_POSITIONS, 4)
        self.assertEqual(scanner.OPPORTUNITY_ENTRY_THRESHOLD, 80.0)
        self.assertEqual(scanner.MAX_POSITION_EQUITY_FRACTION, 0.005)
        self.assertEqual(scanner.MAX_PORTFOLIO_EXPOSURE_FRACTION, 0.02)

    def test_drawdown_circuit_breaker_halts_new_entries(self):
        state = scanner.normalize_state({
            "equity": 700.0, "initial_equity": 1000.0, "positions": {},
            "blacklist": [], "cooldowns": {},
        })
        self.assertEqual(state["risk_control"]["status"], "halted")
        self.assertAlmostEqual(state["risk_control"]["drawdown_pct"], 30.0)
        self.assertFalse(scanner.open_position(state, "mint", pair(), {}, 95, {}))

    def test_risk_halt_skips_discovery_but_still_records_cycle(self):
        state = scanner.normalize_state({"equity": 293.36, "initial_equity": 1000.0})
        with patch.object(scanner, "manage_open_positions"), patch.object(scanner, "save_state"), \
             patch.object(scanner, "fetch_candidate_mints") as fetch_candidates, patch.object(scanner, "log"):
            result = scanner.run_cycle(state)
        self.assertTrue(result["risk_halted"])
        self.assertEqual(result["risk_control"]["status"], "halted")
        fetch_candidates.assert_not_called()
        self.assertTrue(state["last_run"])

    def test_stale_candidate_confirmation_restarts_at_one_hit(self):
        now = 10000.0
        state = {"strategy": {"active_threshold": 80.0}, "candidate_observations": {
            "mint": {"hits": 2, "last_score": 95.0,
                     "last_seen": now - scanner.CANDIDATE_CONFIRMATION_MAX_GAP_SECONDS - 1}
        }}
        with patch.object(scanner.time, "time", return_value=now):
            self.assertFalse(scanner.candidate_confirmation(state, "mint", 96.0))
        self.assertEqual(state["candidate_observations"]["mint"]["hits"], 1)
        self.assertEqual(state["candidate_observations"]["mint"]["last_seen"], now)

    def test_candidate_discovery_ignores_non_solana_tokens(self):
        boosts = Mock()
        boosts.raise_for_status.return_value = None
        boosts.json.return_value = [
            {"chainId": "ethereum", "tokenAddress": "wrong-chain"},
            {"chainId": "solana", "tokenAddress": "sol-mint"},
        ]
        profiles = Mock()
        profiles.raise_for_status.return_value = None
        profiles.json.return_value = [{"chainId": "base", "tokenAddress": "other-chain"}]
        with patch.object(scanner.requests, "get", side_effect=[boosts, profiles]):
            self.assertEqual(scanner.fetch_candidate_mints(limit=10), ["sol-mint"])

    def test_pair_lookup_never_selects_a_non_solana_pair(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"pairs": [
            {**pair(), "chainId": "ethereum", "liquidity": {"usd": 999999}},
            {**pair(), "chainId": "solana", "liquidity": {"usd": 100000}},
        ]}
        with patch.object(scanner.requests, "get", return_value=response):
            found = scanner.fetch_dex_pair("mint")
        self.assertEqual(found["chainId"], "solana")
        self.assertEqual(found["liquidity"]["usd"], 100000)

    def test_trade_log_schema_matches_dashboard_and_migrates_legacy_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trades_log.csv"
            path.write_text(
                "timestamp,mint,symbol,entry_price,exit_price,pnl_pct,reason,amount_usd\n"
                "2026-01-01T00:00:00Z,old-mint,OLD,1,0.9,-10,hard_stop_loss,5\n",
                encoding="utf-8",
            )
            with patch.object(scanner, "Path", return_value=path):
                scanner.append_trade("new-mint", {"symbol": "NEW", "amount_usd": 5.0},
                                     1.0, 1.1, "trailing_stop", 0.1, 0.5, 1000.5)
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(list(rows[0].keys()), [
                "timestamp", "mint", "symbol", "entry_price", "exit_price",
                "pnl_pct", "pnl_usd", "equity_after", "reason",
            ])
            self.assertEqual(len(rows), 2)
            self.assertAlmostEqual(float(rows[0]["pnl_usd"]), -0.5)
            self.assertEqual(rows[1]["reason"], "trailing_stop")
            self.assertAlmostEqual(float(rows[1]["equity_after"]), 1000.5)

    def test_good_pair_passes_current_market_filters(self):
        self.assertEqual(scanner.passes_market_filter(pair()), (True, "ok"))

    def test_pair_missing_age_is_rejected(self):
        data = pair()
        data.pop("pairCreatedAt")
        self.assertEqual(scanner.passes_market_filter(data), (False, "missing_pair_age"))

    def test_new_pair_is_rejected(self):
        result, reason = scanner.passes_market_filter(
            pair(pairCreatedAt=int(time.time() * 1000) - 60_000)
        )
        self.assertFalse(result)
        self.assertTrue(reason.startswith("pair_too_new:"))

    def test_low_liquidity_volume_ratio_and_buy_pressure_are_rejected(self):
        self.assertFalse(scanner.passes_market_filter(pair(liquidity={"usd": 99999}))[0])
        self.assertFalse(scanner.passes_market_filter(pair(volume={"h1": 19999}))[0])
        self.assertFalse(scanner.passes_market_filter(pair(marketCap=300000))[0])
        self.assertFalse(scanner.passes_market_filter(pair(txns={"m5": {"buys": 19, "sells": 10}}))[0])

    def test_negative_hourly_momentum_is_rejected(self):
        self.assertFalse(scanner.passes_market_filter(pair(priceChange={"m5": 15, "h1": -0.1}))[0])

    def test_score_rewards_stronger_momentum_and_buy_pressure(self):
        baseline, _ = scanner.score_market_opportunity(pair())
        weaker_momentum, _ = scanner.score_market_opportunity(
            pair(priceChange={"m5": 2.0, "h1": 10.0})
        )
        weaker_buying, _ = scanner.score_market_opportunity(
            pair(txns={"m5": {"buys": 20, "sells": 10}})
        )
        self.assertGreater(baseline, weaker_momentum)
        self.assertGreater(baseline, weaker_buying)

    def test_learning_bonus_only_applies_with_positive_history(self):
        baseline, _ = scanner.score_market_opportunity(pair())
        learned, components = scanner.score_market_opportunity(
            pair(), {"trades": 10, "wins": 8}
        )
        self.assertGreater(learned, baseline)
        self.assertEqual(components["learning_bonus"], 10.0)

    def test_candidate_confirmation_requires_three_consecutive_hits(self):
        state = {"strategy": {"active_threshold": 80}, "candidate_observations": {}}
        self.assertFalse(scanner.candidate_confirmation(state, "mint", 85))
        self.assertFalse(scanner.candidate_confirmation(state, "mint", 86))
        self.assertTrue(scanner.candidate_confirmation(state, "mint", 87))

    def test_candidate_confirmation_restarts_after_score_drop(self):
        state = {"strategy": {"active_threshold": 80}, "candidate_observations": {}}
        scanner.candidate_confirmation(state, "mint", 85)
        scanner.candidate_confirmation(state, "mint", 86)
        self.assertFalse(scanner.candidate_confirmation(state, "mint", 70))
        self.assertEqual(state["candidate_observations"]["mint"]["hits"], 0)

    def test_open_position_applies_slippage_and_position_caps(self):
        state = {"equity": 1000.0, "positions": {}, "blacklist": [], "cooldowns": {}}
        self.assertTrue(scanner.open_position(state, "mint", pair(), {}, 85, {}))
        position = state["positions"]["mint"]
        self.assertAlmostEqual(position["entry_price"], 1.015)
        self.assertAlmostEqual(position["amount_usd"], 5.0)
        for i in range(scanner.MAX_CONCURRENT_POSITIONS - 1):
            state["positions"][f"mint{i}"] = dict(position)
        self.assertFalse(scanner.open_position(state, "another", pair(), {}, 85, {}))

    def test_small_equity_can_still_open_risk_capped_position(self):
        state = {"equity": 400.0, "initial_equity": 400.0, "positions": {}, "blacklist": [], "cooldowns": {}}
        self.assertTrue(scanner.open_position(state, "small", pair(), {}, 85, {}))
        self.assertAlmostEqual(state["positions"]["small"]["amount_usd"], 2.0)

    def test_open_position_blocks_blacklist_cooldown_and_invalid_price(self):
        state = {"equity": 1000.0, "positions": {}, "blacklist": ["bad"], "cooldowns": {}}
        self.assertFalse(scanner.open_position(state, "bad", pair(), {}, 90, {}))
        state["blacklist"] = []
        state["cooldowns"] = {"cool": time.time() + 60}
        self.assertFalse(scanner.open_position(state, "cool", pair(), {}, 90, {}))
        self.assertFalse(scanner.open_position(state, "zero", pair(priceUsd="0"), {}, 90, {}))

    def test_liquidity_drain_between_25_and_50_percent_exits_immediately(self):
        pos = {"entry_liquidity": 100000}
        self.assertTrue(scanner.liquidity_drain_detected(pos, 70000))

    def test_catastrophic_liquidity_drain_requires_two_hits(self):
        pos = {"entry_liquidity": 100000, "liquidity_drain_hits": 0}
        self.assertFalse(scanner.liquidity_drain_detected(pos, 40000))
        self.assertTrue(scanner.liquidity_drain_detected(pos, 40000))

    def test_liquidity_recovery_resets_confirmation(self):
        pos = {"entry_liquidity": 100000, "liquidity_drain_hits": 1}
        self.assertFalse(scanner.liquidity_drain_detected(pos, 80000))
        self.assertEqual(pos["liquidity_drain_hits"], 0)

    def test_close_position_applies_exit_slippage_updates_learning_and_cooldown(self):
        state = {
            "equity": 1000.0, "trade_count": 0, "positions": {
                "mint": {"symbol": "TEST", "entry_price": 1.015, "amount_usd": 5.0, "signal_score": 85}
            }, "blacklist": [], "cooldowns": {},
        }
        with patch.object(scanner, "append_trade") as trade, patch.object(scanner, "append_learning_observation") as learning:
            scanner.close_position(state, "mint", 0.8, "hard_stop_loss")
        self.assertNotIn("mint", state["positions"])
        self.assertEqual(state["trade_count"], 1)
        self.assertLess(state["equity"], 1000.0)
        self.assertIn("mint", state["cooldowns"])
        trade.assert_called_once()
        learning.assert_called_once()

    def test_manage_positions_uses_live_price_for_hard_stop(self):
        state = {
            "equity": 1000.0, "trade_count": 0, "blacklist": [], "cooldowns": {},
            "positions": {"mint": {"symbol": "TEST", "entry_price": 1.0, "peak_price": 1.0,
                "entry_time": time.time(), "entry_liquidity": 100000, "amount_usd": 10.0,
                "signal_score": 85, "trailing_armed": False}},
        }
        live_pair = {"priceUsd": "0.85", "liquidity": {"usd": 100000}}
        with patch.object(scanner, "fetch_dex_pair", return_value=live_pair), \
             patch.object(scanner, "append_trade"), patch.object(scanner, "append_learning_observation"):
            scanner.manage_open_positions(state)
        self.assertNotIn("mint", state["positions"])
        self.assertEqual(state["trade_count"], 1)

    def test_run_cycle_opens_only_after_three_confirmations(self):
        state = scanner.normalize_state({})
        with patch.object(scanner, "helius_configured", return_value=False), \
             patch.object(scanner, "fetch_candidate_mints", return_value=["mint"]), \
             patch.object(scanner, "fetch_dex_pair", return_value=pair()), \
             patch.object(scanner, "save_state"), patch.object(scanner, "log"):
            first = scanner.run_cycle(state)
            second = scanner.run_cycle(state)
            third = scanner.run_cycle(state)
        self.assertEqual(first["open_positions"], 0)
        self.assertEqual(second["open_positions"], 0)
        self.assertEqual(third["open_positions"], 1)
        self.assertIn("mint", state["positions"])
        self.assertTrue(state.get("last_run"))

    def test_confirmed_whale_dump_closes_open_position(self):
        state = {
            "equity": 1000.0, "trade_count": 0, "blacklist": [], "cooldowns": {},
            "whale_snapshots": {"mint": {"owner-a": 1000.0}},
            "positions": {"mint": {"symbol": "TEST", "entry_price": 1.0, "peak_price": 1.0,
                "entry_time": time.time(), "entry_liquidity": 100000, "amount_usd": 10.0,
                "signal_score": 85, "trailing_armed": False,
                "whale_balances": {"owner-a": 1000.0}}},
        }
        live_pair = {"priceUsd": "0.98", "liquidity": {"usd": 100000},
                     "txns": {"m5": {"buys": 5, "sells": 15}},
                     "priceChange": {"m5": -1.0}}
        with patch.object(scanner, "helius_configured", return_value=True), \
             patch.object(scanner, "fetch_whale_balances", return_value={"owner-a": 700.0}), \
             patch.object(scanner, "fetch_dex_pair", return_value=live_pair), \
             patch.object(scanner, "append_trade"), patch.object(scanner, "append_learning_observation"), \
             patch.object(scanner, "log"):
            scanner.manage_open_positions(state)
        self.assertNotIn("mint", state["positions"])
        self.assertIn("mint", state["blacklist"])
        self.assertEqual(state["trade_count"], 1)

    def test_malformed_persisted_position_does_not_abort_scan(self):
        state = {
            "equity": 1000.0, "trade_count": 0, "blacklist": [], "cooldowns": {},
            "positions": {"broken": {"entry_price": "bad", "amount_usd": 10.0, "entry_time": "bad"}},
        }
        with patch.object(scanner, "fetch_dex_pair", return_value=None), patch.object(scanner, "log"):
            scanner.manage_open_positions(state)
        self.assertNotIn("broken", state["positions"])

    def test_missing_price_does_not_fabricate_total_loss(self):
        state = {
            "equity": 1000.0, "trade_count": 0, "blacklist": [], "cooldowns": {},
            "positions": {"mint": {"symbol": "TEST", "entry_price": 1.0, "peak_price": 1.0,
                "entry_time": 0.0, "entry_liquidity": 100000, "amount_usd": 10.0,
                "signal_score": 85, "trailing_armed": False}},
        }
        with patch.object(scanner, "fetch_dex_pair", return_value=None), \
             patch.object(scanner, "append_trade"), patch.object(scanner, "append_learning_observation"), \
             patch.object(scanner, "log"):
            scanner.manage_open_positions(state)
        self.assertIn("mint", state["positions"])
        self.assertEqual(state["equity"], 1000.0)
        self.assertEqual(state["trade_count"], 0)
        self.assertIn("price_missing_since", state["positions"]["mint"])

    def test_trade_target_disables_new_entries(self):
        state = scanner.normalize_state({"trade_count": scanner.TARGET_TRADES})
        with patch.object(scanner, "manage_open_positions"), patch.object(scanner, "save_state"), \
             patch.object(scanner, "fetch_candidate_mints") as fetch_candidates, patch.object(scanner, "log"):
            result = scanner.run_cycle(state)
        self.assertTrue(result["target_reached"])
        fetch_candidates.assert_not_called()

    def test_learning_bonus_requires_minimum_sample(self):
        baseline, _ = scanner.score_market_opportunity(pair())
        tiny_sample, components = scanner.score_market_opportunity(pair(), {"trades": 1, "wins": 1})
        self.assertEqual(tiny_sample, baseline)
        self.assertNotIn("learning_bonus", components)

    def test_normalize_state_repairs_invalid_types(self):
        fixed = scanner.normalize_state({
            "equity": "bad", "positions": [], "blacklist": "oops", "cooldowns": "nope",
            "trade_count": "abc", "scan_count": "7", "candidate_observations": "bad",
            "strategy": "invalid", "learning": None,
        })
        self.assertEqual(fixed["equity"], 1000.0)
        self.assertIsInstance(fixed["positions"], dict)
        self.assertEqual(fixed["blacklist"], [])
        self.assertEqual(fixed["scan_count"], 7)
        self.assertIsInstance(fixed["strategy"], dict)
        self.assertEqual(fixed["learning"]["trades"], 0)


if __name__ == "__main__":
    unittest.main()
