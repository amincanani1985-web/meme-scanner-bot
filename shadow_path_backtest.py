#!/usr/bin/env python3
"""Walk-forward backtest of exit rules against observed shadow quote paths only.

No candles, interpolated prices, or synthetic trades are generated here. The
baseline outcome is kept as a consistency check; candidate configurations are
selected only on an earlier chronological training segment and judged on a
later untouched holdout.
"""
import argparse
import json
import math
from pathlib import Path
from statistics import median

BASELINE_CONFIG = {
    "hard_stop_pct": 0.10,
    "trailing_arm_pct": 0.08,
    "trailing_stop_pct": 0.04,
    "max_hold_seconds": 600,
}
EXIT_SLIPPAGE_BPS = 200
ROUND_TRIP_COST_PCT = 1.0
MIN_TRAIN_ROWS = 30
MIN_VALIDATION_ROWS = 15
DEFAULT_MIN_TOTAL_PATHS = 50
MIN_PRICE_PATH_SAMPLES = 9
MAX_SAMPLE_GAP_SECONDS = 120.0
MIN_PATH_DURATION_SECONDS = 900.0
OUTLIER_ABS_PNL_PCT = 100.0


def _finite(value):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _wilson_lower_bound(wins, n, z=1.96):
    """Two-sided 95% Wilson lower bound for a binomial win rate."""
    if n <= 0:
        return 0.0
    p = min(1.0, max(0.0, float(wins) / n))
    z2 = z * z
    denominator = 1.0 + z2 / n
    center = p + z2 / (2 * n)
    radius = z * math.sqrt((p * (1.0 - p) + z2 / (4 * n)) / n)
    return max(0.0, (center - radius) / denominator)


def _normalized_path(record):
    """Validate and normalize recorded samples without reordering time or filling gaps."""
    if not isinstance(record, dict):
        return None, "not_object"
    entry_time = _finite(record.get("entry_timestamp"))
    entry_price = _finite(record.get("entry_price"))
    if entry_time is None or entry_price is None or entry_price <= 0:
        return None, "invalid_entry"
    history = record.get("price_history")
    if not isinstance(history, list) or len(history) < 2:
        return None, "insufficient_samples"
    samples = []
    previous_ts = None
    for item in history:
        if not isinstance(item, dict):
            return None, "invalid_sample"
        timestamp = _finite(item.get("timestamp"))
        price = _finite(item.get("price_usd"))
        liquidity = _finite(item.get("liquidity_usd"))
        if timestamp is None or price is None or liquidity is None or price <= 0 or liquidity < 0:
            return None, "invalid_sample"
        if timestamp < entry_time:
            return None, "sample_before_entry"
        if previous_ts is not None and timestamp < previous_ts:
            return None, "non_chronological_samples"
        samples.append({
            "timestamp": timestamp,
            "price_usd": price,
            "liquidity_usd": liquidity,
        })
        previous_ts = timestamp
    duration = max(s["timestamp"] for s in samples) - entry_time
    return {
        **record,
        "entry_timestamp": entry_time,
        "entry_price": entry_price,
        "entry_liquidity_usd": max(0.0, _finite(record.get("entry_liquidity_usd")) or 0.0),
        "price_history": samples,
        "observed_duration_seconds": max(0.0, duration),
    }, None


def simulate_exit(record, config, exit_slippage_bps=EXIT_SLIPPAGE_BPS,
                  fee_reserve_pct=ROUND_TRIP_COST_PCT):
    """Simulate one exit rule on the actual stored quote sequence.

    Exit conditions are checked in the same order as the paper scanner: hard
    stop, trailing stop, liquidity drain, then time stop. If the path ends before
    the configured holding horizon without an exit, the result is censored.
    """
    path, error = _normalized_path(record)
    if error:
        return {
            "status": "invalid_path",
            "reason": None,
            "net_pnl_pct": None,
            "validation_error": error,
        }
    try:
        hard_stop = float(config["hard_stop_pct"])
        trailing_arm = float(config["trailing_arm_pct"])
        trailing_stop = float(config["trailing_stop_pct"])
        max_hold = float(config["max_hold_seconds"])
        slip_bps = float(exit_slippage_bps)
        fees = float(fee_reserve_pct)
    except (KeyError, TypeError, ValueError, OverflowError):
        return {"status": "invalid_config", "reason": None, "net_pnl_pct": None}
    if (not all(math.isfinite(x) for x in (hard_stop, trailing_arm, trailing_stop, max_hold, slip_bps, fees))
            or hard_stop <= 0 or trailing_arm < 0 or trailing_stop <= 0
            or max_hold <= 0 or max_hold > 600 or slip_bps < 0 or fees < 0):
        return {"status": "invalid_config", "reason": None, "net_pnl_pct": None}

    entry_time = path["entry_timestamp"]
    entry_price = path["entry_price"]
    entry_liquidity = path["entry_liquidity_usd"]
    peak_price = entry_price
    trailing_armed = False
    liquidity_drain_hits = 0

    for sample in path["price_history"]:
        age = sample["timestamp"] - entry_time
        if age < 0:
            continue
        price = sample["price_usd"]
        liquidity = sample["liquidity_usd"]
        peak_price = max(peak_price, price)
        pnl_fraction = (price - entry_price) / entry_price

        reason = "hard_stop_loss" if pnl_fraction <= -hard_stop else None
        if reason is None and pnl_fraction >= trailing_arm:
            trailing_armed = True
        if reason is None and trailing_armed and peak_price > 0:
            if (peak_price - price) / peak_price >= trailing_stop:
                reason = "trailing_stop"

        if reason is None and entry_liquidity > 0 and liquidity > 0:
            drop = (entry_liquidity - liquidity) / entry_liquidity
            if drop >= 0.50:
                liquidity_drain_hits += 1
                if liquidity_drain_hits >= 2:
                    reason = "liquidity_drained"
            elif drop >= 0.25:
                reason = "liquidity_drained"
            else:
                liquidity_drain_hits = 0

        if reason is None and age >= max_hold:
            reason = "max_hold_timeout"

        if reason is None:
            continue

        effective_exit = price * (1.0 - slip_bps / 10000.0)
        after_slippage_pnl = (effective_exit / entry_price - 1.0) * 100.0
        return {
            "status": "closed",
            "reason": reason,
            "entry_timestamp": entry_time,
            "exit_timestamp": sample["timestamp"],
            "holding_seconds": age,
            "entry_price": entry_price,
            "exit_price_usd": price,
            "effective_exit_price": effective_exit,
            "after_slippage_pnl_pct": after_slippage_pnl,
            "net_pnl_pct": after_slippage_pnl - fees,
            "config": dict(config),
        }

    # If no stop fired and no observed quote reached the requested time limit, it
    # is censored. Never mark the final available quote as a pretend exit.
    return {
        "status": "unresolved",
        "reason": None,
        "entry_timestamp": entry_time,
        "exit_timestamp": None,
        "holding_seconds": path["observed_duration_seconds"],
        "net_pnl_pct": None,
        "config": dict(config),
    }


def calculate_metrics(results):
    """Calculate cost-adjusted trade metrics; unresolved paths are excluded explicitly."""
    closed = [r for r in results if r.get("status") == "closed" and _finite(r.get("net_pnl_pct")) is not None]
    values = [float(r["net_pnl_pct"]) for r in closed]
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v < 0]
    clean = [v for v in values if abs(v) <= OUTLIER_ABS_PNL_PCT]
    clean_wins = [v for v in clean if v > 0]
    clean_losses = [v for v in clean if v < 0]
    net_wins = len(wins)
    gross_wins = sum(clean_wins)
    gross_losses = abs(sum(clean_losses))
    profit_factor = gross_wins / gross_losses if gross_losses else (999.0 if gross_wins else 0.0)
    return {
        "n": len(values),
        "wins": net_wins,
        "win_rate": net_wins / len(values) if values else 0.0,
        "mean_pnl_pct": sum(values) / len(values) if values else 0.0,
        "median_pnl_pct": median(values) if values else 0.0,
        "profit_factor": profit_factor,
        "wilson_lower_95": _wilson_lower_bound(net_wins, len(values)),
        "outlier_count": sum(abs(v) > OUTLIER_ABS_PNL_PCT for v in values),
        "unresolved": sum(r.get("status") == "unresolved" for r in results),
    }


def _eligible_train(metrics):
    return bool(
        metrics["n"] >= MIN_TRAIN_ROWS
        and metrics["outlier_count"] == 0
        and metrics["mean_pnl_pct"] > 0
        and metrics["median_pnl_pct"] > 0
        and metrics["win_rate"] >= 0.5
        and metrics["wilson_lower_95"] >= 0.5
        and metrics["profit_factor"] > 1.2
    )


def _eligible_holdout(metrics):
    return bool(
        metrics["n"] >= MIN_VALIDATION_ROWS
        and metrics["outlier_count"] == 0
        and metrics["mean_pnl_pct"] > 0
        and metrics["median_pnl_pct"] > 0
        and metrics["win_rate"] >= 0.5
        and metrics["wilson_lower_95"] >= 0.5
        and metrics["profit_factor"] > 1.2
    )


def candidate_configs():
    """Small, predeclared exit grid; selection remains restricted to training data."""
    return [
        {
            "hard_stop_pct": hard_stop,
            "trailing_arm_pct": arm,
            "trailing_stop_pct": trail,
            "max_hold_seconds": hold,
        }
        for hard_stop in (0.05, 0.08, 0.10)
        for arm in (0.04, 0.06, 0.08)
        for trail in (0.02, 0.03, 0.04)
        for hold in (300, 450, 600)
    ]


def baseline_consistency(records, tolerance_pct=0.05):
    """Check whether replaying today's exit policy matches the saved benchmark labels."""
    checked = matches = 0
    mismatches = []
    for original in records:
        benchmark = original.get("benchmark_exit")
        if not isinstance(benchmark, dict):
            continue
        replay = simulate_exit(original, BASELINE_CONFIG, fee_reserve_pct=0.0)
        checked += 1
        reason_ok = replay.get("reason") == benchmark.get("reason")
        pnl = _finite(replay.get("after_slippage_pnl_pct"))
        expected = _finite(benchmark.get("after_slippage_pnl_pct"))
        pnl_ok = pnl is not None and expected is not None and abs(pnl - expected) <= tolerance_pct
        if reason_ok and pnl_ok:
            matches += 1
        elif len(mismatches) < 10:
            mismatches.append({
                "mint": original.get("mint"),
                "expected_reason": benchmark.get("reason"),
                "replayed_reason": replay.get("reason"),
                "expected_after_slippage_pnl_pct": expected,
                "replayed_after_slippage_pnl_pct": pnl,
            })
    return {
        "checked": checked,
        "matches": matches,
        "mismatches": checked - matches,
        "tolerance_pct_points": tolerance_pct,
        "examples": mismatches,
    }


def analyze_records(records, min_total_paths=DEFAULT_MIN_TOTAL_PATHS,
                    round_trip_cost_pct=ROUND_TRIP_COST_PCT):
    """Chronological 70/30 walk-forward selection; no promotion on training profit alone."""
    normalized, rejected, sparse_or_short = [], 0, 0
    for record in records:
        candidate, error = _normalized_path(record)
        if error:
            rejected += 1
            continue
        history = candidate["price_history"]
        gaps = [
            history[i]["timestamp"] - history[i - 1]["timestamp"]
            for i in range(1, len(history))
        ]
        # A long elapsed duration with only an entry/exit quote is not a usable
        # path for testing stop rules. Require a full 15-minute horizon, enough
        # actual samples, and no multi-minute gaps that hide stop crossings.
        if (candidate["observed_duration_seconds"] < MIN_PATH_DURATION_SECONDS
                or len(history) < MIN_PRICE_PATH_SAMPLES
                or not gaps
                or max(gaps) > MAX_SAMPLE_GAP_SECONDS):
            rejected += 1
            sparse_or_short += 1
            continue
        normalized.append(candidate)
    normalized.sort(key=lambda item: (item["entry_timestamp"], str(item.get("mint", ""))))
    result = {
        "status": "insufficient_history",
        "mode": "observed_quotes_only_chronological_walk_forward",
        "round_trip_cost_pct": round_trip_cost_pct,
        "paths_received": len(records),
        "usable_paths": len(normalized),
        "rejected_or_censored_paths": rejected,
        "sparse_or_short_paths_rejected": sparse_or_short,
        "path_quality_policy": {
            "minimum_duration_seconds": MIN_PATH_DURATION_SECONDS,
            "minimum_samples": MIN_PRICE_PATH_SAMPLES,
            "maximum_sample_gap_seconds": MAX_SAMPLE_GAP_SECONDS,
        },
        "required_total_paths": int(min_total_paths),
        "train_rows": 0,
        "validation_rows": 0,
        "eligible_training_candidates": [],
        "selected_config": None,
        "training": None,
        "holdout": None,
    }
    if len(normalized) < int(min_total_paths):
        return result

    split = int(len(normalized) * 0.7)
    train, holdout = normalized[:split], normalized[split:]
    result["train_rows"] = len(train)
    result["validation_rows"] = len(holdout)
    if len(train) < MIN_TRAIN_ROWS or len(holdout) < MIN_VALIDATION_ROWS:
        return result

    eligible = []
    for config in candidate_configs():
        outcomes = [simulate_exit(record, config, fee_reserve_pct=round_trip_cost_pct) for record in train]
        metrics = calculate_metrics(outcomes)
        if _eligible_train(metrics):
            eligible.append({"config": config, "metrics": metrics})
    result["eligible_training_candidates"] = [
        {"config": item["config"], "metrics": item["metrics"]} for item in eligible
    ]
    if not eligible:
        result["status"] = "no_profitable_training_candidate"
        return result

    selected = max(
        eligible,
        key=lambda item: (
            item["metrics"]["mean_pnl_pct"],
            item["metrics"]["median_pnl_pct"],
            item["metrics"]["profit_factor"],
        ),
    )
    validation_outcomes = [
        simulate_exit(record, selected["config"], fee_reserve_pct=round_trip_cost_pct)
        for record in holdout
    ]
    validation_metrics = calculate_metrics(validation_outcomes)
    result["selected_config"] = selected["config"]
    result["training"] = selected["metrics"]
    result["holdout"] = validation_metrics
    result["status"] = (
        "validated_profitable_out_of_sample"
        if _eligible_holdout(validation_metrics)
        else "holdout_not_profitable"
    )
    return result


def load_paths(paths):
    """Load JSONL path records and report malformed/duplicate rows instead of hiding them."""
    records = []
    seen = set()
    diagnostics = {"files_found": 0, "lines_read": 0, "invalid_lines": 0, "duplicates": 0, "missing_files": []}
    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_file():
            diagnostics["missing_files"].append(str(path))
            continue
        diagnostics["files_found"] += 1
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    diagnostics["lines_read"] += 1
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        diagnostics["invalid_lines"] += 1
                        continue
                    if not isinstance(record, dict):
                        diagnostics["invalid_lines"] += 1
                        continue
                    key = (str(record.get("mint", "")), _finite(record.get("entry_timestamp")))
                    if key in seen:
                        diagnostics["duplicates"] += 1
                        continue
                    seen.add(key)
                    records.append(record)
        except OSError:
            diagnostics["missing_files"].append(str(path))
    return records, diagnostics


def run(paths=None, min_total_paths=DEFAULT_MIN_TOTAL_PATHS,
        round_trip_cost_pct=ROUND_TRIP_COST_PCT):
    paths = paths or ["shadow_price_paths.jsonl", "paper_account/shadow_price_paths.jsonl"]
    records, diagnostics = load_paths(paths)
    report = analyze_records(
        records,
        min_total_paths=min_total_paths,
        round_trip_cost_pct=round_trip_cost_pct,
    )
    report["sources"] = diagnostics
    report["baseline_consistency"] = baseline_consistency(records)
    report["policy"] = (
        "Research only. No live threshold or order path is changed. Approval requires a "
        "profitable training segment and a separate profitable chronological holdout after costs."
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--paths", nargs="*", default=["shadow_price_paths.jsonl", "paper_account/shadow_price_paths.jsonl"],
        help="JSONL path logs created from real sampled paper quotes.",
    )
    parser.add_argument("--min-total-paths", type=int, default=DEFAULT_MIN_TOTAL_PATHS)
    parser.add_argument("--round-trip-cost-pct", type=float, default=ROUND_TRIP_COST_PCT)
    args = parser.parse_args()
    print(json.dumps(run(args.paths, args.min_total_paths, args.round_trip_cost_pct), indent=2, sort_keys=True, allow_nan=False))
