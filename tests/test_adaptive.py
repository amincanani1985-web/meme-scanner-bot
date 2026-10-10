import unittest
from unittest.mock import patch
import learn_agent as la
import meme_scanner_cycle as scanner

class AdaptiveTests(unittest.TestCase):
    def test_walk_forward_needs_history(self):
        self.assertFalse(la.walk_forward([],60)["eligible"])

    def test_walk_forward_detects_stronger_high_score_bucket(self):
        rows=[]
        for i in range(30):
            score=65 if i>=15 else 60
            pnl=2.0 if score==65 else -1.0
            rows.append({"timestamp":f"2026-01-{i+1:02d}","score":score,"pnl_pct":pnl,"reason":"x"})
        r=la.walk_forward(rows,65)
        self.assertTrue(r["eligible"])
        self.assertGreater(r["validation"]["avg_pnl"],0)

    def test_rollback_triggers_on_degradation(self):
        rows=[{"timestamp":str(i),"score":65,"pnl_pct":-3.0,"reason":"x"} for i in range(10)]
        ok,reason=la.should_rollback(rows,65)
        self.assertTrue(ok)
        self.assertIn("active_strategy",reason)

    def test_adaptive_activation_is_bounded(self):
        rows=[]
        for i in range(30):
            score=65 if i>=15 else 60
            pnl=2.0 if score==65 else -1.0
            rows.append({"timestamp":f"{i:02d}","score":score,"pnl_pct":pnl,"reason":"x"})
        state={"trade_count":30}
        out=la.adaptive_update_state(state,rows)
        self.assertIn(out["strategy"]["active_threshold"],la.CANDIDATE_THRESHOLDS)

    def test_legacy_untrained_baseline_migrates_to_hardened_threshold(self):
        state = {"strategy": {
            "active_threshold": 60.0,
            "rollback_threshold": 60.0,
            "candidate_threshold": None,
            "status": "waiting_for_history",
        }}
        scanner.normalize_legacy_strategy_baseline(state)
        self.assertEqual(state["strategy"]["active_threshold"], 65.0)
        self.assertEqual(state["strategy"]["rollback_threshold"], 65.0)

    def test_momentum_guard_rejects_negative_h1(self):
        p={"liquidity":{"usd":20000},"volume":{"h1":10000},"marketCap":50000,
           "txns":{"m5":{"buys":20,"sells":10}},"priceChange":{"m5":2,"h1":-1}}
        self.assertFalse(scanner.passes_market_filter(p)[0])

    def test_entry_confirmation_requires_two_hits(self):
        s={"candidate_observations":{}}
        self.assertFalse(scanner.candidate_confirmation(s,"mint",65))
        self.assertTrue(scanner.candidate_confirmation(s,"mint",66))

    def test_slippage_changes_pnl(self):
        s={"equity":1000.0,"positions":{},"trade_count":0,"blacklist":[],"cooldowns":[]}
        p={"liquidity":{"usd":20000},"volume":{"h1":10000},"marketCap":50000,
           "txns":{"m5":{"buys":20,"sells":10}},"priceChange":{"m5":2,"h1":8},
           "priceUsd":"1.0","baseToken":{"symbol":"TEST"}}
        scanner.open_position(s,"mint",p,{"holder":100},65,{})
        with patch.object(scanner,"append_trade"), patch.object(scanner,"append_learning_observation"):
            scanner.close_position(s,"mint",1.0,"max_hold")
        self.assertLess(s["equity"],1000.0)

if __name__=="__main__":
    unittest.main()
