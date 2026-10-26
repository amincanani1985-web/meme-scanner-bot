#!/usr/bin/env python3
"""Transaction-level whale research with an isolated paper-copy account.

This module never signs, submits, or recommends real transactions. It replaces
holder-balance guesses with observed swap transactions, tracks realized wallet
performance conservatively, and paper-follows only corroborated buys from wallets
that have a minimum out-of-sample-like realized history.
"""
import csv
import json
import math
import os
import statistics
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
import meme_scanner_cycle as scanner


HELIUS_ENHANCED_URL = "https://api-mainnet.helius-rpc.com/v0/addresses/{wallet}/transactions"
REQUEST_TIMEOUT_SECONDS = 8
MAX_WALLETS_PER_CYCLE = 8
MAX_TRANSACTIONS_PER_WALLET = 100
REQUEST_MIN_INTERVAL_SECONDS = 0.25
DEFAULT_RATE_LIMIT_BACKOFF_SECONDS = 60.0
MAX_RATE_LIMIT_BACKOFF_SECONDS = 300.0
MAX_STORED_SIGNATURES = 1500
MAX_STORED_CLOSED_TRADES = 250
MAX_NORMALIZED_SWAPS = 600
MAX_SIGNAL_AGE_SECONDS = 600
COPY_CONFIRMATION_WINDOW_SECONDS = 900
MIN_WALLET_CLOSED_TRADES = 8
MIN_WALLET_WIN_RATE = 0.55
MIN_WALLET_PROFIT_FACTOR = 1.25
MIN_WALLET_MEAN_NET_PNL_PCT = 0.5
WALLET_FEE_RESERVE_PCT = 1.0
COPY_POSITION_EQUITY_FRACTION = 0.005
COPY_PORTFOLIO_EXPOSURE_FRACTION = 0.02
COPY_MAX_CONCURRENT_POSITIONS = 4
COPY_MAX_DRAWDOWN_FRACTION = 0.20
COPY_MIN_LIQUIDITY_USD = 25_000.0
COPY_MIN_VOLUME_H1_USD = 10_000.0
COPY_MIN_PRICE_CHANGE_M5_PCT = -5.0
COPY_MAX_PRICE_CHANGE_M5_PCT = 8.0
COPY_MAX_PRICE_DRIFT_PCT = 5.0
COPY_MIN_SIGNAL_WALLETS = 2
COPY_MAX_HOLD_SECONDS = scanner.MAX_HOLD_SECONDS
COPY_HARD_STOP_LOSS_PCT = scanner.HARD_STOP_LOSS_PCT
COPY_TRAILING_STOP_PCT = scanner.TRAILING_STOP_PCT

WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTE_MINTS = {WSOL_MINT: "SOL", USDC_MINT: "USDC", USDT_MINT: "USDT"}
NATIVE_QUOTE = "SOL"


def _finite_number(value):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _token_amount(item):
    """Read a UI amount, accepting Helius rawTokenAmount and tokenAmount forms."""
    if not isinstance(item, dict):
        return None
    raw = item.get("rawTokenAmount")
    if isinstance(raw, dict) and raw.get("tokenAmount") is not None:
        amount = _finite_number(raw.get("tokenAmount"))
        decimals = _finite_number(raw.get("decimals", 0))
        if amount is None or decimals is None or decimals < 0 or decimals > 18:
            return None
        return amount / (10 ** int(decimals))
    amount = _finite_number(item.get("tokenAmount", item.get("amount")))
    return amount


def _native_sol_amount(value):
    """Helius nativeInput/nativeOutput amounts are represented in lamports."""
    raw = value.get("amount") if isinstance(value, dict) else value
    amount = _finite_number(raw)
    if amount is None or amount < 0:
        return None
    return amount / 1_000_000_000.0


def _user_side_items(items, wallet):
    if not isinstance(items, list):
        return []
    valid = [item for item in items if isinstance(item, dict) and item.get("mint")]
    matching = [item for item in valid if item.get("userAccount") == wallet]
    # Enhanced SWAP arrays normally carry the wallet in userAccount. Some API
    # versions omit it; in that case the address-filtered transaction remains the
    # source of truth rather than treating an unrelated account as the owner.
    if matching:
        return matching
    if any(item.get("userAccount") for item in valid):
        return []
    return valid


def parse_swap_transaction(transaction, wallet):
    """Parse one successful Helius enhanced SWAP into a wallet-side token trade.

    Only swaps against native SOL, wrapped SOL, USDC, or USDT are accepted. Token
    transfers, ambiguous token-to-token hops, failed transactions, and malformed
    amounts are ignored instead of being labelled as buys or sells.
    """
    if not isinstance(transaction, dict) or not isinstance(wallet, str) or not wallet:
        return None
    tx_type = str(transaction.get("type") or "").upper()
    if tx_type != "SWAP" or transaction.get("transactionError"):
        return None
    events = transaction.get("events") or {}
    swap = events.get("swap") if isinstance(events, dict) else None
    if not isinstance(swap, dict):
        return None
    signature = transaction.get("signature")
    timestamp = _finite_number(transaction.get("timestamp", transaction.get("blockTime")))
    if not signature or timestamp is None or timestamp <= 0:
        return None

    inputs = _user_side_items(swap.get("tokenInputs"), wallet)
    outputs = _user_side_items(swap.get("tokenOutputs"), wallet)
    nonquote_inputs = [item for item in inputs if item.get("mint") not in QUOTE_MINTS]
    nonquote_outputs = [item for item in outputs if item.get("mint") not in QUOTE_MINTS]
    native_input = _native_sol_amount(swap.get("nativeInput")) if swap.get("nativeInput") is not None else None
    native_output = _native_sol_amount(swap.get("nativeOutput")) if swap.get("nativeOutput") is not None else None

    side = None
    mint = None
    token_amount = None
    quote_amount = None
    quote_symbol = None

    # Buy: quote asset enters the swap and one non-quote token exits to the wallet.
    if len(nonquote_outputs) == 1 and not nonquote_inputs:
        token = nonquote_outputs[0]
        token_amount = _token_amount(token)
        quote_inputs = [item for item in inputs if item.get("mint") in QUOTE_MINTS]
        if native_input is not None and native_input > 0:
            quote_amount, quote_symbol = native_input, NATIVE_QUOTE
        elif len(quote_inputs) == 1:
            quote_amount = _token_amount(quote_inputs[0])
            quote_symbol = QUOTE_MINTS.get(quote_inputs[0].get("mint"))
        if quote_amount is not None and quote_amount > 0 and token_amount is not None and token_amount > 0:
            side, mint = "buy", token.get("mint")

    # Sell: one non-quote token enters and a quote asset exits to the wallet.
    if side is None and len(nonquote_inputs) == 1 and not nonquote_outputs:
        token = nonquote_inputs[0]
        token_amount = _token_amount(token)
        quote_outputs = [item for item in outputs if item.get("mint") in QUOTE_MINTS]
        if native_output is not None and native_output > 0:
            quote_amount, quote_symbol = native_output, NATIVE_QUOTE
        elif len(quote_outputs) == 1:
            quote_amount = _token_amount(quote_outputs[0])
            quote_symbol = QUOTE_MINTS.get(quote_outputs[0].get("mint"))
        if quote_amount is not None and quote_amount > 0 and token_amount is not None and token_amount > 0:
            side, mint = "sell", token.get("mint")

    if side is None or not mint or not quote_symbol:
        return None
    return {
        "signature": str(signature),
        "timestamp": float(timestamp),
        "wallet": wallet,
        "mint": str(mint),
        "side": side,
        "token_amount": float(token_amount),
        "quote_amount": float(quote_amount),
        "quote_symbol": str(quote_symbol),
        "quote_price_per_token": float(quote_amount) / float(token_amount),
    }


def calculate_wallet_metrics(closed_trades):
    """Compute realized wallet performance net of the configured fee reserve."""
    trades = [trade for trade in (closed_trades or [])
              if isinstance(trade, dict) and _finite_number(trade.get("net_pnl_pct")) is not None]
    outcomes = [float(trade["net_pnl_pct"]) for trade in trades]
    wins = [value for value in outcomes if value > 0]
    losses = [value for value in outcomes if value < 0]
    gross_wins = sum(wins)
    gross_losses = abs(sum(losses))
    return {
        "closed_trades": len(outcomes),
        "wins": len(wins),
        "win_rate": len(wins) / len(outcomes) if outcomes else 0.0,
        "profit_factor": gross_wins / gross_losses if gross_losses else (99.0 if gross_wins else 0.0),
        "mean_net_pnl_pct": sum(outcomes) / len(outcomes) if outcomes else 0.0,
        "median_net_pnl_pct": statistics.median(outcomes) if outcomes else 0.0,
    }


def wallet_qualifies(metrics):
    """Require a substantive, positive realized history before copying a wallet."""
    if not isinstance(metrics, dict):
        return False
    n = int(metrics.get("closed_trades", 0) or 0)
    win_rate = _finite_number(metrics.get("win_rate"))
    profit_factor = _finite_number(metrics.get("profit_factor"))
    mean_pnl = _finite_number(metrics.get("mean_net_pnl_pct"))
    return bool(
        n >= MIN_WALLET_CLOSED_TRADES
        and win_rate is not None and win_rate >= MIN_WALLET_WIN_RATE
        and profit_factor is not None and profit_factor >= MIN_WALLET_PROFIT_FACTOR
        and mean_pnl is not None and mean_pnl >= MIN_WALLET_MEAN_NET_PNL_PCT
    )


def signal_is_fresh(timestamp, now=None, max_age_seconds=MAX_SIGNAL_AGE_SECONDS):
    ts = _finite_number(timestamp)
    now = time.time() if now is None else _finite_number(now)
    age = max_age_seconds
    if ts is None or now is None:
        return False
    return 0 <= now - ts <= age


def _prepare_wallet_record_for_replay(wallet_record):
    """Initialize a replayable event ledger; quarantine legacy state with unknown order."""
    if not isinstance(wallet_record, dict):
        raise TypeError("wallet_record must be a dictionary")
    if "normalized_swaps" in wallet_record and isinstance(wallet_record.get("normalized_swaps"), list):
        wallet_record.setdefault("positions", {})
        wallet_record.setdefault("closed_trades", [])
        wallet_record.setdefault("seen_signatures", [])
        return

    legacy_positions = wallet_record.get("positions") or {}
    legacy_signatures = wallet_record.get("seen_signatures") or []
    legacy_closed = wallet_record.get("closed_trades") or []
    legacy_metrics = wallet_record.get("metrics") or {}
    if legacy_signatures or legacy_positions:
        # Old records were mutated as pages arrived, so a later older-page
        # backfill can invalidate their realized PnL and open cost basis. Preserve
        # a small audit summary but never let the unreplayable metrics qualify a
        # wallet for copy trading.
        wallet_record["legacy_unreconstructed"] = {
            "closed_trades": len(legacy_closed),
            "open_positions": len(legacy_positions) if isinstance(legacy_positions, dict) else 0,
            "seen_signatures": len(legacy_signatures) if isinstance(legacy_signatures, list) else 0,
            "prior_metrics": dict(legacy_metrics) if isinstance(legacy_metrics, dict) else {},
            "reason": "missing_normalized_event_archive",
        }
        wallet_record["legacy_baseline_closed_trades"] = []
        wallet_record["positions"] = {}
        wallet_record["closed_trades"] = []
        wallet_record["seen_signatures"] = []
        # Rebuild from the newest page and walk back again in timestamp order.
        wallet_record.pop("history_cursor", None)
        wallet_record["history_complete"] = False
    else:
        # An explicit seeded ledger with no prior signature/position state can be
        # used as a baseline (for tests or imported closed-trade summaries).
        wallet_record["legacy_baseline_closed_trades"] = [
            dict(trade) for trade in legacy_closed if isinstance(trade, dict)
        ]
        wallet_record.setdefault("positions", {})
        wallet_record.setdefault("closed_trades", [])
        wallet_record.setdefault("seen_signatures", [])
    wallet_record["normalized_swaps"] = []


def apply_wallet_trades(wallet_record, trades, now=None, fee_reserve_pct=WALLET_FEE_RESERVE_PCT):
    """Merge newly fetched swaps and rebuild realized PnL in transaction-time order.

    Backfill pages arrive newest-first across polling cycles. Replaying the normalized
    swap ledger chronologically prevents a late-arriving historical buy from being
    incorrectly excluded from the cost basis of a later sell.
    """
    if not isinstance(wallet_record, dict):
        raise TypeError("wallet_record must be a dictionary")
    _prepare_wallet_record_for_replay(wallet_record)
    now = time.time() if now is None else float(now)
    wallet_record.setdefault("positions", {})
    wallet_record.setdefault("seen_signatures", [])
    wallet_record.setdefault("closed_trades", [])

    prior_seen_order = [str(signature) for signature in wallet_record["seen_signatures"]]
    prior_seen = set(prior_seen_order)
    seen_order = list(prior_seen_order)
    normalized = wallet_record.get("normalized_swaps", [])
    if not isinstance(normalized, list):
        normalized = []
    normalized_by_signature = {}
    for item in normalized:
        if not isinstance(item, dict):
            continue
        signature = item.get("signature")
        if signature:
            normalized_by_signature[str(signature)] = item

    fresh_signatures = set()
    for trade in (trades or []):
        if not isinstance(trade, dict):
            continue
        signature = trade.get("signature")
        mint = trade.get("mint")
        side = trade.get("side")
        timestamp = _finite_number(trade.get("timestamp"))
        token_amount = _finite_number(trade.get("token_amount"))
        quote_amount = _finite_number(trade.get("quote_amount"))
        quote_symbol = trade.get("quote_symbol")
        if not signature or not mint or timestamp is None:
            continue
        signature = str(signature)
        if signature in normalized_by_signature:
            continue
        # A signature may exist in an older version's dedupe ring but not in its
        # normalized ledger. Admit it for backfill accounting, but don't replay it
        # as a new copy signal.
        was_seen = signature in prior_seen
        if side not in ("buy", "sell") or token_amount is None or quote_amount is None:
            if not was_seen:
                seen_order.append(signature)
            continue
        if token_amount <= 0 or quote_amount <= 0 or quote_symbol not in ("SOL", "USDC", "USDT"):
            if not was_seen:
                seen_order.append(signature)
            continue
        normalized_item = {
            "signature": signature,
            "timestamp": float(timestamp),
            "wallet": str(trade.get("wallet") or ""),
            "mint": str(mint),
            "side": side,
            "token_amount": float(token_amount),
            "quote_amount": float(quote_amount),
            "quote_symbol": str(quote_symbol),
            "quote_price_per_token": float(quote_amount) / float(token_amount),
        }
        normalized_by_signature[signature] = normalized_item
        if not was_seen:
            fresh_signatures.add(signature)
            seen_order.append(signature)

    all_swaps = sorted(
        normalized_by_signature.values(),
        key=lambda item: (float(item.get("timestamp", 0.0) or 0.0), str(item.get("signature", ""))),
    )
    if len(all_swaps) > MAX_NORMALIZED_SWAPS:
        # Never claim that a truncated history is a complete, qualified wallet
        # history. Keep the newest bounded window, expose the condition in state,
        # and disable follow signals until a complete ledger is available again.
        all_swaps = all_swaps[-MAX_NORMALIZED_SWAPS:]
        wallet_record["history_truncated"] = True
    wallet_record["normalized_swaps"] = all_swaps

    positions = {}
    closed_trades = [
        dict(trade) for trade in (wallet_record.get("legacy_baseline_closed_trades") or [])
        if isinstance(trade, dict)
    ]
    buy_signals = []
    sell_signals = []
    new_closed = 0
    history_eligible = bool(
        wallet_record.get("history_complete", False)
        and not wallet_record.get("history_truncated", False)
    )
    wallet_record["seen_signatures"] = list(dict.fromkeys(seen_order))[-MAX_STORED_SIGNATURES:]

    if not history_eligible:
        # Until historical backfill reaches the end, keep any legacy/current ledger
        # intact. A partial ledger must never be mistaken for complete realized
        # performance, but fresh leader sells still need to be visible for exits.
        sell_signals = [
            {
                "wallet": str(item.get("wallet") or ""),
                "mint": str(item["mint"]),
                "timestamp": float(item["timestamp"]),
                "signature": str(item["signature"]),
            }
            for item in all_swaps
            if str(item.get("signature")) in fresh_signatures
            and item.get("side") == "sell"
            and signal_is_fresh(float(item.get("timestamp", 0)), now=now)
        ]
        wallet_record["metrics"] = calculate_wallet_metrics(wallet_record["closed_trades"])
        wallet_record["last_processed_at"] = now
        return {
            "new_closed_trades": 0,
            "buy_signals": [],
            "sell_signals": sell_signals,
            "metrics": wallet_record["metrics"],
            "history_complete": False,
            "history_truncated": bool(wallet_record.get("history_truncated", False)),
            "normalized_swaps": len(all_swaps),
        }

    # Recompute all open inventory and realized trades in chronological order.
    # This deliberately replaces the old derived ledger rather than appending
    # duplicate PnL entries when old swap pages arrive late.
    for trade in all_swaps:
        signature = str(trade["signature"])
        mint = str(trade["mint"])
        side = trade["side"]
        timestamp = float(trade["timestamp"])
        token_amount = float(trade["token_amount"])
        quote_amount = float(trade["quote_amount"])
        quote_symbol = trade["quote_symbol"]

        if side == "buy":
            current_metrics = calculate_wallet_metrics(closed_trades)
            if (
                signature in fresh_signatures
                and history_eligible
                and wallet_qualifies(current_metrics)
                and signal_is_fresh(timestamp, now=now)
            ):
                buy_signals.append({
                    "wallet": str(trade.get("wallet") or ""),
                    "mint": mint,
                    "timestamp": timestamp,
                    "signature": signature,
                    "quote_symbol": quote_symbol,
                    "leader_price_quote": quote_amount / token_amount,
                    "wallet_metrics": current_metrics,
                })
            position = positions.get(mint)
            if not isinstance(position, dict):
                position = {
                    "token_amount": 0.0,
                    "cost_quote": 0.0,
                    "quote_symbol": quote_symbol,
                    "first_buy_timestamp": timestamp,
                }
                positions[mint] = position
            if position.get("quote_symbol") == quote_symbol:
                position["token_amount"] = float(position.get("token_amount", 0.0)) + token_amount
                position["cost_quote"] = float(position.get("cost_quote", 0.0)) + quote_amount
            continue

        # Sell signals are used to close a copy position even if the leader no
        # longer qualifies for new entries. Only fresh signatures are emitted.
        if signature in fresh_signatures and signal_is_fresh(timestamp, now=now):
            sell_signals.append({
                "wallet": str(trade.get("wallet") or ""),
                "mint": mint,
                "timestamp": timestamp,
                "signature": signature,
            })
        position = positions.get(mint)
        if not isinstance(position, dict) or position.get("quote_symbol") != quote_symbol:
            continue
        old_qty = _finite_number(position.get("token_amount")) or 0.0
        old_cost = _finite_number(position.get("cost_quote")) or 0.0
        sold_qty = min(token_amount, old_qty)
        if old_qty <= 0 or sold_qty <= 0 or old_cost <= 0:
            continue
        basis = old_cost * sold_qty / old_qty
        proceeds = quote_amount * sold_qty / token_amount
        gross_pct = (proceeds / basis - 1.0) * 100.0
        net_pct = gross_pct - float(fee_reserve_pct)
        closed_trades.append({
            "timestamp": timestamp,
            "signature": signature,
            "mint": mint,
            "quote_symbol": quote_symbol,
            "token_amount": sold_qty,
            "cost_quote": basis,
            "proceeds_quote": proceeds,
            "gross_pnl_pct": gross_pct,
            "net_pnl_pct": net_pct,
        })
        if signature in fresh_signatures:
            new_closed += 1
        position["token_amount"] = max(0.0, old_qty - sold_qty)
        position["cost_quote"] = max(0.0, old_cost - basis)
        if position["token_amount"] <= 1e-12:
            positions.pop(mint, None)

    wallet_record["positions"] = positions
    wallet_record["closed_trades"] = closed_trades[-MAX_STORED_CLOSED_TRADES:]
    wallet_record["seen_signatures"] = list(dict.fromkeys(seen_order))[-MAX_STORED_SIGNATURES:]
    wallet_record["metrics"] = calculate_wallet_metrics(wallet_record["closed_trades"])
    wallet_record["last_processed_at"] = now
    return {
        "new_closed_trades": new_closed,
        "buy_signals": buy_signals,
        "sell_signals": sell_signals,
        "metrics": wallet_record["metrics"],
        "history_complete": history_eligible,
        "history_truncated": bool(wallet_record.get("history_truncated", False)),
        "normalized_swaps": len(all_swaps),
    }

def _api_key():
    key = (os.environ.get("HELIUS_API_KEY") or "").strip()
    if key:
        return key
    url = (os.environ.get("HELIUS_RPC_URL") or "").strip()
    return (parse_qs(urlparse(url).query).get("api-key") or [""])[0].strip()


def fetch_wallet_transactions(wallet, api_key, limit=MAX_TRANSACTIONS_PER_WALLET,
                               max_pages=1, before_signature=None, return_metadata=False):
    """Fetch bounded enhanced-transaction history, supporting backwards pagination.

    When return_metadata=True, return the transactions plus the oldest signature,
    whether the address history ended, and whether all attempted requests succeeded.
    A failed older page never discards a successful recent page.
    """
    result = {
        "transactions": [],
        "last_signature": before_signature,
        "reached_end": False,
        "ok": True,
        "pages_fetched": 0,
        "error": "",
        "retry_after_seconds": None,
    }
    if not wallet or not api_key:
        result["ok"] = False
        return result if return_metadata else []

    page_size = min(100, max(1, int(limit)))
    page_limit = max(1, min(10, int(max_pages)))
    cursor = before_signature
    seen_signatures = set()
    for _ in range(page_limit):
        params = {
            "api-key": api_key,
            "limit": page_size,
            "sort-order": "desc",
        }
        if cursor:
            params["before-signature"] = cursor
        try:
            response = requests.get(
                HELIUS_ENHANCED_URL.format(wallet=wallet),
                params=params,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.HTTPError as exc:
            error_response = getattr(exc, "response", None) or response
            status = getattr(error_response, "status_code", None)
            result["error"] = f"http_{status}" if status else "http_error"
            if status == 429:
                headers = getattr(error_response, "headers", {}) or {}
                retry_after = _finite_number(headers.get("Retry-After")) if hasattr(headers, "get") else None
                if retry_after is None or retry_after <= 0:
                    retry_after = DEFAULT_RATE_LIMIT_BACKOFF_SECONDS
                result["retry_after_seconds"] = min(
                    MAX_RATE_LIMIT_BACKOFF_SECONDS,
                    max(1.0, float(retry_after)),
                )
            result["ok"] = False
            break
        except requests.Timeout:
            result["error"] = "timeout"
            result["ok"] = False
            break
        except requests.RequestException as exc:
            result["error"] = type(exc).__name__.lower()
            result["ok"] = False
            break
        except (ValueError, TypeError, OverflowError):
            result["error"] = "invalid_json_or_input"
            result["ok"] = False
            break
        if not isinstance(payload, list):
            result["error"] = "unexpected_payload_shape"
            result["ok"] = False
            break
        if not payload:
            result["reached_end"] = True
            break
        page = [item for item in payload if isinstance(item, dict) and item.get("signature")]
        if not page:
            result["ok"] = False
            break
        for transaction in page:
            signature = str(transaction["signature"])
            if signature not in seen_signatures:
                seen_signatures.add(signature)
                result["transactions"].append(transaction)
        result["pages_fetched"] += 1
        page_last_signature = str(page[-1]["signature"])
        if page_last_signature == cursor:
            result["ok"] = False
            break
        result["last_signature"] = page_last_signature
        if len(payload) < page_size:
            result["reached_end"] = True
            break
        cursor = page_last_signature

    return result if return_metadata else result["transactions"]

def _load_json(path, default):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else dict(default)
    except (OSError, ValueError, TypeError):
        return dict(default)


def _atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def select_wallets(watch_state, research_state, limit=MAX_WALLETS_PER_CYCLE):
    """Keep qualified wallets on every poll; rotate the rest to discover track record."""
    candidates = watch_state.get("wallets", {}) if isinstance(watch_state, dict) else {}
    if not isinstance(candidates, dict):
        return []
    valid = []
    research_wallets = research_state.setdefault("wallets", {})
    for owner, evidence in candidates.items():
        if not isinstance(owner, str) or not owner or not isinstance(evidence, dict):
            continue
        stats = (research_wallets.get(owner) or {}).get("metrics", {})
        if not isinstance(stats, dict):
            stats = {}
        sell_events = int(evidence.get("confirmed_sell_pressure_events", 0) or 0)
        observed_drops = int(evidence.get("observed_balance_drops", 0) or 0)
        buy_events = int(evidence.get("confirmed_buy_pressure_events", 0) or 0)
        observed_increases = int(evidence.get("observed_balance_increases", 0) or 0)
        tokens_seen = len(evidence.get("tokens_seen", []) or [])
        # Holders are not necessarily traders, and falling balances can mean
        # transfers or exits. Prioritize corroborated accumulation for buy-side
        # research, and use sell pressure as a mild risk penalty, not a positive
        # ranking signal. All wallets still have their swaps reconstructed before
        # any wallet can qualify for copying.
        behavior_score = buy_events * 200 + observed_increases * 20 + tokens_seen - sell_events * 50 - observed_drops * 5
        score = (100000 if wallet_qualifies(stats) else 0) + behavior_score
        valid.append((owner, score, stats, evidence))
    valid.sort(key=lambda item: (
        not wallet_qualifies(item[2]),
        -float(item[2].get("profit_factor", 0) or 0),
        -float(item[2].get("mean_net_pnl_pct", 0) or 0),
        -int(item[3].get("confirmed_buy_pressure_events", 0) or 0),
        -int(item[3].get("observed_balance_increases", 0) or 0),
        int(item[3].get("confirmed_sell_pressure_events", 0) or 0),
        -item[1],
        item[0],
    ))
    qualified = [item[0] for item in valid if wallet_qualifies(item[2])]
    chosen = qualified[:limit]
    chosen_set = set(chosen)
    remaining_items = [item for item in valid if item[0] not in chosen_set]

    # Finish bounded history backfills in small batches before rotating across
    # never-initialized wallets. Otherwise a 200-wallet universe and one-page
    # polling would revisit each partial wallet too rarely to establish realized PnL.
    def history_phase(item):
        record = research_wallets.get(item[0]) or {}
        if (
            record.get("normalized_history_initialized")
            and record.get("history_cursor")
            and not record.get("history_complete", False)
            and not record.get("history_truncated", False)
        ):
            return 0
        if not record.get("normalized_history_initialized") or not record.get("history_cursor"):
            return 1
        return 2

    partial_backfills = [item for item in remaining_items if history_phase(item) == 0]
    partial_backfills.sort(key=lambda item: (
        -int((research_wallets.get(item[0]) or {}).get("backfill_visits", 0) or 0),
        -item[1],
        item[0],
    ))
    slots = max(0, limit - len(chosen))
    selected_backfills = partial_backfills[:slots]
    chosen.extend(item[0] for item in selected_backfills)
    slots -= len(selected_backfills)

    # Fill remaining slots using the existing evidence ranking and cursor rotation.
    chosen_set = set(chosen)
    remaining = [
        item[0] for item in remaining_items
        if item[0] not in chosen_set and history_phase(item) != 0
    ]
    if slots and remaining:
        cursor = int(research_state.get("wallet_cursor", 0) or 0) % len(remaining)
        picked = [
            remaining[(cursor + offset) % len(remaining)]
            for offset in range(min(slots, len(remaining)))
        ]
        chosen.extend(picked)
        research_state["wallet_cursor"] = (cursor + len(picked)) % len(remaining)
    return chosen


def _account_metrics(account):
    closed = account.get("closed_trades", [])
    return calculate_wallet_metrics(closed)


def _close_copy_position(account, mint, raw_exit_price, reason, now):
    position = account.get("positions", {}).pop(mint, None)
    if not isinstance(position, dict):
        return None
    entry_price = _finite_number(position.get("entry_price"))
    amount_usd = _finite_number(position.get("amount_usd"))
    raw_exit_price = _finite_number(raw_exit_price)
    if entry_price is None or amount_usd is None or raw_exit_price is None or entry_price <= 0 or amount_usd <= 0 or raw_exit_price <= 0:
        return None
    effective_exit = raw_exit_price * (1.0 - scanner.EXIT_SLIPPAGE_BPS / 10000.0)
    gross_pct = (effective_exit / entry_price - 1.0) * 100.0
    net_pct = gross_pct - scanner.PAPER_ROUND_TRIP_FEE_PCT
    pnl_usd = amount_usd * net_pct / 100.0
    account["equity"] = float(account.get("equity", 1000.0)) + pnl_usd
    account["peak_equity"] = max(float(account.get("peak_equity", account["initial_equity"])), account["equity"])
    trade = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "mint": mint,
        "symbol": position.get("symbol", "?"),
        "entry_price": entry_price,
        "exit_price": effective_exit,
        "pnl_pct": net_pct,
        "pnl_usd": pnl_usd,
        "equity_after": account["equity"],
        "reason": reason,
        "leader_wallets": position.get("leader_wallets", []),
    }
    account.setdefault("closed_trades", []).append(trade)
    account["closed_trades"] = account["closed_trades"][-500:]
    return trade


def _sol_usd_price():
    pair = scanner.fetch_dex_pair(WSOL_MINT)
    if not isinstance(pair, dict):
        return None
    return _finite_number(pair.get("priceUsd"))


def _copy_entry_allowed(pair):
    if not isinstance(pair, dict):
        return False, "pair_unavailable"
    passes, reason = scanner.passes_shadow_market_filter(pair)
    if not passes:
        return False, reason
    try:
        liquidity = float((pair.get("liquidity") or {}).get("usd") or 0)
        volume = float((pair.get("volume") or {}).get("h1") or 0)
        m5 = float((pair.get("priceChange") or {}).get("m5") or 0)
        price = float(pair.get("priceUsd") or 0)
    except (TypeError, ValueError, AttributeError, OverflowError):
        return False, "invalid_market_data"
    if not all(math.isfinite(value) for value in (liquidity, volume, m5, price)) or price <= 0:
        return False, "invalid_market_data"
    if liquidity < COPY_MIN_LIQUIDITY_USD:
        return False, "copy_low_liquidity"
    if volume < COPY_MIN_VOLUME_H1_USD:
        return False, "copy_low_volume"
    if m5 < COPY_MIN_PRICE_CHANGE_M5_PCT or m5 > COPY_MAX_PRICE_CHANGE_M5_PCT:
        return False, "copy_bad_momentum_window"
    return True, "ok"


def run_copy_paper_account(buy_signals, sell_signals, state_path="whale_copy_account/state.json",
                           ledger_path="whale_copy_account/trades_log.csv", now=None):
    """Apply corroborated wallet signals to a separate paper account using fresh quotes."""
    now = time.time() if now is None else float(now)
    state = _load_json(state_path, {
        "initial_equity": 1000.0, "equity": 1000.0, "peak_equity": 1000.0,
        "positions": {}, "closed_trades": [], "pending_signals": [], "trade_count": 0,
    })
    state.setdefault("initial_equity", 1000.0)
    state.setdefault("equity", state["initial_equity"])
    state.setdefault("peak_equity", state["initial_equity"])
    state.setdefault("positions", {})
    state.setdefault("closed_trades", [])
    state.setdefault("pending_signals", [])
    update = []
    exit_by_mint = {}
    for signal in sell_signals or []:
        if not isinstance(signal, dict) or not signal.get("mint") or not signal.get("wallet"):
            continue
        exit_by_mint.setdefault(signal["mint"], set()).add(signal["wallet"])

    # Manage current paper positions first. A followed wallet's own sale is the
    # primary exit; price stops and a maximum hold are fallbacks.
    for mint in list(state["positions"].keys()):
        position = state["positions"].get(mint)
        if not isinstance(position, dict):
            state["positions"].pop(mint, None)
            continue
        pair = scanner.fetch_dex_pair(mint)
        if not isinstance(pair, dict):
            position["quote_status"] = "missing"
            continue
        price = _finite_number(pair.get("priceUsd"))
        if price is None or price <= 0:
            position["quote_status"] = "missing"
            continue
        position.pop("quote_status", None)
        entry_time = _finite_number(position.get("entry_time")) or now
        entry_price = _finite_number(position.get("entry_price")) or price
        peak = max(_finite_number(position.get("peak_price")) or entry_price, price)
        position["peak_price"] = peak
        pnl = (price - entry_price) / entry_price
        if pnl >= 0.08:
            position["trailing_armed"] = True
        reason = None
        selling_leaders = exit_by_mint.get(mint, set()).intersection(position.get("leader_wallets", []))
        if selling_leaders:
            reason = "leader_sell"
        elif position.get("trailing_armed") and (peak - price) / peak >= COPY_TRAILING_STOP_PCT:
            reason = "trailing_stop"
        elif pnl <= -COPY_HARD_STOP_LOSS_PCT:
            reason = "hard_stop_loss"
        elif now - entry_time >= COPY_MAX_HOLD_SECONDS:
            reason = "max_hold_timeout"
        if reason:
            trade = _close_copy_position(state, mint, price, reason, now)
            if trade:
                update.append(trade)

    # Deduplicate and age pending buys; require two independent profitable wallets.
    pending = state["pending_signals"]
    pending.extend(signal for signal in (buy_signals or []) if isinstance(signal, dict))
    valid_pending = []
    seen_pending = set()
    for signal in pending:
        ts = _finite_number(signal.get("timestamp"))
        key = (signal.get("wallet"), signal.get("mint"), signal.get("signature"))
        if not all(key) or ts is None or now - ts < 0 or now - ts > COPY_CONFIRMATION_WINDOW_SECONDS:
            continue
        if key in seen_pending:
            continue
        seen_pending.add(key)
        valid_pending.append(signal)
    state["pending_signals"] = valid_pending

    groups = {}
    for signal in valid_pending:
        groups.setdefault(signal["mint"], []).append(signal)
    opened = []
    rejection_counts = {}
    for mint, signals in groups.items():
        unique_wallets = {signal["wallet"] for signal in signals}
        if len(unique_wallets) < COPY_MIN_SIGNAL_WALLETS:
            continue
        if mint in state["positions"] or len(state["positions"]) >= COPY_MAX_CONCURRENT_POSITIONS:
            rejection_counts["position_limit_or_duplicate"] = rejection_counts.get("position_limit_or_duplicate", 0) + 1
            continue
        risk_dd = (float(state["peak_equity"]) - float(state["equity"])) / max(float(state["peak_equity"]), 1e-9)
        if risk_dd >= COPY_MAX_DRAWDOWN_FRACTION:
            rejection_counts["drawdown_halt"] = rejection_counts.get("drawdown_halt", 0) + 1
            continue
        pair = scanner.fetch_dex_pair(mint)
        passed, reject_reason = _copy_entry_allowed(pair)
        if not passed:
            rejection_counts[reject_reason] = rejection_counts.get(reject_reason, 0) + 1
            continue
        price = _finite_number(pair.get("priceUsd"))
        if price is None or price <= 0:
            rejection_counts["invalid_price"] = rejection_counts.get("invalid_price", 0) + 1
            continue
        reference_prices = []
        needs_sol_price = any(signal.get("quote_symbol") == "SOL" for signal in signals)
        sol_price = _sol_usd_price() if needs_sol_price else None
        for signal in signals:
            leader_price = _finite_number(signal.get("leader_price_quote"))
            if leader_price is None or leader_price <= 0:
                continue
            quote_symbol = signal.get("quote_symbol")
            if quote_symbol == "SOL":
                if sol_price is None or sol_price <= 0:
                    continue
                reference_prices.append(leader_price * sol_price)
            elif quote_symbol in ("USDC", "USDT"):
                reference_prices.append(leader_price)
        if len(reference_prices) < COPY_MIN_SIGNAL_WALLETS:
            rejection_counts["leader_price_unavailable"] = rejection_counts.get("leader_price_unavailable", 0) + 1
            continue
        reference_price = statistics.median(reference_prices)
        drift_pct = (price / reference_price - 1.0) * 100.0 if reference_price > 0 else math.inf
        if not math.isfinite(drift_pct) or drift_pct > COPY_MAX_PRICE_DRIFT_PCT or drift_pct < -12.0:
            rejection_counts["price_drift_from_leaders"] = rejection_counts.get("price_drift_from_leaders", 0) + 1
            continue
        exposure = sum(float(p.get("amount_usd", 0) or 0) for p in state["positions"].values())
        max_total = float(state["equity"]) * COPY_PORTFOLIO_EXPOSURE_FRACTION
        amount = min(float(state["equity"]) * COPY_POSITION_EQUITY_FRACTION, max_total - exposure)
        if amount < 1.0:
            rejection_counts["position_too_small"] = rejection_counts.get("position_too_small", 0) + 1
            continue
        base = pair.get("baseToken") or {}
        state["positions"][mint] = {
            "symbol": base.get("symbol", mint[:8]),
            "entry_price": price * (1.0 + scanner.ENTRY_SLIPPAGE_BPS / 10000.0),
            "entry_time": now,
            "entry_liquidity": float((pair.get("liquidity") or {}).get("usd") or 0),
            "peak_price": price,
            "amount_usd": amount,
            "leader_wallets": sorted(unique_wallets),
            "leader_signatures": sorted({signal["signature"] for signal in signals}),
            "leader_reference_price_usd": reference_price,
            "leader_price_drift_pct": drift_pct,
            "trailing_armed": False,
            "strategy": "qualified_whale_consensus_paper",
        }
        opened.append(mint)
        state["pending_signals"] = [signal for signal in state["pending_signals"] if signal.get("mint") != mint]

    state["trade_count"] = len(state["closed_trades"])
    state["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    state["risk_control"] = {
        "drawdown_pct": round((float(state["peak_equity"]) - float(state["equity"])) / max(float(state["peak_equity"]), 1e-9) * 100.0, 2),
        "status": "halted" if (float(state["peak_equity"]) - float(state["equity"])) / max(float(state["peak_equity"]), 1e-9) >= COPY_MAX_DRAWDOWN_FRACTION else "active",
    }
    _save_copy_ledger(update, ledger_path)
    _atomic_json(state_path, state)
    return {
        "equity": round(float(state["equity"]), 6),
        "closed_trades": len(state["closed_trades"]),
        "wins": sum(1 for trade in state["closed_trades"] if float(trade.get("pnl_usd", 0)) > 0),
        "net_pnl_usd": round(float(state["equity"]) - float(state["initial_equity"]), 6),
        "open_positions": len(state["positions"]),
        "opened_this_cycle": len(opened),
        "closed_this_cycle": len(update),
        "entry_rejections": rejection_counts,
        "status": "paper_only",
    }


def _save_copy_ledger(trades, ledger_path):
    if not trades:
        return
    path = Path(ledger_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["timestamp", "mint", "symbol", "entry_price", "exit_price", "pnl_pct", "pnl_usd", "equity_after", "reason", "leader_wallets"]
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not exists:
            writer.writeheader()
        for trade in trades:
            row = dict(trade)
            row["leader_wallets"] = "|".join(row.get("leader_wallets", []))
            writer.writerow(row)


def run():
    """Research known token-holder wallets and paper-copy only qualifying consensus buys."""
    now = time.time()
    watch_state = _load_json("whale_watch_state.json", {"wallets": {}})
    research_state = _load_json("whale_copy_state.json", {
        "version": 1, "wallets": {}, "wallet_cursor": 0, "scan_count": 0,
    })
    research_state.setdefault("wallets", {})
    for wallet_record in research_state["wallets"].values():
        if isinstance(wallet_record, dict):
            _prepare_wallet_record_for_replay(wallet_record)
    api_key = _api_key()
    selected = select_wallets(watch_state, research_state)
    buy_signals, sell_signals = [], []
    swaps_seen = 0
    new_closed = 0
    fetch_errors = 0
    fetch_error_reasons = {}

    wallets_attempted = 0
    rate_limit_backoff_until = _finite_number(research_state.get("api_backoff_until")) or 0.0

    if not api_key:
        status = "helius_api_key_not_configured"
    elif now < rate_limit_backoff_until:
        status = "helius_rate_limit_backoff"
        fetch_error_reasons["backoff_active"] = 1
    else:
        research_state.pop("api_backoff_until", None)
        status = "ok"
        for owner in selected:
            if wallets_attempted:
                time.sleep(REQUEST_MIN_INTERVAL_SECONDS)
            wallets_attempted += 1
            record = research_state["wallets"].setdefault(owner, {
                "positions": {}, "seen_signatures": [], "closed_trades": [], "metrics": {},
            })
            # Newly discovered/non-qualified wallets are backfilled one page per
            # visit, so API request volume stays bounded. As soon as a wallet meets
            # realized-history gates, every poll reads the newest page for fresh
            # copy signals instead of continuing to walk backward.
            history_complete = bool(record.get("history_complete", False))
            history_truncated = bool(record.get("history_truncated", False))
            history_initialized = bool(record.get("normalized_history_initialized", False))
            cursor = record.get("history_cursor")
            if history_complete and not history_truncated:
                fetched = fetch_wallet_transactions(
                    owner, api_key, limit=MAX_TRANSACTIONS_PER_WALLET,
                    max_pages=1, return_metadata=True,
                )
                fetched_mode = "latest"
            elif not history_initialized or not cursor:
                # Start each legacy wallet with the newest page, then page backward
                # one request per visit until the entire bounded history is known.
                fetched = fetch_wallet_transactions(
                    owner, api_key, limit=MAX_TRANSACTIONS_PER_WALLET,
                    max_pages=1, return_metadata=True,
                )
                fetched_mode = "seed"
            else:
                fetched = fetch_wallet_transactions(
                    owner, api_key, limit=MAX_TRANSACTIONS_PER_WALLET,
                    max_pages=1, before_signature=cursor, return_metadata=True,
                )
                fetched_mode = "backfill"

            retry_delay = None
            if not fetched["ok"] and not fetched["transactions"]:
                fetch_errors += 1
                reason = fetched.get("error") or "empty_or_failed_response"
                fetch_error_reasons[reason] = fetch_error_reasons.get(reason, 0) + 1
                if reason == "http_429":
                    retry_delay = _finite_number(fetched.get("retry_after_seconds"))
                    if retry_delay is None or retry_delay <= 0:
                        retry_delay = DEFAULT_RATE_LIMIT_BACKOFF_SECONDS
                    retry_delay = min(MAX_RATE_LIMIT_BACKOFF_SECONDS, max(1.0, retry_delay))
                    research_state["api_backoff_until"] = now + retry_delay
                    status = "helius_rate_limit_backoff"
                    break
                continue
            if not fetched["ok"]:
                fetch_errors += 1
                reason = fetched.get("error") or "partial_response"
                fetch_error_reasons[reason] = fetch_error_reasons.get(reason, 0) + 1
                if reason == "http_429":
                    retry_delay = _finite_number(fetched.get("retry_after_seconds"))
                    if retry_delay is None or retry_delay <= 0:
                        retry_delay = DEFAULT_RATE_LIMIT_BACKOFF_SECONDS
                    retry_delay = min(MAX_RATE_LIMIT_BACKOFF_SECONDS, max(1.0, retry_delay))

            transaction_batch = list(fetched["transactions"])
            if fetched_mode == "backfill" and fetched["ok"]:
                record["backfill_visits"] = int(record.get("backfill_visits", 0) or 0) + 1
                if fetched["last_signature"]:
                    record["history_cursor"] = fetched["last_signature"]
                if fetched["reached_end"]:
                    record["history_complete"] = True
            elif fetched_mode == "seed" and fetched["ok"]:
                record["normalized_history_initialized"] = True
                record["backfill_visits"] = int(record.get("backfill_visits", 0) or 0) + 1
                if fetched["last_signature"]:
                    record["history_cursor"] = fetched["last_signature"]
                if fetched["reached_end"]:
                    record["history_complete"] = True
            unique_transactions = {}
            for transaction in transaction_batch:
                signature = transaction.get("signature")
                if signature:
                    unique_transactions[str(signature)] = transaction
            parsed = []
            for transaction in unique_transactions.values():
                trade = parse_swap_transaction(transaction, owner)
                if trade:
                    parsed.append(trade)
            swaps_seen += len(parsed)
            result = apply_wallet_trades(record, parsed, now=now)
            new_closed += result["new_closed_trades"]
            buy_signals.extend(result["buy_signals"])
            sell_signals.extend(result["sell_signals"])
            if retry_delay is not None:
                research_state["api_backoff_until"] = now + retry_delay
                status = "helius_rate_limit_backoff"
                break
        if status == "ok" and wallets_attempted and fetch_errors == wallets_attempted:
            status = "helius_wallet_history_unavailable"

    research_state["scan_count"] = int(research_state.get("scan_count", 0) or 0) + 1
    research_state["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    research_state["last_scan"] = {
        "wallets_selected": len(selected),
        "wallets_attempted": wallets_attempted,
        "wallets_with_fetch_errors": fetch_errors,
        "fetch_error_reasons": fetch_error_reasons,
        "api_backoff_seconds_remaining": max(
            0, int(((_finite_number(research_state.get("api_backoff_until")) or 0.0) - now))
        ),
        "swaps_seen": swaps_seen,
        "new_closed_wallet_trades": new_closed,
        "fresh_profitable_wallet_buys": len(buy_signals),
        "fresh_wallet_sells": len(sell_signals),
    }
    _atomic_json("whale_copy_state.json", research_state)
    paper = run_copy_paper_account(buy_signals, sell_signals, now=now)

    all_records = list(research_state["wallets"].values())
    qualifying = [record for record in all_records if wallet_qualifies(record.get("metrics", {}))]
    leaderboard = []
    for owner, record in research_state["wallets"].items():
        metrics = record.get("metrics", {})
        if int(metrics.get("closed_trades", 0) or 0) > 0:
            leaderboard.append({
                "owner": owner,
                "closed_trades": int(metrics.get("closed_trades", 0)),
                "win_rate": round(float(metrics.get("win_rate", 0)), 4),
                "profit_factor": round(float(metrics.get("profit_factor", 0)), 4),
                "mean_net_pnl_pct": round(float(metrics.get("mean_net_pnl_pct", 0)), 4),
                "qualified": wallet_qualifies(metrics),
            })
    leaderboard.sort(key=lambda item: (item["qualified"], item["profit_factor"], item["mean_net_pnl_pct"], item["closed_trades"]), reverse=True)
    report = {
        "status": status,
        "research_only": True,
        "wallets_tracked": len(research_state["wallets"]),
        "wallets_selected": len(selected),
        "wallets_attempted": wallets_attempted,
        "wallets_with_fetch_errors": fetch_errors,
        "fetch_error_reasons": fetch_error_reasons,
        "api_backoff_seconds_remaining": max(
            0, int(((_finite_number(research_state.get("api_backoff_until")) or 0.0) - now))
        ),
        "swaps_seen": swaps_seen,
        "new_closed_wallet_trades": new_closed,
        "qualifying_wallets": len(qualifying),
        "fresh_profitable_wallet_buys": len(buy_signals),
        "fresh_wallet_sells": len(sell_signals),
        "leaderboard": leaderboard[:10],
        "paper_account": paper,
        "note": "Transactions and wallet PnL are observed on-chain estimates; paper entries require two qualifying wallets and a low-drift executable quote. No real transaction is submitted.",
    }
    print(json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return report


if __name__ == "__main__":
    run()
