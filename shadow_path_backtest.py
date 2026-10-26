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
MIN_TRAIN_ROWS = 20
MIN_EXPLORATORY_HOLDOUT_ROWS = 5
MIN_VALIDATION_ROWS = 15
DEFAULT_MIN_TOTAL_PATHS = 50
MIN_PRICE_PATH_SAMPLES = 9
# The scheduled paper cycle has a measured boundary gap near 205 seconds.
# A 240-second cap admits these observed paths but still rejects hidden multi-minute gaps.
MAX_SAMPLE_GAP_SECONDS = 240.0
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
    mint = str(record.get("mint") or "").strip()
    if not mint:
        return None, "missing_mint"
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


def candidate_entry_filters():
    """Finite, predeclared entry filters; every value must be known at entry time."""
    return [
        {"name": "all_research_candidates"},
        {"name": "score_at_least_40", "score_min": 40.0},
        {"name": "score_at_least_50", "score_min": 50.0},
        {"name": "score_at_least_60", "score_min": 60.0},
        {"name": "score_at_least_70", "score_min": 70.0},
        {"name": "hourly_momentum_0_to_30", "h1_min": 0.0, "h1_max": 30.0},
        {"name": "hourly_momentum_5_to_30", "h1_min": 5.0, "h1_max": 30.0},
        {"name": "hourly_momentum_0_to_45", "h1_min": 0.0, "h1_max": 45.0},
        {"name": "m5_momentum_at_most_5", "m5_max": 5.0},
        {"name": "buy_sell_ratio_at_least_1", "ratio_min": 1.0},
        {"name": "buy_sell_ratio_at_least_1_2", "ratio_min": 1.2},
        {"name": "liquidity_at_least_25000", "liquidity_min": 25_000.0},
        {"name": "liquidity_at_least_50000", "liquidity_min": 50_000.0},
        {"name": "hourly_volume_at_least_10000", "volume_min": 10_000.0},
        {"name": "hourly_volume_at_least_25000", "volume_min": 25_000.0},
        {
            "name": "score60_hourly_momentum_0_to_45",
            "score_min": 60.0, "h1_min": 0.0, "h1_max": 45.0,
        },
        {
            "name": "score60_hourly_volume_10000",
            "score_min": 60.0, "volume_min": 10_000.0,
        },
        {
            "name": "score60_liquidity_25000",
            "score_min": 60.0, "liquidity_min": 25_000.0,
        },
    ]


def entry_filter_matches(record, entry_filter):
    """Apply entry-only constraints to an observed path; missing features never pass."""
    if not isinstance(record, dict) or not isinstance(entry_filter, dict):
        return False
    components = record.get("entry_components")
    if not isinstance(components, dict):
        components = {}
    requirements = (
        ("score_min", record.get("score")),
        ("h1_min", components.get("momentum_h1_pct")),
        ("h1_max", components.get("momentum_h1_pct")),
        ("m5_max", components.get("momentum_m5_pct")),
        ("ratio_min", components.get("buy_sell_ratio")),
        ("liquidity_min", components.get("liquidity_usd")),
        ("volume_min", components.get("volume_h1_usd")),
    )
    for requirement, observed in requirements:
        if requirement not in entry_filter:
            continue
        value = _finite(observed)
        bound = _finite(entry_filter.get(requirement))
        if value is None or bound is None:
            return False
        if requirement.endswith("_min") and value < bound:
            return False
        if requirement.endswith("_max") and value > bound:
            return False
    return True


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
    """Search on chronological training data, but never inspect candidate holdout until formal gates pass."""
    normalized, rejected, sparse_or_short = [], 0, 0
    observed_gaps, accepted_gaps = [], []
    rejection_reasons = {}
    for record in records:
        candidate, error = _normalized_path(record)
        if error:
            rejected += 1
            rejection_reasons[error] = rejection_reasons.get(error, 0) + 1
            continue
        history = candidate["price_history"]
        gaps = [
            history[i]["timestamp"] - history[i - 1]["timestamp"]
            for i in range(1, len(history))
        ]
        observed_gaps.extend(gaps)
        reasons = []
        if candidate["observed_duration_seconds"] < MIN_PATH_DURATION_SECONDS:
            reasons.append("duration_under_900_seconds")
        if len(history) < MIN_PRICE_PATH_SAMPLES:
            reasons.append("fewer_than_9_observed_samples")
        if not gaps:
            reasons.append("no_observed_gaps")
        elif max(gaps) > MAX_SAMPLE_GAP_SECONDS:
            reasons.append("sample_gap_over_240_seconds")
        if reasons:
            rejected += 1
            sparse_or_short += 1
            for reason in reasons:
                rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
            continue
        accepted_gaps.extend(gaps)
        normalized.append(candidate)

    # Use a single completed path per mint. This prevents overlapping or repeated
    # observations on one token leaking across the chronological train/holdout split.
    normalized.sort(key=lambda item: (item["entry_timestamp"], str(item.get("mint", ""))))
    quality_usable_path_records = len(normalized)
    first_path_by_mint, seen_mints = [], set()
    repeated_mint_paths_excluded = 0
    for candidate in normalized:
        mint = str(candidate.get("mint") or "").strip()
        if mint in seen_mints:
            repeated_mint_paths_excluded += 1
            continue
        seen_mints.add(mint)
        first_path_by_mint.append(candidate)
    normalized = first_path_by_mint
    independent_gaps = [
        normalized_record["price_history"][i]["timestamp"]
        - normalized_record["price_history"][i - 1]["timestamp"]
        for normalized_record in normalized
        for i in range(1, len(normalized_record["price_history"]))
    ]

    entry_filters = candidate_entry_filters()
    exit_configs = candidate_configs()
    result = {
        "status": "insufficient_history",
        "mode": "observed_quotes_only_chronological_walk_forward_joint_entry_exit_unique_mint",
        "round_trip_cost_pct": round_trip_cost_pct,
        "paths_received": len(records),
        "quality_usable_path_records": quality_usable_path_records,
        "repeated_mint_paths_excluded": repeated_mint_paths_excluded,
        "usable_paths": len(normalized),
        "rejected_or_censored_paths": rejected,
        "sparse_or_short_paths_rejected": sparse_or_short,
        "path_rejection_reasons": rejection_reasons,
        "observed_sample_gaps": {
            "count": len(observed_gaps),
            "median_seconds": median(observed_gaps) if observed_gaps else None,
            "maximum_seconds": max(observed_gaps) if observed_gaps else None,
            "over_120_seconds": sum(gap > 120.0 for gap in observed_gaps),
        },
        "accepted_sample_gaps": {
            "count": len(accepted_gaps),
            "median_seconds": median(accepted_gaps) if accepted_gaps else None,
            "maximum_seconds": max(accepted_gaps) if accepted_gaps else None,
            "over_120_seconds": sum(gap > 120.0 for gap in accepted_gaps),
        },
        "independent_sample_gaps": {
            "count": len(independent_gaps),
            "median_seconds": median(independent_gaps) if independent_gaps else None,
            "maximum_seconds": max(independent_gaps) if independent_gaps else None,
            "over_120_seconds": sum(gap > 120.0 for gap in independent_gaps),
        },
        "path_quality_policy": {
            "one_complete_path_per_mint": True,
            "minimum_duration_seconds": MIN_PATH_DURATION_SECONDS,
            "minimum_samples": MIN_PRICE_PATH_SAMPLES,
            "maximum_sample_gap_seconds": MAX_SAMPLE_GAP_SECONDS,
        },
        "required_total_paths": int(min_total_paths),
        "joint_candidate_configs": len(entry_filters) * len(exit_configs),
        "entry_filter_grid_size": len(entry_filters),
        "exit_config_grid_size": len(exit_configs),
        "evaluated_candidate_configs": 0,
        "eligible_training_candidate_count": 0,
        "eligible_training_candidates": [],
        "exploratory_training_candidates": [],
        "exploratory_status": "not_run",
        "selected_config": None,
        "selected_entry_filter": None,
        "train_rows": 0,
        "validation_rows": 0,
        "training": None,
        "holdout": None,
    }

    baseline_outcomes = [
        simulate_exit(record, BASELINE_CONFIG, fee_reserve_pct=round_trip_cost_pct)
        for record in normalized
    ]
    result["baseline_performance"] = calculate_metrics(baseline_outcomes)

    split = int(len(normalized) * 0.7)
    train, holdout = normalized[:split], normalized[split:]
    result["train_rows"] = len(train)
    result["validation_rows"] = len(holdout)
    if len(train) < MIN_TRAIN_ROWS or len(holdout) < MIN_EXPLORATORY_HOLDOUT_ROWS:
        result["exploratory_status"] = "too_few_rows_for_even_exploratory_search"
        return result

    # Exploration scores the training segment only. The holdout candidate outcomes
    # remain unread until all formal sample gates pass.
    eligible = []
    exploratory = []
    evaluated = 0
    for entry_filter in entry_filters:
        train_subset = [
            record for record in train
            if entry_filter_matches(record, entry_filter)
        ]
        if len(train_subset) < MIN_TRAIN_ROWS:
            continue
        for exit_config in exit_configs:
            evaluated += 1
            outcomes = [
                simulate_exit(record, exit_config, fee_reserve_pct=round_trip_cost_pct)
                for record in train_subset
            ]
            metrics = calculate_metrics(outcomes)
            row = {
                "entry_filter": entry_filter,
                "exit_config": exit_config,
                "train_rows": len(train_subset),
                "metrics": metrics,
            }
            if _eligible_train(metrics):
                eligible.append(row)
            exploratory.append(row)

    sort_key = lambda item: (
        item["metrics"]["mean_pnl_pct"],
        item["metrics"]["median_pnl_pct"],
        item["metrics"]["profit_factor"],
        item["train_rows"],
    )
    eligible.sort(key=sort_key, reverse=True)
    exploratory.sort(key=sort_key, reverse=True)
    result["evaluated_candidate_configs"] = evaluated
    result["eligible_training_candidate_count"] = len(eligible)
    result["eligible_training_candidates"] = eligible[:25]
    result["exploratory_training_candidates"] = exploratory[:10]
    result["exploratory_status"] = "training_only_search_complete_holdout_not_evaluated"

    # These are hard promotion gates. Exploratory results are visible, but no
    # selected rule or holdout claim is made until the independent sample is large.
    formal_ready = (
        len(normalized) >= int(min_total_paths)
        and len(train) >= MIN_TRAIN_ROWS
        and len(holdout) >= MIN_VALIDATION_ROWS
    )
    if not formal_ready:
        return result
    if not eligible:
        result["status"] = "no_profitable_training_candidate"
        result["exploratory_status"] = "formal_training_gate_failed_no_candidate"
        return result

    selected = eligible[0]
    selected_filter = selected["entry_filter"]
    selected_exit = selected["exit_config"]
    holdout_subset = [
        record for record in holdout
        if entry_filter_matches(record, selected_filter)
    ]
    validation_outcomes = [
        simulate_exit(record, selected_exit, fee_reserve_pct=round_trip_cost_pct)
        for record in holdout_subset
    ]
    validation_metrics = calculate_metrics(validation_outcomes)
    result["selected_config"] = {**selected_exit, "entry_filter": selected_filter}
    result["selected_entry_filter"] = selected_filter
    result["training"] = selected["metrics"]
    result["holdout"] = validation_metrics
    result["holdout_entry_rows"] = len(holdout_subset)
    result["status"] = (
        "validated_profitable_out_of_sample"
        if _eligible_holdout(validation_metrics)
        else "holdout_not_profitable"
    )
    result["exploratory_status"] = "formal_walk_forward_completed"
    return result


PROSPECTIVE_ENTRY_TAGS = ("moderate_pullback_v1",)


def analyze_prospective_cohorts(records, min_total_paths=DEFAULT_MIN_TOTAL_PATHS,
                                round_trip_cost_pct=ROUND_TRIP_COST_PCT):
    """Score only paths tagged at entry by the current scanner version.

    Untagged historical paths are deliberately excluded because the research
    rule was identified after those outcomes were observed. The cohort remains
    paper-only; the normal train/holdout promotion gates still apply.
    """
    experiments = {}
    for tag in PROSPECTIVE_ENTRY_TAGS:
        cohort = [
            record for record in records
            if isinstance(record, dict)
            and isinstance(record.get("prospective_entry_filters"), list)
            and tag in record["prospective_entry_filters"]
        ]
        result = analyze_records(
            cohort,
            min_total_paths=min_total_paths,
            round_trip_cost_pct=round_trip_cost_pct,
        )
        result["experiment_tag"] = tag
        result["cohort_policy"] = (
            "Only paths tagged at entry by the deployed scanner version; "
            "untagged historical paths are excluded from this prospective test."
        )
        experiments[tag] = result
    return experiments


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
    # Candidate selection uses only the isolated paper account. The legacy
    # account can share tokens and market windows, so mixing its paths into the
    # training sample would inflate sample size with correlated observations.
    paths = paths or ["paper_account/shadow_price_paths.jsonl"]
    records, diagnostics = load_paths(paths)
    report = analyze_records(
        records,
        min_total_paths=min_total_paths,
        round_trip_cost_pct=round_trip_cost_pct,
    )
    report["sources"] = diagnostics
    report["baseline_consistency"] = baseline_consistency(records)
    report["prospective_experiments"] = analyze_prospective_cohorts(
        records,
        min_total_paths=min_total_paths,
        round_trip_cost_pct=round_trip_cost_pct,
    )
    report["policy"] = (
        "Research only. No live threshold or order path is changed. Approval requires a "
        "profitable training segment and a separate profitable chronological holdout after costs."
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--paths", nargs="*", default=["paper_account/shadow_price_paths.jsonl"],
        help="JSONL path logs created from real sampled paper quotes.",
    )
    parser.add_argument("--min-total-paths", type=int, default=DEFAULT_MIN_TOTAL_PATHS)
    parser.add_argument("--round-trip-cost-pct", type=float, default=ROUND_TRIP_COST_PCT)
    args = parser.parse_args()
    print(json.dumps(run(args.paths, args.min_total_paths, args.round_trip_cost_pct), indent=2, sort_keys=True, allow_nan=False))
