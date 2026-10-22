"""Deterministic paper-only scenario lab for the scanner's current exit rules.

This does not mutate either account, call external APIs, or invent live trades.
It replays synthetic price/liquidity paths through the same risk constants and
liquidity-drain helper used by meme_scanner_cycle.py. Passing scenarios validate
control-flow only, not strategy profitability or real execution quality.
"""
import json
import math
import time

import meme_scanner_cycle as scanner


def simulate_exit_path(observations, entry_price=1.0, entry_liquidity=100_000.0,
                       amount_usd=5.0, start_time=1_000.0):
    """Simulate a paper position from a fixed entry and timestamped quote path.

    Each observation is {"seconds": float, "price": float|None,
    "liquidity": float|None}. Missing/invalid quotes never create a fabricated exit.
    PnL includes modeled entry/exit slippage and the scanner's round-trip fee reserve.
    """
    values = (entry_price, entry_liquidity, amount_usd, start_time)
    if not all(math.isfinite(float(value)) for value in values):
        raise ValueError("scenario inputs must be finite")
    if entry_price <= 0 or entry_liquidity <= 0 or amount_usd <= 0:
        raise ValueError("entry price, liquidity, and amount must be positive")

    effective_entry = entry_price * (1.0 + scanner.ENTRY_SLIPPAGE_BPS / 10_000.0)
    peak_price = entry_price
    trailing_armed = False
    position = {"entry_liquidity": entry_liquidity, "liquidity_drain_hits": 0}
    last_valid_price = None
    last_seconds = 0.0

    for observation in observations:
        seconds = float(observation.get("seconds", 0.0))
        if not math.isfinite(seconds) or seconds < last_seconds:
            raise ValueError("observations must have finite, non-decreasing seconds")
        last_seconds = seconds
        raw_price = observation.get("price")
        raw_liquidity = observation.get("liquidity")
        try:
            price = float(raw_price) if raw_price is not None else 0.0
            liquidity = float(raw_liquidity) if raw_liquidity is not None else 0.0
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(price) or price <= 0:
            continue
        if not math.isfinite(liquidity) or liquidity < 0:
            liquidity = 0.0
        last_valid_price = price
        peak_price = max(peak_price, price)
        pnl = (price - effective_entry) / effective_entry
        reason = None
        if pnl >= 0.08:
            trailing_armed = True
        if trailing_armed and peak_price > 0:
            if (peak_price - price) / peak_price >= scanner.TRAILING_STOP_PCT:
                reason = "trailing_stop"
        if reason is None and pnl <= -scanner.HARD_STOP_LOSS_PCT:
            reason = "hard_stop_loss"
        if reason is None and liquidity > 0 and scanner.liquidity_drain_detected(position, liquidity):
            reason = "liquidity_drained"
        if reason is None and seconds >= scanner.MAX_HOLD_SECONDS:
            reason = "max_hold_timeout"
        if reason:
            exit_price = price * (1.0 - scanner.EXIT_SLIPPAGE_BPS / 10_000.0)
            net_pnl_pct = ((exit_price - effective_entry) / effective_entry) * 100.0 - scanner.PAPER_ROUND_TRIP_FEE_PCT
            return {
                "status": "closed",
                "reason": reason,
                "entry_price_effective": round(effective_entry, 10),
                "exit_price_effective": round(exit_price, 10),
                "net_pnl_pct": round(net_pnl_pct, 4),
                "net_pnl_usd": round(amount_usd * net_pnl_pct / 100.0, 6),
                "observations": len(observations),
                "held_seconds": seconds,
                "quote_missing_unresolved": False,
            }

    return {
        "status": "unresolved" if last_valid_price is None else "open",
        "reason": None,
        "entry_price_effective": round(effective_entry, 10),
        "exit_price_effective": None,
        "net_pnl_pct": None,
        "net_pnl_usd": None,
        "observations": len(observations),
        "held_seconds": last_seconds,
        "quote_missing_unresolved": last_valid_price is None,
    }


def scenario_definitions():
    """Adverse, neutral, and favorable synthetic paths with expected control outcomes."""
    return [
        ("pump_then_reversal", [
            {"seconds": 60, "price": 1.10, "liquidity": 100_000},
            {"seconds": 120, "price": 1.04, "liquidity": 98_000},
        ], "closed", "trailing_stop"),
        ("hard_stop", [
            {"seconds": 60, "price": 0.88, "liquidity": 95_000},
        ], "closed", "hard_stop_loss"),
        ("liquidity_drain_confirmed_twice", [
            {"seconds": 60, "price": 0.99, "liquidity": 20_000},
            {"seconds": 120, "price": 0.98, "liquidity": 18_000},
        ], "closed", "liquidity_drained"),
        ("max_hold_timeout", [
            {"seconds": scanner.MAX_HOLD_SECONDS, "price": 1.03, "liquidity": 100_000},
        ], "closed", "max_hold_timeout"),
        ("missing_quote", [
            {"seconds": 60, "price": None, "liquidity": None},
            {"seconds": 660, "price": None, "liquidity": None},
        ], "unresolved", None),
        ("small_gain_no_exit_yet", [
            {"seconds": 60, "price": 1.03, "liquidity": 100_000},
        ], "open", None),
        ("strong_rally_then_trailing_exit", [
            {"seconds": 30, "price": 1.10, "liquidity": 100_000},
            {"seconds": 60, "price": 1.20, "liquidity": 100_000},
            {"seconds": 90, "price": 1.14, "liquidity": 100_000},
        ], "closed", "trailing_stop"),
        ("trailing_pullback_below_threshold", [
            {"seconds": 30, "price": 1.10, "liquidity": 100_000},
            {"seconds": 60, "price": 1.0571, "liquidity": 100_000},
        ], "open", None),
        ("gap_through_hard_stop", [
            {"seconds": 60, "price": 0.70, "liquidity": 80_000},
            {"seconds": 120, "price": 1.20, "liquidity": 100_000},
        ], "closed", "hard_stop_loss"),
        ("critical_liquidity_drop_immediate", [
            {"seconds": 60, "price": 0.99, "liquidity": 70_000},
        ], "closed", "liquidity_drained"),
        ("catastrophic_liquidity_drop_then_recovery", [
            {"seconds": 60, "price": 1.00, "liquidity": 40_000},
            {"seconds": 120, "price": 1.00, "liquidity": 100_000},
            {"seconds": 180, "price": 1.00, "liquidity": 40_000},
            {"seconds": 240, "price": 1.00, "liquidity": 100_000},
        ], "open", None),
        ("missing_then_valid_quote", [
            {"seconds": 60, "price": None, "liquidity": None},
            {"seconds": 120, "price": 1.02, "liquidity": 100_000},
        ], "open", None),
        ("max_hold_quote_arrives_late", [
            {"seconds": scanner.MAX_HOLD_SECONDS + 1, "price": 1.00, "liquidity": 100_000},
        ], "closed", "max_hold_timeout"),
        ("loss_then_rebound_cannot_undo_exit", [
            {"seconds": 60, "price": 0.88, "liquidity": 100_000},
            {"seconds": 120, "price": 1.30, "liquidity": 100_000},
        ], "closed", "hard_stop_loss"),
        ("nan_quote_is_not_an_exit_price", [
            {"seconds": 60, "price": float("nan"), "liquidity": 100_000},
        ], "unresolved", None),
    ]



def run_entry_gate_scenarios():
    """Exercise production market-entry filters with deterministic synthetic pairs."""
    now_ms = int(time.time() * 1000)
    baseline = {
        "pairCreatedAt": now_ms - 3_600_000,
        "liquidity": {"usd": 50_000},
        "volume": {"h1": 50_000},
        "marketCap": 200_000,
        "txns": {"m5": {"buys": 30, "sells": 10}},
        "priceChange": {"m5": 2.0, "h1": 8.0},
    }
    cases = [
        ("healthy_baseline", {}, True, "ok"),
        ("pair_too_new", {"pairCreatedAt": now_ms - 60_000}, False, "pair_too_new:"),
        ("liquidity_below_floor", {"liquidity": {"usd": scanner.MIN_LIQUIDITY_USD - 1}}, False, "low_liquidity:"),
        ("volume_below_floor", {"volume": {"h1": scanner.MIN_VOLUME_H1_USD - 1}}, False, "low_volume_h1:"),
        ("liquidity_to_mcap_too_low", {"marketCap": 2_000_000}, False, "liq_mcap_ratio:"),
        ("weak_buy_pressure", {"txns": {"m5": {"buys": 8, "sells": 10}}}, False, "weak_buy_pressure:"),
        ("overextended_five_minute_move", {"priceChange": {"m5": 16.0, "h1": 8.0}}, False, "overextended_momentum_m5:"),
        ("weak_hourly_momentum", {"priceChange": {"m5": 2.0, "h1": -1.0}}, False, "negative_momentum_h1:"),
        ("missing_pair_age", {"pairCreatedAt": None}, False, "invalid_pair_age"),
    ]
    results = {}
    for name, override, expected_pass, expected_reason in cases:
        pair = dict(baseline)
        pair.update(override)
        passed, reason = scanner.passes_market_filter(pair)
        expected = reason == expected_reason if expected_reason in ("ok", "invalid_pair_age") else reason.startswith(expected_reason)
        if passed != expected_pass or not expected:
            raise AssertionError(
                f"entry {name}: expected pass={expected_pass}, reason={expected_reason}; "
                f"got pass={passed}, reason={reason}"
            )
        results[name] = {"passed": passed, "reason": reason}
    return results


def run_scenarios():
    entry_gate_results = run_entry_gate_scenarios()
    definitions = scenario_definitions()
    results = {}
    expectations = {}
    for name, path, expected_status, expected_reason in definitions:
        results[name] = simulate_exit_path(path)
        expectations[name] = (expected_status, expected_reason)

    # Fail the workflow if an exit-control regression changes a required outcome.
    for name, result in results.items():
        expected_status, expected_reason = expectations[name]
        if result["status"] != expected_status or result["reason"] != expected_reason:
            raise AssertionError(
                f"{name}: expected ({expected_status}, {expected_reason}), "
                f"got ({result['status']}, {result['reason']})"
            )
    closed = [r for r in results.values() if r["status"] == "closed"]
    return {
        "mode": "deterministic_simulation_not_live_or_historical_performance",
        "scenario_count": len(results),
        "scenarios": results,
        "entry_gate_scenario_count": len(entry_gate_results),
        "entry_gate_scenarios": entry_gate_results,
        "closed_scenarios": len(closed),
        "open_scenarios": sum(r["status"] == "open" for r in results.values()),
        "unresolved_scenarios": sum(r["status"] == "unresolved" for r in results.values()),
        "combined_net_pnl_usd_for_closed_scenarios": round(sum(r["net_pnl_usd"] for r in closed), 6),
        "note": "Synthetic paths test exit control-flow only; they are not evidence of strategy profitability or live execution quality.",
    }


if __name__ == "__main__":
    print(json.dumps(run_scenarios(), separators=(",", ":"), sort_keys=True))
