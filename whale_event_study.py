#!/usr/bin/env python3
"""Descriptive event study of recorded whale swaps against subsequently observed DEX quotes.

No interpolation or synthetic candles are used. The study measures price response
after buy/sell observations at fixed delays and horizons; it does not optimize or
authorize a strategy.
"""
import argparse
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

from shadow_path_backtest import (
    MAX_SAMPLE_GAP_SECONDS,
    MIN_PATH_DURATION_SECONDS,
    MIN_PRICE_PATH_SAMPLES,
    _normalized_path,
)
from whale_follow_backtest import (
    MAX_SIGNAL_AGE_SECONDS,
    CONSENSUS_WINDOW_SECONDS,
    normalize_swap_state,
)

DEFAULT_DELAYS_SECONDS = (0, 30, 60, 120)
DEFAULT_HORIZONS_SECONDS = (30, 60, 120, 300, 600, 900)
MIN_TRAIN_PATHS = 20
MIN_HOLDOUT_PATHS = 15


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def load_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, TypeError, ValueError):
        return {}


def _quality_path(row):
    path, error = _normalized_path(row)
    if error:
        return None, error
    history = path["price_history"]
    gaps = [history[i]["timestamp"] - history[i - 1]["timestamp"] for i in range(1, len(history))]
    if len(history) < MIN_PRICE_PATH_SAMPLES:
        return None, "insufficient_samples"
    if path["observed_duration_seconds"] < MIN_PATH_DURATION_SECONDS:
        return None, "insufficient_observation_horizon"
    if not gaps or any(gap < 0 or gap > MAX_SAMPLE_GAP_SECONDS for gap in gaps):
        return None, "sparse_or_non_chronological_quotes"
    return path, None


def _event_notional(events):
    result = defaultdict(float)
    for event in events:
        result[event["quote_symbol"]] += float(event["quote_amount"])
    return {quote: round(value, 8) for quote, value in sorted(result.items())}


def _signal_events_for_side(events, side, path_start, path_end):
    """Return the first single-wallet signal and first later-observed consensus signal."""
    side_events = sorted(
        [
            event for event in events
            if event["side"] == side
            and path_start - MAX_SIGNAL_AGE_SECONDS <= event["timestamp"] <= path_end
        ],
        key=lambda event: (event["timestamp"], event["wallet"], event["signature"]),
    )
    if not side_events:
        return []

    first = side_events[0]
    signals = [{
        "side": side,
        "signal_class": "single_wallet",
        "signal_timestamp": first["timestamp"],
        "signal_wallets": [first["wallet"]],
        "signal_wallet_count": 1,
        "signal_signatures": [first["signature"]],
        "quote_notional_by_currency": _event_notional([first]),
    }]

    for event in side_events:
        cluster = [
            row for row in side_events
            if event["timestamp"] - CONSENSUS_WINDOW_SECONDS
            <= row["timestamp"] <= event["timestamp"]
        ]
        wallets = sorted({row["wallet"] for row in cluster})
        if len(wallets) < 2:
            continue
        signals.append({
            "side": side,
            "signal_class": "multi_wallet_consensus",
            "signal_timestamp": event["timestamp"],
            "signal_wallets": wallets,
            "signal_wallet_count": len(wallets),
            "signal_signatures": sorted({row["signature"] for row in cluster}),
            "quote_notional_by_currency": _event_notional(cluster),
        })
        break
    return signals


def _first_quote_at_or_after(history, target_timestamp, max_gap_seconds=MAX_SAMPLE_GAP_SECONDS):
    for sample in history:
        timestamp = float(sample["timestamp"])
        if timestamp < target_timestamp:
            continue
        if timestamp - target_timestamp > max_gap_seconds:
            return None
        return sample
    return None


def _measure_signal(path, signal, delay_seconds, horizons_seconds):
    history = path["price_history"]
    signal_time = float(signal["signal_timestamp"])
    start_time = float(path["entry_timestamp"])
    target = max(start_time, signal_time + float(delay_seconds))
    entry = _first_quote_at_or_after(history, target)
    if entry is None:
        return None
    entry_time = float(entry["timestamp"])
    # Don't score a "copy" at a quote that arrives long after the observed signal.
    if entry_time < signal_time or entry_time - signal_time > MAX_SIGNAL_AGE_SECONDS:
        return None
    entry_price = float(entry["price_usd"])
    responses = []
    for horizon in horizons_seconds:
        target_time = entry_time + float(horizon)
        future = _first_quote_at_or_after(history, target_time)
        if future is None:
            responses.append({
                "horizon_seconds": int(horizon),
                "status": "censored_no_observed_quote",
                "target_timestamp": target_time,
                "observed_quote_timestamp": None,
                "raw_return_pct": None,
                "directional_return_pct": None,
            })
            continue
        future_price = float(future["price_usd"])
        raw = (future_price / entry_price - 1.0) * 100.0
        directional = raw if signal["side"] == "buy" else -raw
        responses.append({
            "horizon_seconds": int(horizon),
            "status": "observed",
            "target_timestamp": target_time,
            "observed_quote_timestamp": float(future["timestamp"]),
            "quote_lag_seconds": round(float(future["timestamp"]) - target_time, 3),
            "raw_return_pct": round(raw, 6),
            "directional_return_pct": round(directional, 6),
        })
    return {
        "mint": path["mint"],
        "symbol": path.get("symbol", "?"),
        "partition": path.get("_partition", "unknown"),
        "side": signal["side"],
        "signal_class": signal["signal_class"],
        "signal_timestamp": signal_time,
        "signal_wallets": signal["signal_wallets"],
        "signal_wallet_count": signal["signal_wallet_count"],
        "signal_signatures": signal["signal_signatures"],
        "quote_notional_by_currency": signal["quote_notional_by_currency"],
        "delay_seconds": int(delay_seconds),
        "entry_timestamp": entry_time,
        "entry_price_usd": entry_price,
        "entry_quote_lag_seconds": round(entry_time - max(start_time, signal_time + float(delay_seconds)), 3),
        "horizon_responses": responses,
    }


def _aggregate(observations):
    buckets = defaultdict(list)
    for observation in observations:
        for response in observation["horizon_responses"]:
            if response["status"] != "observed":
                continue
            key = (
                observation["partition"], observation["side"], observation["signal_class"],
                observation["delay_seconds"], response["horizon_seconds"],
            )
            buckets[key].append(response)
    output = []
    for (partition, side, signal_class, delay, horizon), rows in sorted(buckets.items()):
        raw = [float(row["raw_return_pct"]) for row in rows]
        directional = [float(row["directional_return_pct"]) for row in rows]
        output.append({
            "partition": partition,
            "side": side,
            "signal_class": signal_class,
            "delay_seconds": delay,
            "horizon_seconds": horizon,
            "n": len(rows),
            "positive_directional_responses": sum(value > 0 for value in directional),
            "negative_directional_responses": sum(value < 0 for value in directional),
            "directional_win_rate": round(sum(value > 0 for value in directional) / len(directional), 4),
            "mean_raw_return_pct": round(statistics.mean(raw), 6),
            "median_raw_return_pct": round(statistics.median(raw), 6),
            "mean_directional_return_pct": round(statistics.mean(directional), 6),
            "median_directional_return_pct": round(statistics.median(directional), 6),
            "sample_label": "exploratory" if len(rows) < 8 else "descriptive",
        })
    return output


def analyze_event_response(paths, swap_state, delays_seconds=DEFAULT_DELAYS_SECONDS,
                           horizons_seconds=DEFAULT_HORIZONS_SECONDS,
                           min_train_paths=MIN_TRAIN_PATHS, min_holdout_paths=MIN_HOLDOUT_PATHS,
                           rejected_paths=0, rejected_swaps=0):
    """Describe recorded price response by event class without selecting a strategy."""
    normalized, invalid, seen = [], 0, set()
    for row in paths or []:
        path, error = _quality_path(row)
        if error:
            invalid += 1
            continue
        if path["mint"] in seen:
            continue
        seen.add(path["mint"])
        normalized.append(path)
    normalized.sort(key=lambda row: (row["entry_timestamp"], row["mint"]))

    events, malformed = normalize_swap_state(swap_state)
    events_by_mint = defaultdict(list)
    for event in events:
        events_by_mint[event["mint"]].append(event)

    split = int(len(normalized) * 0.7)
    train_mints = {row["mint"] for row in normalized[:split]}
    holdout_mints = {row["mint"] for row in normalized[split:]}
    observations = []
    matched_buy_mints, matched_sell_mints = set(), set()
    for path in normalized:
        path_end = float(path["price_history"][-1]["timestamp"])
        mint_events = events_by_mint.get(path["mint"], [])
        signals = []
        for side in ("buy", "sell"):
            signals.extend(_signal_events_for_side(
                mint_events, side, float(path["entry_timestamp"]), path_end
            ))
        if not signals:
            continue
        path["_partition"] = "train" if path["mint"] in train_mints else "holdout"
        for signal in signals:
            if signal["side"] == "buy":
                matched_buy_mints.add(path["mint"])
            else:
                matched_sell_mints.add(path["mint"])
            for delay in delays_seconds:
                measured = _measure_signal(path, signal, delay, horizons_seconds)
                if measured is not None:
                    observations.append(measured)

    train_matched = len({row["mint"] for row in observations if row["partition"] == "train"})
    holdout_matched = len({row["mint"] for row in observations if row["partition"] == "holdout"})
    status = (
        "descriptive_only"
        if train_matched >= int(min_train_paths) and holdout_matched >= int(min_holdout_paths)
        else "insufficient_history"
    )
    return {
        "status": status,
        "mode": "recorded_whale_swap_directional_price_response",
        "research_only": True,
        "live_trading_authorized": False,
        "strategy_selected": False,
        "strategy_eligibility": "never_evaluated_by_this_descriptive_study",
        "paths_received": len(paths or []),
        "invalid_or_sparse_paths_rejected": int(rejected_paths) + invalid,
        "usable_paths": len(normalized),
        "train_paths": len(train_mints),
        "holdout_paths": len(holdout_mints),
        "train_matched_paths": train_matched,
        "holdout_matched_paths": holdout_matched,
        "paths_with_buy_events": len(matched_buy_mints),
        "paths_with_sell_events": len(matched_sell_mints),
        "wallet_swap_events": len(events),
        "invalid_or_unsupported_swaps_rejected": int(rejected_swaps) + malformed,
        "observed_wallets": len({row["wallet"] for row in events}),
        "matched_mints": len(matched_buy_mints | matched_sell_mints),
        "delays_seconds": [int(value) for value in delays_seconds],
        "horizons_seconds": [int(value) for value in horizons_seconds],
        "event_observations": len(observations),
        "groups": _aggregate(observations),
        "observations": observations[:500],
        "interpretation_limits": [
            "Responses use the next actual quote at or after the requested delay/horizon; no interpolation is performed.",
            "Buy and sell labels are inferred only from normalized swap records. Holder-balance changes are not counted as swaps.",
            "Directional response is price movement in the expected direction, not net executable PnL; fees, slippage and execution impact are not included here.",
            "Single-wallet and multi-wallet consensus outcomes may refer to the same mint/path, so they are not independent samples when compared across groups.",
            "Only one quote path per mint is retained, with earlier entries assigned to training and later entries to an untouched descriptive holdout.",
            "This module is descriptive and never selects a strategy, changes thresholds, or authorizes live trading.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paths", nargs="+", default=[
        "paper_account/whale_quote_paths.jsonl", "paper_account/shadow_price_paths.jsonl"
    ])
    parser.add_argument("--swaps", default="whale_copy_state.json")
    parser.add_argument("--output")
    args = parser.parse_args()

    rows, rejected = [], 0
    for filename in args.paths:
        try:
            lines = Path(filename).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                rejected += 1
    report = analyze_event_response(rows, load_json(args.swaps), rejected_paths=rejected)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
