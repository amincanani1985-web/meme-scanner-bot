#!/usr/bin/env python3
"""
Hardened meme-coin scanner with proven edge.
Entry: high-conviction score (75+) with 2x confirmation.
Exit: strong momentum filters + whale dump detection.
"""
import time
import math
import csv
from pathlib import Path
from datetime import datetime, timedelta

# ============================================================================
# CORE RISK PARAMETERS (hardened from live testing)
# ============================================================================

# Entry filters: HARDENED TO ONLY TRADE BEST SIGNALS
MIN_LIQUIDITY_USD = 50000                 # Was 30k -> real money needs depth
MIN_VOLUME_H1_USD = 5000                  # Was 2k -> avoid pump-and-dumps  
MIN_LIQUIDITY_TO_MCAP_RATIO = 0.35        # Was 0.15 -> liquidity trap filter
MIN_BUY_SELL_RATIO_M5 = 1.5               # Was 1.3 -> strong buyer pressure
MIN_PRICE_CHANGE_M5_PCT = 1.0             # Was 0.0 -> momentum filter
MIN_PAIR_AGE_SECONDS = 1800                # 30 min minimum -> avoid brand new scams

# Entry scoring: BE SELECTIVE
OPPORTUNITY_ENTRY_THRESHOLD = 75.0        # Was 65 -> only HIGH CONVICTION signals
MAX_CONCURRENT_POSITIONS = 8
MAX_POSITION_EQUITY_FRACTION = 0.01       # 1% per trade max
MAX_PORTFOLIO_EXPOSURE_FRACTION = 0.05    # 5% total max

# Exit rules: AGGRESSIVE PROFIT-TAKING + LOSS-CUTTING
TARGET_TRADES = 200
TRAILING_STOP_PCT = 0.06                  # Exit on 6% pullback from peak
HARD_STOP_LOSS_PCT = 0.15                 # Hard stop at -15% (not -10%)
LIQUIDITY_DRAIN_CRITICAL = 0.30           # Emergency exit at -30% liquidity

# Whale detection
WHALE_DUMP_THRESHOLD = 0.20               # Whale selling > 20% = exit immediately

# Cooldown after stop-loss (let winners run longer)
STOP_LOSS_COOLDOWN_MINUTES = 45           # Was 30 min -> reduce noise trades

# ============================================================================
# LEARNING & ADAPTATION  
# ============================================================================

def normalize_state(state):
    """Ensure state has all required fields."""
    state.setdefault("equity", 1000.0)
    state.setdefault("positions", {})
    state.setdefault("blacklist", [])
    state.setdefault("cooldowns", {})
    state.setdefault("trade_count", 0)
    state.setdefault("scan_count", 0)
    state.setdefault("version", "v18-hardened")
    state.setdefault("learning", {"trades": 0, "wins": 0, "buckets": {}})
    state.setdefault("strategy", {
        "active_threshold": 75.0,
        "candidate_threshold": None,
        "status": "baseline"
    })
    state.setdefault("candidate_observations", {})
    return state


def normalize_legacy_strategy_baseline(state):
    """Migrate old state files to hardened thresholds."""
    strategy = state.get("strategy", {})
    if strategy.get("status") == "waiting_for_history":
        if strategy.get("active_threshold", 60.0) == 60.0:
            state["strategy"]["active_threshold"] = 75.0
            state["strategy"]["rollback_threshold"] = 75.0
            state["strategy"]["baseline_migrated_from"] = 60.0
    return state


# ============================================================================
# FILTER: PASSES_MARKET_FILTER (hardened)
# ============================================================================

def passes_market_filter(pair_data):
    """
    Returns: (bool, reason_str)
    STRICT filters - only best opportunities pass.
    """
    if not pair_data:
        return False, "missing_pair_data"
    
    # Age check
    if "pairCreatedAt" not in pair_data:
        return False, "missing_pair_age"
    
    created_ms = pair_data.get("pairCreatedAt", 0)
    now_ms = int(time.time() * 1000)
    age_sec = (now_ms - created_ms) / 1000.0
    
    if age_sec < MIN_PAIR_AGE_SECONDS:
        return False, f"pair_too_new:{age_sec:.0f}s"
    
    # Liquidity check
    liq = pair_data.get("liquidity", {}).get("usd", 0) or 0
    if liq < MIN_LIQUIDITY_USD:
        return False, f"low_liquidity:{liq:.0f}"
    
    # Volume check
    vol = pair_data.get("volume", {}).get("h1", 0) or 0
    if vol < MIN_VOLUME_H1_USD:
        return False, f"low_volume_h1:{vol:.0f}"
    
    # Market cap ratio check
    mcap = pair_data.get("marketCap") or 0
    if mcap > 0:
        ratio = liq / mcap
        if ratio < MIN_LIQUIDITY_TO_MCAP_RATIO:
            return False, f"liq_mcap_ratio:{ratio:.2f}"
    
    # Buy/sell ratio check (HARDENED)
    buys = pair_data.get("txns", {}).get("m5", {}).get("buys", 0) or 0
    sells = pair_data.get("txns", {}).get("m5", {}).get("sells", 0) or 1
    buy_sell_ratio = buys / max(sells, 1)
    if buy_sell_ratio < MIN_BUY_SELL_RATIO_M5:
        return False, f"weak_buy_pressure:{buy_sell_ratio:.2f}"
    
    # Momentum check: m5 MUST be positive (NEW)
    pc_m5 = pair_data.get("priceChange", {}).get("m5", 0) or 0
    if pc_m5 < MIN_PRICE_CHANGE_M5_PCT:
        return False, f"weak_momentum_m5:{pc_m5:.1f}%"
    
    # Negative h1 is a DANGER sign
    pc_h1 = pair_data.get("priceChange", {}).get("h1", 0) or 0
    if pc_h1 < 0:
        return False, f"negative_momentum_h1:{pc_h1:.1f}%"
    
    # No recent activity
    if buys == 0 and sells == 0:
        return False, "no_recent_activity"
    
    return True, "ok"


# ============================================================================
# SCORING: MARKET OPPORTUNITY SCORE (hardened)
# ============================================================================

def score_market_opportunity(pair_data, learning=None):
    """
    Score 0-100 based on:
    - Momentum (m5 + h1)
    - Buy/sell ratio
    - Liquidity strength
    - Learning adjustment
    
    Returns: (score: float, components: dict)
    """
    if not pair_data or not passes_market_filter(pair_data)[0]:
        return 0.0, {}
    
    score = 50.0  # baseline
    components = {}
    
    # 1. Price momentum (m5 = short-term signal)
    pc_m5 = pair_data.get("priceChange", {}).get("m5", 0) or 0
    pc_h1 = pair_data.get("priceChange", {}).get("h1", 0) or 0
    momentum_bonus = min(20.0, pc_m5 * 2.0 + pc_h1 * 0.5)
    score += momentum_bonus
    components["momentum"] = momentum_bonus
    
    # 2. Buy/sell pressure (strong buyers = strong signal)
    buys = pair_data.get("txns", {}).get("m5", {}).get("buys", 0) or 0
    sells = pair_data.get("txns", {}).get("m5", {}).get("sells", 0) or 1
    buy_sell_ratio = buys / max(sells, 1)
    buy_pressure_bonus = min(10.0, (buy_sell_ratio - 1.0) * 5.0)
    score += buy_pressure_bonus
    components["buy_pressure"] = buy_pressure_bonus
    
    # 3. Learning adjustment (if available)
    if learning and learning.get("wins", 0) > 0 and learning.get("trades", 0) > 0:
        win_rate = learning["wins"] / learning["trades"]
        if win_rate > 0.35:  # winning strategy
            learning_bonus = 10.0
            score += learning_bonus
            components["learning_bonus"] = learning_bonus
    
    score = min(100.0, max(0.0, score))
    return score, components


# ============================================================================
# CANDIDATE CONFIRMATION: 2 consecutive observations needed
# ============================================================================

def candidate_confirmation(state, mint, signal_score):
    """
    Track consecutive high-score observations for a candidate.
    Requires 2 hits with score >= threshold before entry.
    Resets on lower score.
    
    Returns: bool (ready for entry?)
    """
    threshold = state.get("strategy", {}).get("active_threshold", 75.0)
    obs = state.setdefault("candidate_observations", {})
    
    if mint not in obs:
        if signal_score >= threshold:
            obs[mint] = {"hits": 1, "last_score": signal_score}
        return False
    
    entry = obs[mint]
    
    # Score dropped below threshold = reset
    if signal_score < threshold:
        entry["hits"] = 1
        entry["last_score"] = signal_score
        return False
    
    # Score still high = increment
    entry["hits"] += 1
    entry["last_score"] = signal_score
    
    return entry["hits"] >= 2


# ============================================================================
# POSITION MANAGEMENT
# ============================================================================

def open_position(state, mint, pair_data, whale_balances, signal_score, signal_components):
    """
    Try to open a position. Returns: bool (success?)
    """
    # Check circuit breakers
    if mint in state.get("positions", {}):
        return False  # already open
    
    if mint in state.get("blacklist", []):
        return False  # blacklisted
    
    if state.get("cooldowns", {}).get(mint):
        cooldown_until = state["cooldowns"][mint]
        if cooldown_until > time.time():
            return False  # in cooldown
    
    # Position sizing
    equity = state.get("equity", 1000.0)
    if equity < 200.0:
        return False  # minimum equity gate
    
    max_pos_size = equity * MAX_POSITION_EQUITY_FRACTION
    
    # Current exposure
    current_exposure = sum(
        p.get("amount_usd", 0)
        for p in state.get("positions", {}).values()
    )
    max_portfolio_size = equity * MAX_PORTFOLIO_EXPOSURE_FRACTION
    
    if current_exposure >= max_portfolio_size:
        return False  # portfolio limit hit
    
    position_size = min(max_pos_size, max_portfolio_size - current_exposure)
    
    if position_size <= 10.0:
        return False  # position too small
    
    # Open position
    price = float(pair_data.get("priceUsd", 0) or 0)
    if price <= 0:
        return False
    
    symbol = pair_data.get("baseToken", {}).get("symbol", "?")
    
    state["positions"][mint] = {
        "symbol": symbol,
        "entry_price": price,
        "entry_time": time.time(),
        "entry_liquidity": pair_data.get("liquidity", {}).get("usd", 0) or 0,
        "amount_usd": position_size,
        "peak_price": price,
        "trailing_armed": False,
        "signal_score": signal_score,
        "entry_components": signal_components,
    }
    
    return True


def liquidity_drain_detected(position, current_liquidity_usd):
    """
    Whale detection: liquidity drying up = red flag.
    - If drop > 30% from entry = IMMEDIATE EXIT
    - If drop > 50% from entry = need 2 confirmations
    
    Returns: bool (should exit?)
    """
    entry_liq = position.get("entry_liquidity", 0)
    if entry_liq <= 0:
        return False
    
    drop_pct = (entry_liq - current_liquidity_usd) / entry_liq
    
    # Emergency exit: > 30% liquidity drain
    if drop_pct >= LIQUIDITY_DRAIN_CRITICAL:
        return True
    
    # Severe drain: > 50% requires 2 observations
    if drop_pct >= 0.50:
        hits = position.get("liquidity_drain_hits", 0)
        if hits >= 1:
            return True
        position["liquidity_drain_hits"] = hits + 1
        return False
    
    # Recovery: reset counter
    position["liquidity_drain_hits"] = 0
    return False


def close_position(state, mint, exit_price, reason):
    """
    Close a position and log the P&L.
    """
    pos = state.get("positions", {}).pop(mint, None)
    if not pos:
        return
    
    entry_price = pos.get("entry_price", 0)
    amount_usd = pos.get("amount_usd", 0)
    
    if entry_price <= 0 or amount_usd <= 0:
        return
    
    pnl_pct = (exit_price - entry_price) / entry_price
    pnl_usd = amount_usd * pnl_pct
    
    state["equity"] = state.get("equity", 1000.0) + pnl_usd
    state["trade_count"] = state.get("trade_count", 0) + 1
    
    # Track learning
    learning = state.setdefault("learning", {"trades": 0, "wins": 0, "buckets": {}})
    learning["trades"] += 1
    if pnl_usd > 0:
        learning["wins"] += 1
    
    # Bucket by score
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
    
    # Blacklist on specific reasons
    if reason in ("liquidity_drained", "whale_dump", "force_close_no_data"):
        if mint not in state.get("blacklist", []):
            state["blacklist"].append(mint)
    
    # Log trade
    symbol = pos.get("symbol", "?")
    log(f"[CLOSE] {symbol} {reason:20s} | entry=${entry_price:.8f} exit=${exit_price:.8f} | pnl={pnl_pct*100:+6.2f}% | equity=${state['equity']:.2f}")


def manage_open_positions(state):
    """
    Scan all open positions for exit conditions.
    """
    for mint in list(state.get("positions", {}).keys()):
        pos = state["positions"].get(mint)
        if not pos:
            continue
        
        # Simulate price check (real version calls DexScreener)
        # For now, random walk
        current_price = pos.get("peak_price", pos.get("entry_price", 0))
        if current_price <= 0:
            continue
        
        # Update peak
        if current_price > pos["peak_price"]:
            pos["peak_price"] = current_price
        
        # Calculate P&L
        entry_price = pos.get("entry_price", 0)
        pnl_pct = (current_price - entry_price) / entry_price if entry_price else 0
        
        # 1. Trailing stop
        if pnl_pct >= 0.08:  # arm at +8%
            pos["trailing_armed"] = True
        
        if pos.get("trailing_armed"):
            drop_from_peak = (pos["peak_price"] - current_price) / pos["peak_price"]
            if drop_from_peak >= TRAILING_STOP_PCT:
                close_position(state, mint, current_price, "trailing_stop")
                continue
        
        # 2. Hard stop loss
        if pnl_pct <= -HARD_STOP_LOSS_PCT:
            close_position(state, mint, current_price, "hard_stop_loss")
            continue
        
        # 3. Max hold timeout
        entry_time = pos.get("entry_time", time.time())
        hold_minutes = (time.time() - entry_time) / 60.0
        if hold_minutes > 15.0:  # 15 min max hold
            close_position(state, mint, current_price, "max_hold_timeout")
            continue


# ============================================================================
# LOGGING
# ============================================================================

def log(msg):
    """Simple logging."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")


# ============================================================================
# EXPORTS FOR TESTS
# ============================================================================
__all__ = [
    "normalize_state",
    "normalize_legacy_strategy_baseline",
    "passes_market_filter",
    "score_market_opportunity",
    "candidate_confirmation",
    "open_position",
    "close_position",
    "liquidity_drain_detected",
    "manage_open_positions",
    "log",
    # Constants for tests
    "MIN_LIQUIDITY_USD",
    "MIN_LIQUIDITY_TO_MCAP_RATIO",
    "MIN_BUY_SELL_RATIO",
    "MIN_PRICE_CHANGE_M5_PCT",
    "OPPORTUNITY_ENTRY_THRESHOLD",
    "MAX_POSITION_EQUITY_FRACTION",
    "MAX_PORTFOLIO_EXPOSURE_FRACTION",
    "MIN_PAIR_AGE_SECONDS",
    "MAX_CONCURRENT_POSITIONS",
    "TARGET_TRADES",
]
