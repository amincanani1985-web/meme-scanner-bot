#!/usr/bin/env python3
"""Conservative, research-only evaluation of shadow paper-trade outcomes.

This module never mutates the live strategy threshold. It evaluates the later
chronological holdout, reports score-bucket results, and refuses recommendations
when samples are sparse, outcomes are dominated by extreme moves, or robust
profitability gates are not met.
"""
import argparse
import csv
import json
import math
from pathlib import Path
from statistics import median

SCORE_BUCKETS = ((0.0, 20.0), (20.0, 40.0), (40.0, 60.0),
                 (60.0, 70.0), (70.0, 80.0), (80.0, 90.0), (90.0, 101.0))
MIN_HISTORY_ROWS = 30
MIN_VALIDATION_SAMPLES = 8
OUTLIER_ABS_PNL_PCT = 100.0


def load_observations(path="shadow_observations.csv"):
    """Load complete, finite score/PnL rows in chronological order."""
    source = Path(path)
    if not source.exists():
        return []
    rows = []
    with source.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                timestamp = str(row.get("timestamp", "")).strip()
                score = float(row["score"])
                pnl = float(row["pnl_pct"])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if not timestamp or not math.isfinite(score) or not math.isfinite(pnl):
                continue
            if not 0.0 <= score <= 100.0:
                continue
            try:
                features = json.loads(row.get("features_json") or "{}")
                if not isinstance(features, dict):
                    features = {}
            except (TypeError, ValueError):
                features = {}
            rows.append({"timestamp": timestamp, "score": score,
                         "pnl_pct": pnl, "reason": row.get("reason", ""),
                         "features": features})
    return sorted(rows, key=lambda row: row["timestamp"])


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


def feature_associations(rows):
    """Describe Pearson association between recorded entry features and PnL; not causal."""
    keys = sorted({key for row in rows for key in (row.get("features") or {})})
    result = {}
    for key in keys:
        pairs = []
        for row in rows:
            try:
                x = float((row.get("features") or {}).get(key))
                y = float(row.get("pnl_pct"))
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(x) and math.isfinite(y):
                pairs.append((x, y))
        n = len(pairs)
        if n < 2:
            continue
        xs = [pair[0] for pair in pairs]
        ys = [pair[1] for pair in pairs]
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        numerator = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
        denominator = math.sqrt(
            sum((x - mean_x) ** 2 for x in xs) *
            sum((y - mean_y) ** 2 for y in ys)
        )
        result[key] = {
            "n": n,
            "correlation": numerator / denominator if denominator else 0.0,
        }
    return result


def metrics(rows, round_trip_cost_pct=2.0):
    """Return gross and cost-adjusted metrics; extreme outliers cannot validate a bucket."""
    values = [float(row["pnl_pct"]) for row in rows
              if math.isfinite(float(row["pnl_pct"]))]
    if not values:
        return {
            "n": 0, "wins": 0, "win_rate": 0.0, "mean_pnl": 0.0,
            "median_pnl": 0.0, "robust_mean_pnl": 0.0,
            "profit_factor": 0.0, "outlier_count": 0,
            "net_wins": 0, "net_win_rate": 0.0, "net_mean_pnl": 0.0,
            "net_median_pnl": 0.0, "net_robust_mean_pnl": 0.0,
            "net_profit_factor": 0.0, "net_win_rate_wilson_lower": 0.0,
        }
    net_values = [value - round_trip_cost_pct for value in values]
    outlier_mask = [abs(value) > OUTLIER_ABS_PNL_PCT for value in values]
    ordinary = [value for value, outlier in zip(values, outlier_mask) if not outlier]
    net_ordinary = [value - round_trip_cost_pct for value, outlier in zip(values, outlier_mask) if not outlier]
    gross_wins = sum(value for value in ordinary if value > 0)
    gross_losses = abs(sum(value for value in ordinary if value < 0))
    factor = gross_wins / gross_losses if gross_losses else (999.0 if gross_wins else 0.0)
    net_wins = sum(value > 0 for value in net_values)
    net_gross_wins = sum(value for value in net_ordinary if value > 0)
    net_gross_losses = abs(sum(value for value in net_ordinary if value < 0))
    net_factor = (net_gross_wins / net_gross_losses if net_gross_losses
                  else (999.0 if net_gross_wins else 0.0))
    return {
        "n": len(values),
        "wins": sum(value > 0 for value in values),
        "win_rate": sum(value > 0 for value in values) / len(values),
        "mean_pnl": sum(values) / len(values),
        "median_pnl": median(values),
        "robust_mean_pnl": sum(ordinary) / len(ordinary) if ordinary else 0.0,
        "profit_factor": factor,
        "outlier_count": sum(outlier_mask),
        "round_trip_cost_pct": round_trip_cost_pct,
        "net_wins": net_wins,
        "net_win_rate": net_wins / len(net_values),
        "net_mean_pnl": sum(net_values) / len(net_values),
        "net_median_pnl": median(net_values),
        "net_robust_mean_pnl": sum(net_ordinary) / len(net_ordinary) if net_ordinary else 0.0,
        "net_profit_factor": net_factor,
        "net_win_rate_wilson_lower": wilson_lower_bound(net_wins, len(net_values)),
    }


def analyze(observations, min_history_rows=MIN_HISTORY_ROWS,
            min_validation_samples=MIN_VALIDATION_SAMPLES,
            round_trip_cost_pct=2.0):
    """Evaluate fixed score buckets on a chronological holdout only."""
    rows = sorted(observations, key=lambda row: row["timestamp"])
    if len(rows) < min_history_rows:
        return {"status": "insufficient_history", "rows": len(rows),
                "required_rows": min_history_rows, "train_rows": 0,
                "validation_rows": 0, "buckets": {}, "recommendations": [],
                "feature_associations": {}, "cost_sensitivity": []}

    split = max(1, int(len(rows) * 0.7))
    train, validation = rows[:split], rows[split:]
    buckets = {}
    recommendations = []
    for lower, upper in SCORE_BUCKETS:
        label = f"{int(lower)}-{100 if upper > 100 else int(upper)}"
        selected = [row for row in validation if lower <= row["score"] < upper]
        result = metrics(selected, round_trip_cost_pct=round_trip_cost_pct)
        result["minimum_validation_samples"] = min_validation_samples
        result["eligible"] = (
            result["n"] >= min_validation_samples
            and result["outlier_count"] == 0
            and result["net_median_pnl"] > 0
            and result["net_robust_mean_pnl"] > 0
            and result["net_win_rate"] >= 0.5
            and result["net_win_rate_wilson_lower"] >= 0.5
            and result["net_profit_factor"] > 1.2
        )
        buckets[label] = result
        if result["eligible"]:
            recommendations.append({
                "score_bucket": label,
                "reason": "positive_out_of_sample_metrics_and_95pct_wilson_lower_bound",
                "validation": result,
            })

    return {
        "status": "ok" if recommendations else "no_validated_bucket",
        "rows": len(rows),
        "train_rows": len(train),
        "validation_rows": len(validation),
        "minimum_validation_samples": min_validation_samples,
        "assumed_round_trip_cost_pct": round_trip_cost_pct,
        "outlier_abs_pnl_pct": OUTLIER_ABS_PNL_PCT,
        "buckets": buckets,
        "recommendations": recommendations,
        "feature_associations": feature_associations(validation),
        "cost_sensitivity": [
            {"assumed_round_trip_cost_pct": cost,
             **metrics(validation, round_trip_cost_pct=cost)}
            for cost in (1.0, 2.0, 3.0, 5.0)
        ],
        "policy": "research_only_no_live_threshold_changes",
        "confidence_policy": "95pct_wilson_lower_bound_of_net_win_rate_must_be_at_least_0.5",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--observations", default="shadow_observations.csv")
    parser.add_argument("--round-trip-cost-pct", type=float, default=2.0,
                        help="Assumed total fees and slippage as percentage points per round trip")
    args = parser.parse_args()
    print(json.dumps(analyze(load_observations(args.observations),
                             round_trip_cost_pct=args.round_trip_cost_pct), indent=2))
