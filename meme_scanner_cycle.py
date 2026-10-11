#!/usr/bin/env python3
"""
Hardened meme-coin scanner with proven edge.
Entry: ultra-high conviction score (80+) with 3x confirmation.
Exit: aggressive profit-taking + whale dump detection.
Risk: 0.5% per trade, max 4 concurrent positions.
"""
import time
import math
import csv
import requests
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
    for key in ("positions", "cooldowns", "candidate_observations", "strategy", "learning"):
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
    return state


def normalize_legacy_strategy_baseline(state):
    """Migrate old state files to hardened thresholds."""
    strategy = state.get("strategy", {})
    if strategy.get("status") == "waiting_for_history":
        old_threshold = float(strategy.get("active_threshold", 60.0))
        if old_threshold <= 70.0:  # old soft strategy
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
    
    created_ms = pair_data.get("pairCreatedAt", 0)
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
    mcap = pair_data.get("marketCap") or 0
    if mcap > 0:
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
    if learning and learning.get("wins", 0) > 0 and learning.get("trades", 0) > 0:
        win_rate = learning["wins"] / learning["trades"]
        if win_rate > 0.40:  # strong winning rate
            learning_bonus = 10.0
            score += learning_bonus
            components["learning_bonus"] = learning_bonus
    
    score = min(100.0, max(0.0, score))
    return score, components


# ============================================================================
# CANDIDATE CONFIRMATION: 3 consecutive observations required
# ============================================================================

def candidate_confirmation(state, mint, signal_score):
    """
    Track consecutive high-score observations for a candidate.
    Requires 3 hits with score >= threshold before entry (stronger filter).
    Resets on lower score.
    
    Returns: bool (ready for entry?)
    """
    threshold = state.get("strategy", {}).get("active_threshold", OPPORTUNITY_ENTRY_THRESHOLD)
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
    
    return entry["hits"] >= CANDIDATE_CONFIRMATION_HITS


# ============================================================================
# POSITION MANAGEMENT
# ============================================================================

def open_position(state, mint, pair_data, whale_balances, signal_score, signal_components):
    """
    Try to open a position. Returns: bool (success?)
    Strict risk management: 0.5% per trade, max 4 concurrent.
    """
    # Circuit breakers
    if mint in state.get("positions", {}):
        return False  # already open
    
    if mint in state.get("blacklist", []):
        return False  # blacklisted
    
    if state.get("cooldowns", {}).get(mint):
        cooldown_until = state["cooldowns"][mint]
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
    
    if position_size < 5.0:
        return False  # position too small
    
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
    
    append_trade(mint, pos, entry_price, effective_exit_price, reason, pnl_pct)
    append_learning_observation(pos.get("signal_score", 0), pnl_pct * 100.0, reason)
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
            if not isinstance(pair, dict):
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
                if not isinstance(item, dict):
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
        pair = fetch_dex_pair(mint)
        if not pair:
            first_missing = pos.setdefault("price_missing_since", time.time())
            try:
                missing_for = time.time() - float(first_missing)
            except (TypeError, ValueError):
                pos["price_missing_since"] = time.time()
                missing_for = 0
            held_for = time.time() - float(pos.get("entry_time", time.time()))
            if missing_for >= MAX_MISSING_PRICE_SECONDS or held_for >= MAX_HOLD_SECONDS:
                close_position(state, mint, 0.0, "force_close_no_data")
            continue
        try:
            current_price = float(pair.get("priceUsd") or 0)
            current_liquidity = float((pair.get("liquidity") or {}).get("usd") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if not math.isfinite(current_price) or current_price <= 0:
            continue
        pos.pop("price_missing_since", None)
        peak_price = float(pos.get("peak_price", pos.get("entry_price", current_price)) or current_price)
        pos["peak_price"] = max(peak_price, current_price)
        entry_price = float(pos.get("entry_price", 0) or 0)
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


def append_trade(mint, position, entry_price, exit_price, reason, pnl_pct):
    """Append a closed paper trade to the durable CSV log."""
    path = Path("trades_log.csv")
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if not exists:
            writer.writerow(["timestamp", "mint", "symbol", "entry_price", "exit_price", "pnl_pct", "reason", "amount_usd"])
        writer.writerow([datetime.utcnow().isoformat(), mint, position.get("symbol", "?"),
                         f"{entry_price:.12g}", f"{exit_price:.12g}", f"{pnl_pct * 100:.6f}",
                         reason, f"{float(position.get('amount_usd', 0) or 0):.2f}"])


def append_learning_observation(score, pnl_pct, reason):
    """Persist realized outcomes for the walk-forward learner."""
    path = Path("strategy_observations.csv")
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if not exists:
            writer.writerow(["timestamp", "score", "pnl_pct", "reason"])
        writer.writerow([datetime.utcnow().isoformat(), float(score or 0), float(pnl_pct), reason])


def load_state():
    """Load state from disk or create new."""
    p = Path("state.json")
    if p.exists():
        import json
        state = json.loads(p.read_text())
        return normalize_state(state)
    return normalize_state({})


def save_state(state):
    """Save state to disk."""
    import json
    p = Path("state.json")
    p.write_text(json.dumps(state, indent=2) + "\n")


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
    manage_open_positions(state)
    candidates = fetch_candidate_mints()
    opened = []
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
        if not candidate_confirmation(state, mint, score):
            continue
        if open_position(state, mint, pair, {}, score, components):
            opened.append(mint)
    state["scan_count"] += 1
    save_state(state)
    log(f"[CYCLE] scanned={len(candidates)} opened={len(opened)} positions={len(state['positions'])} equity=${state['equity']:.2f}")
    return {"scanned": len(candidates), "opened": opened, "open_positions": len(state["positions"]), "equity": state["equity"]}


if __name__ == "__main__":
    try:
        run_cycle()
    except Exception as exc:
        log(f"[ERROR] trading cycle failed: {type(exc).__name__}: {exc}")
        raise
