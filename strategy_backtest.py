#!/usr/bin/env python3
"""Offline entry-strategy backtest and chronological walk-forward validator."""
import argparse
import csv
import json
import math
from pathlib import Path

THRESHOLDS = (80.0, 82.5, 85.0, 87.5, 90.0)
MIN_HISTORY_ROWS = 30
MIN_VALIDATION_ROWS = 8
MIN_THRESHOLD_VALIDATION_SAMPLES = 8


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


def metrics(rows, threshold):
    selected = [row for row in rows if row["score"] >= threshold]
    wins = [row for row in selected if row["pnl_pct"] > 0]
    losses = [row for row in selected if row["pnl_pct"] < 0]
    gross_wins = sum(row["pnl_pct"] for row in wins)
    gross_losses = abs(sum(row["pnl_pct"] for row in losses))
    count = len(selected)
    return {
        "n": count,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / count if count else 0.0,
        "avg_pnl": sum(row["pnl_pct"] for row in selected) / count if count else 0.0,
        "profit_factor": gross_wins / gross_losses if gross_losses else (999.0 if gross_wins else 0.0),
        "eligible": count >= MIN_THRESHOLD_VALIDATION_SAMPLES,
    }


def run(rows):
    rows = sorted(rows, key=lambda item: item["timestamp"])
    if len(rows) < MIN_HISTORY_ROWS:
        return {
            "status": "insufficient_history",
            "rows": len(rows),
            "required_rows": MIN_HISTORY_ROWS,
            "validation_rows": 0,
            "eligible_thresholds": [],
            "thresholds": {},
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
        }

    result = {}
    eligible = []
    for threshold in THRESHOLDS:
        train_metrics = metrics(train, threshold)
        validation_metrics = metrics(validation, threshold)
        result[str(threshold)] = {
            "train": train_metrics,
            "validation": validation_metrics,
            "eligible": validation_metrics["eligible"],
        }
        if validation_metrics["eligible"]:
            eligible.append(threshold)

    return {
        "status": "ok" if eligible else "insufficient_threshold_samples",
        "rows": len(rows),
        "train_rows": len(train),
        "validation_rows": len(validation),
        "required_validation_samples_per_threshold": MIN_THRESHOLD_VALIDATION_SAMPLES,
        "eligible_thresholds": eligible,
        "thresholds": result,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--observations", default="strategy_observations.csv")
    args = parser.parse_args()
    report = run(load(args.observations))
    print(json.dumps(report, indent=2))
