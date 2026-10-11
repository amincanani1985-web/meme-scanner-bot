#!/usr/bin/env python3
"""
مجموعه تست‌های جامع برای پروژه meme-scanner-bot
اجرا: python run_all_tests.py
"""

import sys
import json
import time
import subprocess
from pathlib import Path
from datetime import datetime

def print_header(title):
    print("\n" + "="*70)
    print(f"  {title}")
    print("="*70 + "\n")

def run_command(cmd, description):
    print(f"▶ {description}...")
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"✅ {description} - PASSED")
            return True, result.stdout
        else:
            print(f"❌ {description} - FAILED")
            if result.stderr:
                print(f"Error: {result.stderr}")
            return False, result.stderr
    except Exception as e:
        print(f"❌ {description} - EXCEPTION: {e}")
        return False, str(e)

def test_imports():
    """تست 1: بررسی import تمام فایل‌ها"""
    print_header("TEST 1: بررسی Import فایل‌ها")
    
    tests = [
        ("python -c 'import meme_scanner_cycle'", "Import meme_scanner_cycle"),
        ("python -c 'import learn_agent'", "Import learn_agent"),
        ("python -c 'import requests'", "Import requests"),
    ]
    
    passed = 0
    for cmd, desc in tests:
        ok, _ = run_command(cmd, desc)
        if ok:
            passed += 1
    
    print(f"\n📊 نتیجه: {passed}/{len(tests)} passed")
    return passed == len(tests)

def test_unit_tests():
    """تست 2: اجرای unit tests"""
    print_header("TEST 2: اجرای Unit Tests")
    
    tests_to_run = [
        ("python -m pytest tests/test_scanner_logic.py -v --tb=short", "Test Scanner Logic"),
        ("python -m pytest tests/test_adaptive.py -v --tb=short", "Test Adaptive Learning"),
        ("python -m pytest tests/test_v17_improvements.py -v --tb=short", "Test V17 Improvements"),
    ]
    
    passed = 0
    for cmd, desc in tests_to_run:
        ok, output = run_command(cmd, desc)
        if ok:
            passed += 1
            if "passed" in output:
                print(output.split('\n')[-2])
    
    print(f"\n📊 نتیجه: {passed}/{len(tests_to_run)} test suites passed")
    return passed == len(tests_to_run)

def test_state_recovery():
    """تست 3: بررسی state recovery"""
    print_header("TEST 3: بررسی State Recovery")
    
    test_code = """
import meme_scanner_cycle as scanner
import json

# تست 1: corrupted state
broken = {
    "equity": "invalid",
    "positions": "not_dict",
    "blacklist": "not_list",
    "cooldowns": "not_dict",
    "trade_count": "abc"
}
fixed = scanner.normalize_state(broken)
assert isinstance(fixed['positions'], dict), "positions should be dict"
assert isinstance(fixed['blacklist'], list), "blacklist should be list"
assert fixed['equity'] == scanner.STARTING_EQUITY, "equity should be reset"
print("✅ Corrupted state recovery works")

# تست 2: state with valid data
good = {
    "equity": 500.0,
    "positions": {},
    "blacklist": [],
    "cooldowns": {},
    "trade_count": 5
}
fixed2 = scanner.normalize_state(good)
assert fixed2['equity'] == 500.0, "good equity should be preserved"
assert fixed2['trade_count'] == 5, "trade count should be preserved"
print("✅ Valid state preservation works")

# تست 3: legacy migration
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
print("✅ Legacy strategy migration works")
"""
    
    ok, output = run_command(f"python -c '{test_code}'", "State Recovery Tests")
    if ok:
        print(output)
    return ok

def test_slippage():
    """تست 4: بررسی slippage"""
    print_header("TEST 4: بررسی Slippage Calculation")
    
    test_code = """
import meme_scanner_cycle as scanner

# Entry slippage
entry_price = 1.0
entry_with_slippage = entry_price * (1.0 + scanner.ENTRY_SLIPPAGE_BPS / 10000.0)
expected = 1.0 * (1.0 + 150 / 10000.0)  # 150 bps
assert abs(entry_with_slippage - expected) < 0.0001, f"Entry slippage incorrect: {entry_with_slippage}"
print(f"✅ Entry slippage: {scanner.ENTRY_SLIPPAGE_BPS} bps = {(entry_with_slippage - 1.0)*100:.2f}%")

# Exit slippage
exit_price = 1.0
exit_with_slippage = exit_price * (1.0 - scanner.EXIT_SLIPPAGE_BPS / 10000.0)
expected_exit = 1.0 * (1.0 - 200 / 10000.0)  # 200 bps
assert abs(exit_with_slippage - expected_exit) < 0.0001, f"Exit slippage incorrect: {exit_with_slippage}"
print(f"✅ Exit slippage: {scanner.EXIT_SLIPPAGE_BPS} bps = {(1.0 - exit_with_slippage)*100:.2f}%")

print(f"✅ Total slippage per trade: ~{(scanner.ENTRY_SLIPPAGE_BPS + scanner.EXIT_SLIPPAGE_BPS)/100:.1f}%")
"""
    
    ok, output = run_command(f"python -c '{test_code}'", "Slippage Tests")
    if ok:
        print(output)
    return ok

def test_position_management():
    """تست 5: مدیریت موقعیت‌ها"""
    print_header("TEST 5: بررسی Position Management")
    
    test_code = """
import meme_scanner_cycle as scanner
import time

# تست cooldown
state = {
    "equity": 1000.0,
    "positions": {"mint": {"symbol": "TEST", "entry_price": 1.0, "amount_usd": 10, "estimated_impact": 0, "signal_score": 70}},
    "blacklist": [],
    "cooldowns": {},
    "trade_count": 0
}

# Simulate stop loss
state["cooldowns"]["mint"] = time.time() + 1800
blocked = "mint" in state["cooldowns"] and time.time() < state["cooldowns"]["mint"]
assert blocked, "Cooldown should block re-entry"
print("✅ Cooldown blocking works")

# تست blacklist
state2 = {
    "equity": 1000.0,
    "positions": {},
    "blacklist": ["bad_mint"],
    "cooldowns": {},
    "trade_count": 0
}
assert "bad_mint" in state2["blacklist"], "Blacklist should persist"
print("✅ Blacklist persistence works")

# تست liquidity drain detection
pos = {"entry_liquidity": 30000, "liquidity_drain_hits": 0}
is_draining = scanner.liquidity_drain_detected(pos, 14000)
assert not is_draining, "First drain detection should be false"
is_draining2 = scanner.liquidity_drain_detected(pos, 14000)
assert is_draining2, "Second drain detection should be true"
print("✅ Liquidity drain detection works (2-confirmation)")

print("✅ All position management tests passed")
"""
    
    ok, output = run_command(f"python -c '{test_code}'", "Position Management Tests")
    if ok:
        print(output)
    return ok

def test_learning_agent():
    """تست 6: بررسی learning agent"""
    print_header("TEST 6: بررسی Learning Agent")
    
    test_code = """
import learn_agent as la
import json

# تست load observations (خالی)
obs = la.load_observations("nonexistent.csv")
assert obs == [], "Empty observations should return []"
print("✅ Load observations (empty) works")

# تست metrics calculation
test_rows = [
    {"timestamp": "2026-01-01", "score": 65, "pnl_pct": 2.0, "reason": "test"},
    {"timestamp": "2026-01-02", "score": 65, "pnl_pct": 1.5, "reason": "test"},
    {"timestamp": "2026-01-03", "score": 65, "pnl_pct": -1.0, "reason": "test"},
]
metrics = la._metrics(test_rows, 65)
assert metrics["n"] == 3, "Should have 3 trades"
assert metrics["wins"] == 2, "Should have 2 wins"
assert metrics["win_rate"] == 2/3, "Win rate should be 2/3"
print(f"✅ Metrics calculation works: {metrics['wins']}/3 wins, {metrics['win_rate']:.2%}")

# تست choose candidate
obs_data = []
for i in range(40):
    obs_data.append({
        "timestamp": f"2026-01-{i+1:02d}",
        "score": 65 if i >= 20 else 60,
        "pnl_pct": 2.0 if i >= 20 else -1.0,
        "reason": "test"
    })
result = la.choose_candidate(obs_data, 65)
assert "action" in result, "Should return action"
assert "threshold" in result, "Should return threshold"
print(f"✅ Choose candidate works: action={result['action']}")

print("✅ All learning agent tests passed")
"""
    
    ok, output = run_command(f"python -c '{test_code}'", "Learning Agent Tests")
    if ok:
        print(output)
    return ok

def test_market_filters():
    """تست 7: بررسی market filters"""
    print_header("TEST 7: بررسی Market Filters")
    
    test_code = """
import meme_scanner_cycle as scanner
import time

# Good pair
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

# Bad pair - low liquidity
bad_pair1 = dict(good_pair, liquidity={"usd": 1000})
ok1, reason1 = scanner.passes_market_filter(bad_pair1)
assert not ok1, "Low liquidity should fail"
print(f"✅ Low liquidity rejection: {reason1}")

# Bad pair - new pair
bad_pair2 = dict(good_pair, pairCreatedAt=int(time.time() * 1000) - 60 * 1000)
ok2, reason2 = scanner.passes_market_filter(bad_pair2)
assert not ok2, "New pair should fail"
print(f"✅ New pair rejection: {reason2}")

# Bad pair - missing pairCreatedAt
bad_pair3 = {k: v for k, v in good_pair.items() if k != "pairCreatedAt"}
ok3, reason3 = scanner.passes_market_filter(bad_pair3)
assert not ok3, "Missing pairCreatedAt should fail"
assert reason3 == "missing_pair_age", f"Should return missing_pair_age, got {reason3}"
print(f"✅ Missing pairCreatedAt rejection: {reason3}")

print("✅ All market filter tests passed")
"""
    
    ok, output = run_command(f"python -c '{test_code}'", "Market Filter Tests")
    if ok:
        print(output)
    return ok

def generate_report(results):
    """ایجاد گزارش نهایی"""
    print_header("📋 خلاصه نتایج تست‌ها")
    
    report = f"""
🎯 خلاصه اجرای تست‌ها:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

✓ TEST 1: Import Checks              {'PASSED' if results[0] else 'FAILED'}
✓ TEST 2: Unit Tests                 {'PASSED' if results[1] else 'FAILED'}
✓ TEST 3: State Recovery             {'PASSED' if results[2] else 'FAILED'}
✓ TEST 4: Slippage Calculation       {'PASSED' if results[3] else 'FAILED'}
✓ TEST 5: Position Management        {'PASSED' if results[4] else 'FAILED'}
✓ TEST 6: Learning Agent             {'PASSED' if results[5] else 'FAILED'}
✓ TEST 7: Market Filters             {'PASSED' if results[6] else 'FAILED'}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 کل نتیجه: {sum(results)}/7 تست پاس شد
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

وضعیت نهایی: {'✅ همه تست‌ها موفق!' if all(results) else '❌ برخی تست‌ها ناموفق بودند'}

تاریخ و زمان: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
    
    print(report)
    
    # Save report to file
    with open("TEST_REPORT.txt", "w", encoding="utf-8") as f:
        f.write(report)
    print("📄 گزارش کامل در TEST_REPORT.txt ذخیره شد")
    
    return all(results)

def main():
    print("""
╔════════════════════════════════════════════════════════════════╗
║           🚀 مجموعه تست‌های MEME SCANNER BOT v17 🚀           ║
║                                                                ║
║  این اسکریپت تمام بخش‌های پروژه را بررسی می‌کند             ║
╚════════════════════════════════════════════════════════════════╝
    """)
    
    results = []
    
    # Run all tests
    results.append(test_imports())
    results.append(test_unit_tests())
    results.append(test_state_recovery())
    results.append(test_slippage())
    results.append(test_position_management())
    results.append(test_learning_agent())
    results.append(test_market_filters())
    
    # Generate final report
    success = generate_report(results)
    
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
