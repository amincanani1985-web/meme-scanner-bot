#!/usr/bin/env python3
"""
Hardened meme-coin paper scanner.
Entry: high-conviction score (80+) with three consecutive confirmations.
Risk controls are modeled estimates and require validation against real market data.
"""
import time
import math
import csv
import io
import json
import os
import requests
from whale_monitor import fetch_whale_balances, helius_configured, confirmed_whale_dump
from pathlib import Path
from datetime import datetime, timedelta

DEXSCREENER_TOKEN_BOOSTS_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
DEXSCREENER_TOKEN_TOP_BOOSTS_URL = "https://api.dexscreener.com/token-boosts/top/v1"
DEXSCREENER_TOKEN_PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"
DISCOVERY_SEARCH_TERMS = ("BONK", "WIF", "POPCAT", "MEW", "PONKE", "BOME", "MYRO", "SAMO")
DEXSCREENER_TOKEN_PAIRS_URL = "https://api.dexscreener.com/latest/dex/tokens/{}"
ENTRY_SLIPPAGE_BPS = 150
EXIT_SLIPPAGE_BPS = 200
MAX_HOLD_SECONDS = 600
MAX_MISSING_PRICE_SECONDS = 600
# Do not label shadow outcomes from stale prices after the intended hold window.
# A 5-minute scheduling tolerance is allowed beyond the 10-minute maximum hold.
SHADOW_STALE_OBSERVATION_SECONDS = MAX_HOLD_SECONDS + 300
HTTP_TIMEOUT_SECONDS = 10


# ============================================================================
# CORE RISK PARAMETERS (HARDENED v20 - profit-focused)
# ============================================================================

# Experimental paper profile: prefer a sustained hourly trend with a modest 5m pullback.
# The historical score buckets did not validate momentum-chasing; these settings remain
# paper-only and must earn promotion through forward outcomes, not assumed profitability.
MIN_LIQUIDITY_USD = 20000
MIN_VOLUME_H1_USD = 20000
MIN_LIQUIDITY_TO_MCAP_RATIO = 0.25
MIN_BUY_SELL_RATIO_M5 = 1.5
MIN_PRICE_CHANGE_M5_PCT = -2.0
MAX_PRICE_CHANGE_M5_PCT = 15.0
MIN_PAIR_AGE_SECONDS = 1800               # 30 min; still excludes the newest launches

# Entry scoring: experimental pullback threshold, paper account only.
OPPORTUNITY_ENTRY_THRESHOLD = 75.0
LEGACY_BASELINE_ENTRY_THRESHOLD = 80.0
MAX_CONCURRENT_POSITIONS = 4              # Was 8 → reduce correlation
MAX_POSITION_EQUITY_FRACTION = 0.005      # 0.5% per trade (was 1%)
MAX_PORTFOLIO_EXPOSURE_FRACTION = 0.02    # 2% total (was 5%)

# Exit rules: AGGRESSIVE PROFIT LOCK & LOSS CUT
TARGET_TRADES = 200
TRAILING_STOP_PCT = 0.04                  # Exit on 4% pullback (was 6%)
HARD_STOP_LOSS_PCT = 0.10                 # Hard stop at -10% (was -15%)
LIQUIDITY_DRAIN_CRITICAL = 0.25           # Emergency exit at -25% liquidity

# Whale detection
WHALE_DUMP_THRESHOLD = 0.20               # Whale selling > 20% = exit

# Cooldown after stop-loss
STOP_LOSS_COOLDOWN_MINUTES = 60           # Was 45 min → let emotions settle

# Candidate confirmation threshold
CANDIDATE_CONFIRMATION_HITS = 3           # Require 3 observations (was 2)
CANDIDATE_CONFIRMATION_MAX_GAP_SECONDS = 600  # A missed/stale cycle cannot count as consecutive
MAX_EQUITY_DRAWDOWN_FRACTION = 0.20       # Halt new entries after 20% drawdown from initial paper equity
SHADOW_MIN_SCORE = 50.0                    # Exclude bearish/zero-clamped signals from the useful research cohort
MAX_SHADOW_POSITIONS = 24                   # Broader isolated shadow cohort for faster learning

# ============================================================================
# STATE MANAGEMENT
# ============================================================================

def normalize_state(state):
    """Validate and normalize persisted state without discarding valid values."""
    if not isinstance(state, dict):
        state = {}
    try:
        equity = float(state.get("equity", 1000.0))
        if not math.isfinite(equity) or equity < 0:
            equity = 1000.0
    except (TypeError, ValueError):
        equity = 1000.0
    state["equity"] = equity
    try:
        initial_equity = float(state.get("initial_equity", 1000.0))
        if not math.isfinite(initial_equity) or initial_equity <= 0:
            initial_equity = 1000.0
    except (TypeError, ValueError, OverflowError):
        initial_equity = 1000.0
    state["initial_equity"] = initial_equity
    if not isinstance(state.get("risk_control"), dict):
        state["risk_control"] = {}
    for key in ("positions", "shadow_positions", "cooldowns", "candidate_observations", "strategy", "learning", "whale_snapshots"):
        if not isinstance(state.get(key), dict):
            state[key] = {}
    if not isinstance(state.get("blacklist"), list):
        state["blacklist"] = []
    for key in ("trade_count", "scan_count"):
        try:
            state[key] = max(0, int(state.get(key, 0)))
        except (TypeError, ValueError):
            state[key] = 0
    state.setdefault("version", "v21-live-price")
    learning = state["learning"]
    for key in ("trades", "wins"):
        try:
            learning[key] = max(0, int(learning.get(key, 0)))
        except (TypeError, ValueError):
            learning[key] = 0
    if not isinstance(learning.get("buckets"), dict):
        learning["buckets"] = {}
    strategy = state["strategy"]
    try:
        threshold = float(strategy.get("active_threshold", OPPORTUNITY_ENTRY_THRESHOLD))
        if not math.isfinite(threshold):
            threshold = OPPORTUNITY_ENTRY_THRESHOLD
    except (TypeError, ValueError):
        threshold = OPPORTUNITY_ENTRY_THRESHOLD
    strategy["active_threshold"] = threshold
    strategy.setdefault("candidate_threshold", None)
    strategy.setdefault("status", "baseline")
    update_risk_control(state)
    return state


def update_risk_control(state):
    """Apply the drawdown circuit breaker against the account high-water mark."""
    initial = float(state.get("initial_equity", 1000.0) or 1000.0)
    equity = float(state.get("equity", initial) or 0.0)
    try:
        prior_peak = float(state.get("peak_equity", initial) or initial)
    except (TypeError, ValueError, OverflowError):
        prior_peak = initial
    peak = max(initial, prior_peak, equity)
    state["peak_equity"] = round(peak, 6)
    drawdown = max(0.0, (peak - equity) / peak) if peak > 0 else 0.0
    initial_loss = max(0.0, (initial - equity) / initial) if initial > 0 else 0.0
    risk = state.setdefault("risk_control", {})
    if not isinstance(risk, dict):
        risk = {}
        state["risk_control"] = risk
    risk["drawdown_pct"] = round(drawdown * 100.0, 2)
    risk["loss_from_initial_pct"] = round(initial_loss * 100.0, 2)
    risk["peak_equity"] = round(peak, 2)
    risk["max_drawdown_pct"] = round(MAX_EQUITY_DRAWDOWN_FRACTION * 100.0, 2)
    risk["status"] = "halted" if drawdown >= MAX_EQUITY_DRAWDOWN_FRACTION else "active"
    risk["reason"] = "max_drawdown_exceeded" if risk["status"] == "halted" else ""
    if state.get("state_recovery_required"):
        risk["status"] = "halted"
        risk["reason"] = "state_recovery_required"
    return risk


def normalize_legacy_strategy_baseline(state):
    """Migrate old state files to hardened thresholds."""
    strategy = state.get("strategy", {})
    if strategy.get("status") == "waiting_for_history":
        old_threshold = float(strategy.get("active_threshold", 60.0))
        if old_threshold < LEGACY_BASELINE_ENTRY_THRESHOLD:
            # Historical untrained accounts retain the original conservative baseline.
            # The experimental threshold applies only to the isolated paper profile.
            state["strategy"]["active_threshold"] = LEGACY_BASELINE_ENTRY_THRESHOLD
            state["strategy"]["rollback_threshold"] = LEGACY_BASELINE_ENTRY_THRESHOLD
            state["strategy"]["baseline_migrated_from"] = old_threshold
            state["strategy"]["status"] = "baseline"
    return state


# ============================================================================
# FILTER: PASSES_MARKET_FILTER (ultra-hardened)
# ============================================================================

def passes_market_filter(pair_data):
    """
    Returns: (bool, reason_str)
    STRICT filters - only ELITE opportunities pass.
    """
    if not pair_data:
        return False, "missing_pair_data"
    
    # Age check - avoid new rugpulls
    if "pairCreatedAt" not in pair_data:
        return False, "missing_pair_age"
    
    try:
        created_ms = int(pair_data.get("pairCreatedAt", 0))
    except (TypeError, ValueError):
        return False, "invalid_pair_age"
    if created_ms <= 0:
        return False, "invalid_pair_age"
    now_ms = int(time.time() * 1000)
    age_sec = (now_ms - created_ms) / 1000.0
    
    if age_sec < MIN_PAIR_AGE_SECONDS:
        return False, f"pair_too_new:{age_sec:.0f}s"
    
    # Liquidity check - THICK order book
    liq = pair_data.get("liquidity", {}).get("usd", 0) or 0
    if liq < MIN_LIQUIDITY_USD:
        return False, f"low_liquidity:{liq:.0f}"
    
    # Volume check - proven activity
    vol = pair_data.get("volume", {}).get("h1", 0) or 0
    if vol < MIN_VOLUME_H1_USD:
        return False, f"low_volume_h1:{vol:.0f}"
    
    # Market cap ratio check - avoid traps
    mcap = pair_data.get("marketCap") or pair_data.get("fdv") or 0
    try:
        mcap = float(mcap)
    except (TypeError, ValueError):
        mcap = 0
    if not math.isfinite(mcap) or mcap <= 0:
        return False, "missing_market_cap"
    ratio = liq / mcap
    if ratio < MIN_LIQUIDITY_TO_MCAP_RATIO:
        return False, f"liq_mcap_ratio:{ratio:.2f}"
    
    # Buy/sell ratio check - STRONG buyers
    buys = pair_data.get("txns", {}).get("m5", {}).get("buys", 0) or 0
    sells = pair_data.get("txns", {}).get("m5", {}).get("sells", 0) or 1
    buy_sell_ratio = buys / max(sells, 1)
    if buy_sell_ratio < MIN_BUY_SELL_RATIO_M5:
        return False, f"weak_buy_pressure:{buy_sell_ratio:.2f}"
    
    # Momentum checks - REAL momentum
    pc_m5 = pair_data.get("priceChange", {}).get("m5", 0) or 0
    if pc_m5 < MIN_PRICE_CHANGE_M5_PCT:
        return False, f"weak_momentum_m5:{pc_m5:.1f}%"
    if pc_m5 > MAX_PRICE_CHANGE_M5_PCT:
        return False, f"overextended_momentum_m5:{pc_m5:.1f}%"
    
    pc_h1 = pair_data.get("priceChange", {}).get("h1", 0) or 0
    if pc_h1 < 0:
        return False, f"negative_momentum_h1:{pc_h1:.1f}%"
    
    # No recent activity
    if buys == 0 and sells == 0:
        return False, "no_recent_activity"
    
    return True, "ok"


def passes_shadow_market_filter(pair_data):
    """Apply data-quality/liquidity floors for virtual research only, never live entries."""
    if not isinstance(pair_data, dict):
        return False, "missing_pair_data"
    try:
        created_ms = int(pair_data.get("pairCreatedAt", 0) or 0)
        liquidity = float((pair_data.get("liquidity") or {}).get("usd") or 0)
        volume = float((pair_data.get("volume") or {}).get("h1") or 0)
        market_cap = float(pair_data.get("marketCap") or pair_data.get("fdv") or 0)
        price = float(pair_data.get("priceUsd") or 0)
        txns = (pair_data.get("txns") or {}).get("m5") or {}
        activity = float(txns.get("buys", 0) or 0) + float(txns.get("sells", 0) or 0)
    except (TypeError, ValueError, AttributeError, OverflowError):
        return False, "invalid_market_data"
    values = (liquidity, volume, market_cap, price, activity)
    if not all(math.isfinite(value) for value in values):
        return False, "non_finite_market_data"
    if created_ms <= 0:
        return False, "missing_pair_age"
    age_seconds = (time.time() * 1000 - created_ms) / 1000.0
    if age_seconds < 300:
        return False, f"shadow_pair_too_new:{age_seconds:.0f}s"
    if liquidity < 1000:
        return False, f"shadow_low_liquidity:{liquidity:.0f}"
    if volume < 500:
        return False, f"shadow_low_volume_h1:{volume:.0f}"
    if market_cap <= 0:
        return False, "shadow_missing_market_cap"
    if liquidity / market_cap < 0.02:
        return False, f"shadow_liq_mcap_ratio:{liquidity / market_cap:.2f}"
    if price <= 0:
        return False, "shadow_invalid_price"
    if activity < 1:
        return False, "shadow_low_activity"
    return True, "ok"


# ============================================================================
# SCORING: MARKET OPPORTUNITY SCORE (ultra-hardened)
# ============================================================================

def _market_feature_snapshot(pair_data, pc_m5=None, pc_h1=None, buy_sell_ratio=None):
    """Capture raw entry features separately from score contributions for later analysis."""
    pair_data = pair_data if isinstance(pair_data, dict) else {}
    try:
        m5 = float(pc_m5 if pc_m5 is not None else ((pair_data.get("priceChange") or {}).get("m5") or 0))
        h1 = float(pc_h1 if pc_h1 is not None else ((pair_data.get("priceChange") or {}).get("h1") or 0))
        buys = float((((pair_data.get("txns") or {}).get("m5") or {}).get("buys")) or 0)
        sells = float((((pair_data.get("txns") or {}).get("m5") or {}).get("sells")) or 0)
        ratio = float(buy_sell_ratio if buy_sell_ratio is not None else buys / max(sells, 1.0))
        liquidity = float(((pair_data.get("liquidity") or {}).get("usd")) or 0)
        volume_h1 = float(((pair_data.get("volume") or {}).get("h1")) or 0)
        market_cap = float(pair_data.get("marketCap") or pair_data.get("fdv") or 0)
    except (TypeError, ValueError, AttributeError, OverflowError, ZeroDivisionError):
        return {}
    values = {
        "momentum_m5_pct": m5,
        "momentum_h1_pct": h1,
        "buy_sell_ratio": ratio,
        "liquidity_usd": liquidity,
        "volume_h1_usd": volume_h1,
        "market_cap_usd": market_cap,
        "liquidity_to_mcap_ratio": liquidity / market_cap if market_cap > 0 else 0.0,
    }
    return {key: value for key, value in values.items() if math.isfinite(value)}


def score_market_opportunity(pair_data, learning=None):
    """
    Score 0-100 based on proven factors:
    - Momentum (m5 strong short-term signal)
    - Buy/sell ratio (organic demand)
    - Liquidity strength
    - Learning adjustment
    
    Returns: (score: float, components: dict)
    """
    if not pair_data or not passes_market_filter(pair_data)[0]:
        return 0.0, {}
    
    score = 50.0  # baseline
    components = {}
    
    # 1. Price momentum (m5 = short-term signal, h1 = confirmation)
    pc_m5 = pair_data.get("priceChange", {}).get("m5", 0) or 0
    pc_h1 = pair_data.get("priceChange", {}).get("h1", 0) or 0
    momentum_bonus = min(25.0, pc_m5 * 3.0 + pc_h1 * 1.0)
    score += momentum_bonus
    components["momentum"] = momentum_bonus
    
    # 2. Buy/sell pressure (strong buyers = strong signal)
    buys = pair_data.get("txns", {}).get("m5", {}).get("buys", 0) or 0
    sells = pair_data.get("txns", {}).get("m5", {}).get("sells", 0) or 1
    buy_sell_ratio = buys / max(sells, 1)
    buy_pressure_bonus = min(15.0, (buy_sell_ratio - 1.0) * 7.0)
    score += buy_pressure_bonus
    components["buy_pressure"] = buy_pressure_bonus
    components.update(_market_feature_snapshot(pair_data, pc_m5, pc_h1, buy_sell_ratio))

    # 3. Learning adjustment (if available)
    if learning and learning.get("trades", 0) >= 10 and learning.get("wins", 0) >= 0:
        win_rate = learning["wins"] / learning["trades"]
        if win_rate > 0.40:  # only reward learning after a minimum sample
            learning_bonus = 10.0
            score += learning_bonus
            components["learning_bonus"] = learning_bonus
    
    score = min(100.0, max(0.0, score))
    return score, components


def score_shadow_opportunity(pair_data, learning=None):
    """Score a research-only candidate without applying live-entry filters."""
    if not isinstance(pair_data, dict):
        return 0.0, {}
    try:
        pc_m5 = float((pair_data.get("priceChange") or {}).get("m5") or 0)
        pc_h1 = float((pair_data.get("priceChange") or {}).get("h1") or 0)
        buys = float(((pair_data.get("txns") or {}).get("m5") or {}).get("buys") or 0)
        sells = float(((pair_data.get("txns") or {}).get("m5") or {}).get("sells") or 0)
    except (TypeError, ValueError, AttributeError, OverflowError):
        return 0.0, {}
    if not all(math.isfinite(value) for value in (pc_m5, pc_h1, buys, sells)) or min(buys, sells) < 0:
        return 0.0, {}
    score = 50.0
    momentum_bonus = min(25.0, pc_m5 * 3.0 + pc_h1)
    score += momentum_bonus
    ratio = buys / max(sells, 1.0)
    buy_pressure_bonus = min(15.0, (ratio - 1.0) * 7.0)
    score += buy_pressure_bonus
    components = {"momentum": momentum_bonus, "buy_pressure": buy_pressure_bonus}
    components.update(_market_feature_snapshot(pair_data, pc_m5, pc_h1, ratio))
    if isinstance(learning, dict):
        try:
            trades = int(learning.get("trades", 0) or 0)
            wins = int(learning.get("wins", 0) or 0)
            if trades >= 10 and 0 <= wins <= trades and wins / trades > 0.40:
                score += 10.0
                components["learning_bonus"] = 10.0
        except (TypeError, ValueError, OverflowError, ZeroDivisionError):
            pass
    return min(100.0, max(0.0, score)), components


# ============================================================================
# CANDIDATE CONFIRMATION: 3 consecutive observations required
# ============================================================================

def candidate_confirmation(state, mint, signal_score):
    """Require fresh, consecutive above-threshold observations before entry."""
    threshold = state.get("strategy", {}).get("active_threshold", OPPORTUNITY_ENTRY_THRESHOLD)
    obs = state.setdefault("candidate_observations", {})
    now = time.time()
    entry = obs.get(mint)

    if not isinstance(entry, dict):
        if signal_score >= threshold:
            obs[mint] = {"hits": 1, "last_score": signal_score, "last_seen": now}
        else:
            obs.pop(mint, None)
        return False

    try:
        last_seen = float(entry.get("last_seen", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        last_seen = 0.0
    gap = now - last_seen
    if last_seen <= 0 or not math.isfinite(last_seen) or gap < 0 or gap > CANDIDATE_CONFIRMATION_MAX_GAP_SECONDS:
        # Legacy observations without timestamps and stale candidates restart at one hit.
        entry["hits"] = 1 if signal_score >= threshold else 0
        entry["last_score"] = signal_score
        entry["last_seen"] = now
        return False

    if signal_score < threshold:
        entry["hits"] = 0
        entry["last_score"] = signal_score
        entry["last_seen"] = now
        return False

    entry["hits"] = max(0, int(entry.get("hits", 0))) + 1
    entry["last_score"] = signal_score
    entry["last_seen"] = now
    return entry["hits"] >= CANDIDATE_CONFIRMATION_HITS


# ============================================================================
# POSITION MANAGEMENT
# ============================================================================

def open_position(state, mint, pair_data, whale_balances, signal_score, signal_components):
    """
    Try to open a position. Returns: bool (success?)
    Strict risk management: 0.5% per trade, max 4 concurrent.
    """
    # Drawdown circuit breaker: preserve the paper account after a severe strategy failure.
    if update_risk_control(state).get("status") == "halted":
        return False

    # Circuit breakers
    if mint in state.get("positions", {}):
        return False  # already open
    
    if mint in state.get("blacklist", []):
        return False  # blacklisted
    
    cooldowns = state.get("cooldowns", {})
    if isinstance(cooldowns, dict) and cooldowns.get(mint):
        try:
            cooldown_until = float(cooldowns[mint])
        except (TypeError, ValueError):
            cooldown_until = 0
        if cooldown_until > time.time():
            return False  # in cooldown
    
    # Max concurrent positions
    num_open = len(state.get("positions", {}))
    if num_open >= MAX_CONCURRENT_POSITIONS:
        return False  # position limit
    
    # Minimum equity check
    equity = state.get("equity", 1000.0)
    if equity < 200.0:
        return False  # minimum equity gate
    
    # Position sizing: 0.5% risk per trade
    max_pos_size = equity * MAX_POSITION_EQUITY_FRACTION
    
    # Current exposure
    current_exposure = sum(
        p.get("amount_usd", 0)
        for p in state.get("positions", {}).values()
    )
    max_portfolio_size = equity * MAX_PORTFOLIO_EXPOSURE_FRACTION
    
    if current_exposure >= max_portfolio_size:
        return False  # portfolio limit
    
    position_size = min(max_pos_size, max_portfolio_size - current_exposure)
    
    if position_size < 1.0:
        return False  # position too small for a meaningful paper-trade sample
    
    # Validate price and account for conservative estimated entry slippage.
    try:
        price = float(pair_data.get("priceUsd", 0) or 0)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(price) or price <= 0:
        return False
    entry_price = price * (1.0 + ENTRY_SLIPPAGE_BPS / 10000.0)
    symbol = pair_data.get("baseToken", {}).get("symbol", "?")
    
    # Open position
    state["positions"][mint] = {
        "symbol": symbol,
        "entry_price": entry_price,
        "entry_time": time.time(),
        "entry_liquidity": pair_data.get("liquidity", {}).get("usd", 0) or 0,
        "amount_usd": position_size,
        "peak_price": price,
        "trailing_armed": False,
        "signal_score": signal_score,
        "entry_components": signal_components,
        "whale_balances": dict(whale_balances) if isinstance(whale_balances, dict) else {},
    }
    
    return True


def liquidity_drain_detected(position, current_liquidity_usd):
    """
    Whale detection: liquidity drying up = red flag.
    - If drop > 25% from entry = IMMEDIATE EXIT (was 30%)
    - If drop > 50% from entry = need 2 confirmations
    
    Returns: bool (should exit?)
    """
    entry_liq = position.get("entry_liquidity", 0)
    if entry_liq <= 0:
        return False
    
    drop_pct = (entry_liq - current_liquidity_usd) / entry_liq
    
    # Catastrophic drains need two consecutive observations; critical drains
    # between 25% and 50% exit immediately.
    if drop_pct >= 0.50:
        hits = int(position.get("liquidity_drain_hits", 0) or 0)
        if hits >= 1:
            return True
        position["liquidity_drain_hits"] = hits + 1
        return False
    if drop_pct >= LIQUIDITY_DRAIN_CRITICAL:
        return True
    
    # Recovery: reset counter
    position["liquidity_drain_hits"] = 0
    return False


def close_position(state, mint, exit_price, reason):
    """
    Close a position and log the P&L.
    Updates learning, applies cooldown/blacklist as needed.
    """
    pos = state.get("positions", {}).pop(mint, None)
    if not pos:
        return
    
    entry_price = pos.get("entry_price", 0)
    amount_usd = pos.get("amount_usd", 0)
    
    if entry_price <= 0 or amount_usd <= 0:
        return
    
    effective_exit_price = (
        float(exit_price) * (1.0 - EXIT_SLIPPAGE_BPS / 10000.0)
        if float(exit_price) > 0 else 0.0
    )
    pnl_pct = (effective_exit_price - entry_price) / entry_price
    pnl_usd = amount_usd * pnl_pct
    
    # Update equity
    state["equity"] = state.get("equity", 1000.0) + pnl_usd
    state["trade_count"] = state.get("trade_count", 0) + 1
    
    # Track learning by score bucket
    learning = state.setdefault("learning", {"trades": 0, "wins": 0, "buckets": {}})
    learning["trades"] += 1
    if pnl_usd > 0:
        learning["wins"] += 1
    
    score = pos.get("signal_score", 0)
    score_bucket = str(int(score // 10) * 10)
    bucket = learning["buckets"].setdefault(score_bucket, {"n": 0, "wins": 0, "avg_pnl": 0.0})
    old_n = bucket["n"]
    bucket["n"] += 1
    bucket["wins"] += 1 if pnl_usd > 0 else 0
    bucket["avg_pnl"] = (bucket["avg_pnl"] * old_n + pnl_pct * 100) / bucket["n"]
    
    # Cooldown on stop-loss
    if reason in ("stop_loss", "hard_stop_loss"):
        state.setdefault("cooldowns", {})[mint] = time.time() + (STOP_LOSS_COOLDOWN_MINUTES * 60)
    
    # Blacklist on dangerous reasons
    if reason in ("liquidity_drained", "whale_dump", "force_close_no_data"):
        if mint not in state.get("blacklist", []):
            state["blacklist"].append(mint)
    
    append_trade(mint, pos, entry_price, effective_exit_price, reason, pnl_pct, pnl_usd, state["equity"])
    append_learning_observation(pos.get("signal_score"), pnl_pct * 100.0, reason, pos.get("entry_components"))
    symbol = pos.get("symbol", "?")
    log(f"[CLOSE] {symbol:8s} {reason:20s} | entry=${entry_price:.8f} exit=${effective_exit_price:.8f} | pnl={pnl_pct*100:+6.2f}% | equity=${state['equity']:.2f}")


def fetch_dex_pair(mint):
    """Fetch the most liquid current DexScreener pair for a token mint."""
    if not mint:
        return None
    try:
        response = requests.get(DEXSCREENER_TOKEN_PAIRS_URL.format(mint), timeout=HTTP_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()
        pairs = payload.get("pairs") or []
        if not isinstance(pairs, list):
            return None
        valid = []
        for pair in pairs:
            if not isinstance(pair, dict) or pair.get("chainId") != "solana":
                continue
            try:
                liquidity = float((pair.get("liquidity") or {}).get("usd") or 0)
                price = float(pair.get("priceUsd") or 0)
            except (TypeError, ValueError, AttributeError):
                continue
            if liquidity > 0 and price > 0 and math.isfinite(liquidity) and math.isfinite(price):
                valid.append(pair)
        return max(valid, key=lambda p: float(p["liquidity"]["usd"])) if valid else None
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        return None


def fetch_candidate_mints(limit=40):
    """Fetch a bounded, interleaved Solana candidate list from boost, profile, and search feeds."""
    if limit <= 0:
        return []
    feeds = []
    for url in (DEXSCREENER_TOKEN_BOOSTS_URL, DEXSCREENER_TOKEN_TOP_BOOSTS_URL, DEXSCREENER_TOKEN_PROFILES_URL):
        items_for_feed = []
        local_seen = set()
        try:
            response = requests.get(url, timeout=HTTP_TIMEOUT_SECONDS)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                feeds.append(items_for_feed)
                continue
            for item in payload:
                if not isinstance(item, dict) or item.get("chainId") != "solana":
                    continue
                mint = item.get("tokenAddress")
                if isinstance(mint, str) and mint and mint not in local_seen:
                    local_seen.add(mint)
                    items_for_feed.append(mint)
        except (requests.RequestException, ValueError, TypeError):
            items_for_feed = []
        feeds.append(items_for_feed)

    # Latest boosts/profiles are heavily skewed toward newly surfaced tokens.
    # Add pair-search results for established Solana meme assets so the age and
    # liquidity filters have candidates they can realistically admit.
    search_items = []
    search_seen = set()
    for term in DISCOVERY_SEARCH_TERMS:
        try:
            response = requests.get(DEXSCREENER_SEARCH_URL, params={"q": term}, timeout=HTTP_TIMEOUT_SECONDS)
            response.raise_for_status()
            payload = response.json()
            pairs = payload.get("pairs") or [] if isinstance(payload, dict) else []
            if not isinstance(pairs, list):
                continue
            for pair in pairs:
                if not isinstance(pair, dict) or pair.get("chainId") != "solana":
                    continue
                for token_key in ("baseToken", "quoteToken"):
                    token = pair.get(token_key) or {}
                    identity = f"{token.get('symbol', '')} {token.get('name', '')}".upper()
                    mint = token.get("address")
                    if term in identity and isinstance(mint, str) and mint and mint not in search_seen:
                        search_seen.add(mint)
                        search_items.append(mint)
                        break
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            continue
    feeds.append(search_items)

    source_counts = {
        "latest_boosts": len(feeds[0]),
        "top_boosts": len(feeds[1]),
        "profiles": len(feeds[2]),
        "meme_search": len(feeds[3]),
    }
    # Interleave feeds so a large first response cannot starve any one source.
    found, seen = [], set()
    max_rows = max((len(feed) for feed in feeds), default=0)
    for index in range(max_rows):
        for feed in feeds:
            if index < len(feed):
                mint = feed[index]
                if mint not in seen:
                    seen.add(mint)
                    found.append(mint)
                    if len(found) >= limit:
                        log(f"[DISCOVERY] selected={len(found)} source_counts={source_counts}")
                        return found
    log(f"[DISCOVERY] selected={len(found)} source_counts={source_counts}")
    return found


def manage_open_positions(state):
    """Manage positions using fresh market data; never reuse a stale peak as a quote."""
    for mint in list(state.get("positions", {}).keys()):
        pos = state["positions"].get(mint)
        if not isinstance(pos, dict):
            state["positions"].pop(mint, None)
            continue
        # Validate persisted position fields before arithmetic; malformed legacy state
        # must not abort the entire scheduled scan.
        try:
            entry_price = float(pos.get("entry_price", 0) or 0)
            amount_usd = float(pos.get("amount_usd", 0) or 0)
            entry_time = float(pos.get("entry_time", 0) or 0)
            peak_price = float(pos.get("peak_price", entry_price) or entry_price)
        except (TypeError, ValueError, OverflowError):
            state["positions"].pop(mint, None)
            log(f"[WARN] discarded malformed position state for {mint}")
            continue
        if (not all(math.isfinite(v) for v in (entry_price, amount_usd, entry_time, peak_price))
                or entry_price <= 0 or amount_usd <= 0 or entry_time < 0 or peak_price <= 0):
            state["positions"].pop(mint, None)
            log(f"[WARN] discarded invalid position values for {mint}")
            continue
        pos["entry_time"] = entry_time
        pair = fetch_dex_pair(mint)
        if not pair:
            # A missing quote is not an executable price. Closing at $0 fabricated
            # near-100% paper losses whenever DexScreener temporarily omitted a pair.
            # Keep the position unresolved until a valid quote returns; do not
            # pretend a sale occurred at a price the feed never supplied.
            first_missing = pos.setdefault("price_missing_since", time.time())
            try:
                missing_for = max(0.0, time.time() - float(first_missing))
            except (TypeError, ValueError, OverflowError):
                pos["price_missing_since"] = time.time()
                missing_for = 0.0
            pos["data_status"] = "stale"
            if missing_for >= MAX_MISSING_PRICE_SECONDS:
                log(f"[WARN] no executable quote for {mint} for {missing_for:.0f}s; position retained as unresolved, no zero-price close")
            continue
        try:
            current_price = float(pair.get("priceUsd") or 0)
            current_liquidity = float((pair.get("liquidity") or {}).get("usd") or 0)
        except (TypeError, ValueError, AttributeError, OverflowError):
            continue
        if not math.isfinite(current_price) or current_price <= 0:
            continue
        if not math.isfinite(current_liquidity) or current_liquidity < 0:
            current_liquidity = 0.0
        pos.pop("price_missing_since", None)
        pos.pop("data_status", None)
        if helius_configured():
            current_whales = fetch_whale_balances(mint)
            if current_whales:
                previous_whales = pos.get("whale_balances") or state["whale_snapshots"].get(mint, {})
                if previous_whales and confirmed_whale_dump(previous_whales, current_whales, pair):
                    state["whale_snapshots"][mint] = current_whales
                    close_position(state, mint, current_price, "whale_dump")
                    log(f"[WHALE] confirmed holder-balance drop with sell pressure for {mint}")
                    continue
                pos["whale_balances"] = current_whales
                state["whale_snapshots"][mint] = current_whales
            else:
                log(f"[WARN] whale snapshot unavailable for open position {mint}; price/risk exits remain active")
        pos["peak_price"] = max(peak_price, current_price)
        pnl_pct = (current_price - entry_price) / entry_price if entry_price > 0 else 0.0
        if pnl_pct >= 0.08:
            pos["trailing_armed"] = True
        if pos.get("trailing_armed") and pos["peak_price"] > 0:
            drop_from_peak = (pos["peak_price"] - current_price) / pos["peak_price"]
            if drop_from_peak >= TRAILING_STOP_PCT:
                close_position(state, mint, current_price, "trailing_stop")
                continue
        if pnl_pct <= -HARD_STOP_LOSS_PCT:
            close_position(state, mint, current_price, "hard_stop_loss")
            continue
        if current_liquidity > 0 and liquidity_drain_detected(pos, current_liquidity):
            close_position(state, mint, current_price, "liquidity_drained")
            continue
        if time.time() - float(pos.get("entry_time", time.time())) >= MAX_HOLD_SECONDS:
            close_position(state, mint, current_price, "max_hold_timeout")



def manage_shadow_positions(state):
    """Close isolated shadow positions using the same price-risk rules, without touching account equity."""
    for mint in list(state.get("shadow_positions", {}).keys()):
        pos = state["shadow_positions"].get(mint)
        if not isinstance(pos, dict):
            state["shadow_positions"].pop(mint, None)
            continue
        try:
            entry_price = float(pos.get("entry_price", 0) or 0)
            entry_time = float(pos.get("entry_time", 0) or 0)
            peak_price = float(pos.get("peak_price", entry_price) or entry_price)
        except (TypeError, ValueError, OverflowError):
            state["shadow_positions"].pop(mint, None)
            continue
        if not all(math.isfinite(v) for v in (entry_price, entry_time, peak_price)) or entry_price <= 0:
            state["shadow_positions"].pop(mint, None)
            continue
        age_seconds = time.time() - entry_time
        if age_seconds > SHADOW_STALE_OBSERVATION_SECONDS:
            # Price is only known at this late scan, not at the intended 10-minute
            # exit. Recording it would contaminate research with unbounded hold times.
            state["shadow_positions"].pop(mint, None)
            log(f"[SHADOW DISCARD] {pos.get('symbol', '?')} stale observation age={age_seconds:.0f}s; excluded from strategy data")
            continue
        pair = fetch_dex_pair(mint)
        if not pair:
            continue
        try:
            price = float(pair.get("priceUsd") or 0)
            liquidity = float((pair.get("liquidity") or {}).get("usd") or 0)
        except (TypeError, ValueError, AttributeError, OverflowError):
            continue
        if not math.isfinite(price) or price <= 0:
            continue
        pos["peak_price"] = max(peak_price, price)
        pnl = (price - entry_price) / entry_price
        reason = None
        if pnl >= 0.08:
            pos["trailing_armed"] = True
        if pos.get("trailing_armed") and pos["peak_price"] > 0:
            if (pos["peak_price"] - price) / pos["peak_price"] >= TRAILING_STOP_PCT:
                reason = "trailing_stop"
        if reason is None and pnl <= -HARD_STOP_LOSS_PCT:
            reason = "hard_stop_loss"
        if reason is None and liquidity > 0 and liquidity_drain_detected(pos, liquidity):
            reason = "liquidity_drained"
        if reason is None and time.time() - entry_time >= MAX_HOLD_SECONDS:
            reason = "max_hold_timeout"
        if reason is None:
            continue
        effective_exit = price * (1.0 - EXIT_SLIPPAGE_BPS / 10000.0)
        net_pnl_pct = (effective_exit - entry_price) / entry_price * 100.0
        append_learning_observation(pos.get("signal_score"), net_pnl_pct, f"shadow_{reason}", pos.get("components"))
        state["shadow_positions"].pop(mint, None)
        log(f"[SHADOW CLOSE] {pos.get('symbol', '?')} {reason} pnl={net_pnl_pct:+.2f}% (isolated; account equity unchanged)")


def scan_shadow_candidates(state, candidates=None, increment_scan_count=True):
    """Collect labeled virtual-trade samples independently of account entry eligibility."""
    candidates = fetch_candidate_mints() if candidates is None else candidates
    opened = []
    rejections = {}
    filter_passed = 0
    # Shadow samples cover a broad score range; this never changes live entry rules.
    threshold = SHADOW_MIN_SCORE
    for mint in candidates:
        if len(state["shadow_positions"]) >= MAX_SHADOW_POSITIONS:
            break
        if mint in state["shadow_positions"] or mint in state["blacklist"]:
            continue
        pair = fetch_dex_pair(mint)
        if not pair:
            rejections["pair_data_unavailable"] = rejections.get("pair_data_unavailable", 0) + 1
            state["candidate_observations"].pop(mint, None)
            continue
        passes, reason = passes_shadow_market_filter(pair)
        if not passes:
            rejections[reason] = rejections.get(reason, 0) + 1
            state["candidate_observations"].pop(mint, None)
            continue
        filter_passed += 1
        score, components = score_shadow_opportunity(pair, state.get("learning"))
        if score < threshold:
            state["candidate_observations"].pop(mint, None)
            continue
        # Shadow samples need no live three-hit confirmation because no account capital is at risk.
        try:
            price = float(pair.get("priceUsd") or 0)
            liquidity = float((pair.get("liquidity") or {}).get("usd") or 0)
        except (TypeError, ValueError, AttributeError, OverflowError):
            continue
        if not math.isfinite(price) or price <= 0 or not math.isfinite(liquidity) or liquidity <= 0:
            continue
        entry_price = price * (1.0 + ENTRY_SLIPPAGE_BPS / 10000.0)
        state["shadow_positions"][mint] = {
            "symbol": (pair.get("baseToken") or {}).get("symbol", "?"),
            "entry_price": entry_price,
            "entry_time": time.time(),
            "entry_liquidity": liquidity,
            "peak_price": entry_price,
            "signal_score": score,
            "trailing_armed": False,
            "components": components,
        }
        opened.append(mint)
    if increment_scan_count:
        state["scan_count"] = int(state.get("scan_count", 0)) + 1
    state["shadow_scan_diagnostics"] = {
        "timestamp": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "candidate_count": len(candidates),
        "filter_passed": filter_passed,
        "shadow_opened": len(opened),
        "rejections": rejections,
    }
    log(f"[SHADOW DIAGNOSTICS] candidates={len(candidates)} filter_passed={filter_passed} rejections={rejections}")
    return {"scanned": len(candidates), "shadow_opened": opened, "filter_passed": filter_passed, "rejections": rejections}


def append_trade(mint, position, entry_price, exit_price, reason, pnl_pct, pnl_usd, equity_after):
    """Append a closed paper trade using the exact schema consumed by the dashboard."""
    path = Path("trades_log.csv")
    headers = ["timestamp", "mint", "symbol", "entry_price", "exit_price", "pnl_pct", "pnl_usd", "equity_after", "reason"]
    exists = path.exists() and path.stat().st_size > 0

    # Migrate any legacy header/rows before appending, so one old format cannot
    # silently corrupt every subsequent dashboard row.
    if exists:
        with path.open("r", newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            old_headers = reader.fieldnames or []
            old_rows = list(reader)
        if old_headers != headers:
            migrated = []
            for row in old_rows:
                try:
                    pct = float(row.get("pnl_pct") or 0)
                except (TypeError, ValueError):
                    pct = 0.0
                try:
                    amount = float(row.get("amount_usd") or 0)
                except (TypeError, ValueError):
                    amount = 0.0
                migrated.append([
                    row.get("timestamp", ""), row.get("mint", ""), row.get("symbol", ""),
                    row.get("entry_price", ""), row.get("exit_price", ""), row.get("pnl_pct", ""),
                    row.get("pnl_usd") or (f"{amount * pct / 100.0:.6f}" if "amount_usd" in row else ""),
                    row.get("equity_after", ""), row.get("reason", ""),
                ])
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(headers)
                writer.writerows(migrated)
            exists = True

    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if not exists:
            writer.writerow(headers)
        writer.writerow([
            datetime.utcnow().isoformat(), mint, position.get("symbol", "?"),
            f"{entry_price:.12g}", f"{exit_price:.12g}", f"{pnl_pct * 100:.6f}",
            f"{pnl_usd:.6f}", f"{equity_after:.6f}", reason,
        ])


def append_learning_observation(score, pnl_pct, reason, features=None):
    """Persist outcomes and entry features; migrate old CSVs without fabricating historical features."""
    # Shadow outcomes are research-only and must never train or validate the live-entry threshold.
    filename = "shadow_observations.csv" if str(reason).startswith("shadow_") else "strategy_observations.csv"
    path = Path(filename)
    headers = ["timestamp", "score", "pnl_pct", "reason", "features_json"]
    exists = path.exists() and path.stat().st_size > 0
    if exists:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            old_headers = reader.fieldnames or []
            old_rows = list(reader)
        if "features_json" not in old_headers:
            migrated = io.StringIO(newline="")
            writer = csv.DictWriter(migrated, fieldnames=headers, extrasaction="ignore")
            writer.writeheader()
            for old_row in old_rows:
                old_row["features_json"] = "{}"
                writer.writerow(old_row)
            path.write_text(migrated.getvalue(), encoding="utf-8")
    try:
        score_value = float(score) if score is not None else None
        if score_value is not None and (not math.isfinite(score_value) or not 0.0 <= score_value <= 100.0):
            score_value = None
    except (TypeError, ValueError, OverflowError):
        score_value = None
    try:
        pnl_value = float(pnl_pct)
        if not math.isfinite(pnl_value):
            return
    except (TypeError, ValueError, OverflowError):
        return
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if not exists:
            writer.writerow(headers)
        feature_values = features if isinstance(features, dict) else {}
        safe_features = {}
        for key, value in feature_values.items():
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(number):
                safe_features[str(key)] = number
        writer.writerow([
            datetime.utcnow().isoformat(), "" if score_value is None else score_value,
            pnl_value, reason, json.dumps(safe_features, sort_keys=True, separators=(",", ":")),
        ])


def reconcile_learning_from_trade_log(state, trades_path="trades_log.csv"):
    """Reconcile headline learning totals to the ledger; preserve unknown historical scores."""
    path = Path(trades_path)
    if not path.exists() or path.stat().st_size == 0:
        return state
    valid_rows = []
    peak = float(state.get("initial_equity", 1000.0) or 1000.0)
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if not row.get("timestamp") or not row.get("mint"):
                continue
            try:
                pnl_usd = float(row.get("pnl_usd", ""))
                equity_after = float(row.get("equity_after", ""))
                if not math.isfinite(pnl_usd) or not math.isfinite(equity_after):
                    continue
            except (TypeError, ValueError, OverflowError):
                continue
            valid_rows.append((pnl_usd, equity_after))
            peak = max(peak, equity_after)
    if not valid_rows:
        return state
    learning = state.setdefault("learning", {})
    if not isinstance(learning, dict):
        learning = {}
        state["learning"] = learning
    bucketed = 0
    buckets = learning.get("buckets", {})
    if isinstance(buckets, dict):
        for bucket in buckets.values():
            if isinstance(bucket, dict):
                try:
                    bucketed += max(0, int(bucket.get("n", 0)))
                except (TypeError, ValueError, OverflowError):
                    continue
    learning["trades"] = len(valid_rows)
    learning["wins"] = sum(1 for pnl_usd, _ in valid_rows if pnl_usd > 0)
    learning["bucketed_trades"] = bucketed
    learning["unbucketed_trades"] = max(0, len(valid_rows) - bucketed)
    learning["totals_source"] = "trades_log.csv"
    state["trade_count"] = len(valid_rows)
    state["peak_equity"] = max(float(state.get("peak_equity", peak) or peak), peak)
    return state

def _recover_state_fail_closed(reason):
    """Recover ledger totals after corrupt state, and keep all account entries halted."""
    state = normalize_state({})
    ledger = Path("trades_log.csv")
    valid_rows = []
    peak = float(state.get("initial_equity", 1000.0))
    if ledger.exists():
        try:
            with ledger.open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    try:
                        equity_after = float(row.get("equity_after", ""))
                        pnl_usd = float(row.get("pnl_usd", ""))
                        if not row.get("timestamp") or not row.get("mint") or not math.isfinite(equity_after) or not math.isfinite(pnl_usd):
                            continue
                    except (TypeError, ValueError, OverflowError):
                        continue
                    valid_rows.append((equity_after, pnl_usd))
                    peak = max(peak, equity_after)
        except OSError:
            pass
    if valid_rows:
        state["equity"] = valid_rows[-1][0]
        state["trade_count"] = len(valid_rows)
        state["peak_equity"] = peak
    state["state_recovery_required"] = True
    update_risk_control(state)
    state["risk_control"]["status"] = "halted"
    state["risk_control"]["reason"] = "state_recovery_required"
    log(f"[CRITICAL] {reason}; recovered ledger equity=\u0024{state['equity']:.2f}; account entries remain halted pending state repair")
    return state


def load_state():
    """Load state; corrupt state is reconstructed from the ledger and fails closed."""
    import json
    path = Path("state.json")
    if not path.exists():
        return normalize_state({})
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _recover_state_fail_closed(f"state.json unreadable ({type(exc).__name__})")
    if not isinstance(loaded, dict):
        return _recover_state_fail_closed("state.json root is not an object")
    return normalize_legacy_strategy_baseline(normalize_state(loaded))


def save_state(state):
    """Atomically save state to disk to avoid partial JSON writes."""
    import json
    path = Path("state.json")
    temp_path = path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(normalize_state(state), indent=2) + "\n", encoding="utf-8")
    temp_path.replace(path)


# ============================================================================
# LOGGING
# ============================================================================

def log(msg):
    """Simple logging with timestamp."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")


# ============================================================================
# EXPORTS FOR TESTS
# ============================================================================

__all__ = [
    "normalize_state",
    "normalize_legacy_strategy_baseline",
    "fetch_dex_pair",
    "fetch_candidate_mints",
    "append_trade",
    "append_learning_observation",
    "passes_market_filter",
    "passes_shadow_market_filter",
    "score_market_opportunity",
    "score_shadow_opportunity",
    "candidate_confirmation",
    "open_position",
    "close_position",
    "liquidity_drain_detected",
    "manage_open_positions",
    "manage_shadow_positions",
    "scan_shadow_candidates",
    "run_isolated_paper_cycle",
    "load_state",
    "save_state",
    "log",
    # Constants
    "MIN_LIQUIDITY_USD",
    "MIN_VOLUME_H1_USD",
    "MIN_LIQUIDITY_TO_MCAP_RATIO",
    "MIN_BUY_SELL_RATIO_M5",
    "MIN_PRICE_CHANGE_M5_PCT",
    "MIN_PAIR_AGE_SECONDS",
    "ENTRY_SLIPPAGE_BPS",
    "EXIT_SLIPPAGE_BPS",
    "MAX_HOLD_SECONDS",
    "OPPORTUNITY_ENTRY_THRESHOLD",
    "MAX_CONCURRENT_POSITIONS",
    "MAX_POSITION_EQUITY_FRACTION",
    "MAX_PORTFOLIO_EXPOSURE_FRACTION",
    "TRAILING_STOP_PCT",
    "HARD_STOP_LOSS_PCT",
    "LIQUIDITY_DRAIN_CRITICAL",
    "STOP_LOSS_COOLDOWN_MINUTES",
    "CANDIDATE_CONFIRMATION_HITS",
]


def run_cycle(state=None):
    """Run one paper-trading scan cycle using current DexScreener data."""
    state = normalize_state(state if state is not None else load_state())
    state = normalize_legacy_strategy_baseline(state)
    reconcile_learning_from_trade_log(state)
    manage_open_positions(state)
    manage_shadow_positions(state)
    opened = []
    if int(state.get("trade_count", 0)) >= TARGET_TRADES:
        state["last_run"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        save_state(state)
        log(f"[CYCLE] paper-trade target reached ({TARGET_TRADES}); new entries disabled")
        return {"scanned": 0, "opened": opened, "open_positions": len(state["positions"]), "equity": state["equity"], "target_reached": True}
    risk = update_risk_control(state)
    if risk["status"] == "halted":
        # No account entries. Continue scans with isolated shadow positions to gather
        # labeled evidence without risking the already impaired paper account.
        shadow = scan_shadow_candidates(state)
        state["last_run"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        save_state(state)
        log(f"[RISK HALT] drawdown={risk['drawdown_pct']:.2f}% exceeded max {risk['max_drawdown_pct']:.2f}%; account entries blocked; shadow_scanned={shadow['scanned']} shadow_opened={len(shadow['shadow_opened'])}")
        return {"scanned": shadow["scanned"], "opened": opened, "shadow_opened": shadow["shadow_opened"], "open_positions": len(state["positions"]), "shadow_positions": len(state["shadow_positions"]), "equity": state["equity"], "risk_halted": True, "risk_control": risk}
    candidates = fetch_candidate_mints()
    # Keep a research-only cohort running even while the paper account is healthy.
    # Without this, the independent account would produce no labeled outcomes until
    # a drawdown halt, leaving the active strategy without current-market evidence.
    scan_shadow_candidates(state, candidates=candidates, increment_scan_count=False)
    diagnostics = {
        "timestamp": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "candidate_count": len(candidates),
        "market_filter_passed": 0,
        "score_passed": 0,
        "confirmation_pending": 0,
        "opened": 0,
        "evaluated_candidates": [],
        "near_misses": [],
        "rejections": {},
    }

    def reject(reason):
        diagnostics["rejections"][reason] = diagnostics["rejections"].get(reason, 0) + 1

    for mint in candidates:
        if mint in state["positions"] or mint in state["blacklist"]:
            reject("already_open_or_blacklisted")
            continue
        pair = fetch_dex_pair(mint)
        if not pair:
            reject("pair_data_unavailable")
            continue
        market_ok, market_reason = passes_market_filter(pair)
        if not market_ok:
            reject(market_reason)
            continue
        diagnostics["market_filter_passed"] += 1
        score, components = score_market_opportunity(pair, state.get("learning"))
        threshold = float(state["strategy"].get("active_threshold", OPPORTUNITY_ENTRY_THRESHOLD))
        base_token = pair.get("baseToken") or {}
        candidate_snapshot = {
            "mint": mint,
            "symbol": base_token.get("symbol") or mint[:8],
            "score": round(float(score), 2),
            "threshold": round(threshold, 2),
            "eligible_by_score": score >= threshold,
            "momentum_m5_pct": components.get("momentum_m5_pct", (pair.get("priceChange") or {}).get("m5", 0)),
            "momentum_h1_pct": components.get("momentum_h1_pct", (pair.get("priceChange") or {}).get("h1", 0)),
            "buy_sell_ratio": components.get("buy_sell_ratio", 0),
            "liquidity_usd": components.get("liquidity_usd", (pair.get("liquidity") or {}).get("usd", 0)),
            "volume_h1_usd": components.get("volume_h1_usd", (pair.get("volume") or {}).get("h1", 0)),
            "liquidity_to_mcap_ratio": components.get("liquidity_to_mcap_ratio", 0),
        }
        if len(diagnostics["evaluated_candidates"]) < 5:
            diagnostics["evaluated_candidates"].append(candidate_snapshot)
        if score < threshold:
            state["candidate_observations"].pop(mint, None)
            reject("score_below_threshold")
            if score >= threshold - 10 and len(diagnostics["near_misses"]) < 5:
                diagnostics["near_misses"].append(candidate_snapshot)
            continue
        diagnostics["score_passed"] += 1
        whale_balances = {}
        if helius_configured():
            snapshot = fetch_whale_balances(mint)
            if not snapshot:
                log(f"[WARN] skipping entry for {mint}: whale data unavailable")
                reject("whale_data_unavailable")
                continue
            previous_snapshot = state["whale_snapshots"].get(mint, {})
            if previous_snapshot and confirmed_whale_dump(previous_snapshot, snapshot, pair):
                state["whale_snapshots"][mint] = snapshot
                state["candidate_observations"].pop(mint, None)
                log(f"[WHALE] entry blocked for {mint}: holder-balance drop plus market sell pressure")
                reject("confirmed_whale_dump")
                continue
            whale_balances = snapshot
            state["whale_snapshots"][mint] = snapshot
            while len(state["whale_snapshots"]) > 100:
                state["whale_snapshots"].pop(next(iter(state["whale_snapshots"])))
        if not candidate_confirmation(state, mint, score):
            diagnostics["confirmation_pending"] += 1
            reject("confirmation_pending")
            continue
        if open_position(state, mint, pair, whale_balances, score, components):
            opened.append(mint)
            diagnostics["opened"] += 1
        else:
            reject("position_or_risk_gate")
    state["paper_scan_diagnostics"] = diagnostics
    log(
        f"[PAPER DIAGNOSTICS] candidates={diagnostics['candidate_count']} "
        f"market_filter_passed={diagnostics['market_filter_passed']} "
        f"score_passed={diagnostics['score_passed']} opened={diagnostics['opened']} "
        f"rejections={diagnostics['rejections']}"
    )
    if diagnostics["near_misses"]:
        log(f"[PAPER NEAR MISSES] {diagnostics['near_misses']}")
    state["scan_count"] += 1
    state["last_run"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    save_state(state)
    log(f"[CYCLE] scanned={len(candidates)} opened={len(opened)} positions={len(state['positions'])} equity=${state['equity']:.2f}")
    return {"scanned": len(candidates), "opened": opened, "open_positions": len(state["positions"]), "equity": state["equity"]}


def run_isolated_paper_cycle(root_dir=None):
    """Run the same scanner against a fully separate virtual account directory."""
    root = Path(root_dir or Path.cwd()).resolve()
    paper_dir = root / "paper_account"
    paper_dir.mkdir(parents=True, exist_ok=True)
    previous_cwd = Path.cwd()
    try:
        os.chdir(paper_dir)
        return run_cycle()
    finally:
        os.chdir(previous_cwd)


if __name__ == "__main__":
    try:
        run_cycle()  # Preserve the historical account and its existing risk lock.
        paper_result = run_isolated_paper_cycle(Path(__file__).resolve().parent)
        log(
            "[PAPER ACCOUNT] independent cycle complete | "
            f"equity=${float(paper_result.get('equity', 0.0)):.2f} "
            f"positions={paper_result.get('open_positions', 0)} "
            f"trades={paper_result.get('trade_count', 'see paper ledger')}"
        )
    except Exception as exc:
        log(f"[ERROR] trading cycle failed: {type(exc).__name__}: {exc}")
        raise
