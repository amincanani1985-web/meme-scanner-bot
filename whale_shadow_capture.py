#!/usr/bin/env python3
"""Passive, paper-only capture of observed quotes after tracked-wallet buy activity.

This module does not sign transactions, open positions, create historical prices,
or authorize trades. It records only quotes already returned by the scanner's
read-only market-data cache so a later walk-forward test can measure copy delay.
"""
import json
import math
import os
import time
from pathlib import Path

from shadow_path_backtest import (
    BASELINE_CONFIG,
    MAX_SAMPLE_GAP_SECONDS,
    MIN_PATH_DURATION_SECONDS,
    MIN_PRICE_PATH_SAMPLES,
    _normalized_path,
    simulate_exit,
)

STATE_FILENAME = "whale_quote_capture_state.json"
PATHS_FILENAME = "whale_quote_paths.jsonl"
SWAPS_FILENAME = "whale_copy_state.json"
MAX_ACTIVE_PATHS = 30
MAX_NEW_MINTS_PER_PASS = 5
MAX_COMPLETED_MINTS = 5000
SIGNAL_WINDOW_SECONDS = 900.0
CONSENSUS_WINDOW_SECONDS = 300.0
CAPTURE_HORIZON_SECONDS = 900.0
VALID_QUOTES = {"SOL", "USDC", "USDT"}


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _load_json(path, default):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else default
    except (OSError, TypeError, ValueError):
        return default


def _atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def recent_buy_signals(swap_state, now=None, window_seconds=SIGNAL_WINDOW_SECONDS):
    """Return valid observed buy clusters, counting unique wallet addresses only."""
    now = float(time.time() if now is None else now)
    window_seconds = max(0.0, float(window_seconds))
    records = swap_state.get("wallets", {}) if isinstance(swap_state, dict) else {}
    if not isinstance(records, dict):
        return {}, 0

    unique = {}
    rejected = 0
    seen = set()
    for owner, record in records.items():
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
                or timestamp <= 0 or timestamp > now or timestamp < now - window_seconds
                or token_amount is None or token_amount <= 0
                or quote_amount is None or quote_amount <= 0
                or side != "buy" or quote_symbol not in VALID_QUOTES
            ):
                rejected += 1
                continue
            key = (wallet, signature)
            if key in seen:
                continue
            seen.add(key)
            unique[(wallet, signature)] = {
                "wallet": wallet,
                "mint": mint,
                "timestamp": timestamp,
                "signature": signature,
                "quote_symbol": quote_symbol,
            }

    by_mint = {}
    for item in unique.values():
        by_mint.setdefault(item["mint"], []).append(item)

    signals = {}
    for mint, events in by_mint.items():
        events.sort(key=lambda row: (row["timestamp"], row["wallet"], row["signature"]))
        latest_time = max(row["timestamp"] for row in events)
        consensus_events = [
            row for row in events
            if latest_time - CONSENSUS_WINDOW_SECONDS <= row["timestamp"] <= latest_time
        ]
        wallets = sorted({row["wallet"] for row in consensus_events})
        signals[mint] = {
            "mint": mint,
            "signal_timestamp": latest_time,
            "wallets": wallets,
            "wallet_count": len(wallets),
            "buy_event_count": len(events),
            "signal_mode": "two_wallet_consensus" if len(wallets) >= 2 else "all_tracked_buys",
            "signal_signatures": sorted({row["signature"] for row in consensus_events}),
        }
    return signals, rejected


def _root_paths(root):
    root = Path(root).resolve()
    paper = root / "paper_account"
    return root, paper, paper / STATE_FILENAME, paper / PATHS_FILENAME


def _fresh_state():
    return {
        "version": 1,
        "active": {},
        "completed_mints": [],
        "path_log_offset": 0,
        "counters": {
            "paths_started": 0,
            "paths_completed": 0,
            "paths_rejected": 0,
            "quotes_failed": 0,
            "samples_recorded": 0,
        },
        "last_run": None,
        "research_only": True,
        "live_trading_authorized": False,
    }


def _sync_completed_mints(state, paths_path):
    """Read appended JSONL records since the last offset, recovering after a crash."""
    completed = state.setdefault("completed_mints", [])
    if not isinstance(completed, list):
        completed = []
        state["completed_mints"] = completed
    completed_set = {str(mint) for mint in completed if isinstance(mint, str)}
    offset = int(max(0, _finite(state.get("path_log_offset", 0)) or 0))
    changed = False
    try:
        with Path(paths_path).open("rb") as handle:
            size = Path(paths_path).stat().st_size
            if offset > size:
                offset = 0
                changed = True
            handle.seek(offset)
            while True:
                start = handle.tell()
                line = handle.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    handle.seek(start)
                    break
                try:
                    row = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    continue
                mint = row.get("mint") if isinstance(row, dict) else None
                if isinstance(mint, str) and mint.strip() and mint not in completed_set:
                    completed_set.add(mint)
                    completed.append(mint)
                    changed = True
            new_offset = handle.tell()
            if new_offset != offset:
                state["path_log_offset"] = new_offset
                changed = True
    except OSError:
        return changed
    state["completed_mints"] = completed[-MAX_COMPLETED_MINTS:]
    if len(completed) > MAX_COMPLETED_MINTS:
        changed = True
    return changed


def _load_capture(root):
    root, paper, state_path, paths_path = _root_paths(root)
    paper.mkdir(parents=True, exist_ok=True)
    fresh = _fresh_state()
    state = _load_json(state_path, fresh)
    state.setdefault("version", 1)
    if not isinstance(state.get("active"), dict):
        state["active"] = {}
    if not isinstance(state.get("completed_mints"), list):
        state["completed_mints"] = []
    if not isinstance(state.get("counters"), dict):
        state["counters"] = fresh["counters"]
    for key, value in fresh["counters"].items():
        state["counters"].setdefault(key, value)
    state["research_only"] = True
    state["live_trading_authorized"] = False
    changed = _sync_completed_mints(state, paths_path)
    if changed or not state_path.exists():
        _atomic_json(state_path, state)
    return root, paper, state_path, paths_path, state


def _current_signals(root, now):
    swap_state = _load_json(Path(root) / SWAPS_FILENAME, {"wallets": {}})
    return recent_buy_signals(swap_state, now=now, window_seconds=SIGNAL_WINDOW_SECONDS)


def _candidate_mints(state, signals, max_new_mints=MAX_NEW_MINTS_PER_PASS):
    active = state.get("active", {})
    completed = {str(mint) for mint in state.get("completed_mints", [])}
    candidates = [row for mint, row in signals.items() if mint not in active and mint not in completed]
    candidates.sort(key=lambda row: (
        row["wallet_count"],
        row["signal_timestamp"],
        row["buy_event_count"],
        row["mint"],
    ), reverse=True)
    return [row["mint"] for row in candidates[:max(0, int(max_new_mints))]]


def monitored_mints(root, now=None, max_new_mints=MAX_NEW_MINTS_PER_PASS):
    """Mints to add to the existing quote batch; this never discovers trade candidates."""
    now = float(time.time() if now is None else now)
    root, _, state_path, _, state = _load_capture(root)
    signals, _ = _current_signals(root, now)
    active = list(state.get("active", {}).keys())
    new_mints = _candidate_mints(state, signals, max_new_mints=max_new_mints)
    return list(dict.fromkeys([*active, *new_mints]))


def _quote_fields(pair):
    if not isinstance(pair, dict):
        return None
    price = _finite(pair.get("priceUsd"))
    liquidity_info = pair.get("liquidity")
    liquidity = _finite(liquidity_info.get("usd")) if isinstance(liquidity_info, dict) else None
    base_token = pair.get("baseToken")
    symbol = base_token.get("symbol") if isinstance(base_token, dict) else None
    if price is None or price <= 0 or liquidity is None or liquidity < 0:
        return None
    return price, liquidity, str(symbol or "?")


def _path_quality(record):
    path, error = _normalized_path(record)
    if error:
        return False, error
    history = path["price_history"]
    gaps = [
        history[i]["timestamp"] - history[i - 1]["timestamp"]
        for i in range(1, len(history))
    ]
    if len(history) < MIN_PRICE_PATH_SAMPLES:
        return False, "insufficient_samples"
    if path["observed_duration_seconds"] < MIN_PATH_DURATION_SECONDS:
        return False, "insufficient_observation_horizon"
    if not gaps or any(gap < 0 or gap > MAX_SAMPLE_GAP_SECONDS for gap in gaps):
        return False, "sparse_or_non_chronological_quotes"
    return True, None


def _append_path(paths_path, state, record):
    paths_path = Path(paths_path)
    paths_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    with paths_path.open("a", encoding="utf-8") as handle:
        handle.write(encoded + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    state["path_log_offset"] = paths_path.stat().st_size


def sample(root, quote_fetcher, now=None, candidate_mints=None, max_new_mints=MAX_NEW_MINTS_PER_PASS):
    """Append one real scanner quote per mint/pass and finalize paths after 15 minutes."""
    now = float(time.time() if now is None else now)
    root, paper, state_path, paths_path, state = _load_capture(root)
    signals, rejected = _current_signals(root, now)
    candidates = (
        list(candidate_mints) if candidate_mints is not None
        else _candidate_mints(state, signals, max_new_mints=max_new_mints)
    )
    active = state["active"]
    completed = set(state["completed_mints"])
    targets = list(dict.fromkeys([*active.keys(), *candidates]))
    result = {
        "mode": "passive_whale_quote_capture",
        "timestamp": now,
        "active_paths": len(active),
        "new_paths_started": 0,
        "paths_completed": 0,
        "paths_rejected": 0,
        "quotes_failed": 0,
        "samples_recorded": 0,
        "rejected_swap_rows": rejected,
        "research_only": True,
        "live_trading_authorized": False,
        "trade_signal": False,
    }

    for mint in targets:
        mint = str(mint or "").strip()
        if not mint:
            continue
        is_active = mint in active
        if not is_active:
            if mint in completed or mint not in signals or len(active) >= MAX_ACTIVE_PATHS:
                continue
        try:
            pair = quote_fetcher(mint)
        except Exception:
            pair = None
        quote_fields = _quote_fields(pair)
        if quote_fields is None:
            result["quotes_failed"] += 1
            state["counters"]["quotes_failed"] += 1
            continue
        price, liquidity, symbol = quote_fields

        if not is_active:
            signal = signals[mint]
            record = {
                "schema_version": 1,
                "mint": mint,
                "symbol": symbol,
                "entry_timestamp": now,
                "entry_price": price,
                "entry_liquidity_usd": liquidity,
                "signal_timestamp": signal["signal_timestamp"],
                "signal_wallets": signal["wallets"],
                "signal_wallet_count": signal["wallet_count"],
                "signal_mode": signal["signal_mode"],
                "signal_signatures": signal["signal_signatures"],
                "source": "tracked_whale_observed_quote_capture",
                "prospective_entry_filters": ["tracked_whale_buy_capture_v1"],
                "price_history": [],
                "research_only": True,
                "live_trading_authorized": False,
            }
            active[mint] = record
            state["counters"]["paths_started"] += 1
            result["new_paths_started"] += 1
        else:
            record = active[mint]
            record["symbol"] = symbol if symbol != "?" else record.get("symbol", "?")

        history = record.setdefault("price_history", [])
        if not history or now > float(history[-1].get("timestamp", 0)):
            history.append({
                "timestamp": now,
                "price_usd": price,
                "liquidity_usd": liquidity,
            })
            if len(history) > 128:
                # Keep the start anchor plus the newest observed points. This
                # bounds state size without inventing points between observations.
                history[:] = [history[0], *history[-127:]]
            state["counters"]["samples_recorded"] += 1
            result["samples_recorded"] += 1

        record["observation_end_timestamp"] = float(history[-1]["timestamp"])
        record["observed_duration_seconds"] = max(
            0.0, record["observation_end_timestamp"] - float(record["entry_timestamp"])
        )

        if record["observed_duration_seconds"] < CAPTURE_HORIZON_SECONDS:
            continue

        quality_ok, reason = _path_quality(record)
        if quality_ok:
            outcome = simulate_exit(record, BASELINE_CONFIG)
            if outcome.get("status") == "closed":
                record["benchmark_exit"] = {
                    "reason": outcome.get("reason"),
                    "timestamp": outcome.get("exit_timestamp"),
                    "price_usd": outcome.get("exit_price_usd"),
                    "net_pnl_pct": outcome.get("net_pnl_pct"),
                }
            _append_path(paths_path, state, record)
            if mint not in state["completed_mints"]:
                state["completed_mints"].append(mint)
            result["paths_completed"] += 1
            state["counters"]["paths_completed"] += 1
            del active[mint]
            # The JSONL record is durable before state is saved. _sync_completed_mints
            # recovers it if the process exits before the atomic state write.
            state["path_log_offset"] = Path(paths_path).stat().st_size
        else:
            result["paths_rejected"] += 1
            state["counters"]["paths_rejected"] += 1
            state["counters"].setdefault("rejection_reasons", {})
            reasons = state["counters"]["rejection_reasons"]
            reasons[reason or "quality_rejected"] = reasons.get(reason or "quality_rejected", 0) + 1
            del active[mint]

    state["completed_mints"] = state["completed_mints"][-MAX_COMPLETED_MINTS:]
    state["last_run"] = now
    result["active_paths"] = len(active)
    _atomic_json(state_path, state)
    return result


if __name__ == "__main__":
    root_dir = Path(__file__).resolve().parent
    print(json.dumps(sample(
        root_dir,
        quote_fetcher=lambda mint: None,
    ), sort_keys=True, allow_nan=False))
