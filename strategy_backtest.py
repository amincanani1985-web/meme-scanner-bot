#!/usr/bin/env python3
"""Offline entry-strategy backtest and chronological walk-forward validator."""
import argparse
import csv
import json
import math
from pathlib import Path
from statistics import median

THRESHOLDS = (80.0, 82.5, 85.0, 87.5, 90.0)
MIN_HISTORY_ROWS = 30
MIN_VALIDATION_ROWS = 8
MIN_THRESHOLD_VALIDATION_SAMPLES = 8
ROUND_TRIP_COST_PCT = 2.0
OUTLIER_ABS_PNL_PCT = 100.0


def load(path):
    """Load only complete, finite observations with plausible strategy scores."""
    p = Path(path)
    if not p.exists():
        return []
    out = []
    with p.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                timestamp = str(row.get("timestamp", "")).strip()
                score = float(row["score"])
                pnl_pct = float(row["pnl_pct"])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if not timestamp or not math.isfinite(score) or not math.isfinite(pnl_pct):
                continue
            if not 0.0 <= score <= 100.0:
                continue
            out.append({"timestamp": timestamp, "score": score, "pnl_pct": pnl_pct})
    return sorted(out, key=lambda item: item["timestamp"])


def wilson_lower_bound(wins, n, z=1.96):
    """Two-sided 95% Wilson lower confidence bound for a binomial win rate."""
    if n <= 0:
        return 0.0
    p = min(1.0, max(0.0, float(wins) / n))
    z2 = z * z
    denominator = 1.0 + z2 / n
    center = p + z2 / (2.0 * n)
    margin = z * math.sqrt((p * (1.0 - p) / n) + z2 / (4.0 * n * n))
    return max(0.0, (center - margin) / denominator)


def metrics(rows, threshold, round_trip_cost_pct=None):
    if round_trip_cost_pct is None:
        round_trip_cost_pct = ROUND_TRIP_COST_PCT
    selected = [row for row in rows if row["score"] >= threshold]
    values = [float(row["pnl_pct"]) for row in selected]
    net_values = [value - round_trip_cost_pct for value in values]
    outlier_mask = [abs(value) > OUTLIER_ABS_PNL_PCT for value in values]
    ordinary_net = [value for value, outlier in zip(net_values, outlier_mask) if not outlier]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    net_wins = sum(value > 0 for value in net_values)
    net_profit_wins = sum(value for value in ordinary_net if value > 0)
    net_profit_losses = abs(sum(value for value in ordinary_net if value < 0))
    gross_wins = sum(wins)
    gross_losses = abs(sum(losses))
    count = len(selected)
    net_profit_factor = (net_profit_wins / net_profit_losses if net_profit_losses
                         else (999.0 if net_profit_wins else 0.0))
    result = {
        "n": count,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / count if count else 0.0,
        "avg_pnl": sum(values) / count if count else 0.0,
        "profit_factor": gross_wins / gross_losses if gross_losses else (999.0 if gross_wins else 0.0),
        "round_trip_cost_pct": round_trip_cost_pct,
        "net_wins": net_wins,
        "net_win_rate": net_wins / count if count else 0.0,
        "net_avg_pnl": sum(net_values) / count if count else 0.0,
        "net_median_pnl": median(net_values) if net_values else 0.0,
        "net_robust_mean_pnl": sum(ordinary_net) / len(ordinary_net) if ordinary_net else 0.0,
        "net_profit_factor": net_profit_factor,
        "net_win_rate_wilson_lower": wilson_lower_bound(net_wins, count),
        "outlier_count": sum(outlier_mask),
        "sample_eligible": count >= MIN_THRESHOLD_VALIDATION_SAMPLES,
    }
    result["eligible"] = (
        result["sample_eligible"]
        and result["outlier_count"] == 0
        and result["net_median_pnl"] > 0
        and result["net_robust_mean_pnl"] > 0
        and result["net_win_rate"] >= 0.5
        and result["net_win_rate_wilson_lower"] >= 0.5
        and result["net_profit_factor"] > 1.2
    )
    return result

def run(rows, round_trip_cost_pct=ROUND_TRIP_COST_PCT):
    rows = sorted(rows, key=lambda item: item["timestamp"])
    if len(rows) < MIN_HISTORY_ROWS:
        return {
            "status": "insufficient_history",
            "rows": len(rows),
            "required_rows": MIN_HISTORY_ROWS,
            "validation_rows": 0,
            "eligible_thresholds": [],
            "thresholds": {},
            "assumed_round_trip_cost_pct": round_trip_cost_pct,
            "cost_sensitivity": [],
        }

    split = max(1, int(len(rows) * 0.7))
    train, validation = rows[:split], rows[split:]
    if len(validation) < MIN_VALIDATION_ROWS:
        return {
            "status": "insufficient_validation_history",
            "rows": len(rows),
            "train_rows": len(train),
            "validation_rows": len(validation),
            "required_validation_rows": MIN_VALIDATION_ROWS,
            "eligible_thresholds": [],
            "thresholds": {},
            "assumed_round_trip_cost_pct": round_trip_cost_pct,
            "cost_sensitivity": [],
        }

    result = {}
    eligible = []
    for threshold in THRESHOLDS:
        train_metrics = metrics(train, threshold, round_trip_cost_pct)
        validation_metrics = metrics(validation, threshold, round_trip_cost_pct)
        result[str(threshold)] = {
            "train": train_metrics,
            "validation": validation_metrics,
            "eligible": validation_metrics["eligible"],
        }
        if validation_metrics["eligible"]:
            eligible.append(threshold)

    return {
        "status": ("ok" if eligible else
                   "no_validated_threshold" if any(v["validation"]["sample_eligible"] for v in result.values())
                   else "insufficient_threshold_samples"),
        "rows": len(rows),
        "train_rows": len(train),
        "validation_rows": len(validation),
        "required_validation_samples_per_threshold": MIN_THRESHOLD_VALIDATION_SAMPLES,
        "assumed_round_trip_cost_pct": round_trip_cost_pct,
        "eligible_thresholds": eligible,
        "thresholds": result,
        "cost_sensitivity": [
            {"assumed_round_trip_cost_pct": cost, **metrics(validation, 0.0, cost)}
            for cost in (1.0, 2.0, 3.0, 5.0)
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--observations", default="strategy_observations.csv")
    parser.add_argument("--round-trip-cost-pct", type=float, default=ROUND_TRIP_COST_PCT)
    args = parser.parse_args()
    report = run(load(args.observations), round_trip_cost_pct=args.round_trip_cost_pct)
    print(json.dumps(report, indent=2))
