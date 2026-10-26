#!/usr/bin/env python3
"""Portfolio-aware paper replay for the DexScreener memecoin scanner.

This is a read-only research tool. It consumes actual recorded quote paths,
uses the existing observed-quote exit engine, and models a single capital slot
so overlapping signals cannot spend the same paper capital twice.
"""
import argparse
import json
from pathlib import Path

from shadow_path_backtest import (
    BASELINE_CONFIG,
    MIN_TRAIN_ROWS,
    MIN_VALIDATION_ROWS,
    _eligible_holdout,
    _eligible_train,
    _normalized_path,
    calculate_metrics,
    candidate_configs,
    candidate_entry_filters,
    entry_filter_matches,
    simulate_exit,
)


def load_paths(paths):
    """Read JSONL quote paths and keep one quality-usable path per mint."""
    records = []
    rejected = 0
    seen = set()
    for filename in paths:
        try:
            lines = Path(filename).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                rejected += 1
                continue
            record, error = _normalized_path(raw)
            if error:
                rejected += 1
                continue
            history = record["price_history"]
            gaps = [history[i]["timestamp"] - history[i - 1]["timestamp"]
                    for i in range(1, len(history))]
            if (len(history) < 9 or record["observed_duration_seconds"] < 900
                    or not gaps or max(gaps) > 240):
                rejected += 1
                continue
            mint = str(record.get("mint") or "").strip()
            if not mint or mint in seen:
                continue
            seen.add(mint)
            records.append(record)
    records.sort(key=lambda row: (row["entry_timestamp"], str(row.get("mint", ""))))
    return records, rejected


def portfolio_replay(records, entry_filter, exit_config, initial_equity=1000.0,
                     allocation_pct=10.0):
    """Replay chronologically with one position at a time and fractional sizing."""
    equity = float(initial_equity)
    allocation = float(allocation_pct) / 100.0
    available_at = float("-inf")
    peak = equity
    max_drawdown_pct = 0.0
    trades, skipped_busy, unresolved = [], 0, 0
    for record in sorted(records, key=lambda row: row["entry_timestamp"]):
        if not entry_filter_matches(record, entry_filter):
            continue
        if record["entry_timestamp"] < available_at:
            skipped_busy += 1
            continue
        result = simulate_exit(record, exit_config)
        if result.get("status") != "closed":
            unresolved += 1
            continue
        pnl_pct = float(result["net_pnl_pct"])
        before = equity
        equity *= max(0.0, 1.0 + allocation * pnl_pct / 100.0)
        available_at = float(result.get("exit_timestamp") or record["entry_timestamp"])
        peak = max(peak, equity)
        drawdown = ((peak - equity) / peak * 100.0) if peak else 0.0
        max_drawdown_pct = max(max_drawdown_pct, drawdown)
        trades.append({
            "mint": record.get("mint"),
            "entry_timestamp": record["entry_timestamp"],
            "exit_timestamp": available_at,
            "net_trade_pnl_pct": round(pnl_pct, 6),
            "equity_before": round(before, 6),
            "equity_after": round(equity, 6),
        })
    return {
        "initial_equity": initial_equity,
        "ending_equity": round(equity, 6),
        "return_pct": round((equity / initial_equity - 1.0) * 100.0, 6)
            if initial_equity else 0.0,
        "max_drawdown_pct": round(max_drawdown_pct, 6),
        "trades": len(trades),
        "skipped_while_capital_busy": skipped_busy,
        "unresolved_paths": unresolved,
        "allocation_pct": allocation_pct,
        "max_concurrent_positions": 1,
        "trade_log": trades,
    }


def analyze_portfolio(records, initial_equity=1000.0, allocation_pct=10.0):
    """Choose only on chronological training rows; evaluate once on holdout."""
    split = int(len(records) * 0.7)
    train, holdout = records[:split], records[split:]
    result = {
        "status": "insufficient_history",
        "mode": "observed_dexscreener_quote_paths_single_slot_portfolio_replay",
        "paths": len(records),
        "train_rows": len(train),
        "holdout_rows": len(holdout),
        "entry_filters": len(candidate_entry_filters()),
        "exit_configs": len(candidate_configs()),
        "evaluated_candidates": 0,
        "eligible_training_candidates": 0,
        "selected_entry_filter": None,
        "selected_exit_config": None,
        "training_portfolio": None,
        "holdout_portfolio": None,
        "baseline_all_data_descriptive_only": None,
    }
    baseline_filter = {"name": "all_research_candidates"}
    result["baseline_all_data_descriptive_only"] = portfolio_replay(
        records, baseline_filter, BASELINE_CONFIG, initial_equity, allocation_pct
    )
    if len(train) < MIN_TRAIN_ROWS or len(holdout) < MIN_VALIDATION_ROWS:
        return result

    eligible = []
    evaluated = 0
    for entry_filter in candidate_entry_filters():
        subset = [r for r in train if entry_filter_matches(r, entry_filter)]
        if len(subset) < MIN_TRAIN_ROWS:
            continue
        for exit_config in candidate_configs():
            outcomes = [simulate_exit(r, exit_config) for r in subset]
            metrics = calculate_metrics(outcomes)
            evaluated += 1
            if _eligible_train(metrics):
                eligible.append({
                    "entry_filter": entry_filter,
                    "exit_config": exit_config,
                    "metrics": metrics,
                    "train_rows": len(subset),
                })
    result["evaluated_candidates"] = evaluated
    result["eligible_training_candidates"] = len(eligible)
    if not eligible:
        result["status"] = "no_profitable_training_candidate"
        return result
    eligible.sort(key=lambda row: (
        row["metrics"]["mean_pnl_pct"],
        row["metrics"]["median_pnl_pct"],
        row["metrics"]["profit_factor"],
        row["train_rows"],
    ), reverse=True)
    selected = eligible[0]
    holdout_portfolio = portfolio_replay(
        holdout, selected["entry_filter"], selected["exit_config"],
        initial_equity, allocation_pct
    )
    holdout_outcomes = [
        simulate_exit(r, selected["exit_config"])
        for r in holdout if entry_filter_matches(r, selected["entry_filter"])
    ]
    holdout_metrics = calculate_metrics(holdout_outcomes)
    result["selected_entry_filter"] = selected["entry_filter"]
    result["selected_exit_config"] = selected["exit_config"]
    result["training_trade_metrics"] = selected["metrics"]
    result["training_portfolio"] = portfolio_replay(
        train, selected["entry_filter"], selected["exit_config"],
        initial_equity, allocation_pct
    )
    result["holdout_trade_metrics"] = holdout_metrics
    result["holdout_portfolio"] = holdout_portfolio
    result["status"] = (
        "validated_profitable_out_of_sample"
        if _eligible_holdout(holdout_metrics) and holdout_portfolio["return_pct"] > 0
        else "holdout_not_profitable"
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paths", nargs="+", default=[
        "shadow_price_paths.jsonl", "paper_account/shadow_price_paths.jsonl"
    ])
    parser.add_argument("--initial-equity", type=float, default=1000.0)
    parser.add_argument("--allocation-pct", type=float, default=10.0)
    parser.add_argument("--output")
    args = parser.parse_args()
    records, rejected = load_paths(args.paths)
    report = analyze_portfolio(records, args.initial_equity, args.allocation_pct)
    report["rejected_invalid_sparse_or_short_paths"] = rejected
    report["source_files"] = args.paths
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
