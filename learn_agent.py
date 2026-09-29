#!/usr/bin/env python3
"""Constrained learning agent for the paper-trading scanner.

Analyzes closed trades, proposes bounded strategy adjustments, and emits a
reviewable recommendation. It never executes trades or changes source code.
"""
import json
import math
from pathlib import Path

MIN_TRADES = 10
MAX_THRESHOLD_DELTA = 5.0
MIN_THRESHOLD = 55.0
MAX_THRESHOLD = 70.0


def load_trades(path="trades_log.csv"):
    import csv
    p = Path(path)
    if not p.exists():
        return []
    with p.open(newline="") as f:
        return list(csv.DictReader(f))


def analyze_trades(trades):
    pnls = []
    reasons = {}
    for row in trades:
        try:
            pnl = float(row.get("pnl_pct", 0.0))
        except (TypeError, ValueError):
            continue
        pnls.append(pnl)
        reason = row.get("reason") or "unknown"
        reasons[reason] = reasons.get(reason, 0) + 1
    if not pnls:
        return {"trades": 0, "wins": 0, "win_rate": 0.0, "avg_pnl_pct": 0.0, "exit_reasons": {}}
    wins = sum(1 for x in pnls if x > 0)
    return {
        "trades": len(pnls),
        "wins": wins,
        "win_rate": wins / len(pnls),
        "avg_pnl_pct": sum(pnls) / len(pnls),
        "exit_reasons": reasons,
    }


def propose(stats, current_threshold=60.0):
    """Return a bounded recommendation; no code or risk limit is modified."""
    if stats["trades"] < MIN_TRADES:
        return {"action": "hold", "threshold": current_threshold, "reason": "insufficient_trade_history"}
    threshold = float(current_threshold)
    if stats["win_rate"] < 0.40 or stats["avg_pnl_pct"] < -0.01:
        threshold += MAX_THRESHOLD_DELTA
        reason = "raise_entry_threshold_after_weak_results"
    elif stats["win_rate"] >= 0.60 and stats["avg_pnl_pct"] > 0:
        threshold -= min(MAX_THRESHOLD_DELTA, 2.0)
        reason = "small_threshold_relaxation_after_consistent_results"
    else:
        reason = "hold_threshold"
    threshold = max(MIN_THRESHOLD, min(MAX_THRESHOLD, threshold))
    return {"action": "recommend", "threshold": round(threshold, 2), "reason": reason}


def run(trades_path="trades_log.csv", output_path="learn_agent_recommendation.json", current_threshold=60.0):
    stats = analyze_trades(load_trades(trades_path))
    recommendation = propose(stats, current_threshold)
    result = {"stats": stats, "recommendation": recommendation}
    Path(output_path).write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2))
