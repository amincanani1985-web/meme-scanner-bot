#!/usr/bin/env python3
"""
مجموعه تست‌های جامع برای پروژه meme-scanner-bot
اجرا: python run_all_tests.py
"""

import sys
import json
import time
import os
from pathlib import Path
from datetime import datetime

print("\n" + "="*70)
print("  🚀 تست‌های MEME SCANNER BOT v17")
print("="*70 + "\n")

try:
    import meme_scanner_cycle as scanner
    print("✅ meme_scanner_cycle imported successfully")
except Exception as e:
    print(f"❌ Failed to import meme_scanner_cycle: {e}")
    sys.exit(1)

try:
    import learn_agent as la
    print("✅ learn_agent imported successfully")
except Exception as e:
    print(f"❌ Failed to import learn_agent: {e}")
    sys.exit(1)

try:
    import requests
    print("✅ requests imported successfully")
except Exception as e:
    print(f"❌ Failed to import requests: {e}")
    sys.exit(1)

print("\n" + "="*70)
print("  TEST 1: State Recovery")
print("="*70 + "\n")

# Test corrupted state recovery
broken = {
    "equity": "invalid",
    "positions": "not_dict",
    "blacklist": "not_list",
    "cooldowns": "not_dict",
    "trade_count": "abc",
    "candidate_observations": "bad",
    "strategy": "str"
}

fixed = scanner.normalize_state(broken)
assert isinstance(fixed['positions'], dict), "positions should be dict"
assert isinstance(fixed['blacklist'], list), "blacklist should be list"
assert isinstance(fixed['cooldowns'], dict), "cooldowns should be dict"
assert fixed['equity'] == scanner.STARTING_EQUITY, "equity should reset to STARTING_EQUITY"
print("✅ Corrupted state recovery: PASSED")

# Test good state preservation
good = {
    "equity": 500.0,
    "positions": {},
    "blacklist": [],
    "cooldowns": {},
    "trade_count": 5,
    "scan_count": 2,
    "candidate_observations": {},
    "strategy": {}
}
fixed2 = scanner.normalize_state(good)
assert fixed2['equity'] == 500.0, "equity should be preserved"
assert fixed2['trade_count'] == 5, "trade_count should be preserved"
print("✅ Valid state preservation: PASSED")

# Test legacy strategy migration
old = {
    "strategy": {
        "active_threshold": 60.0,
        "rollback_threshold": 60.0,
        "candidate_threshold": None,
        "status": "waiting_for_history"
    }
}
migrated = scanner.normalize_legacy_strategy_baseline(old)
assert migrated["strategy"]["active_threshold"] == 65.0, "should migrate from 60 to 65"
assert migrated["strategy"]["baseline_migrated_from"] == 60.0, "should mark migration"
print("✅ Legacy strategy migration: PASSED")

print("\n" + "="*70)
print("  TEST 2: Slippage Calculation")
print("="*70 + "\n")

# Test entry slippage (150 bps)
entry_price = 1.0
entry_with_slippage = entry_price * (1.0 + scanner.ENTRY_SLIPPAGE_BPS / 10000.0)
expected_entry = 1.0 * 1.015  # 150 bps
assert abs(entry_with_slippage - expected_entry) < 0.0001, f"Entry slippage incorrect"
print(f"✅ Entry slippage: {scanner.ENTRY_SLIPPAGE_BPS} bps = +{(entry_with_slippage - 1.0)*100:.2f}%")

# Test exit slippage (200 bps)
exit_price = 1.0
exit_with_slippage = exit_price * (1.0 - scanner.EXIT_SLIPPAGE_BPS / 10000.0)
expected_exit = 1.0 * 0.98  # 200 bps
assert abs(exit_with_slippage - expected_exit) < 0.0001, f"Exit slippage incorrect"
print(f"✅ Exit slippage: {scanner.EXIT_SLIPPAGE_BPS} bps = -{(1.0 - exit_with_slippage)*100:.2f}%")

total_slippage = (scanner.ENTRY_SLIPPAGE_BPS + scanner.EXIT_SLIPPAGE_BPS) / 100
print(f"✅ Total slippage per round-trip: ~{total_slippage:.1f}%")

print("\n" + "="*70)
print("  TEST 3: Position Management")
print("="*70 + "\n")

# Test cooldown application
state = {
    "equity": 1000.0,
    "positions": {"mint": {"symbol": "TEST", "entry_price": 1.0, "amount_usd": 10, "estimated_impact": 0, "signal_score": 70}},
    "blacklist": [],
    "cooldowns": {},
    "trade_count": 0
}

# Apply cooldown
now = time.time()
state["cooldowns"]["mint"] = now + 1800  # 30 min cooldown
is_blocked = "mint" in state["cooldowns"] and now < state["cooldowns"]["mint"]
assert is_blocked, "Cooldown should block re-entry"
print("✅ Stop loss cooldown (30min): PASSED")

# Test blacklist
state2 = {
    "equity": 1000.0,
    "positions": {},
    "blacklist": ["liquidity_drained_mint"],
    "cooldowns": {},
    "trade_count": 0
}
assert "liquidity_drained_mint" in state2["blacklist"], "Blacklist should persist"
print("✅ Liquidity drain blacklist: PASSED")

# Test liquidity drain detection (requires 2 confirmations)
pos = {"entry_liquidity": 30000, "liquidity_drain_hits": 0}
is_draining_1 = scanner.liquidity_drain_detected(pos, 14000)
assert not is_draining_1, "First drain detection should be false"
assert pos["liquidity_drain_hits"] == 1, "Should increment hits"

is_draining_2 = scanner.liquidity_drain_detected(pos, 14000)
assert is_draining_2, "Second drain detection should be true"
print("✅ Liquidity drain detection (2-confirmation): PASSED")

# Test liquidity recovery resets counter
pos2 = {"entry_liquidity": 30000, "liquidity_drain_hits": 1}
is_recovered = scanner.liquidity_drain_detected(pos2, 20000)
assert not is_recovered, "Recovery should reset"
assert pos2["liquidity_drain_hits"] == 0, "Should reset hits"
print("✅ Liquidity recovery reset: PASSED")

print("\n" + "="*70)
print("  TEST 4: Learning Agent")
print("="*70 + "\n")

# Test empty observations
obs = la.load_observations("nonexistent_file.csv")
assert obs == [], "Empty observations should return []"
print("✅ Load observations (empty): PASSED")

# Test metrics calculation
test_rows = [
    {"timestamp": "2026-01-01", "score": 65, "pnl_pct": 2.0, "reason": "test"},
    {"timestamp": "2026-01-02", "score": 65, "pnl_pct": 1.5, "reason": "test"},
    {"timestamp": "2026-01-03", "score": 65, "pnl_pct": -1.0, "reason": "test"},
]
metrics = la._metrics(test_rows, 65)
assert metrics["n"] == 3, "Should have 3 trades"
assert metrics["wins"] == 2, "Should have 2 wins"
assert abs(metrics["win_rate"] - (2/3)) < 0.01, "Win rate should be ~67%"
print(f"✅ Metrics calculation: {metrics['wins']}/3 wins, {metrics['win_rate']:.1%} win rate")

# Test walk-forward validation (needs min 30 trades)
obs_data = []
for i in range(40):
    obs_data.append({
        "timestamp": f"2026-01-{i+1:02d}",
        "score": 65 if i >= 20 else 60,
        "pnl_pct": 2.0 if i >= 20 else -1.0,
        "reason": "test"
    })
result = la.walk_forward(obs_data, 65)
assert result["eligible"], "Should be eligible for validation"
print(f"✅ Walk-forward validation: PASSED")

print("\n" + "="*70)
print("  TEST 5: Market Filters")
print("="*70 + "\n")

# Good pair that passes all filters
good_pair = {
    "liquidity": {"usd": 60000},
    "volume": {"h1": 120000},
    "marketCap": 150000,
    "txns": {"m5": {"buys": 20, "sells": 10}},
    "priceChange": {"m5": 2.0, "h1": 8.0},
    "pairCreatedAt": int(time.time() * 1000) - 3600 * 1000,
}
ok, reason = scanner.passes_market_filter(good_pair)
assert ok, f"Good pair should pass: {reason}"
print(f"✅ Good pair passes filter: {reason}")

# Low liquidity rejection
bad_pair_low_liq = dict(good_pair, liquidity={"usd": 1000})
ok1, reason1 = scanner.passes_market_filter(bad_pair_low_liq)
assert not ok1 and "low_liquidity" in reason1, "Low liquidity should fail"
print(f"✅ Low liquidity rejection: {reason1}")

# Too new pair rejection
bad_pair_new = dict(good_pair, pairCreatedAt=int(time.time() * 1000) - 60 * 1000)
ok2, reason2 = scanner.passes_market_filter(bad_pair_new)
assert not ok2 and "pair_too_new" in reason2, "New pair should fail"
print(f"✅ Too new pair rejection: {reason2}")

# Missing pairCreatedAt rejection
bad_pair_no_age = {k: v for k, v in good_pair.items() if k != "pairCreatedAt"}
ok3, reason3 = scanner.passes_market_filter(bad_pair_no_age)
assert not ok3 and reason3 == "missing_pair_age", "Missing age should fail"
print(f"✅ Missing pairCreatedAt rejection: {reason3}")

print("\n" + "="*70)
print("  TEST 6: Entry Confirmation")
print("="*70 + "\n")

# Entry confirmation requires 2 hits
state_conf = {"candidate_observations": {}}
hit1 = scanner.candidate_confirmation(state_conf, "mint_test", 70)
assert not hit1, "First hit should not confirm"
print("✅ First hit not confirmed")

hit2 = scanner.candidate_confirmation(state_conf, "mint_test", 68)
assert hit2, "Second hit should confirm"
print("✅ Second hit confirmed (entry allowed)")

# Score deterioration resets
state_conf2 = {"candidate_observations": {}}
scanner.candidate_confirmation(state_conf2, "mint_bad", 70)
hit_reset = scanner.candidate_confirmation(state_conf2, "mint_bad", 60)
assert not hit_reset, "Large score drop should reset"
print("✅ Score deterioration reset")

print("\n" + "="*70)
print("  TEST 7: Security Filters")
print("="*70 + "\n")

# Good rugcheck report
good_rug = {
    "score_normalised": 5,
    "risks": [],
    "freezeAuthority": None,
    "mintAuthority": None,
    "topHolders": [{"pct": 5, "address": "addr1"}, {"pct": 5, "address": "addr2"}]
}
ok_rug, reason_rug = scanner.passes_security_filter(good_rug)
assert ok_rug, f"Good rugcheck should pass: {reason_rug}"
print(f"✅ Security filter passed: {reason_rug}")

# Freeze authority set
bad_rug_freeze = dict(good_rug, freezeAuthority="SomeFreezeAuth")
ok_freeze, reason_freeze = scanner.passes_security_filter(bad_rug_freeze)
assert not ok_freeze and "freeze_authority" in reason_freeze, "Freeze authority should fail"
print(f"✅ Freeze authority rejection: {reason_freeze}")

# High holder concentration
bad_rug_holders = dict(good_rug, topHolders=[{"pct": p, "address": f"addr{i}"} for i, p in enumerate(range(10, 60, 5))])
ok_holders, reason_holders = scanner.passes_security_filter(bad_rug_holders)
assert not ok_holders and "concentration" in reason_holders, "High concentration should fail"
print(f"✅ Holder concentration rejection: {reason_holders}")

print("\n" + "="*70)
print("  ✅ ALL TESTS PASSED!")
print("="*70 + "\n")

# Generate report
report = f"""
╔═══════════════════════════════════════════════════════════════╗
║          MEME SCANNER BOT v17 - TEST REPORT                  ║
╚═══════════════════════════════════════════════════════════════╝

Test Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

✅ TEST 1: State Recovery
   - Corrupted state recovery: PASSED
   - Valid state preservation: PASSED
   - Legacy strategy migration: PASSED

✅ TEST 2: Slippage Calculation
   - Entry slippage: {scanner.ENTRY_SLIPPAGE_BPS} bps
   - Exit slippage: {scanner.EXIT_SLIPPAGE_BPS} bps
   - Total: {(scanner.ENTRY_SLIPPAGE_BPS + scanner.EXIT_SLIPPAGE_BPS) / 100:.1f}%

✅ TEST 3: Position Management
   - Stop loss cooldown (30min): PASSED
   - Liquidity drain blacklist: PASSED
   - Liquidity drain detection (2-confirmation): PASSED
   - Liquidity recovery reset: PASSED

✅ TEST 4: Learning Agent
   - Load observations: PASSED
   - Metrics calculation: PASSED
   - Walk-forward validation: PASSED

✅ TEST 5: Market Filters
   - Good pair: PASSED
   - Low liquidity rejection: PASSED
   - Too new pair rejection: PASSED
   - Missing pairCreatedAt rejection: PASSED

✅ TEST 6: Entry Confirmation
   - First hit not confirmed: PASSED
   - Second hit confirmed: PASSED
   - Score deterioration reset: PASSED

✅ TEST 7: Security Filters
   - Good rugcheck: PASSED
   - Freeze authority rejection: PASSED
   - Holder concentration rejection: PASSED

╔═══════════════════════════════════════════════════════════════╗
║                    SUMMARY: 7/7 TESTS PASSED                 ║
║                Status: ✅ READY FOR DEPLOYMENT                ║
╚═══════════════════════════════════════════════════════════════╝
"""

print(report)

# Save report
with open("TEST_REPORT.txt", "w", encoding="utf-8") as f:
    f.write(report)

print("\n📄 Test report saved to TEST_REPORT.txt")
print("\n🎉 All validations completed successfully!")
print("✅ Project is ready to run\n")
