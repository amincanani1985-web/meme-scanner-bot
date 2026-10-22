"""Deterministic paper-only scenario lab for the scanner's current exit rules.

This does not mutate either account, call external APIs, or invent live trades.
It replays explicitly defined price/liquidity paths through the same risk constants
and liquidity-drain helper used by meme_scanner_cycle.py.
"""
import json
import math

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
    last_reason = None
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
            # Same principle as production: a missing quote is not an executable price.
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
            last_reason = reason
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
        "status": "unresolved" if last_valid_price is None or last_reason is None else "open",
        "reason": last_reason,
        "entry_price_effective": round(effective_entry, 10),
        "exit_price_effective": None,
        "net_pnl_pct": None,
        "net_pnl_usd": None,
        "observations": len(observations),
        "held_seconds": last_seconds,
        "quote_missing_unresolved": last_valid_price is None,
    }


def run_scenarios():
    """Run a compact, deterministic set of adverse and favorable paper scenarios."""
    definitions = [
        ("pump_then_reversal", [
            {"seconds": 60, "price": 1.10, "liquidity": 100_000},
            {"seconds": 120, "price": 1.04, "liquidity": 98_000},
        ]),
        ("hard_stop", [
            {"seconds": 60, "price": 0.88, "liquidity": 95_000},
        ]),
        ("liquidity_drain_confirmed_twice", [
            {"seconds": 60, "price": 0.99, "liquidity": 20_000},
            {"seconds": 120, "price": 0.98, "liquidity": 18_000},
        ]),
        ("max_hold_timeout", [
            {"seconds": scanner.MAX_HOLD_SECONDS, "price": 1.03, "liquidity": 100_000},
        ]),
        ("missing_quote", [
            {"seconds": 60, "price": None, "liquidity": None},
            {"seconds": 660, "price": None, "liquidity": None},
        ]),
        ("small_gain_no_exit_yet", [
            {"seconds": 60, "price": 1.03, "liquidity": 100_000},
        ]),
    ]
    results = {}
    for name, path in definitions:
        results[name] = simulate_exit_path(path)
    closed = [r for r in results.values() if r["status"] == "closed"]
    total_net = round(sum(r["net_pnl_usd"] for r in closed), 6)
    return {
        "mode": "deterministic_simulation_not_live_or_historical_performance",
        "scenarios": results,
        "closed_scenarios": len(closed),
        "unresolved_scenarios": sum(r["status"] == "unresolved" for r in results.values()),
        "combined_net_pnl_usd_for_closed_scenarios": total_net,
        "note": "Synthetic paths test code behavior only; they are not evidence of strategy profitability.",
    }


if __name__ == "__main__":
    print(json.dumps(run_scenarios(), separators=(",", ":"), sort_keys=True))
