import csv
from contextlib import ExitStack
import json
import time
import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import meme_scanner_cycle as scanner
import paper_scenario_lab


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
    def test_whale_follow_filter_rejects_overextended_hourly_pumps(self):
        candidate = pair(**{"priceChange": {"m5": 3.0, "h1": 120.0}})
        self.assertEqual(scanner.passes_whale_follow_market_filter(candidate),
                         (False, "whale_chase_risk_h1:120.0%"))

    def test_whale_follow_filter_rejects_single_digit_transaction_noise(self):
        candidate = pair(**{"priceChange": {"m5": 3.0, "h1": 10.0},
                            "txns": {"m5": {"buys": 3, "sells": 2}}})
        self.assertEqual(scanner.passes_whale_follow_market_filter(candidate),
                         (False, "whale_low_activity_m5:5"))

    def test_whale_follow_filter_accepts_liquid_token_with_three_percent_liquidity_ratio(self):
        candidate = pair(
            **{"priceChange": {"m5": 3.0, "h1": 10.0},
               "marketCap": 2_000_000,
               "liquidity": {"usd": 60_000},
               "volume": {"h1": 50_000},
               "txns": {"m5": {"buys": 30, "sells": 10}}}
        )
        self.assertEqual(scanner.passes_whale_follow_market_filter(candidate), (True, "ok"))

    def test_whale_follow_filter_accepts_fifteen_thousand_hourly_volume(self):
        candidate = pair(
            **{"priceChange": {"m5": 3.0, "h1": 10.0},
               "marketCap": 2_000_000,
               "liquidity": {"usd": 60_000},
               "volume": {"h1": 15_000},
               "txns": {"m5": {"buys": 30, "sells": 10}}}
        )
        self.assertEqual(scanner.passes_whale_follow_market_filter(candidate), (True, "ok"))

    def test_whale_follow_filter_rejects_liquidity_ratio_below_two_percent(self):
        candidate = pair(
            **{"priceChange": {"m5": 3.0, "h1": 10.0},
               "marketCap": 4_000_000,
               "liquidity": {"usd": 60_000},
               "volume": {"h1": 50_000},
               "txns": {"m5": {"buys": 30, "sells": 10}}}
        )
        passed, reason = scanner.passes_whale_follow_market_filter(candidate)
        self.assertFalse(passed)
        self.assertEqual(reason, "shadow_liq_mcap_ratio:0.01")

    def test_whale_follow_filter_accepts_only_basic_non_chasing_market_shape(self):
        candidate = pair(**{"priceChange": {"m5": 3.0, "h1": 10.0},
                            "txns": {"m5": {"buys": 30, "sells": 10}}})
        self.assertEqual(scanner.passes_whale_follow_market_filter(candidate), (True, "ok"))

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

    def test_whale_confirmed_outcome_is_written_to_separate_cohort(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(scanner, "Path", side_effect=lambda name: Path(tmp) / name):
                scanner.append_learning_observation(
                    72.0, 4.0, "max_hold_timeout",
                    {"whale_gate_enabled": 1.0, "whale_accumulation_confirmed": 1.0,
                     "momentum_m5_pct": 2.0},
                )
            path = Path(tmp) / "whale_strategy_observations.csv"
            self.assertTrue(path.exists())
            with path.open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["score"], "72.0")
            self.assertIn('"whale_accumulation_confirmed":1.0', row["features_json"])
            self.assertFalse((Path(tmp) / "strategy_observations.csv").exists())

    def test_malformed_whale_flags_do_not_break_trade_close_logging(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(scanner, "Path", side_effect=lambda name: Path(tmp) / name):
                scanner.append_learning_observation(
                    72.0, -4.0, "max_hold_timeout",
                    {"whale_gate_enabled": "bad", "whale_accumulation_confirmed": None},
                )
            self.assertTrue((Path(tmp) / "strategy_observations.csv").exists())
            self.assertFalse((Path(tmp) / "whale_strategy_observations.csv").exists())

    def test_hardened_risk_constants(self):
        self.assertEqual(scanner.MAX_CONCURRENT_POSITIONS, 4)
        self.assertEqual(scanner.OPPORTUNITY_ENTRY_THRESHOLD, 60.0)
        self.assertEqual(scanner.MAX_PRICE_CHANGE_M5_PCT, 15.0)
        self.assertEqual(scanner.MIN_BUY_SELL_RATIO_M5, 1.0)
        self.assertEqual(scanner.MIN_LIQUIDITY_TO_MCAP_RATIO, 0.05)
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

    def test_risk_halt_continues_shadow_scanning_without_account_entries(self):
        state = scanner.normalize_state({"equity": 293.36, "initial_equity": 1000.0})
        with patch.object(scanner, "reconcile_learning_from_trade_log"), \
             patch.object(scanner, "manage_open_positions"), \
             patch.object(scanner, "save_state"), \
             patch.object(scanner, "fetch_candidate_mints", return_value=["mint"]), \
             patch.object(scanner, "fetch_dex_pair", return_value=pair()), \
             patch.object(scanner, "score_market_opportunity", return_value=(95.0, {})), \
             patch.object(scanner, "log"):
            results = [scanner.run_cycle(state) for _ in range(3)]
        self.assertTrue(results[-1]["risk_halted"])
        self.assertEqual(results[-1]["risk_control"]["status"], "halted")
        self.assertEqual(results[-1]["scanned"], 1)
        self.assertEqual(len(state["shadow_positions"]), 1)
        self.assertEqual(state["positions"], {})
        self.assertEqual(state["equity"], 293.36)
        self.assertEqual(state["trade_count"], 0)
        self.assertEqual(state["scan_count"], 3)
        self.assertTrue(state["last_run"])

    def test_shadow_collection_samples_below_live_entry_threshold_without_confirmation(self):
        state = scanner.normalize_state({"equity": 293.36, "initial_equity": 1000.0})
        with patch.object(scanner, "reconcile_learning_from_trade_log"), \
             patch.object(scanner, "manage_open_positions"), \
             patch.object(scanner, "save_state"), \
             patch.object(scanner, "fetch_candidate_mints", return_value=["mint"]), \
             patch.object(scanner, "fetch_dex_pair", return_value=pair()), \
             patch.object(scanner, "score_market_opportunity", return_value=(42.0, {})), \
             patch.object(scanner, "candidate_confirmation") as confirmation, \
             patch.object(scanner, "log"):
            result = scanner.run_cycle(state)
        self.assertTrue(result["risk_halted"])
        self.assertEqual(result["shadow_opened"], ["mint"])
        self.assertEqual(state["equity"], 293.36)
        self.assertEqual(state["trade_count"], 0)
        confirmation.assert_not_called()

    def test_shadow_scan_records_market_filter_rejection_reasons(self):
        state = scanner.normalize_state({"equity": 293.36, "initial_equity": 1000.0})
        with patch.object(scanner, "reconcile_learning_from_trade_log"), \
             patch.object(scanner, "manage_open_positions"), \
             patch.object(scanner, "save_state"), \
             patch.object(scanner, "fetch_candidate_mints", return_value=["mint"]), \
             patch.object(scanner, "fetch_dex_pair", return_value=pair(liquidity={"usd": 500})), \
             patch.object(scanner, "log"):
            result = scanner.run_cycle(state)
        self.assertEqual(state["shadow_scan_diagnostics"]["rejections"], {"shadow_low_liquidity:500": 1})
        self.assertEqual(state["shadow_scan_diagnostics"]["filter_passed"], 0)
        self.assertEqual(state["shadow_positions"], {})
        self.assertEqual(state["equity"], 293.36)

    def test_shadow_observations_are_separated_from_live_strategy_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(scanner, "Path", side_effect=lambda name: root / name):
                scanner.append_learning_observation(85, 4.0, "shadow_max_hold_timeout")
                scanner.append_learning_observation(85, -2.0, "hard_stop_loss")
            self.assertTrue((root / "shadow_observations.csv").exists())
            self.assertTrue((root / "strategy_observations.csv").exists())
            self.assertIn("shadow_max_hold_timeout", (root / "shadow_observations.csv").read_text(encoding="utf-8"))
            self.assertIn("hard_stop_loss", (root / "strategy_observations.csv").read_text(encoding="utf-8"))

    def test_shadow_close_records_labeled_sample_without_touching_account(self):
        state = scanner.normalize_state({
            "equity": 293.36, "initial_equity": 1000.0, "trade_count": 53,
            "shadow_positions": {"mint": {
                "symbol": "TEST", "entry_price": 1.0, "entry_time": time.time(),
                "entry_liquidity": 100000, "peak_price": 1.0, "signal_score": 91,
                "trailing_armed": False,
            }},
        })
        with patch.object(scanner, "fetch_dex_pair", return_value=pair(priceUsd="0.85")), \
             patch.object(scanner, "append_learning_observation") as observation, \
             patch.object(scanner, "log"):
            scanner.manage_shadow_positions(state)
        self.assertNotIn("mint", state["shadow_positions"])
        self.assertEqual(state["equity"], 293.36)
        self.assertEqual(state["trade_count"], 53)
        observation.assert_called_once()
        self.assertEqual(observation.call_args.args[0], 91)
        self.assertTrue(observation.call_args.args[2].startswith("shadow_"))

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
            {"chainId": "solana", "tokenAddress": "sol-mint"},
            {"chainId": "ethereum", "tokenAddress": "wrong-chain"},
        ]
        top_boosts = Mock()
        top_boosts.raise_for_status.return_value = None
        top_boosts.json.return_value = [{"chainId": "solana", "tokenAddress": "top-mint"}]
        profiles = Mock()
        profiles.raise_for_status.return_value = None
        profiles.json.return_value = [{"chainId": "base", "tokenAddress": "other-chain"}]
        recent_profiles = Mock()
        recent_profiles.raise_for_status.return_value = None
        recent_profiles.json.return_value = [{"chainId": "solana", "tokenAddress": "recent-mint"}]
        empty_searches = []
        for _ in range(len(scanner.DISCOVERY_SEARCH_TERMS)):
            response = Mock()
            response.raise_for_status.return_value = None
            response.json.return_value = {"pairs": []}
            empty_searches.append(response)
        with patch.object(scanner.requests, "get", side_effect=[boosts, top_boosts, profiles, recent_profiles, *empty_searches]):
            self.assertEqual(scanner.fetch_candidate_mints(limit=10), ["sol-mint", "top-mint", "recent-mint"])

    def test_default_discovery_expands_candidate_universe_to_eighty(self):
        feeds = []
        for prefix in ("boost", "top", "profile", "recent"):
            response = Mock()
            response.raise_for_status.return_value = None
            response.json.return_value = [
                {"chainId": "solana", "tokenAddress": f"{prefix}-{index}"}
                for index in range(100)
            ]
            feeds.append(response)
        empty_searches = []
        for _ in range(len(scanner.DISCOVERY_SEARCH_TERMS)):
            response = Mock()
            response.raise_for_status.return_value = None
            response.json.return_value = {"pairs": []}
            empty_searches.append(response)
        with patch.object(scanner.requests, "get", side_effect=[*feeds, *empty_searches]):
            candidates = scanner.fetch_candidate_mints()
        self.assertEqual(len(candidates), 80)
        self.assertEqual(len(set(candidates)), 80)
        self.assertEqual(scanner.LAST_DISCOVERY_DIAGNOSTICS["selected"], 80)
        self.assertEqual(scanner.LAST_CANDIDATE_MINTS, candidates)

    def test_pair_lookup_never_selects_a_non_solana_pair(self):
        scanner.PAIR_CACHE.clear()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"pairs": [
            {**pair(), "chainId": "ethereum", "liquidity": {"usd": 999999}},
            {**pair(), "chainId": "solana", "baseToken": {"address": "mint", "symbol": "TEST"}, "liquidity": {"usd": 100000}},
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

    def test_candidate_feed_interleaves_sources_and_deduplicates(self):
        boosts = Mock()
        boosts.json.return_value = [
            {"chainId": "solana", "tokenAddress": "boost-a"},
            {"chainId": "solana", "tokenAddress": "shared"},
            {"chainId": "ethereum", "tokenAddress": "not-solana"},
        ]
        top_boosts = Mock()
        top_boosts.json.return_value = [
            {"chainId": "solana", "tokenAddress": "top-a"},
            {"chainId": "solana", "tokenAddress": "shared"},
            {"chainId": "solana", "tokenAddress": "top-b"},
        ]
        profiles = Mock()
        profiles.json.return_value = [
            {"chainId": "solana", "tokenAddress": "profile-a"},
            {"chainId": "solana", "tokenAddress": "shared"},
        ]
        empty_searches = []
        for _ in range(len(scanner.DISCOVERY_SEARCH_TERMS)):
            response = Mock()
            response.raise_for_status.return_value = None
            response.json.return_value = {"pairs": []}
            empty_searches.append(response)
        with patch.object(scanner.requests, "get", side_effect=[boosts, top_boosts, profiles, Mock(), *empty_searches]):
            self.assertEqual(
                scanner.fetch_candidate_mints(limit=10),
                ["boost-a", "top-a", "profile-a", "shared", "top-b"],
            )

    def test_shadow_filter_accepts_research_candidate_below_live_liquidity_floor(self):
        research_pair = pair(liquidity={"usd": 30000}, volume={"h1": 5000}, marketCap=200000)
        self.assertFalse(scanner.passes_market_filter(research_pair)[0])
        self.assertEqual(scanner.passes_shadow_market_filter(research_pair), (True, "ok"))
        score, components = scanner.score_shadow_opportunity(research_pair)
        self.assertGreater(score, 0)
        self.assertIn("momentum", components)

    def test_shadow_score_does_not_reward_extreme_short_term_pump_with_hourly_reversal(self):
        healthy = pair(priceChange={"m5": 3.0, "h1": 12.0}, txns={"m5": {"buys": 30, "sells": 10}})
        pump_reversal = pair(priceChange={"m5": 35.0, "h1": -14.0}, txns={"m5": {"buys": 30, "sells": 10}})
        healthy_score, _ = scanner.score_shadow_opportunity(healthy)
        reversal_score, _ = scanner.score_shadow_opportunity(pump_reversal)
        self.assertGreater(healthy_score, reversal_score)

    def test_shadow_score_penalizes_negative_hourly_trend(self):
        healthy = pair(priceChange={"m5": 2.0, "h1": 10.0})
        bearish = pair(priceChange={"m5": 2.0, "h1": -20.0})
        healthy_score, _ = scanner.score_shadow_opportunity(healthy)
        bearish_score, _ = scanner.score_shadow_opportunity(bearish)
        self.assertGreater(healthy_score, bearish_score)

    def test_corrupt_state_recovery_reconstructs_ledger_and_halts_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "state.json").write_text("{broken", encoding="utf-8")
            (root / "trades_log.csv").write_text(
                "timestamp,mint,symbol,entry_price,exit_price,pnl_pct,pnl_usd,equity_after,reason\n"
                "2026-01-01T00:00:00Z,mint,TEST,1,0.9,-10,-5,995,hard_stop_loss\n",
                encoding="utf-8",
            )
            with patch.object(scanner, "Path", side_effect=lambda name: root / name):
                state = scanner.load_state()
            self.assertAlmostEqual(state["equity"], 995.0)
            self.assertEqual(state["trade_count"], 1)
            self.assertTrue(state["state_recovery_required"])
            self.assertEqual(state["risk_control"]["status"], "halted")
            self.assertEqual(state["risk_control"]["reason"], "state_recovery_required")

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
        self.assertFalse(scanner.passes_market_filter(pair(liquidity={"usd": 9999}))[0])
        self.assertFalse(scanner.passes_market_filter(pair(volume={"h1": 9999}))[0])
        self.assertFalse(scanner.passes_market_filter(pair(marketCap=2100000))[0])
        self.assertFalse(scanner.passes_market_filter(pair(txns={"m5": {"buys": 8, "sells": 10}}))[0])

    def test_negative_hourly_momentum_is_rejected(self):
        self.assertFalse(scanner.passes_market_filter(pair(priceChange={"m5": 15, "h1": -0.1}))[0])

    def test_score_rewards_controlled_movement_and_stronger_buy_pressure(self):
        controlled_pullback, _ = scanner.score_market_opportunity(
            pair(priceChange={"m5": 2.0, "h1": 10.0})
        )
        overextended, _ = scanner.score_market_opportunity(
            pair(priceChange={"m5": 15.0, "h1": 10.0})
        )
        weaker_buying, _ = scanner.score_market_opportunity(
            pair(txns={"m5": {"buys": 20, "sells": 10}})
        )
        self.assertGreater(controlled_pullback, overextended)
        self.assertGreater(controlled_pullback, weaker_buying)

    def test_learning_bonus_ignores_unscored_legacy_win_totals(self):
        baseline, _ = scanner.score_market_opportunity(pair())
        learned, components = scanner.score_market_opportunity(
            pair(), {"trades": 100, "wins": 90, "buckets": {}}
        )
        self.assertEqual(learned, baseline)
        self.assertNotIn("learning_bonus", components)

    def test_learning_bonus_requires_positive_net_scored_history(self):
        baseline, _ = scanner.score_market_opportunity(
            pair(), {"trades": 10, "wins": 8,
                     "buckets": {"70": {"n": 10, "wins": 8, "avg_pnl": -2.0}}}
        )
        self.assertNotIn("learning_bonus", scanner.score_market_opportunity(
            pair(), {"trades": 10, "wins": 8,
                     "buckets": {"70": {"n": 10, "wins": 8, "avg_pnl": -2.0}}}
        )[1])
        learned, components = scanner.score_market_opportunity(
            pair(), {"trades": 10, "wins": 8,
                     "buckets": {"70": {"n": 10, "wins": 8, "avg_pnl": 2.0}}}
        )
        self.assertGreater(learned, baseline)
        self.assertEqual(components["learning_bonus"], 10.0)

    def test_learning_bonus_requires_at_least_ten_scored_trades(self):
        _, components = scanner.score_market_opportunity(
            pair(), {"trades": 100, "wins": 90,
                     "buckets": {"70": {"n": 9, "wins": 8, "avg_pnl": 2.0}}}
        )
        self.assertNotIn("learning_bonus", components)

    def test_shadow_score_uses_the_same_net_profitable_learning_gate(self):
        candidate = pair(priceChange={"m5": 2.0, "h1": 10.0})
        _, no_edge = scanner.score_shadow_opportunity(
            candidate, {"trades": 100, "wins": 90,
                        "buckets": {"70": {"n": 10, "wins": 8, "avg_pnl": -3.0}}}
        )
        _, positive_edge = scanner.score_shadow_opportunity(
            candidate, {"trades": 100, "wins": 90,
                        "buckets": {"70": {"n": 10, "wins": 8, "avg_pnl": 3.0}}}
        )
        self.assertNotIn("learning_bonus", no_edge)
        self.assertEqual(positive_edge["learning_bonus"], 10.0)

    def test_candidate_confirmation_requires_three_consecutive_hits(self):
        state = {"strategy": {"active_threshold": 80}, "candidate_observations": {}}
        with patch.object(scanner, "CANDIDATE_CONFIRMATION_HITS", 3):
            self.assertFalse(scanner.candidate_confirmation(state, "mint", 85))
            self.assertFalse(scanner.candidate_confirmation(state, "mint", 86))
            self.assertTrue(scanner.candidate_confirmation(state, "mint", 87))

    def test_paper_profile_can_confirm_on_first_qualified_observation(self):
        state = {"strategy": {"active_threshold": 60}, "candidate_observations": {}}
        with patch.object(scanner, "CANDIDATE_CONFIRMATION_HITS", 1):
            self.assertTrue(scanner.candidate_confirmation(state, "mint", 75))

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

    def test_profit_lock_requires_validated_whale_cohort_and_positive_realized_equity(self):
        valid = {
            "initial_equity": 1000.0,
            "equity": 1001.0,
            "strategy": {"validation_status": "validated", "history_rows": 30},
        }
        self.assertTrue(scanner.should_lock_paper_profit(valid))
        self.assertFalse(scanner.should_lock_paper_profit({
            **valid, "equity": 999.0,
        }))
        self.assertFalse(scanner.should_lock_paper_profit({
            **valid, "strategy": {"validation_status": "exploration_unvalidated", "history_rows": 30},
        }))
        self.assertFalse(scanner.should_lock_paper_profit({
            **valid, "strategy": {"validation_status": "validated", "history_rows": 29},
        }))

    def test_profit_locked_paper_account_manages_positions_but_opens_no_new_entries(self):
        state = scanner.normalize_state({
            "equity": 1001.0,
            "initial_equity": 1000.0,
            "profit_lock_active": True,
        })
        with patch.object(scanner, "fetch_candidate_mints", side_effect=AssertionError("discovery must be skipped")), \
             patch.object(scanner, "save_state"), patch.object(scanner, "log"):
            result = scanner.run_cycle(state)
        self.assertTrue(result["profit_locked"])
        self.assertEqual(result["opened"], [])
        self.assertEqual(result["scanned"], 0)

    def test_minute_monitor_skips_whale_rpc_when_snapshot_is_fresh_but_still_checks_price(self):
        now = time.time()
        state = {
            "equity": 1000.0, "initial_equity": 1000.0, "trade_count": 0,
            "blacklist": [], "cooldowns": {}, "positions": {
                "mint": {"symbol": "TEST", "entry_price": 1.0, "peak_price": 1.0,
                         "entry_time": now, "last_whale_check_at": now,
                         "entry_liquidity": 100000, "amount_usd": 10.0,
                         "signal_score": 70, "trailing_armed": False,
                         "whale_balances": {"owner-a": 1000.0}}
            }
        }
        live_pair = {"priceUsd": "0.88", "liquidity": {"usd": 100000},
                     "txns": {"m5": {"buys": 10, "sells": 10}},
                     "priceChange": {"m5": -12.0}}
        with patch.object(scanner.time, "time", return_value=now), \
             patch.object(scanner, "helius_configured", return_value=True), \
             patch.object(scanner, "fetch_whale_balances", side_effect=AssertionError("fresh whale snapshot should be reused")), \
             patch.object(scanner, "fetch_dex_pair", return_value=live_pair), \
             patch.object(scanner, "append_trade"), patch.object(scanner, "append_learning_observation"), \
             patch.object(scanner, "log"):
            scanner.manage_open_positions(state)
        self.assertNotIn("mint", state["positions"])
        self.assertEqual(state["trade_count"], 1)

    def test_run_cycle_opens_on_first_qualified_paper_observation(self):
        state = scanner.normalize_state({})
        with patch.object(scanner, "helius_configured", return_value=False), \
             patch.object(scanner, "fetch_candidate_mints", return_value=["mint"]), \
             patch.object(scanner, "fetch_dex_pair", return_value=pair(priceChange={"m5": 2.0, "h1": 10.0})), \
             patch.object(scanner, "save_state"), patch.object(scanner, "log"):
            result = scanner.run_cycle(state)
        self.assertEqual(result["open_positions"], 1)
        self.assertIn("mint", state["positions"])
        self.assertTrue(state.get("last_run"))

    def test_non_whale_paper_mode_does_not_block_entry_when_helius_snapshot_fails(self):
        state = scanner.normalize_state({})
        candidate = pair(priceChange={"m5": 3.0, "h1": 10.0})
        with ExitStack() as stack:
            stack.enter_context(patch.object(scanner, "helius_configured", return_value=True))
            stack.enter_context(patch.object(scanner, "fetch_whale_balances", return_value=None))
            stack.enter_context(patch.object(scanner, "fetch_candidate_mints", return_value=["mint"]))
            stack.enter_context(patch.object(scanner, "fetch_dex_pair", return_value=candidate))
            stack.enter_context(patch.object(scanner, "fetch_dex_pairs", return_value={"mint": candidate}))
            stack.enter_context(patch.object(scanner, "manage_open_positions"))
            stack.enter_context(patch.object(scanner, "manage_shadow_positions"))
            stack.enter_context(patch.object(scanner, "scan_shadow_candidates",
                                             return_value={"scanned": 0, "shadow_opened": []}))
            stack.enter_context(patch.object(scanner, "save_state"))
            stack.enter_context(patch.object(scanner, "log"))
            result = scanner.run_cycle(state, require_whale_accumulation=False)
        self.assertEqual(result["opened"], ["mint"])
        self.assertIn("mint", state["positions"])
        self.assertNotIn("whale_data_unavailable", state["paper_scan_diagnostics"]["rejections"])

    def test_whale_gated_entry_records_accumulation_evidence(self):
        owners = {f"owner-{i}": 1000.0 for i in range(6)}
        current = {key: value * 1.02 for key, value in owners.items()}
        state = scanner.normalize_state({"whale_snapshots": {"mint": owners}})
        with patch.object(scanner, "helius_configured", return_value=True), \
             patch.object(scanner, "fetch_candidate_mints", return_value=["mint"]), \
             patch.object(scanner, "fetch_dex_pair", return_value=pair(priceChange={"m5": 2.0, "h1": 10.0})), \
             patch.object(scanner, "fetch_whale_balances", return_value=current), \
             patch.object(scanner, "confirmed_whale_dump", return_value=False), \
             patch.object(scanner, "whale_accumulation_confirmed", return_value=True), \
             patch.object(scanner, "save_state"), patch.object(scanner, "log"):
            scanner.run_cycle(state, require_whale_accumulation=True)
        self.assertIn("mint", state["positions"])
        components = state["positions"]["mint"]["entry_components"]
        self.assertEqual(components["whale_gate_enabled"], 1.0)
        self.assertEqual(components["whale_accumulation_confirmed"], 1.0)
        self.assertEqual(components["entry_liquidity_usd"], 100000.0)
        self.assertEqual(components["entry_volume_h1_usd"], 50000.0)

    def test_whale_gated_paper_cycle_fails_closed_without_helius(self):
        state = scanner.normalize_state({})
        with patch.object(scanner, "helius_configured", return_value=False), \
             patch.object(scanner, "fetch_candidate_mints", return_value=["mint"]), \
             patch.object(scanner, "fetch_dex_pair", return_value=pair(priceChange={"m5": 2.0, "h1": 10.0})), \
             patch.object(scanner, "save_state"), patch.object(scanner, "log"):
            scanner.run_cycle(state, require_whale_accumulation=True)
        self.assertNotIn("mint", state["positions"])
        self.assertEqual(state["paper_scan_diagnostics"]["rejections"]["whale_data_unavailable"], 1)

    def test_confirmed_whale_dump_closes_open_position(self):
        state = {
            "equity": 1000.0, "trade_count": 0, "blacklist": [], "cooldowns": {},
            "whale_snapshots": {"mint": {"owner-a": 1000.0}},
            "positions": {"mint": {"symbol": "TEST", "entry_price": 1.0, "peak_price": 1.0,
                "entry_time": time.time(), "entry_liquidity": 100000, "amount_usd": 10.0,
                "signal_score": 85, "trailing_armed": False,
                "last_whale_check_at": time.time() - 301,
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

    def test_stale_shadow_position_is_discarded_without_training_sample(self):
        state = {
            "equity": 293.36, "trade_count": 0,
            "shadow_positions": {"mint": {
                "symbol": "STALE", "entry_price": 1.0,
                "entry_time": time.time() - scanner.SHADOW_STALE_OBSERVATION_SECONDS - 60,
                "entry_liquidity": 100000, "peak_price": 1.0,
                "signal_score": 85.0, "trailing_armed": False,
            }},
        }
        with patch.object(scanner, "fetch_dex_pair") as fetch_pair, \
             patch.object(scanner, "append_learning_observation") as append_observation, \
             patch.object(scanner, "log"):
            scanner.manage_shadow_positions(state)
        self.assertEqual(state["shadow_positions"], {})
        fetch_pair.assert_not_called()
        append_observation.assert_not_called()
        self.assertEqual(state["equity"], 293.36)
        self.assertEqual(state["trade_count"], 0)

    def test_trade_target_disables_new_entries(self):
        state = scanner.normalize_state({"trade_count": scanner.TARGET_TRADES})
        with patch.object(scanner, "reconcile_learning_from_trade_log"), patch.object(scanner, "manage_open_positions"), patch.object(scanner, "save_state"), \
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


    def test_learning_observation_persists_features_and_migrates_legacy_csv(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shadow_observations.csv"
            path.write_text(
                "timestamp,score,pnl_pct,reason\n2026-01-01T00:00:00,80,-3,shadow_hard_stop_loss\n",
                encoding="utf-8",
            )
            with patch.object(scanner, "Path", side_effect=lambda name: Path(tmp) / name):
                scanner.append_learning_observation(
                    85.0, 4.0, "shadow_max_hold_timeout",
                    {"momentum_m5_pct": 2.5, "buy_sell_ratio": 1.8},
                )
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 2)
            self.assertEqual(json.loads(rows[0]["features_json"]), {})
            self.assertEqual(json.loads(rows[1]["features_json"])["momentum_m5_pct"], 2.5)

    def test_unvalidated_paper_account_uses_exploration_threshold_without_touching_main_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paper = root / "paper_account"
            paper.mkdir()
            (paper / "state.json").write_text(json.dumps({
                "equity": 1000.0, "initial_equity": 1000.0, "trade_count": 51,
                "strategy": {"active_threshold": 80.0, "status": "waiting_for_validation",
                             "validation_status": "no_validated_candidate"},
            }), encoding="utf-8")
            (root / "state.json").write_text("main-account-must-not-change", encoding="utf-8")
            captured = {}
            def fake_cycle(state=None, **kwargs):
                captured["state"] = state
                captured["kwargs"] = kwargs
                return {"equity": state["equity"]}
            with patch.object(scanner, "helius_configured", return_value=False):
                with patch.object(scanner, "run_cycle", side_effect=fake_cycle):
                    scanner.run_isolated_paper_cycle(root)
            self.assertEqual(captured["state"]["strategy"]["active_threshold"], scanner.OPPORTUNITY_ENTRY_THRESHOLD)
            self.assertEqual(captured["state"]["strategy"]["validation_status"], "exploration_unvalidated")
            self.assertEqual((root / "state.json").read_text(encoding="utf-8"), "main-account-must-not-change")

    def test_validated_or_rolled_back_paper_threshold_is_preserved(self):
        for validation_status, active_threshold in (("validated", 70.0), ("rolled_back", 80.0)):
            with self.subTest(validation_status=validation_status), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                paper = root / "paper_account"
                paper.mkdir()
                (paper / "state.json").write_text(json.dumps({
                    "equity": 1000.0, "initial_equity": 1000.0, "trade_count": 51,
                    "strategy": {"active_threshold": active_threshold, "status": validation_status,
                                 "validation_status": validation_status},
                }), encoding="utf-8")
                captured = {}
                def fake_cycle(state=None, **kwargs):
                    captured["state"] = state
                    return {"equity": state["equity"]}
                with patch.object(scanner, "helius_configured", return_value=False):
                    with patch.object(scanner, "run_cycle", side_effect=fake_cycle):
                        scanner.run_isolated_paper_cycle(root)
                self.assertEqual(captured["state"]["strategy"]["active_threshold"], active_threshold)

    def test_isolated_paper_cycle_requires_whale_accumulation_by_default_when_provider_is_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            captured = {}
            def fake_cycle(state=None, **kwargs):
                captured.update(kwargs)
                return {"equity": 1000.0}
            with patch.dict("os.environ", {}, clear=False):
                import os
                os.environ.pop("PAPER_REQUIRE_WHALE_ACCUMULATION", None)
                with patch.object(scanner, "helius_configured", return_value=True):
                    with patch.object(scanner, "run_cycle", side_effect=fake_cycle):
                        scanner.run_isolated_paper_cycle(Path(tmp))
            self.assertTrue(captured["require_whale_accumulation"])

    def test_isolated_paper_cycle_keeps_whale_gate_on_when_provider_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            captured = {}
            def fake_cycle(state=None, **kwargs):
                captured.update(kwargs)
                return {"equity": 1000.0}
            with patch.object(scanner, "helius_configured", return_value=False), \
                 patch.object(scanner, "run_cycle", side_effect=fake_cycle):
                scanner.run_isolated_paper_cycle(Path(tmp))
            self.assertTrue(captured["require_whale_accumulation"])

    def test_isolated_paper_cycle_writes_to_its_own_directory_and_reuses_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "state.json").write_text("legacy-state", encoding="utf-8")
            (root / "trades_log.csv").write_text("legacy-ledger", encoding="utf-8")
            original_cwd = Path.cwd()
            captured = {}

            def fake_cycle(state=None, **kwargs):
                captured.update(kwargs)
                Path("state.json").write_text("paper-state", encoding="utf-8")
                Path("trades_log.csv").write_text("paper-ledger", encoding="utf-8")
                return {"equity": 1000.0}

            with patch.object(scanner, "run_cycle", side_effect=fake_cycle):
                result = scanner.run_isolated_paper_cycle(root, candidates=["reused-mint"])

            self.assertEqual(result["equity"], 1000.0)
            self.assertEqual(captured["candidates"], ["reused-mint"])
            self.assertEqual((root / "state.json").read_text(encoding="utf-8"), "legacy-state")
            self.assertEqual((root / "trades_log.csv").read_text(encoding="utf-8"), "legacy-ledger")
            self.assertEqual((root / "paper_account" / "state.json").read_text(encoding="utf-8"), "paper-state")
            self.assertEqual((root / "paper_account" / "trades_log.csv").read_text(encoding="utf-8"), "paper-ledger")
            self.assertEqual(Path.cwd(), original_cwd)


    def test_active_paper_cycle_collects_shadow_outcomes_without_double_counting_scans(self):
        state = scanner.normalize_state({})
        with patch.object(scanner, "reconcile_learning_from_trade_log"), \
             patch.object(scanner, "manage_open_positions"), \
             patch.object(scanner, "save_state"), \
             patch.object(scanner, "fetch_candidate_mints", return_value=["mint"]), \
             patch.object(scanner, "fetch_dex_pair", return_value=pair()), \
             patch.object(scanner, "score_market_opportunity", return_value=(50.0, {})), \
             patch.object(scanner, "log"):
            scanner.run_cycle(state)
        self.assertIn("mint", state["shadow_positions"])
        self.assertEqual(state["scan_count"], 1)
        self.assertEqual(state["positions"], {})


    def test_shadow_scan_rejects_bearish_signals_clamped_to_zero_score(self):
        state = scanner.normalize_state({})
        bearish = pair(
            priceChange={"m5": -10.0, "h1": -90.0},
            txns={"m5": {"buys": 0, "sells": 10}},
            liquidity={"usd": 5000},
            volume={"h1": 10000},
            marketCap=100000,
        )
        with patch.object(scanner, "fetch_candidate_mints", return_value=["bearish"]), \
             patch.object(scanner, "fetch_dex_pair", return_value=bearish), \
             patch.object(scanner, "log"):
            result = scanner.scan_shadow_candidates(state)
        self.assertEqual(result["shadow_opened"], [])
        self.assertEqual(state["shadow_positions"], {})


    def test_active_scan_records_market_filter_rejection_reason(self):
        state = scanner.normalize_state({})
        with patch.object(scanner, "reconcile_learning_from_trade_log"), \
             patch.object(scanner, "manage_open_positions"), \
             patch.object(scanner, "save_state"), \
             patch.object(scanner, "fetch_candidate_mints", return_value=["mint"]), \
             patch.object(scanner, "fetch_dex_pair", return_value=pair(liquidity={"usd": 9000})), \
             patch.object(scanner, "score_market_opportunity", return_value=(90.0, {})), \
             patch.object(scanner, "log"):
            scanner.run_cycle(state)
        diagnostics = state["paper_scan_diagnostics"]
        self.assertEqual(diagnostics["candidate_count"], 1)
        self.assertEqual(diagnostics["market_filter_passed"], 0)
        self.assertEqual(diagnostics["rejections"], {"low_liquidity:9000": 1})


    def test_candidate_discovery_includes_established_meme_search_results(self):
        empty_feed = Mock()
        empty_feed.raise_for_status.return_value = None
        empty_feed.json.return_value = []
        search_result = Mock()
        search_result.raise_for_status.return_value = None
        search_result.json.return_value = {
            "pairs": [
                {
                    "chainId": "solana",
                    "baseToken": {"address": "search-mint", "symbol": "BONK", "name": "Bonk"},
                    "quoteToken": {"address": "usdc-mint", "symbol": "USDC", "name": "USD Coin"},
                    "liquidity": {"usd": 50000},
                    "volume": {"h1": 40000},
                    "pairCreatedAt": 1760000000000,
                },
                {
                    "chainId": "solana",
                    "baseToken": {"address": "bonk-clone", "symbol": "BONK2", "name": "BONK 2.0"},
                    "quoteToken": {"address": "usdc-mint", "symbol": "USDC", "name": "USD Coin"},
                    "liquidity": {"usd": 50000},
                    "volume": {"h1": 40000},
                    "pairCreatedAt": 1760000000000,
                },
                {
                    "chainId": "solana",
                    "baseToken": {"address": "illiquid-mint", "symbol": "WIF", "name": "dogwifhat"},
                    "quoteToken": {"address": "usdc-mint", "symbol": "USDC", "name": "USD Coin"},
                    "liquidity": {"usd": 10000},
                    "volume": {"h1": 40000},
                    "pairCreatedAt": 1760000000000,
                },
            ]
        }
        empty_searches = []
        for _ in range(len(scanner.DISCOVERY_SEARCH_TERMS) - 1):
            response = Mock()
            response.raise_for_status.return_value = None
            response.json.return_value = {"pairs": []}
            empty_searches.append(response)
        with patch.object(
            scanner.requests, "get",
            side_effect=[empty_feed, empty_feed, empty_feed, empty_feed, search_result, *empty_searches],
        ):
            candidates = scanner.fetch_candidate_mints(limit=10)
        self.assertIn("search-mint", candidates)
        self.assertNotIn("bonk-clone", candidates)
        self.assertNotIn("illiquid-mint", candidates)
        self.assertNotIn("usdc-mint", candidates)


    def test_pullback_profile_accepts_hourly_strength_but_rejects_m5_chasing(self):
        pullback = pair(
            liquidity={"usd": 22000},
            volume={"h1": 50000},
            marketCap=70000,
            txns={"m5": {"buys": 15, "sells": 10}},
            priceChange={"m5": -1.0, "h1": 60.0},
        )
        self.assertEqual(scanner.passes_market_filter(pullback), (True, "ok"))
        score, _ = scanner.score_market_opportunity(pullback)
        self.assertGreaterEqual(score, scanner.OPPORTUNITY_ENTRY_THRESHOLD)

        overextended = pair(
            liquidity={"usd": 25000},
            volume={"h1": 50000},
            marketCap=80000,
            txns={"m5": {"buys": 30, "sells": 10}},
            priceChange={"m5": 16.89, "h1": 22.47},
        )
        accepted, reason = scanner.passes_market_filter(overextended)
        self.assertFalse(accepted)
        self.assertTrue(reason.startswith("overextended_momentum_m5:"))


    def test_active_scan_persists_near_miss_score_and_features(self):
        state = scanner.normalize_state({})
        state["strategy"]["active_threshold"] = 75.0
        components = {
            "momentum_m5_pct": 3.0,
            "momentum_h1_pct": 25.0,
            "buy_sell_ratio": 1.8,
            "liquidity_usd": 100000.0,
            "volume_h1_usd": 50000.0,
            "liquidity_to_mcap_ratio": 0.4,
        }
        with patch.object(scanner, "reconcile_learning_from_trade_log"), \
             patch.object(scanner, "manage_open_positions"), \
             patch.object(scanner, "save_state"), \
             patch.object(scanner, "fetch_candidate_mints", return_value=["mint"]), \
             patch.object(scanner, "fetch_dex_pair", return_value=pair()), \
             patch.object(scanner, "score_market_opportunity", return_value=(72.0, components)), \
             patch.object(scanner, "log"):
            scanner.run_cycle(state)
        diagnostics = state["paper_scan_diagnostics"]
        near_miss = diagnostics["near_misses"][0]
        self.assertEqual(near_miss["score"], 72.0)
        self.assertEqual(near_miss["symbol"], "TEST")
        self.assertEqual(near_miss["momentum_m5_pct"], 3.0)
        evaluated = diagnostics["evaluated_candidates"][0]
        self.assertEqual(evaluated["score"], 72.0)
        self.assertFalse(evaluated["eligible_by_score"])


    def test_paper_trade_net_pnl_deducts_estimated_fees_after_slippage(self):
        state = {
            "equity": 1000.0,
            "trade_count": 0,
            "positions": {
                "mint": {
                    "symbol": "TEST",
                    "entry_price": 1.015,
                    "amount_usd": 5.0,
                    "signal_score": 85,
                    "entry_components": {"momentum_m5_pct": 3.0},
                }
            },
            "blacklist": [],
            "cooldowns": {},
        }
        with patch.object(scanner, "append_trade") as trade, \
             patch.object(scanner, "append_learning_observation") as learning:
            scanner.close_position(state, "mint", 1.2, "trailing_stop")
        gross_pct = ((1.2 * (1 - scanner.EXIT_SLIPPAGE_BPS / 10000)) / 1.015 - 1) * 100
        net_pct = gross_pct - scanner.PAPER_ROUND_TRIP_FEE_PCT
        self.assertAlmostEqual(trade.call_args.args[5] * 100, net_pct, places=6)
        self.assertAlmostEqual(trade.call_args.args[6], 5.0 * net_pct / 100, places=6)
        self.assertAlmostEqual(learning.call_args.args[1], gross_pct, places=6)
        self.assertAlmostEqual(state["equity"], 1000.0 + 5.0 * net_pct / 100, places=6)


class PaperScenarioLabTests(unittest.TestCase):
    def test_scenario_lab_exercises_exit_and_missing_quote_paths(self):
        report = paper_scenario_lab.run_scenarios()
        scenarios = report["scenarios"]
        self.assertEqual(report["mode"], "deterministic_simulation_not_live_or_historical_performance")
        self.assertEqual(report["scenario_count"], 16)
        self.assertEqual(report["closed_scenarios"], sum(item.get("status") == "closed" for item in scenarios.values()))
        self.assertEqual(report["open_scenarios"], 4)
        self.assertEqual(report["unresolved_scenarios"], 2)
        self.assertEqual(report["entry_gate_scenario_count"], 9)
        self.assertTrue(report["entry_gate_scenarios"]["healthy_baseline"]["passed"])
        self.assertFalse(report["entry_gate_scenarios"]["liquidity_below_floor"]["passed"])
        self.assertFalse(report["entry_gate_scenarios"]["weak_buy_pressure"]["passed"])
        self.assertEqual(scenarios["pump_then_reversal"]["reason"], "trailing_stop")
        self.assertLess(scenarios["pump_then_reversal"]["net_pnl_pct"], 0.0)
        self.assertEqual(scenarios["hard_stop"]["reason"], "hard_stop_loss")
        self.assertEqual(scenarios["trailing_armed_then_gap_through_hard_stop"]["reason"], "hard_stop_loss")
        self.assertLess(scenarios["hard_stop"]["net_pnl_usd"], 0.0)
        self.assertEqual(scenarios["liquidity_drain_confirmed_twice"]["reason"], "liquidity_drained")
        self.assertEqual(scenarios["max_hold_timeout"]["reason"], "max_hold_timeout")
        self.assertEqual(scenarios["missing_quote"]["status"], "unresolved")
        self.assertIsNone(scenarios["missing_quote"]["net_pnl_pct"])
        self.assertEqual(scenarios["small_gain_no_exit_yet"]["status"], "open")
        self.assertIsNone(scenarios["small_gain_no_exit_yet"]["exit_price_effective"])

    def test_scenario_lab_rejects_invalid_and_non_monotonic_inputs(self):
        with self.assertRaises(ValueError):
            paper_scenario_lab.simulate_exit_path([], entry_price=0)
        with self.assertRaises(ValueError):
            paper_scenario_lab.simulate_exit_path([
                {"seconds": 30, "price": 1.0, "liquidity": 1000},
                {"seconds": 20, "price": 1.0, "liquidity": 1000},
            ])


    def test_pullback_profile_accepts_liquidity_ratio_just_above_0_15(self):
        candidate = pair(
            liquidity={"usd": 50000},
            volume={"h1": 50000},
            marketCap=312500,
            txns={"m5": {"buys": 13, "sells": 10}},
            priceChange={"m5": 2.0, "h1": 10.0},
        )
        self.assertEqual(scanner.passes_market_filter(candidate), (True, "ok"))

    def test_pullback_profile_rejects_negative_hourly_trend(self):
        negative_trend = pair(priceChange={"m5": 0.5, "h1": -0.1})
        accepted, reason = scanner.passes_market_filter(negative_trend)
        self.assertFalse(accepted)
        self.assertTrue(reason.startswith("negative_momentum_h1:"))




class DexScreenerMintAttributionTests(unittest.TestCase):
    def _pair(self, base_mint, quote_mint, liquidity, price):
        return {
            "chainId": "solana",
            "pairAddress": f"pool-{base_mint}-{quote_mint}",
            "baseToken": {"address": base_mint, "symbol": "BASE"},
            "quoteToken": {"address": quote_mint, "symbol": "QUOTE"},
            "liquidity": {"usd": liquidity},
            "priceUsd": str(price),
            "priceChange": {"m5": 2.0, "h1": 10.0},
            "volume": {"h1": 50000},
            "txns": {"m5": {"buys": 20, "sells": 10}},
        }

    def test_single_mint_lookup_never_returns_quote_side_price(self):
        requested = "target-mint"
        wrong_but_more_liquid = self._pair("other-token", requested, 900000, 42.0)
        correct = self._pair(requested, "wrapped-sol", 20000, 0.0004)
        response = Mock()
        response.json.return_value = {"pairs": [wrong_but_more_liquid, correct]}
        response.raise_for_status.return_value = None
        with patch.object(scanner, "PAIR_CACHE", {}), patch.object(scanner.requests, "get", return_value=response):
            selected = scanner.fetch_dex_pair(requested)
        self.assertEqual(selected["baseToken"]["address"], requested)
        self.assertEqual(selected["priceUsd"], "0.0004")

    def test_single_mint_lookup_accepts_documented_token_pairs_array(self):
        requested = "target-mint"
        correct = self._pair(requested, "wrapped-sol", 20000, 0.0004)
        response = Mock()
        response.json.return_value = [correct]
        response.raise_for_status.return_value = None
        with patch.object(scanner, "PAIR_CACHE", {}), patch.object(scanner.requests, "get", return_value=response):
            selected = scanner.fetch_dex_pair(requested)
        self.assertEqual(selected["baseToken"]["address"], requested)
        self.assertEqual(selected["priceUsd"], "0.0004")
        self.assertEqual(scanner.DEXSCREENER_TOKEN_PAIRS_URL, "https://api.dexscreener.com/token-pairs/v1/solana/{}")

    def test_batch_lookup_maps_requested_base_mint_only(self):
        requested = "target-mint"
        wrong_quote_pair = self._pair("other-token", requested, 900000, 42.0)
        correct_base_pair = self._pair(requested, "wrapped-sol", 20000, 0.0004)
        response = Mock()
        response.json.return_value = [wrong_quote_pair, correct_base_pair]
        response.raise_for_status.return_value = None
        with patch.object(scanner, "PAIR_CACHE", {}), patch.object(scanner.requests, "get", return_value=response):
            selected = scanner.fetch_dex_pairs([requested])[requested]
        self.assertEqual(selected["baseToken"]["address"], requested)
        self.assertEqual(selected["priceUsd"], "0.0004")

    def test_batch_lookup_does_not_fabricate_pair_for_quote_only_mint(self):
        requested = "target-mint"
        quote_only_pair = self._pair("other-token", requested, 900000, 42.0)
        response = Mock()
        response.json.return_value = [quote_only_pair]
        response.raise_for_status.return_value = None
        with patch.object(scanner, "PAIR_CACHE", {}), patch.object(scanner.requests, "get", return_value=response):
            selected = scanner.fetch_dex_pairs([requested])[requested]
        self.assertIsNone(selected)


if __name__ == "__main__":
    unittest.main()