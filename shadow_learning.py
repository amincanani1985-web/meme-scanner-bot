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
            rows.append({"timestamp": timestamp, "score": score,
                         "pnl_pct": pnl, "reason": row.get("reason", "")})
    return sorted(rows, key=lambda row: row["timestamp"])


def metrics(rows):
    """Return raw and outlier-resistant metrics without silently clipping data."""
    values = [float(row["pnl_pct"]) for row in rows
              if math.isfinite(float(row["pnl_pct"]))]
    if not values:
        return {"n": 0, "wins": 0, "win_rate": 0.0, "mean_pnl": 0.0,
                "median_pnl": 0.0, "robust_mean_pnl": 0.0,
                "profit_factor": 0.0, "outlier_count": 0}
    wins = sum(value > 0 for value in values)
    outliers = [value for value in values if abs(value) > OUTLIER_ABS_PNL_PCT]
    ordinary = [value for value in values if abs(value) <= OUTLIER_ABS_PNL_PCT]
    gross_wins = sum(value for value in ordinary if value > 0)
    gross_losses = abs(sum(value for value in ordinary if value < 0))
    factor = gross_wins / gross_losses if gross_losses else (999.0 if gross_wins else 0.0)
    return {
        "n": len(values),
        "wins": wins,
        "win_rate": wins / len(values),
        "mean_pnl": sum(values) / len(values),
        "median_pnl": median(values),
        "robust_mean_pnl": sum(ordinary) / len(ordinary) if ordinary else 0.0,
        "profit_factor": factor,
        "outlier_count": len(outliers),
    }


def analyze(observations, min_history_rows=MIN_HISTORY_ROWS,
            min_validation_samples=MIN_VALIDATION_SAMPLES):
    """Evaluate fixed score buckets on a chronological holdout only."""
    rows = sorted(observations, key=lambda row: row["timestamp"])
    if len(rows) < min_history_rows:
        return {"status": "insufficient_history", "rows": len(rows),
                "required_rows": min_history_rows, "train_rows": 0,
                "validation_rows": 0, "buckets": {}, "recommendations": []}

    split = max(1, int(len(rows) * 0.7))
    train, validation = rows[:split], rows[split:]
    buckets = {}
    recommendations = []
    for lower, upper in SCORE_BUCKETS:
        label = f"{int(lower)}-{100 if upper > 100 else int(upper)}"
        selected = [row for row in validation if lower <= row["score"] < upper]
        result = metrics(selected)
        result["minimum_validation_samples"] = min_validation_samples
        result["eligible"] = (
            result["n"] >= min_validation_samples
            and result["outlier_count"] == 0
            and result["median_pnl"] > 0
            and result["robust_mean_pnl"] > 0
            and result["win_rate"] >= 0.5
            and result["profit_factor"] > 1.2
        )
        buckets[label] = result
        if result["eligible"]:
            recommendations.append({
                "score_bucket": label,
                "reason": "positive_out_of_sample_median_and_robust_mean",
                "validation": result,
            })

    return {
        "status": "ok" if recommendations else "no_validated_bucket",
        "rows": len(rows),
        "train_rows": len(train),
        "validation_rows": len(validation),
        "minimum_validation_samples": min_validation_samples,
        "outlier_abs_pnl_pct": OUTLIER_ABS_PNL_PCT,
        "buckets": buckets,
        "recommendations": recommendations,
        "policy": "research_only_no_live_threshold_changes",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--observations", default="shadow_observations.csv")
    args = parser.parse_args()
    print(json.dumps(analyze(load_observations(args.observations)), indent=2))
