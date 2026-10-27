#!/usr/bin/env python3
"""Walk-forward, paper-only backtest of delayed whale-follow entries on recorded quotes.

This module never uses interpolated prices, predicts intent as fact, changes the
active scanner threshold, or submits transactions. Wallet eligibility is computed
from realized swaps strictly before each signal to avoid future-information leaks.
"""
import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

from shadow_path_backtest import (
    BASELINE_CONFIG,
    MIN_TRAIN_ROWS,
    MIN_VALIDATION_ROWS,
    MAX_SAMPLE_GAP_SECONDS,
    MIN_PATH_DURATION_SECONDS,
    MIN_PRICE_PATH_SAMPLES,
    _eligible_holdout,
    _eligible_train,
    _normalized_path,
    calculate_metrics,
    simulate_exit,
)
from whale_copy_paper import apply_wallet_trades, wallet_qualifies

DEFAULT_DELAYS_SECONDS = (0, 30, 60, 120)
SIGNAL_MODES = ("all_tracked_buys", "two_wallet_consensus", "qualified_wallet_buy")
MAX_SIGNAL_AGE_SECONDS = 900.0
CONSENSUS_WINDOW_SECONDS = 300.0
QUOTE_SYMBOLS = {"SOL", "USDC", "USDT"}


def _finite(value):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def load_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, TypeError, ValueError):
        return {}


def load_paths(filenames):
    """Use one quality-valid observed quote path per mint, without filling gaps."""
    paths, rejected, seen = [], 0, set()
    for filename in filenames:
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
            path, error = _normalized_path(raw)
            if error:
                rejected += 1
                continue
            history = path["price_history"]
            gaps = [
                history[i]["timestamp"] - history[i - 1]["timestamp"]
                for i in range(1, len(history))
            ]
            if (
                len(history) < MIN_PRICE_PATH_SAMPLES
                or path["observed_duration_seconds"] < MIN_PATH_DURATION_SECONDS
                or not gaps
                or max(gaps) > MAX_SAMPLE_GAP_SECONDS
            ):
                rejected += 1
                continue
            mint = path["mint"]
            if mint in seen:
                continue
            seen.add(mint)
            paths.append(path)
    paths.sort(key=lambda row: (row["entry_timestamp"], row["mint"]))
    return paths, rejected


def normalize_swap_state(swap_state):
    """Normalize real recorded swaps; deduplicate per wallet/signature, not globally."""
    source = swap_state.get("wallets", {}) if isinstance(swap_state, dict) else {}
    if not isinstance(source, dict):
        return [], 0
    events, rejected = [], 0
    seen = set()
    for owner, record in source.items():
        if not isinstance(record, dict):
            continue
        swaps = record.get("normalized_swaps", [])
        if not isinstance(swaps, list):
            continue
        for raw in swaps:
            if not isinstance(raw, dict):
                rejected += 1
                continue
            wallet = str(raw.get("wallet") or owner).strip()
            mint = str(raw.get("mint") or "").strip()
            signature = str(raw.get("signature") or "").strip()
            timestamp = _finite(raw.get("timestamp"))
            token_amount = _finite(raw.get("token_amount"))
            quote_amount = _finite(raw.get("quote_amount"))
            side = raw.get("side")
            quote_symbol = str(raw.get("quote_symbol") or "").upper()
            if (
                not wallet or not mint or not signature or timestamp is None
                or timestamp <= 0 or token_amount is None or token_amount <= 0
                or quote_amount is None or quote_amount <= 0
                or side not in ("buy", "sell") or quote_symbol not in QUOTE_SYMBOLS
            ):
                rejected += 1
                continue
            key = (wallet, signature)
            if key in seen:
                continue
            seen.add(key)
            events.append({
                "wallet": wallet,
                "mint": mint,
                "signature": signature,
                "timestamp": timestamp,
                "side": side,
                "token_amount": token_amount,
                "quote_amount": quote_amount,
                "quote_symbol": quote_symbol,
            })
    events.sort(key=lambda item: (item["timestamp"], item["signature"], item["wallet"]))
    return events, rejected


def _wallet_metrics_as_of(wallet, timestamp, wallet_records, events_by_wallet):
    """Replay only the wallet's realized history up to this signal's timestamp."""
    record = wallet_records.get(wallet)
    if not isinstance(record, dict):
        return None
    if not record.get("history_complete", False) or record.get("history_truncated", False):
        return None
    prior = [
        event for event in events_by_wallet.get(wallet, [])
        if event["timestamp"] <= timestamp
    ]
    if not prior:
        return None
    ledger = {"history_complete": True, "history_truncated": False}
    apply_wallet_trades(ledger, prior, now=timestamp)
    metrics = ledger.get("metrics", {})
    return {
        **metrics,
        "qualifies": wallet_qualifies(metrics),
        "history_complete": True,
        "prior_swap_count": len(prior),
    }


def _select_signal(path, events_by_mint, signal_mode, wallet_records, events_by_wallet,
                   max_signal_age_seconds=MAX_SIGNAL_AGE_SECONDS,
                   consensus_window_seconds=CONSENSUS_WINDOW_SECONDS):
    if signal_mode not in SIGNAL_MODES:
        raise ValueError(f"unsupported signal mode: {signal_mode}")
    mint = path.get("mint")
    start = float(path["entry_timestamp"])
    samples = path.get("price_history") or []
    end = float(samples[-1]["timestamp"]) if samples else start
    min_signal_time = start - float(max_signal_age_seconds)
    events = events_by_mint.get(mint, [])
    buys = [event for event in events if event["side"] == "buy"]

    if signal_mode == "two_wallet_consensus":
        for later in buys:
            if later["timestamp"] < min_signal_time or later["timestamp"] > end:
                continue
            window = [
                event for event in buys
                if later["timestamp"] - consensus_window_seconds
                <= event["timestamp"] <= later["timestamp"]
                and event["wallet"] != later["wallet"]
            ]
            wallets = sorted({event["wallet"] for event in window} | {later["wallet"]})
            if len(wallets) >= 2:
                return {
                    "mint": mint,
                    "signal_mode": signal_mode,
                    "timestamp": later["timestamp"],
                    "wallet": later["wallet"],
                    "wallet_count": len(wallets),
                    "wallets": wallets,
                    "signal_signatures": sorted({
                        event["signature"] for event in window
                    } | {later["signature"]}),
                }
        return None

    for event in buys:
        if event["timestamp"] < min_signal_time or event["timestamp"] > end:
            continue
        if signal_mode == "qualified_wallet_buy":
            metrics = _wallet_metrics_as_of(
                event["wallet"], event["timestamp"], wallet_records, events_by_wallet
            )
            if not metrics or not metrics.get("qualifies"):
                continue
            return {
                "mint": mint,
                "signal_mode": signal_mode,
                "timestamp": event["timestamp"],
                "wallet": event["wallet"],
                "wallet_count": 1,
                "wallets": [event["wallet"]],
                "signal_signatures": [event["signature"]],
                "past_closed_trades": int(metrics.get("closed_trades", 0)),
                "past_win_rate": float(metrics.get("win_rate", 0.0)),
                "past_profit_factor": float(metrics.get("profit_factor", 0.0)),
                "past_mean_net_pnl_pct": float(metrics.get("mean_net_pnl_pct", 0.0)),
            }
        return {
            "mint": mint,
            "signal_mode": signal_mode,
            "timestamp": event["timestamp"],
            "wallet": event["wallet"],
            "wallet_count": 1,
            "wallets": [event["wallet"]],
            "signal_signatures": [event["signature"]],
        }
    return None


def select_signal_for_path(path, swap_state, signal_mode,
                           max_signal_age_seconds=MAX_SIGNAL_AGE_SECONDS,
                           consensus_window_seconds=CONSENSUS_WINDOW_SECONDS):
    events, _ = normalize_swap_state(swap_state)
    events_by_mint = defaultdict(list)
    events_by_wallet = defaultdict(list)
    for event in events:
        events_by_mint[event["mint"]].append(event)
        events_by_wallet[event["wallet"]].append(event)
    wallet_records = swap_state.get("wallets", {}) if isinstance(swap_state, dict) else {}
    return _select_signal(
        path, events_by_mint, signal_mode, wallet_records, events_by_wallet,
        max_signal_age_seconds, consensus_window_seconds,
    )


def prepare_entry_path(path, signal, delay_seconds=0, max_hold_seconds=None,
                       max_signal_age_seconds=MAX_SIGNAL_AGE_SECONDS):
    """Return an exit-ready path entered at a recorded quote after the signal delay."""
    delay = _finite(delay_seconds)
    if delay is None or delay < 0:
        return None, "invalid_delay"
    if not isinstance(signal, dict):
        return None, "missing_signal"
    signal_time = _finite(signal.get("timestamp"))
    if signal_time is None or signal_time <= 0:
        return None, "invalid_signal_timestamp"
    history = path.get("price_history")
    if not isinstance(history, list) or not history:
        return None, "missing_quote_history"
    entry_reference = _finite(path.get("entry_timestamp"))
    if entry_reference is None:
        return None, "invalid_path_entry"
    target = max(entry_reference, signal_time + delay)
    entry_index = next(
        (i for i, sample in enumerate(history)
         if _finite(sample.get("timestamp")) is not None
         and float(sample["timestamp"]) >= target),
        None,
    )
    if entry_index is None:
        return None, "no_quote_after_delay"
    sample = history[entry_index]
    entry_time = float(sample["timestamp"])
    if entry_time < signal_time or entry_time - signal_time > float(max_signal_age_seconds):
        return None, "signal_too_old_at_executable_quote"
    hold = float(max_hold_seconds if max_hold_seconds is not None else BASELINE_CONFIG["max_hold_seconds"])
    end_time = float(history[-1]["timestamp"])
    if hold <= 0 or end_time - entry_time < hold:
        return None, "censored_insufficient_forward_quotes"
    price = _finite(sample.get("price_usd"))
    liquidity = _finite(sample.get("liquidity_usd"))
    if price is None or price <= 0 or liquidity is None or liquidity < 0:
        return None, "invalid_entry_quote"
    prepared = {
        **path,
        "entry_timestamp": entry_time,
        "entry_price": price,
        "entry_liquidity_usd": liquidity,
        "observation_end_timestamp": end_time,
        "observed_duration_seconds": end_time - entry_time,
        "price_history": history[entry_index:],
        "whale_signal": dict(signal),
        "copy_delay_seconds": delay,
    }
    return prepared, None


def _portfolio_replay(paths, events_by_mint, wallet_records, events_by_wallet,
                      signal_mode, delay_seconds, initial_equity, allocation_pct):
    signal_count = 0
    reasons = Counter()
    proposed = []
    for path in paths:
        signal = _select_signal(
            path, events_by_mint, signal_mode, wallet_records, events_by_wallet
        )
        if signal is None:
            continue
        signal_count += 1
        prepared, reason = prepare_entry_path(
            path, signal, delay_seconds=delay_seconds,
            max_hold_seconds=BASELINE_CONFIG["max_hold_seconds"],
        )
        if reason:
            reasons[reason] += 1
            continue
        outcome = simulate_exit(prepared, BASELINE_CONFIG)
        proposed.append({
            "mint": path["mint"],
            "symbol": path.get("symbol", "?"),
            "signal_timestamp": signal["timestamp"],
            "signal_wallets": signal.get("wallets", []),
            "signal_wallet_count": signal.get("wallet_count", 0),
            "copy_delay_seconds": delay_seconds,
            "entry_timestamp": prepared["entry_timestamp"],
            "entry_price": prepared["entry_price"],
            "exit": outcome,
        })

    proposed.sort(key=lambda row: (row["entry_timestamp"], row["mint"]))
    equity = float(initial_equity)
    peak = equity
    max_drawdown_pct = 0.0
    available_at = float("-inf")
    executed = []
    skipped_busy = 0
    unresolved = 0
    allocation = float(allocation_pct) / 100.0
    for item in proposed:
        if item["entry_timestamp"] < available_at:
            skipped_busy += 1
            continue
        exit_result = item["exit"]
        if exit_result.get("status") != "closed" or _finite(exit_result.get("net_pnl_pct")) is None:
            unresolved += 1
            reasons["exit_not_closed"] += 1
            continue
        before = equity
        pnl_pct = float(exit_result["net_pnl_pct"])
        equity *= max(0.0, 1.0 + allocation * pnl_pct / 100.0)
        available_at = float(exit_result.get("exit_timestamp") or item["entry_timestamp"])
        peak = max(peak, equity)
        drawdown = ((peak - equity) / peak * 100.0) if peak else 0.0
        max_drawdown_pct = max(max_drawdown_pct, drawdown)
        executed.append({
            "status": "closed",
            "net_pnl_pct": pnl_pct,
            "reason": exit_result.get("reason"),
            "mint": item["mint"],
            "symbol": item["symbol"],
            "signal_timestamp": item["signal_timestamp"],
            "signal_wallets": item["signal_wallets"],
            "signal_wallet_count": item["signal_wallet_count"],
            "entry_timestamp": item["entry_timestamp"],
            "entry_price": item["entry_price"],
            "exit_timestamp": exit_result.get("exit_timestamp"),
            "exit_price": exit_result.get("exit_price"),
            "holding_seconds": exit_result.get("holding_seconds"),
            "equity_before": round(before, 6),
            "equity_after": round(equity, 6),
        })
    metrics = calculate_metrics(executed)
    portfolio = {
        "initial_equity": float(initial_equity),
        "ending_equity": round(equity, 6),
        "return_pct": round((equity / initial_equity - 1.0) * 100.0, 6)
            if initial_equity else 0.0,
        "max_drawdown_pct": round(max_drawdown_pct, 6),
        "allocation_pct": float(allocation_pct),
        "max_concurrent_positions": 1,
        "signals_found": signal_count,
        "entries_with_quotes": len(proposed),
        "closed_trades": len(executed),
        "skipped_while_capital_busy": skipped_busy,
        "unresolved_trades": unresolved,
        "rejection_reasons": dict(sorted(reasons.items())),
        "sample_trades": executed[:10],
    }
    return metrics, portfolio


def analyze_follow(paths, swap_state, initial_equity=1000.0, allocation_pct=10.0,
                   delays_seconds=DEFAULT_DELAYS_SECONDS, rejected_paths=0,
                   rejected_swaps=0):
    """Compare predeclared whale-follow rules with a chronological untouched holdout."""
    normalized_paths, local_rejected = [], 0
    seen_mints = set()
    for raw in paths or []:
        path, error = _normalized_path(raw)
        if error:
            local_rejected += 1
            continue
        history = path["price_history"]
        gaps = [history[i]["timestamp"] - history[i - 1]["timestamp"]
                for i in range(1, len(history))]
        if (len(history) < MIN_PRICE_PATH_SAMPLES
                or path["observed_duration_seconds"] < MIN_PATH_DURATION_SECONDS
                or not gaps or max(gaps) > MAX_SAMPLE_GAP_SECONDS):
            local_rejected += 1
            continue
        if path["mint"] in seen_mints:
            continue
        seen_mints.add(path["mint"])
        normalized_paths.append(path)
    normalized_paths.sort(key=lambda row: (row["entry_timestamp"], row["mint"]))

    events, malformed = normalize_swap_state(swap_state)
    wallet_records = swap_state.get("wallets", {}) if isinstance(swap_state, dict) else {}
    if not isinstance(wallet_records, dict):
        wallet_records = {}
    events_by_mint = defaultdict(list)
    events_by_wallet = defaultdict(list)
    for event in events:
        events_by_mint[event["mint"]].append(event)
        events_by_wallet[event["wallet"]].append(event)

    split = int(len(normalized_paths) * 0.7)
    train_paths, holdout_paths = normalized_paths[:split], normalized_paths[split:]
    report = {
        "status": "insufficient_history",
        "mode": "recorded_whale_swap_follow_walk_forward",
        "research_only": True,
        "live_trading_authorized": False,
        "active_strategy_changed": False,
        "signal_modes": list(SIGNAL_MODES),
        "delays_seconds": list(delays_seconds),
        "exit_config": dict(BASELINE_CONFIG),
        "paths_received": len(paths or []),
        "usable_paths": len(normalized_paths),
        "rejected_invalid_sparse_or_gapped_paths": int(rejected_paths) + local_rejected,
        "wallet_swap_events": len(events),
        "rejected_malformed_or_unsupported_swaps": int(rejected_swaps) + malformed,
        "wallets_with_swap_events": len(events_by_wallet),
        "unique_wallet_tokens_with_swaps": len(events_by_mint),
        "paths_sharing_mints_with_wallet_swaps": sum(
            1 for path in normalized_paths if path["mint"] in events_by_mint
        ),
        "train_paths": len(train_paths),
        "holdout_paths": len(holdout_paths),
        "min_train_closed_trades": MIN_TRAIN_ROWS,
        "min_holdout_closed_trades": MIN_VALIDATION_ROWS,
        "allocation_pct": float(allocation_pct),
        "selected_candidate": None,
        "eligible_training_candidates": 0,
        "candidates": [],
        "interpretation_limits": [
            "Entries occur only at the first actually recorded quote after the configured signal delay; no price interpolation is used.",
            "Wallet qualification is replayed only from recorded swaps at or before that wallet's buy signal.",
            "The exit rules and one-slot portfolio sizing are held fixed; no candidate is chosen by holdout results.",
            "The dataset is filtered to one valid observed price path per mint, so repeated scans do not inflate the sample count.",
            "Historical wallet swap prices and recorded DEX USD quotes may differ due execution price, routing, slippage and quote timing.",
            "A profitable backtest is not proof of future profitability; live trading is never authorized by this module.",
        ],
    }
    if len(train_paths) < MIN_TRAIN_ROWS or len(holdout_paths) < MIN_VALIDATION_ROWS:
        report["status"] = "insufficient_history"
        return report

    for signal_mode in SIGNAL_MODES:
        for delay in delays_seconds:
            train_metrics, train_portfolio = _portfolio_replay(
                train_paths, events_by_mint, wallet_records, events_by_wallet,
                signal_mode, float(delay), initial_equity, allocation_pct,
            )
            holdout_metrics, holdout_portfolio = _portfolio_replay(
                holdout_paths, events_by_mint, wallet_records, events_by_wallet,
                signal_mode, float(delay), initial_equity, allocation_pct,
            )
            train_eligible = _eligible_train(train_metrics)
            holdout_eligible = (
                _eligible_holdout(holdout_metrics) and holdout_portfolio["return_pct"] > 0
            )
            report["candidates"].append({
                "signal_mode": signal_mode,
                "delay_seconds": float(delay),
                "training": train_metrics,
                "training_portfolio": {k: v for k, v in train_portfolio.items() if k != "sample_trades"},
                "holdout": holdout_metrics,
                "holdout_portfolio": {k: v for k, v in holdout_portfolio.items() if k != "sample_trades"},
                "eligible_training": train_eligible,
                "eligible_holdout": holdout_eligible,
                "validated": False,
            })

    eligible = [row for row in report["candidates"] if row["eligible_training"]]
    report["eligible_training_candidates"] = len(eligible)
    if not eligible:
        report["status"] = "no_profitable_training_candidate"
        return report
    eligible.sort(key=lambda row: (
        row["training"]["mean_pnl_pct"],
        row["training"]["median_pnl_pct"],
        row["training"]["profit_factor"],
        row["training"]["n"],
    ), reverse=True)
    selected = eligible[0]
    selected["validated"] = bool(
        selected["eligible_holdout"]
        and selected["holdout_portfolio"]["return_pct"] > 0
    )
    report["selected_candidate"] = {
        key: value for key, value in selected.items()
        if key not in ("training_portfolio", "holdout_portfolio")
    } | {
        "training_portfolio": selected["training_portfolio"],
        "holdout_portfolio": selected["holdout_portfolio"],
    }
    report["status"] = "validated_profitable_out_of_sample" if selected["validated"] else "holdout_not_profitable"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paths", nargs="+", default=["paper_account/whale_quote_paths.jsonl", "paper_account/shadow_price_paths.jsonl"])
    parser.add_argument("--swaps", default="whale_copy_state.json")
    parser.add_argument("--output")
    parser.add_argument("--initial-equity", type=float, default=1000.0)
    parser.add_argument("--allocation-pct", type=float, default=10.0)
    args = parser.parse_args()
    swap_state = load_json(args.swaps)
    paths, rejected_paths = load_paths(args.paths)
    # analyze_follow accepts the already normalized paths and keeps validation
    # failures visible instead of silently treating sparse data as a trade result.
    report = analyze_follow(
        paths, swap_state, initial_equity=args.initial_equity,
        allocation_pct=args.allocation_pct, rejected_paths=rejected_paths,
    )
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
    if args.output:
        Path(args.output).write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
