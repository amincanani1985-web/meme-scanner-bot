#!/usr/bin/env python3
"""
Hardened meme-coin paper scanner.
Entry: high-conviction score (80+) with three consecutive confirmations.
Risk controls are modeled estimates and require validation against real market data.
"""
import time
import math
import csv
import requests
from whale_monitor import fetch_whale_balances, helius_configured, confirmed_whale_dump
from pathlib import Path
from datetime import datetime, timedelta

DEXSCREENER_TOKEN_BOOSTS_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
DEXSCREENER_TOKEN_PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_TOKEN_PAIRS_URL = "https://api.dexscreener.com/latest/dex/tokens/{}"
ENTRY_SLIPPAGE_BPS = 150
EXIT_SLIPPAGE_BPS = 200
MAX_HOLD_SECONDS = 600
MAX_MISSING_PRICE_SECONDS = 600
HTTP_TIMEOUT_SECONDS = 10


# ============================================================================
# CORE RISK PARAMETERS (HARDENED v20 - profit-focused)
# ============================================================================

# Entry filters: EXTREME SELECTIVITY
MIN_LIQUIDITY_USD = 100000                # Was 50k → thick order book only
MIN_VOLUME_H1_USD = 20000                 # Was 5k → proven volume
MIN_LIQUIDITY_TO_MCAP_RATIO = 0.40        # Was 0.35 → stronger filter
MIN_BUY_SELL_RATIO_M5 = 2.0               # Was 1.5 → strong buyer pressure
MIN_PRICE_CHANGE_M5_PCT = 1.5             # Was 1.0 → real momentum
MIN_PAIR_AGE_SECONDS = 3600               # 60 min → avoid brand new scams

# Entry scoring: ULTRA SELECTIVE
OPPORTUNITY_ENTRY_THRESHOLD = 80.0        # Was 75.0 → only ELITE signals
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
SHADOW_MIN_SCORE = 60.0                     # Broad score distribution for threshold validation
MAX_SHADOW_POSITIONS = 8                    # Cap independent shadow observations

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
    return risk


def normalize_legacy_strategy_baseline(state):
    """Migrate old state files to hardened thresholds."""
    strategy = state.get("strategy", {})
    if strategy.get("status") == "waiting_for_history":
        old_threshold = float(strategy.get("active_threshold", 60.0))
        if old_threshold < OPPORTUNITY_ENTRY_THRESHOLD:  # any untrained strategy below hardened baseline
            state["strategy"]["active_threshold"] = OPPORTUNITY_ENTRY_THRESHOLD
            state["strategy"]["rollback_threshold"] = OPPORTUNITY_ENTRY_THRESHOLD
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
    
    pc_h1 = pair_data.get("priceChange", {}).get("h1", 0) or 0
    if pc_h1 < 0:
        return False, f"negative_momentum_h1:{pc_h1:.1f}%"
    
    # No recent activity
    if buys == 0 and sells == 0:
        return False, "no_recent_activity"
    
    return True, "ok"


# ============================================================================
# SCORING: MARKET OPPORTUNITY SCORE (ultra-hardened)
# ============================================================================

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
    
    # 3. Learning adjustment (if available)
    if learning and learning.get("trades", 0) >= 10 and learning.get("wins", 0) >= 0:
        win_rate = learning["wins"] / learning["trades"]
        if win_rate > 0.40:  # only reward learning after a minimum sample
            learning_bonus = 10.0
            score += learning_bonus
            components["learning_bonus"] = learning_bonus
    
    score = min(100.0, max(0.0, score))
    return score, components


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
    append_learning_observation(pos.get("signal_score"), pnl_pct * 100.0, reason)
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


def fetch_candidate_mints(limit=30):
    """Fetch a bounded list of candidate token mints from DexScreener discovery feeds."""
    found, seen = [], set()
    for url in (DEXSCREENER_TOKEN_BOOSTS_URL, DEXSCREENER_TOKEN_PROFILES_URL):
        try:
            response = requests.get(url, timeout=HTTP_TIMEOUT_SECONDS)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                continue
            for item in payload:
                if not isinstance(item, dict) or item.get("chainId") != "solana":
                    continue
                mint = item.get("tokenAddress")
                if mint and mint not in seen:
                    seen.add(mint)
                    found.append(mint)
                    if len(found) >= limit:
                        return found
        except (requests.RequestException, ValueError, TypeError):
            continue
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
        append_learning_observation(pos.get("signal_score"), net_pnl_pct, f"shadow_{reason}")
        state["shadow_positions"].pop(mint, None)
        log(f"[SHADOW CLOSE] {pos.get('symbol', '?')} {reason} pnl={net_pnl_pct:+.2f}% (isolated; account equity unchanged)")


def scan_shadow_candidates(state):
    """Collect labeled virtual-trade samples while the real paper-account circuit breaker is halted."""
    candidates = fetch_candidate_mints()
    opened = []
    # Shadow samples cover a broad score range; this never changes live entry rules.
    threshold = SHADOW_MIN_SCORE
    for mint in candidates:
        if len(state["shadow_positions"]) >= MAX_SHADOW_POSITIONS:
            break
        if mint in state["shadow_positions"] or mint in state["blacklist"]:
            continue
        pair = fetch_dex_pair(mint)
        if not pair or not passes_market_filter(pair)[0]:
            state["candidate_observations"].pop(mint, None)
            continue
        score, components = score_market_opportunity(pair, state.get("learning"))
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
    state["scan_count"] = int(state.get("scan_count", 0)) + 1
    return {"scanned": len(candidates), "shadow_opened": opened}


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


def append_learning_observation(score, pnl_pct, reason):
    """Persist realized outcomes without inventing a score for legacy positions."""
    path = Path("strategy_observations.csv")
    exists = path.exists() and path.stat().st_size > 0
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
            writer.writerow(["timestamp", "score", "pnl_pct", "reason"])
        writer.writerow([datetime.utcnow().isoformat(), "" if score_value is None else score_value, pnl_value, reason])


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

def load_state():
    """Load and normalize state; recover safely if the JSON file is corrupted."""
    import json
    path = Path("state.json")
    if not path.exists():
        return normalize_state({})
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log(f"[WARN] state.json unreadable; starting from safe defaults ({type(exc).__name__})")
        return normalize_state({})
    if not isinstance(loaded, dict):
        log("[WARN] state.json root is not an object; starting from safe defaults")
        return normalize_state({})
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
    "score_market_opportunity",
    "candidate_confirmation",
    "open_position",
    "close_position",
    "liquidity_drain_detected",
    "manage_open_positions",
    "manage_shadow_positions",
    "scan_shadow_candidates",
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
    for mint in candidates:
        if mint in state["positions"] or mint in state["blacklist"]:
            continue
        pair = fetch_dex_pair(mint)
        if not pair or not passes_market_filter(pair)[0]:
            continue
        score, components = score_market_opportunity(pair, state.get("learning"))
        threshold = float(state["strategy"].get("active_threshold", OPPORTUNITY_ENTRY_THRESHOLD))
        if score < threshold:
            state["candidate_observations"].pop(mint, None)
            continue
        whale_balances = {}
        if helius_configured():
            snapshot = fetch_whale_balances(mint)
            if not snapshot:
                log(f"[WARN] skipping entry for {mint}: whale data unavailable")
                continue
            previous_snapshot = state["whale_snapshots"].get(mint, {})
            if previous_snapshot and confirmed_whale_dump(previous_snapshot, snapshot, pair):
                state["whale_snapshots"][mint] = snapshot
                state["candidate_observations"].pop(mint, None)
                log(f"[WHALE] entry blocked for {mint}: holder-balance drop plus market sell pressure")
                continue
            whale_balances = snapshot
            state["whale_snapshots"][mint] = snapshot
            while len(state["whale_snapshots"]) > 100:
                state["whale_snapshots"].pop(next(iter(state["whale_snapshots"])))
        if not candidate_confirmation(state, mint, score):
            continue
        if open_position(state, mint, pair, whale_balances, score, components):
            opened.append(mint)
    state["scan_count"] += 1
    state["last_run"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    save_state(state)
    log(f"[CYCLE] scanned={len(candidates)} opened={len(opened)} positions={len(state['positions'])} equity=${state['equity']:.2f}")
    return {"scanned": len(candidates), "opened": opened, "open_positions": len(state["positions"]), "equity": state["equity"]}


if __name__ == "__main__":
    try:
        run_cycle()
    except Exception as exc:
        log(f"[ERROR] trading cycle failed: {type(exc).__name__}: {exc}")
        raise
