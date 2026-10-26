#!/usr/bin/env python3
"""
Hardened meme-coin paper scanner.
Entry: paper-only exploratory profile; risk controls remain enabled.
Risk controls are modeled estimates and require validation against real market data.
"""
import time
import math
import csv
import io
import json
import os
import requests
from memeswap_research import fetch_token_profile as fetch_memeswap_token_profile
from memeswap_research import is_enabled as memeswap_research_enabled
from rugcheck_research import fetch_token_report as fetch_rugcheck_token_report
from rugcheck_research import is_enabled as rugcheck_research_enabled
from whale_monitor import fetch_whale_balances, helius_configured, confirmed_whale_dump, whale_accumulation_confirmed
from pathlib import Path
from datetime import datetime, timedelta

DEXSCREENER_TOKEN_BOOSTS_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
DEXSCREENER_TOKEN_TOP_BOOSTS_URL = "https://api.dexscreener.com/token-boosts/top/v1"
DEXSCREENER_TOKEN_PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_RECENT_PROFILES_URL = "https://api.dexscreener.com/token-profiles/recent-updates/v1"
DEXSCREENER_TOKEN_BATCH_URL = "https://api.dexscreener.com/tokens/v1/solana/{}"
PAIR_CACHE = {}
DEXSCREENER_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"
DISCOVERY_SEARCH_TERMS = (
    "BONK", "WIF", "POPCAT", "MEW", "PONKE", "BOME", "FARTCOIN", "GOAT",
    "PNUT", "GIGA", "PENGU", "TRUMP", "MELANIA", "USELESS", "MOODENG", "PIPPIN",
)
LAST_DISCOVERY_DIAGNOSTICS = {"selected": 0, "source_counts": {}}
DEXSCREENER_TOKEN_PAIRS_URL = "https://api.dexscreener.com/token-pairs/v1/solana/{}"
ENTRY_SLIPPAGE_BPS = 150
EXIT_SLIPPAGE_BPS = 200
PAPER_ROUND_TRIP_FEE_PCT = 1.0  # Additional fee reserve; entry/exit slippage is modeled separately
MAX_HOLD_SECONDS = 600
MAX_MISSING_PRICE_SECONDS = 600
WHALE_SNAPSHOT_INTERVAL_SECONDS = 300
# Do not label shadow outcomes from stale prices after the intended hold window.
# A 5-minute scheduling tolerance is allowed beyond the 10-minute maximum hold.
SHADOW_STALE_OBSERVATION_SECONDS = MAX_HOLD_SECONDS + 300
SHADOW_PATH_MIN_SAMPLES = 9
SHADOW_PATH_MAX_SAMPLE_GAP_SECONDS = 240.0
MAX_SHADOW_COMPLETED_MINTS = 5000
# Optional external risk data is read only and capped to protect cycle time and API budgets.
MAX_MEMESWAP_RESEARCH_PROFILES_PER_CYCLE = 4
MAX_RUGCHECK_RESEARCH_PROFILES_PER_CYCLE = 3
RUGCHECK_MAX_REPORT_AGE_SECONDS = 6 * 60 * 60
HTTP_TIMEOUT_SECONDS = 10


# ============================================================================
# CORE RISK PARAMETERS (HARDENED v20 - profit-focused)
# ============================================================================

# Experimental paper profile: prefer a sustained hourly trend with a modest 5m pullback.
# The historical score buckets did not validate momentum-chasing; these settings remain
# paper-only and must earn promotion through forward outcomes, not assumed profitability.
MIN_LIQUIDITY_USD = 10000
MIN_VOLUME_H1_USD = 10000
MIN_LIQUIDITY_TO_MCAP_RATIO = 0.05
MIN_BUY_SELL_RATIO_M5 = 1.0
MIN_PRICE_CHANGE_M5_PCT = -8.0
MAX_PRICE_CHANGE_M5_PCT = 15.0
MIN_PRICE_CHANGE_H1_PCT = 0.0
MIN_PAIR_AGE_SECONDS = 600                # 10 min; paper-only exploration profile

# Entry scoring: experimental low-score/pullback threshold, paper account only.
# The 60-70 historical holdout bucket looked promising but had only four samples,
# so this is forward-testing only and is not a validated profitability claim.
OPPORTUNITY_ENTRY_THRESHOLD = 60.0
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
CANDIDATE_CONFIRMATION_HITS = 1           # Paper-only: enter on first qualified observation (was 2)
CANDIDATE_CONFIRMATION_MAX_GAP_SECONDS = 600  # A missed/stale cycle cannot count as consecutive
MAX_EQUITY_DRAWDOWN_FRACTION = 0.20       # Halt new entries after 20% drawdown from initial paper equity
SHADOW_MIN_SCORE = 0.0                     # Sample the full non-negative score range for entry-filter research only
SHADOW_MIN_LIQUIDITY_USD = 1000.0            # Research floor only; production entry floors are unchanged
SHADOW_MIN_VOLUME_H1_USD = 100.0             # Keep lower-volume candidates in the isolated research cohort
SHADOW_MIN_PAIR_AGE_SECONDS = 300.0           # Five minutes; the live scanner retains its separate age floor
SHADOW_MIN_LIQ_MCAP_RATIO = 0.005             # Measure weak liquidity ratios; never relax the live entry gate
MAX_SHADOW_POSITIONS = 24                   # Broader isolated shadow cohort for faster learning

# ============================================================================
# STATE MANAGEMENT
# ============================================================================

def _load_shadow_completed_mints():
    """Load completed mints from the canonical paper log and legacy root log."""
    configured = os.environ.get("SHADOW_PRICE_PATH_LOG", "shadow_price_paths.jsonl")
    workspace = Path(__file__).resolve().parent
    candidates = [
        Path(configured),
        Path("shadow_price_paths.jsonl"),
        Path("paper_account/shadow_price_paths.jsonl"),
        workspace / "shadow_price_paths.jsonl",
        workspace / "paper_account" / "shadow_price_paths.jsonl",
    ]
    completed = []
    seen = set()
    visited_paths = set()
    for path in candidates:
        try:
            path_key = str(path.resolve())
        except OSError:
            path_key = str(path)
        if path_key in visited_paths:
            continue
        visited_paths.add(path_key)
        if not path.is_file():
            continue
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    mint = str(record.get("mint") or "").strip() if isinstance(record, dict) else ""
                    if not mint or mint in seen:
                        continue
                    # A persisted record may have been saved even when its quote
                    # path was too short or sparse for research. Only completed,
                    # quality-qualified paths should permanently suppress resampling.
                    position_like = {
                        "entry_time": record.get("entry_timestamp"),
                        "price_history": record.get("price_history"),
                    }
                    if not _shadow_path_meets_research_quality(position_like):
                        continue
                    seen.add(mint)
                    completed.append(mint)
        except OSError:
            # One broken/missing legacy file must not hide completions in another.
            continue

    # The historical account may still have legacy shadow paths in flight. Treat
    # those mints as reserved while the isolated paper cohort creates new paths so
    # the same mint is not independently opened twice in the same workflow.
    try:
        root_state = json.loads((workspace / "state.json").read_text(encoding="utf-8"))
        active_positions = root_state.get("shadow_positions", {})
        if isinstance(active_positions, dict):
            for raw_mint in active_positions:
                mint = str(raw_mint or "").strip()
                if mint and mint not in seen:
                    seen.add(mint)
                    completed.append(mint)
    except (OSError, json.JSONDecodeError, TypeError, AttributeError):
        pass
    return completed[-MAX_SHADOW_COMPLETED_MINTS:]


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
    if "shadow_completed_mints" not in state:
        state["shadow_completed_mints"] = _load_shadow_completed_mints()
    elif not isinstance(state.get("shadow_completed_mints"), list):
        state["shadow_completed_mints"] = []
    state["shadow_completed_mints"] = list(dict.fromkeys(
        str(mint).strip() for mint in state["shadow_completed_mints"] if str(mint).strip()
    ))[-MAX_SHADOW_COMPLETED_MINTS:]
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


def should_lock_paper_profit(state):
    """Lock new paper entries only after the whale cohort is validated and net profitable."""
    if not isinstance(state, dict):
        return False
    strategy = state.get("strategy") if isinstance(state.get("strategy"), dict) else {}
    try:
        initial = float(state.get("initial_equity", 1000.0))
        equity = float(state.get("equity", initial))
        history_rows = int(strategy.get("history_rows", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    if not all(math.isfinite(value) for value in (initial, equity)) or initial <= 0:
        return False
    return (
        strategy.get("validation_status") == "validated"
        and history_rows >= 30
        and equity > initial
    )


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
    Returns: (bool, reason_str). Experimental paper profile favors sustained
    hourly strength and a non-overextended 5-minute entry.
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
    if pc_h1 < MIN_PRICE_CHANGE_H1_PCT:
        return False, f"weak_momentum_h1:{pc_h1:.1f}%"
    
    # No recent activity
    if buys == 0 and sells == 0:
        return False, "no_recent_activity"
    
    return True, "ok"



def passes_whale_follow_market_filter(pair_data):
    """Entry gate for the isolated whale-follow experiment.

    Use the shadow data-quality floor as the base, then apply whale-specific
    liquidity, activity, momentum and anti-chasing checks. Do not inherit the
    legacy 5%-liquidity/market-cap rule, which was excluding otherwise liquid
    markets before the accumulation signal could be evaluated.
    """
    passed, reason = passes_shadow_market_filter(pair_data)
    if not passed:
        return False, reason
    try:
        created_ms = int(pair_data.get("pairCreatedAt", 0) or 0)
        changes = pair_data.get("priceChange") or {}
        m5 = float(changes.get("m5") or 0)
        h1 = float(changes.get("h1") or 0)
        txns = (pair_data.get("txns") or {}).get("m5") or {}
        buys = float(txns.get("buys") or 0)
        sells = float(txns.get("sells") or 0)
        liquidity = float((pair_data.get("liquidity") or {}).get("usd") or 0)
        volume = float((pair_data.get("volume") or {}).get("h1") or 0)
        market_cap = float(pair_data.get("marketCap") or pair_data.get("fdv") or 0)
    except (TypeError, ValueError, AttributeError, OverflowError):
        return False, "whale_invalid_market_data"
    values = (m5, h1, buys, sells, liquidity, volume, market_cap)
    if not all(math.isfinite(value) for value in values) or min(buys, sells, liquidity, volume) < 0:
        return False, "whale_invalid_market_data"
    # Do not inherit the broader research-only floors. Whale-copy uses its own
    # stable base-quality floor before applying the stricter whale signal checks.
    if liquidity < 1000:
        return False, f"shadow_low_liquidity:{liquidity:.0f}"
    if volume < 500:
        return False, f"shadow_low_volume_h1:{volume:.0f}"
    if market_cap <= 0:
        return False, "shadow_missing_market_cap"
    if liquidity / market_cap < 0.02:
        return False, f"shadow_liq_mcap_ratio:{liquidity / market_cap:.2f}"
    age_seconds = (time.time() * 1000 - created_ms) / 1000.0
    if age_seconds < MIN_PAIR_AGE_SECONDS:
        return False, f"whale_pair_too_new:{age_seconds:.0f}s"
    if liquidity < 20_000:
        return False, f"whale_thin_liquidity:{liquidity:.0f}"
    if volume < MIN_VOLUME_H1_USD:
        return False, f"whale_thin_volume_h1:{volume:.0f}"
    if h1 <= 0:
        return False, f"whale_weak_momentum_h1:{h1:.1f}%"
    if h1 > 60.0:
        return False, f"whale_chase_risk_h1:{h1:.1f}%"
    if m5 < -2.0:
        return False, f"whale_falling_momentum_m5:{m5:.1f}%"
    if m5 > 8.0:
        return False, f"whale_chase_risk_m5:{m5:.1f}%"
    activity = buys + sells
    if activity < 10:
        return False, f"whale_low_activity_m5:{activity:g}"
    if buys <= sells:
        return False, f"whale_weak_buy_pressure:{buys / max(sells, 1.0):.2f}"
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
    if age_seconds < SHADOW_MIN_PAIR_AGE_SECONDS:
        return False, f"shadow_pair_too_new:{age_seconds:.0f}s"
    if liquidity < SHADOW_MIN_LIQUIDITY_USD:
        return False, f"shadow_low_liquidity:{liquidity:.0f}"
    if volume < SHADOW_MIN_VOLUME_H1_USD:
        return False, f"shadow_low_volume_h1:{volume:.0f}"
    if market_cap <= 0:
        return False, "shadow_missing_market_cap"
    if liquidity / market_cap < SHADOW_MIN_LIQ_MCAP_RATIO:
        return False, f"shadow_liq_mcap_ratio:{liquidity / market_cap:.3f}"
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


def _validated_learning_bonus(learning):
    """Reward prior results only when scored trades show a positive net edge."""
    if not isinstance(learning, dict):
        return 0.0
    buckets = learning.get("buckets")
    if not isinstance(buckets, dict):
        return 0.0
    total = 0
    wins = 0
    weighted_net_pnl = 0.0
    for bucket in buckets.values():
        if not isinstance(bucket, dict):
            continue
        try:
            n = int(bucket.get("n", 0) or 0)
            bucket_wins = int(bucket.get("wins", 0) or 0)
            avg_pnl = float(bucket.get("avg_pnl", 0.0) or 0.0)
        except (TypeError, ValueError, OverflowError):
            continue
        if n <= 0 or bucket_wins < 0 or bucket_wins > n or not math.isfinite(avg_pnl):
            continue
        total += n
        wins += bucket_wins
        weighted_net_pnl += avg_pnl * n
    if total < 10:
        return 0.0
    return 10.0 if wins / total > 0.5 and weighted_net_pnl / total > 0.0 else 0.0


def score_market_opportunity(pair_data, learning=None, require_whale_accumulation=False):
    """
    Score 0-100 based on proven factors:
    - Momentum (m5 strong short-term signal)
    - Buy/sell ratio (organic demand)
    - Liquidity strength
    - Learning adjustment
    
    Returns: (score: float, components: dict)
    """
    market_filter = passes_whale_follow_market_filter if require_whale_accumulation else passes_market_filter
    if not pair_data or not market_filter(pair_data)[0]:
        return 0.0, {}
    
    score = 50.0  # baseline
    components = {}
    
    # Prefer a controlled move with a positive hourly trend over a vertical 5m pump.
    pc_m5 = pair_data.get("priceChange", {}).get("m5", 0) or 0
    pc_h1 = pair_data.get("priceChange", {}).get("h1", 0) or 0
    hourly_bonus = max(-20.0, min(20.0, pc_h1 * 0.4))
    if pc_m5 < -2.0:
        short_term_bonus = max(-25.0, 8.0 + (pc_m5 + 2.0) * 1.5)
    elif pc_m5 <= 8.0:
        short_term_bonus = 12.0 - abs(pc_m5 - 3.0) * 1.2
    else:
        short_term_bonus = max(-25.0, 12.0 - (pc_m5 - 8.0) * 3.0)
    momentum_bonus = hourly_bonus + short_term_bonus
    score += momentum_bonus
    components["momentum"] = momentum_bonus
    components["hourly_bonus"] = hourly_bonus
    components["short_term_bonus"] = short_term_bonus

    # Buy/sell pressure is bounded to prevent extreme ratios dominating the score.
    buys = pair_data.get("txns", {}).get("m5", {}).get("buys", 0) or 0
    sells = pair_data.get("txns", {}).get("m5", {}).get("sells", 0) or 1
    buy_sell_ratio = buys / max(sells, 1)
    buy_pressure_bonus = max(-12.0, min(12.0, (buy_sell_ratio - 1.0) * 6.0))
    score += buy_pressure_bonus
    components["buy_pressure"] = buy_pressure_bonus
    components.update(_market_feature_snapshot(pair_data, pc_m5, pc_h1, buy_sell_ratio))

    # Learning bonus uses scored buckets with positive net PnL, not unrelated
    # legacy/unscored wins from the headline ledger totals.
    learning_bonus = _validated_learning_bonus(learning)
    if learning_bonus:
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
    hourly_bonus = max(-20.0, min(20.0, pc_h1 * 0.4))
    if pc_m5 < -2.0:
        short_term_bonus = max(-25.0, 8.0 + (pc_m5 + 2.0) * 1.5)
    elif pc_m5 <= 8.0:
        short_term_bonus = 12.0 - abs(pc_m5 - 3.0) * 1.2
    else:
        short_term_bonus = max(-25.0, 12.0 - (pc_m5 - 8.0) * 3.0)
    momentum_bonus = hourly_bonus + short_term_bonus
    ratio = buys / max(sells, 1.0)
    buy_pressure_bonus = max(-12.0, min(12.0, (ratio - 1.0) * 6.0))
    score = 50.0 + momentum_bonus + buy_pressure_bonus
    components = {
        "momentum": momentum_bonus,
        "hourly_bonus": hourly_bonus,
        "short_term_bonus": short_term_bonus,
        "buy_pressure": buy_pressure_bonus,
    }
    components.update(_market_feature_snapshot(pair_data, pc_m5, pc_h1, ratio))
    learning_bonus = _validated_learning_bonus(learning)
    if learning_bonus:
        score += learning_bonus
        components["learning_bonus"] = learning_bonus
    return min(100.0, max(0.0, score)), components


# ============================================================================
# CANDIDATE CONFIRMATION: configurable consecutive observations; paper profile uses one hit
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
            return CANDIDATE_CONFIRMATION_HITS <= 1
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
        "last_whale_check_at": time.time(),
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
    gross_pnl_pct = (effective_exit_price - entry_price) / entry_price
    # Ledger PnL is net of both modeled entry/exit slippage and a conservative
    # additional round-trip fee reserve. Strategy observations retain gross-after-
    # slippage PnL so the validator can subtract that fee reserve exactly once.
    pnl_pct = gross_pnl_pct - PAPER_ROUND_TRIP_FEE_PCT / 100.0
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
    append_learning_observation(pos.get("signal_score"), gross_pnl_pct * 100.0, reason, pos.get("entry_components"))
    symbol = pos.get("symbol", "?")
    log(f"[CLOSE] {symbol:8s} {reason:20s} | entry=${entry_price:.8f} exit=${effective_exit_price:.8f} | pnl={pnl_pct*100:+6.2f}% | equity=${state['equity']:.2f}")


def _valid_solana_pair(pair):
    """Validate a DEX Screener Solana pair before it can feed scoring or exits."""
    if not isinstance(pair, dict) or pair.get("chainId") != "solana":
        return False
    try:
        liquidity = float((pair.get("liquidity") or {}).get("usd") or 0)
        price = float(pair.get("priceUsd") or 0)
    except (TypeError, ValueError, AttributeError, OverflowError):
        return False
    return math.isfinite(liquidity) and liquidity > 0 and math.isfinite(price) and price > 0


def fetch_dex_pair(mint):
    """Fetch the most liquid current DexScreener pair for a token mint."""
    if not mint:
        return None
    if mint in PAIR_CACHE:
        return PAIR_CACHE[mint]
    try:
        response = requests.get(DEXSCREENER_TOKEN_PAIRS_URL.format(mint), timeout=HTTP_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()
        # The documented token-pairs endpoint returns a JSON array.
        # Keep dict support for compatibility, and reject unknown response shapes.
        if isinstance(payload, list):
            pairs = payload
        elif isinstance(payload, dict):
            pairs = payload.get("pairs") or []
        else:
            pairs = []
        if not isinstance(pairs, list):
            PAIR_CACHE[mint] = None
            return None
        # DEX Screener's priceUsd/priceChange fields describe baseToken.
        # Never treat a quote-side mint as if those fields belonged to it.
        valid = [
            pair for pair in pairs
            if _valid_solana_pair(pair)
            and (pair.get("baseToken") or {}).get("address") == mint
        ]
        result = max(valid, key=lambda p: float(p["liquidity"]["usd"])) if valid else None
        PAIR_CACHE[mint] = result
        return result
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        return None


def fetch_dex_pairs(mints):
    """Batch-fetch up to 30 Solana mints per documented DEX Screener request.

    The API returns pools, not one row per mint, so choose the most liquid valid
    pool for each requested mint. Missing tokens from a successful batch are cached
    as unavailable for this cycle; a failed batch leaves them eligible for fallback.
    """
    unique = list(dict.fromkeys(mint for mint in mints if isinstance(mint, str) and mint))
    missing = [mint for mint in unique if mint not in PAIR_CACHE]
    for offset in range(0, len(missing), 30):
        chunk = missing[offset:offset + 30]
        try:
            url = DEXSCREENER_TOKEN_BATCH_URL.format(",".join(chunk))
            response = requests.get(url, timeout=HTTP_TIMEOUT_SECONDS)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                continue
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            continue

        best = {}
        requested = set(chunk)
        for pair in payload:
            if not _valid_solana_pair(pair):
                continue
            liquidity = float(pair["liquidity"]["usd"])
            # Pair priceUsd and priceChange refer to baseToken. Mapping a
            # requested quoteToken mint to this pair silently trades the wrong asset.
            token = pair.get("baseToken") or {}
            address = token.get("address")
            if address not in requested:
                continue
            previous = best.get(address)
            if previous is None or liquidity > float(previous["liquidity"]["usd"]):
                best[address] = pair
        for mint in chunk:
            PAIR_CACHE[mint] = best.get(mint)

    return {mint: PAIR_CACHE.get(mint) for mint in unique}


def fetch_candidate_mints(limit=120):
    """Fetch a bounded, interleaved Solana candidate list from boost, profile, and search feeds."""
    global LAST_DISCOVERY_DIAGNOSTICS
    if limit <= 0:
        return []
    feeds = []
    for url in (DEXSCREENER_TOKEN_BOOSTS_URL, DEXSCREENER_TOKEN_TOP_BOOSTS_URL, DEXSCREENER_TOKEN_PROFILES_URL, DEXSCREENER_RECENT_PROFILES_URL):
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
                try:
                    pair_liquidity = float((pair.get("liquidity") or {}).get("usd") or 0)
                    pair_volume_h1 = float((pair.get("volume") or {}).get("h1") or 0)
                    pair_created_ms = float(pair.get("pairCreatedAt") or 0)
                except (TypeError, ValueError):
                    continue
                pair_age_seconds = time.time() - pair_created_ms / 1000 if pair_created_ms > 0 else 0
                # Discovery is shared by live-paper and shadow research. Apply
                # the research-quality floor here; the live entry filter still
                # enforces MIN_LIQUIDITY_USD/MIN_VOLUME_H1_USD/MIN_PAIR_AGE_SECONDS.
                # This avoids discarding valid research candidates before the
                # isolated shadow scanner can classify them.
                if (
                    pair_liquidity < SHADOW_MIN_LIQUIDITY_USD
                    or pair_volume_h1 < SHADOW_MIN_VOLUME_H1_USD
                    or pair_age_seconds < SHADOW_MIN_PAIR_AGE_SECONDS
                ):
                    continue
                # DEX Screener's price/volume/momentum metrics describe baseToken.
                # Discover only the base mint so later scoring cannot be assigned
                # to a different quote-side asset.
                token = pair.get("baseToken") or {}
                symbol = str(token.get("symbol", "")).strip().upper()
                name = str(token.get("name", "")).strip().upper()
                mint = token.get("address")
                # Exact ticker/name matching avoids flooding the feed with clones
                # such as BONK2, WIFINU, or "POPCAT 2.0".
                if (symbol == term or name == term) and isinstance(mint, str) and mint:
                    # Reuse the verified search pair during this cycle. The batch
                    # endpoint can omit valid mints; pre-caching the actual base-token
                    # pair prevents a successful-but-partial batch from converting
                    # usable discovery data into a cached None. Keep the most liquid
                    # exact-base pair when several search terms return the same mint.
                    if _valid_solana_pair(pair):
                        previous = PAIR_CACHE.get(mint)
                        try:
                            previous_liquidity = float((previous.get("liquidity") or {}).get("usd") or 0) if isinstance(previous, dict) else 0.0
                        except (TypeError, ValueError, AttributeError, OverflowError):
                            previous_liquidity = 0.0
                        if not isinstance(previous, dict) or pair_liquidity > previous_liquidity:
                            PAIR_CACHE[mint] = pair
                    if mint not in search_seen:
                        search_seen.add(mint)
                        search_items.append(mint)
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            continue
    # Prioritize established, liquid search results before the noisy launch feeds.
    feeds = [search_items, *feeds]
    source_counts = {
        "meme_search": len(feeds[0]),
        "latest_boosts": len(feeds[1]),
        "top_boosts": len(feeds[2]),
        "latest_profiles": len(feeds[3]),
        "recent_profile_updates": len(feeds[4]),
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
                        LAST_DISCOVERY_DIAGNOSTICS = {"selected": len(found), "source_counts": source_counts}
                        return found
    LAST_DISCOVERY_DIAGNOSTICS = {"selected": len(found), "source_counts": source_counts}
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
        pos["peak_price"] = max(peak_price, current_price)
        pnl_pct = (current_price - entry_price) / entry_price if entry_price > 0 else 0.0
        # A gap through the hard-stop level must be classified as a hard stop,
        # even if a trailing stop was previously armed at a higher price.
        if pnl_pct <= -HARD_STOP_LOSS_PCT:
            close_position(state, mint, current_price, "hard_stop_loss")
            continue
        if pnl_pct >= 0.08:
            pos["trailing_armed"] = True
        if pos.get("trailing_armed") and pos["peak_price"] > 0:
            drop_from_peak = (pos["peak_price"] - current_price) / pos["peak_price"]
            if drop_from_peak >= TRAILING_STOP_PCT:
                close_position(state, mint, current_price, "trailing_stop")
                continue
        if current_liquidity > 0 and liquidity_drain_detected(pos, current_liquidity):
            close_position(state, mint, current_price, "liquidity_drained")
            continue
        if time.time() - float(pos.get("entry_time", time.time())) >= MAX_HOLD_SECONDS:
            close_position(state, mint, current_price, "max_hold_timeout")
            continue

        # Fetching holder snapshots may take seconds. All price/time-based exits
        # run first so a slow or failing RPC cannot delay a stop or max-hold close.
        try:
            last_whale_check_at = float(pos.get("last_whale_check_at", entry_time) or entry_time)
        except (TypeError, ValueError, OverflowError):
            last_whale_check_at = entry_time
        if not math.isfinite(last_whale_check_at) or last_whale_check_at < 0:
            last_whale_check_at = entry_time
        if (
            helius_configured()
            and time.time() - last_whale_check_at >= WHALE_SNAPSHOT_INTERVAL_SECONDS
        ):
            # Price/stop checks run every monitor tick; holder RPCs are slower and
            # only need a fresh comparison every five minutes.
            pos["last_whale_check_at"] = time.time()
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




def _append_shadow_price_path(mint, position, observed_at=None):
    """Persist the sampled quote path for offline, chronological exit-rule research."""
    if not isinstance(position, dict) or not position.get("benchmark_exit_reason"):
        return False
    if position.get("price_path_written"):
        return True
    history = position.get("price_history")
    if not isinstance(history, list) or len(history) < 2:
        return False
    try:
        timestamps = [
            float(sample["timestamp"]) for sample in history
            if isinstance(sample, dict) and math.isfinite(float(sample.get("timestamp", 0)))
        ]
        if len(timestamps) < 2:
            return False
        entry_time = float(position["entry_time"])
        path = Path(os.environ.get("SHADOW_PRICE_PATH_LOG", "shadow_price_paths.jsonl"))
        record = {
            "schema_version": 1,
            "mint": str(mint),
            "symbol": str(position.get("symbol", "?")),
            "score": float(position.get("signal_score", 0) or 0),
            "entry_timestamp": entry_time,
            "entry_price": float(position["entry_price"]),
            "entry_liquidity_usd": float(position.get("entry_liquidity", 0) or 0),
            "entry_components": position.get("components", {}) if isinstance(position.get("components"), dict) else {},
            "prospective_entry_filters": sorted({
                str(tag) for tag in (position.get("prospective_entry_filters") or [])
                if isinstance(tag, str) and tag.strip()
            }),
            "benchmark_exit": {
                "reason": str(position["benchmark_exit_reason"]),
                "timestamp": float(position.get("benchmark_exit_timestamp", timestamps[-1])),
                "price_usd": float(position.get("benchmark_exit_price_usd", 0) or 0),
                "liquidity_usd": float(position.get("benchmark_exit_liquidity_usd", 0) or 0),
                "after_slippage_pnl_pct": float(position.get("benchmark_exit_after_slippage_pnl_pct", 0) or 0),
            },
            "observation_end_timestamp": max(timestamps),
            "observed_duration_seconds": max(0.0, max(timestamps) - entry_time),
            "price_history": history,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
        position["price_path_written"] = True
        return True
    except (OSError, TypeError, ValueError, OverflowError) as exc:
        log(f"[SHADOW PATH ERROR] {position.get('symbol', '?')} could not persist sampled path: {type(exc).__name__}")
        return False


def _shadow_path_meets_research_quality(position):
    """Return true only for a dense, full-horizon path usable in independent backtests."""
    history = position.get("price_history") if isinstance(position, dict) else None
    if not isinstance(history, list) or len(history) < SHADOW_PATH_MIN_SAMPLES:
        return False
    try:
        entry_time = float(position.get("entry_time", 0) or 0)
        timestamps = [float(sample["timestamp"]) for sample in history if isinstance(sample, dict)]
        if not math.isfinite(entry_time) or len(timestamps) != len(history):
            return False
        if any(not math.isfinite(ts) or ts < entry_time for ts in timestamps):
            return False
        gaps = [timestamps[i] - timestamps[i - 1] for i in range(1, len(timestamps))]
        return bool(
            timestamps
            and max(timestamps) - entry_time >= SHADOW_STALE_OBSERVATION_SECONDS
            and gaps
            and all(gap >= 0 for gap in gaps)
            and max(gaps) <= SHADOW_PATH_MAX_SAMPLE_GAP_SECONDS
        )
    except (TypeError, ValueError, KeyError, OverflowError):
        return False


def _mark_shadow_mint_completed(state, mint):
    """Prevent repeated windows on one token from masquerading as independent samples."""
    if not isinstance(state.get("shadow_completed_mints"), list):
        state["shadow_completed_mints"] = []
    mint = str(mint or "").strip()
    completed = state["shadow_completed_mints"]
    if mint and mint not in completed:
        completed.append(mint)
    state["shadow_completed_mints"] = completed[-MAX_SHADOW_COMPLETED_MINTS:]


def manage_shadow_positions(state):
    """Label the existing exit once, then retain quotes for research through the full horizon."""
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

        now = time.time()
        age_seconds = max(0.0, now - entry_time)
        benchmark_reason = pos.get("benchmark_exit_reason")
        if age_seconds > SHADOW_STALE_OBSERVATION_SECONDS and not benchmark_reason:
            # Never label a price observed after the intended research window as the exit.
            state["shadow_positions"].pop(mint, None)
            log(f"[SHADOW DISCARD] {pos.get('symbol', '?')} stale observation age={age_seconds:.0f}s; excluded from strategy data")
            continue

        pair = fetch_dex_pair(mint)
        if not pair:
            if age_seconds >= SHADOW_STALE_OBSERVATION_SECONDS and benchmark_reason:
                saved = _append_shadow_price_path(mint, pos, observed_at=now)
                state["shadow_positions"].pop(mint, None)
                if saved and _shadow_path_meets_research_quality(pos):
                    _mark_shadow_mint_completed(state, mint)
            continue
        try:
            price = float(pair.get("priceUsd") or 0)
            liquidity = float((pair.get("liquidity") or {}).get("usd") or 0)
        except (TypeError, ValueError, AttributeError, OverflowError):
            continue
        if not math.isfinite(price) or price <= 0:
            continue
        if not math.isfinite(liquidity) or liquidity < 0:
            liquidity = 0.0

        pos["peak_price"] = max(peak_price, price)
        history = pos.get("price_history")
        if not isinstance(history, list):
            history = []
            pos["price_history"] = history
        sample = {"timestamp": now, "price_usd": price, "liquidity_usd": liquidity}
        if history and isinstance(history[-1], dict) and float(history[-1].get("timestamp", -1)) == now:
            history[-1] = sample
        else:
            history.append(sample)
        # Each shadow trade is sampled once per monitor pass. Bound state size even
        # if the process is delayed; never manufacture intermediate candle points.
        if len(history) > 64:
            pos["price_history"] = history[-64:]

        if not benchmark_reason:
            pnl = (price - entry_price) / entry_price
            reason = "hard_stop_loss" if pnl <= -HARD_STOP_LOSS_PCT else None
            if reason is None and pnl >= 0.08:
                pos["trailing_armed"] = True
            if reason is None and pos.get("trailing_armed") and pos["peak_price"] > 0:
                if (pos["peak_price"] - price) / pos["peak_price"] >= TRAILING_STOP_PCT:
                    reason = "trailing_stop"
            if reason is None and liquidity > 0 and liquidity_drain_detected(pos, liquidity):
                reason = "liquidity_drained"
            if reason is None and age_seconds >= MAX_HOLD_SECONDS:
                reason = "max_hold_timeout"

            if reason:
                effective_exit = price * (1.0 - EXIT_SLIPPAGE_BPS / 10000.0)
                net_pnl_pct = (effective_exit - entry_price) / entry_price * 100.0
                append_learning_observation(
                    pos.get("signal_score"), net_pnl_pct, f"shadow_{reason}", pos.get("components")
                )
                pos["benchmark_exit_reason"] = reason
                pos["benchmark_exit_timestamp"] = now
                pos["benchmark_exit_price_usd"] = price
                pos["benchmark_exit_liquidity_usd"] = liquidity
                pos["benchmark_exit_after_slippage_pnl_pct"] = net_pnl_pct
                log(
                    f"[SHADOW BENCHMARK EXIT] {pos.get('symbol', '?')} {reason} "
                    f"pnl={net_pnl_pct:+.2f}% (price-path sampling continues; account equity unchanged)"
                )

        # Preserve the same baseline label in the outcome ledger, but continue
        # sampling observed quotes until the full 15-minute research horizon.
        # This allows alternate exit rules to be evaluated on observed prices only.
        if age_seconds >= SHADOW_STALE_OBSERVATION_SECONDS:
            saved = _append_shadow_price_path(mint, pos, observed_at=now)
            state["shadow_positions"].pop(mint, None)
            if saved and _shadow_path_meets_research_quality(pos):
                _mark_shadow_mint_completed(state, mint)
                log(
                    f"[SHADOW PATH SAVED] {pos.get('symbol', '?')} "
                    f"samples={len(pos.get('price_history', []))} horizon={age_seconds:.0f}s; mint marked complete"
                )
            elif saved:
                log(
                    f"[SHADOW PATH REJECTED FOR RESEARCH] {pos.get('symbol', '?')} "
                    f"samples={len(pos.get('price_history', []))}; token remains eligible for a later clean sample"
                )

PROSPECTIVE_SHADOW_RULE_TAG = "moderate_pullback_v1"
PROSPECTIVE_SHADOW_ENTRY_TAGS = (
    "moderate_pullback_v1",
    "broad_pullback_v1",
    "liquid_pullback_v1",
    "tight_pullback_v1",
    "recovery_pullback_v1",
)


def _prospective_shadow_entry_filters(components):
    """Apply preregistered, paper-only entry cohorts; never changes live entries."""
    if not isinstance(components, dict):
        return []
    try:
        h1 = float(components["momentum_h1_pct"])
        m5 = float(components["momentum_m5_pct"])
        ratio = float(components["buy_sell_ratio"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return []
    if not all(math.isfinite(value) for value in (h1, m5, ratio)):
        return []

    def feature(name):
        try:
            value = float(components[name])
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        return value if math.isfinite(value) else None

    liquidity = feature("liquidity_usd")
    volume = feature("volume_h1_usd")
    risk_available = feature("memeswap_risk_data_available")
    full_risk_screen = feature("memeswap_full_risk_screen_available")
    gate_pass = feature("memeswap_gate_pass")
    tags = []

    # Original cohorts remain unchanged so their future samples are comparable.
    if 5.0 <= h1 <= 30.0 and -5.0 <= m5 <= 3.0 and 1.0 <= ratio <= 4.0:
        tags.append("moderate_pullback_v1")
    if 0.0 <= h1 <= 45.0 and -5.0 <= m5 <= 5.0 and 1.0 <= ratio <= 5.0:
        tags.append("broad_pullback_v1")
    if (5.0 <= h1 <= 45.0 and -5.0 <= m5 <= 3.0 and 1.0 <= ratio <= 4.0
            and liquidity is not None and liquidity >= 25_000.0
            and volume is not None and volume >= 10_000.0):
        tags.append("liquid_pullback_v1")
    if (5.0 <= h1 <= 30.0 and -2.0 <= m5 <= 2.0 and 1.2 <= ratio <= 4.0
            and liquidity is not None and liquidity >= 50_000.0
            and volume is not None and volume >= 25_000.0):
        tags.append("tight_pullback_v1")
    if (10.0 <= h1 <= 60.0 and -5.0 <= m5 <= 0.0 and 1.0 <= ratio <= 6.0
            and liquidity is not None and liquidity >= 25_000.0
            and volume is not None and volume >= 10_000.0):
        tags.append("recovery_pullback_v1")

    # New hypotheses intentionally broaden prospective sampling. They remain
    # paper-only and are tested on paths tagged at entry, not retrofitted to old
    # outcomes. They are not live-entry settings and carry no profitability claim.
    if (0.0 <= h1 <= 30.0 and -20.0 <= m5 <= 5.0 and 1.0 <= ratio <= 4.0
            and liquidity is not None and liquidity >= 10_000.0
            and volume is not None and volume >= 10_000.0):
        tags.append("broad_moderate_momentum_v2")
    if (5.0 <= h1 <= 45.0 and -10.0 <= m5 <= 5.0 and 1.2 <= ratio <= 4.0
            and liquidity is not None and liquidity >= 10_000.0
            and volume is not None and volume >= 10_000.0):
        tags.append("volume_confirmed_pullback_v1")
    if (0.0 <= h1 <= 30.0 and -2.0 <= m5 <= 5.0 and 1.2 <= ratio <= 4.0
            and liquidity is not None and liquidity >= 10_000.0
            and volume is not None and volume >= 10_000.0):
        tags.append("steady_pressure_v1")
    if (5.0 <= h1 <= 60.0 and -10.0 <= m5 <= 0.0 and 1.2 <= ratio <= 6.0
            and liquidity is not None and liquidity >= 25_000.0
            and volume is not None and volume >= 10_000.0):
        tags.append("continuation_cooldown_v1")
    if (-10.0 <= h1 <= 20.0 and -10.0 <= m5 <= 3.0 and 1.0 <= ratio <= 6.0
            and liquidity is not None and liquidity >= 10_000.0
            and volume is not None and volume >= 10_000.0):
        tags.append("weak_trend_recovery_v1")

    # Strict risk-screened cohort requires complete, fresh, explicitly safe data.
    # A basic token lookup (including a gatePass=false row) is not a risk screen.
    risk_fields = {
        name: feature(name) for name in (
            "memeswap_price_age_seconds", "memeswap_profile_age_seconds",
            "memeswap_top1_pct", "memeswap_top10_pct", "memeswap_top20_pct",
            "memeswap_honeypot", "memeswap_mintable", "memeswap_fake_score",
            "memeswap_lp_locked_pct", "memeswap_buy_tax_value", "memeswap_sell_tax_value",
        )
    }
    if (
        5.0 <= h1 <= 45.0 and -10.0 <= m5 <= 5.0 and 1.2 <= ratio <= 4.0
        and liquidity is not None and liquidity >= 10_000.0
        and volume is not None and volume >= 10_000.0
        and risk_available == 1.0 and full_risk_screen == 1.0 and gate_pass == 1.0
        and risk_fields["memeswap_price_age_seconds"] is not None
        and risk_fields["memeswap_price_age_seconds"] <= 60.0
        and risk_fields["memeswap_profile_age_seconds"] is not None
        and risk_fields["memeswap_profile_age_seconds"] <= 300.0
        and risk_fields["memeswap_top1_pct"] is not None
        and risk_fields["memeswap_top1_pct"] <= 10.0
        and risk_fields["memeswap_top10_pct"] is not None
        and risk_fields["memeswap_top10_pct"] <= 35.0
        and risk_fields["memeswap_top20_pct"] is not None
        and risk_fields["memeswap_top20_pct"] <= 60.0
        and risk_fields["memeswap_honeypot"] == 0.0
        and risk_fields["memeswap_mintable"] == 0.0
        and risk_fields["memeswap_fake_score"] is not None
        and risk_fields["memeswap_fake_score"] <= 10.0
        and risk_fields["memeswap_lp_locked_pct"] is not None
        and risk_fields["memeswap_lp_locked_pct"] >= 50.0
        and risk_fields["memeswap_buy_tax_value"] == 0.0
        and risk_fields["memeswap_sell_tax_value"] == 0.0
    ):
        tags.append("risk_screened_pullback_v1")

    rugcheck = {
        name: feature(name) for name in (
            "rugcheck_available", "rugcheck_risk_data_available",
            "rugcheck_full_risk_screen_available", "rugcheck_report_age_seconds",
            "rugcheck_mint_authority_active", "rugcheck_freeze_authority_active",
            "rugcheck_mint_close_authority_active", "rugcheck_metadata_mutable",
            "rugcheck_rugged", "rugcheck_danger_risk_count",
            "rugcheck_critical_risk_count", "rugcheck_warning_risk_count",
            "rugcheck_top1_pct", "rugcheck_top10_pct", "rugcheck_top20_pct",
            "rugcheck_lp_locked_pct", "rugcheck_holder_count",
        )
    }
    rugcheck_quality = (
        rugcheck["rugcheck_available"] == 1.0
        and rugcheck["rugcheck_risk_data_available"] == 1.0
        and rugcheck["rugcheck_full_risk_screen_available"] == 1.0
        and rugcheck["rugcheck_report_age_seconds"] is not None
        and rugcheck["rugcheck_report_age_seconds"] <= RUGCHECK_MAX_REPORT_AGE_SECONDS
        and rugcheck["rugcheck_mint_authority_active"] == 0.0
        and rugcheck["rugcheck_freeze_authority_active"] == 0.0
        and rugcheck["rugcheck_mint_close_authority_active"] == 0.0
        and rugcheck["rugcheck_metadata_mutable"] == 0.0
        and rugcheck["rugcheck_rugged"] == 0.0
        and rugcheck["rugcheck_danger_risk_count"] == 0.0
        and rugcheck["rugcheck_critical_risk_count"] == 0.0
        and rugcheck["rugcheck_top1_pct"] is not None
        and rugcheck["rugcheck_top10_pct"] is not None
        and rugcheck["rugcheck_top20_pct"] is not None
        and rugcheck["rugcheck_top10_pct"] <= 35.0
        and rugcheck["rugcheck_top20_pct"] <= 60.0
        and rugcheck["rugcheck_lp_locked_pct"] is not None
        and rugcheck["rugcheck_lp_locked_pct"] >= 50.0
        and rugcheck["rugcheck_holder_count"] is not None
        and rugcheck["rugcheck_holder_count"] >= 100.0
    )
    if (
        5.0 <= h1 <= 45.0 and -10.0 <= m5 <= 5.0 and 1.2 <= ratio <= 4.0
        and liquidity is not None and liquidity >= 10_000.0
        and volume is not None and volume >= 10_000.0
        and rugcheck_quality
    ):
        tags.append("rugcheck_screened_pullback_v1")
        if rugcheck["rugcheck_warning_risk_count"] == 0.0:
            tags.append("rugcheck_no_warning_pullback_v1")

    return tags


def scan_shadow_candidates(state, candidates=None, increment_scan_count=True):
    """Collect labeled virtual-trade samples independently of account entry eligibility."""
    fetched_here = candidates is None
    candidates = fetch_candidate_mints() if candidates is None else candidates
    fetch_dex_pairs(candidates)
    if fetched_here:
        log(f"[DISCOVERY] selected={LAST_DISCOVERY_DIAGNOSTICS.get('selected', len(candidates))} source_counts={LAST_DISCOVERY_DIAGNOSTICS.get('source_counts', {})}")
    opened = []
    rejections = {}
    filter_passed = 0
    memeswap_profile_budget = iter(range(MAX_MEMESWAP_RESEARCH_PROFILES_PER_CYCLE))
    memeswap_enabled = memeswap_research_enabled()
    rugcheck_profile_budget = iter(range(MAX_RUGCHECK_RESEARCH_PROFILES_PER_CYCLE))
    rugcheck_enabled = rugcheck_research_enabled()
    # Shadow samples cover a broad score range; this never changes live entry rules.
    threshold = SHADOW_MIN_SCORE
    for mint in candidates:
        if len(state["shadow_positions"]) >= MAX_SHADOW_POSITIONS:
            break
        if not isinstance(state.get("shadow_completed_mints"), list):
            state["shadow_completed_mints"] = []
        if not state["shadow_completed_mints"]:
            state["shadow_completed_mints"] = _load_shadow_completed_mints()
        completed_mints = set(state["shadow_completed_mints"])
        # A live strategy blacklist is not a research filter. Shadow-only outcomes
        # need to include prior live losers so the model can learn from them; each
        # mint is sampled at most once after a quality-complete path has been saved.
        if mint in state["shadow_positions"] or mint in completed_mints:
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
        # Optional, bounded, read-only risk enrichment. This runs only after a
        # virtual candidate has passed the research gate; it never affects live
        # entries, sizing, or transaction execution.
        profile_slot = next(memeswap_profile_budget, None) if memeswap_enabled else None
        if profile_slot is not None:
            try:
                risk_features = fetch_memeswap_token_profile(mint)
            except Exception as exc:
                risk_features = None
                log(f"[MEMESWAP RESEARCH] {mint[:8]} profile fetch failed: {type(exc).__name__}")
            if isinstance(risk_features, dict):
                # Only allow finite scalar features from the normalizer into the
                # stored entry snapshot. Unknown values remain absent/None.
                for feature_name, feature_value in risk_features.items():
                    if feature_name.startswith("memeswap_") and (
                        feature_value is None
                        or isinstance(feature_value, (int, float)) and math.isfinite(float(feature_value))
                    ):
                        components[feature_name] = feature_value
        # Independent public RugCheck risk report. Keep requests bounded and
        # attach the report only to virtual entry-time features; live entries,
        # sizing and transaction paths do not consume this enrichment.
        rugcheck_slot = next(rugcheck_profile_budget, None) if rugcheck_enabled else None
        if rugcheck_slot is not None:
            try:
                rugcheck_features = fetch_rugcheck_token_report(mint)
            except Exception as exc:
                rugcheck_features = None
                log(f"[RUGCHECK RESEARCH] {mint[:8]} report fetch failed: {type(exc).__name__}")
            if isinstance(rugcheck_features, dict):
                for feature_name, feature_value in rugcheck_features.items():
                    if feature_name.startswith("rugcheck_") and (
                        feature_value is None
                        or isinstance(feature_value, (int, float)) and math.isfinite(float(feature_value))
                    ):
                        components[feature_name] = feature_value
        entry_price = price * (1.0 + ENTRY_SLIPPAGE_BPS / 10000.0)
        opened_at = time.time()
        state["shadow_positions"][mint] = {
            "symbol": (pair.get("baseToken") or {}).get("symbol", "?"),
            "entry_price": entry_price,
            "entry_time": opened_at,
            "entry_liquidity": liquidity,
            "peak_price": entry_price,
            "signal_score": score,
            "trailing_armed": False,
            "components": components,
            "prospective_entry_filters": _prospective_shadow_entry_filters(components),
            "price_history": [{
                "timestamp": opened_at,
                "price_usd": price,
                "liquidity_usd": liquidity,
            }],
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
    # Keep outcome cohorts separate: broad shadow research, legacy/momentum paper
    # trades, and entries that actually passed the whale-accumulation gate.
    feature_values = features if isinstance(features, dict) else {}
    try:
        whale_gate_enabled = float(feature_values.get("whale_gate_enabled", 0) or 0) >= 1.0
        whale_accumulation_confirmed_flag = float(
            feature_values.get("whale_accumulation_confirmed", 0) or 0
        ) >= 1.0
    except (TypeError, ValueError, OverflowError):
        whale_gate_enabled = False
        whale_accumulation_confirmed_flag = False
    if str(reason).startswith("shadow_"):
        filename = "shadow_observations.csv"
    elif whale_gate_enabled and whale_accumulation_confirmed_flag:
        filename = "whale_strategy_observations.csv"
    else:
        filename = "strategy_observations.csv"
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
    "fetch_dex_pairs",
    "fetch_candidate_mints",
    "append_trade",
    "append_learning_observation",
    "passes_market_filter",
    "passes_whale_follow_market_filter",
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


def run_cycle(state=None, require_whale_accumulation=False, research_shadow=True):
    """Run a scan cycle; shadow discovery can be isolated from historical-account monitoring."""
    # The process runs the historical and isolated paper accounts sequentially.
    # Clear between cycles, then share each fresh pair response between shadow and
    # strategy evaluation so the same token is not fetched/scored twice inconsistently.
    PAIR_CACHE.clear()
    state = normalize_state(state if state is not None else load_state())
    state = normalize_legacy_strategy_baseline(state)
    reconcile_learning_from_trade_log(state)
    manage_open_positions(state)
    manage_shadow_positions(state)
    opened = []
    if state.get("profit_lock_active"):
        state["last_run"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        save_state(state)
        log("[PROFIT LOCK] validated whale strategy is net profitable; new paper entries disabled")
        return {
            "scanned": 0,
            "opened": opened,
            "open_positions": len(state["positions"]),
            "shadow_positions": len(state["shadow_positions"]),
            "equity": state["equity"],
            "profit_locked": True,
        }
    if int(state.get("trade_count", 0)) >= TARGET_TRADES:
        state["last_run"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        save_state(state)
        log(f"[CYCLE] paper-trade target reached ({TARGET_TRADES}); new entries disabled")
        return {"scanned": 0, "opened": opened, "open_positions": len(state["positions"]), "equity": state["equity"], "target_reached": True}
    risk = update_risk_control(state)
    if risk["status"] == "halted":
        # No account entries. The historical account still manages any already-open
        # shadow positions, but only the isolated paper cycle discovers new research mints.
        shadow = (
            scan_shadow_candidates(state)
            if research_shadow else {"scanned": 0, "shadow_opened": []}
        )
        state["last_run"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        save_state(state)
        log(f"[RISK HALT] drawdown={risk['drawdown_pct']:.2f}% exceeded max {risk['max_drawdown_pct']:.2f}%; account entries blocked; shadow_scanned={shadow['scanned']} shadow_opened={len(shadow['shadow_opened'])}")
        return {"scanned": shadow["scanned"], "opened": opened, "shadow_opened": shadow["shadow_opened"], "open_positions": len(state["positions"]), "shadow_positions": len(state["shadow_positions"]), "equity": state["equity"], "risk_halted": True, "risk_control": risk}
    candidates = fetch_candidate_mints()
    fetch_dex_pairs(candidates)
    log(f"[DISCOVERY] selected={LAST_DISCOVERY_DIAGNOSTICS.get('selected', len(candidates))} source_counts={LAST_DISCOVERY_DIAGNOSTICS.get('source_counts', {})}")
    # The historical account can discover/evaluate entries, but only the independent
    # paper account creates new shadow research paths. This avoids duplicate token
    # windows across two accounts sharing the same discovery stream.
    if research_shadow:
        scan_shadow_candidates(state, candidates=candidates, increment_scan_count=False)
    validation_status = state.get("strategy", {}).get("validation_status")
    if validation_status and validation_status != "validated":
        # The paper account is capital preservation, not a place to keep testing
        # unvalidated thresholds. Isolated shadow research above continues collecting
        # observed quotes without changing account equity.
        state["scan_count"] += 1
        state["last_run"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        save_state(state)
        log(
            f"[ENTRY LOCK] paper entries disabled until an entry/exit rule passes "
            f"independent net-profit validation; status={validation_status}; "
            f"shadow_positions={len(state.get('shadow_positions', {}))}"
        )
        return {
            "scanned": len(candidates),
            "opened": [],
            "open_positions": len(state["positions"]),
            "shadow_positions": len(state["shadow_positions"]),
            "equity": state["equity"],
            "entry_locked": True,
            "validation_status": validation_status,
        }

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
        market_ok, market_reason = (
            passes_whale_follow_market_filter(pair)
            if require_whale_accumulation else passes_market_filter(pair)
        )
        if not market_ok:
            reject(market_reason)
            continue
        diagnostics["market_filter_passed"] += 1
        score, components = score_market_opportunity(
            pair, state.get("learning"), require_whale_accumulation=require_whale_accumulation
        )
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
        whale_accumulation_passed = False
        has_whale_provider = helius_configured()
        if require_whale_accumulation and not has_whale_provider:
            reject("whale_data_unavailable")
            continue
        if has_whale_provider:
            snapshot = fetch_whale_balances(mint)
            if not snapshot:
                if require_whale_accumulation:
                    log(f"[WARN] skipping entry for {mint}: required whale data unavailable")
                    reject("whale_data_unavailable")
                    continue
                # Whale snapshots are optional in the exploratory paper profile.
                # A configured but flaky RPC must not silently disable every entry.
                log(f"[WARN] whale snapshot unavailable for {mint}; continuing paper-only without holder confirmation")
            else:
                previous_snapshot = state["whale_snapshots"].get(mint, {})
                if previous_snapshot and confirmed_whale_dump(previous_snapshot, snapshot, pair):
                    state["whale_snapshots"][mint] = snapshot
                    state["candidate_observations"].pop(mint, None)
                    log(f"[WHALE] entry blocked for {mint}: holder-balance drop plus market sell pressure")
                    reject("confirmed_whale_dump")
                    continue
                if require_whale_accumulation:
                    if not previous_snapshot:
                        state["whale_snapshots"][mint] = snapshot
                        reject("whale_baseline_pending")
                        continue
                    if not whale_accumulation_confirmed(previous_snapshot, snapshot, pair):
                        state["whale_snapshots"][mint] = snapshot
                        reject("whale_accumulation_not_confirmed")
                        continue
                    whale_accumulation_passed = True
                whale_balances = snapshot
                state["whale_snapshots"][mint] = snapshot
                while len(state["whale_snapshots"]) > 100:
                    state["whale_snapshots"].pop(next(iter(state["whale_snapshots"])))
        # Persist whether this entry was actually admitted by the whale gate.
        # These numeric labels flow into the outcome ledger for later walk-forward
        # analysis; they never alter the score itself.
        components["whale_gate_enabled"] = 1.0 if require_whale_accumulation else 0.0
        components["whale_accumulation_confirmed"] = 1.0 if whale_accumulation_passed else 0.0
        components["entry_liquidity_usd"] = float((pair.get("liquidity") or {}).get("usd") or 0.0)
        components["entry_volume_h1_usd"] = float((pair.get("volume") or {}).get("h1") or 0.0)
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
    if diagnostics["evaluated_candidates"]:
        log(f"[PAPER SCORED CANDIDATES] {diagnostics['evaluated_candidates']}")
    state["scan_count"] += 1
    state["last_run"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    save_state(state)
    log(f"[CYCLE] scanned={len(candidates)} opened={len(opened)} positions={len(state['positions'])} equity=${state['equity']:.2f}")
    return {"scanned": len(candidates), "opened": opened, "open_positions": len(state["positions"]), "equity": state["equity"]}


def run_isolated_paper_cycle(root_dir=None):
    """Run an independent paper experiment; never modifies the historical account."""
    root = Path(root_dir or Path.cwd()).resolve()
    paper_dir = root / "paper_account"
    paper_dir.mkdir(parents=True, exist_ok=True)
    previous_cwd = Path.cwd()
    try:
        os.chdir(paper_dir)
        paper_state = load_state()
        strategy = paper_state.setdefault("strategy", {})
        if should_lock_paper_profit(paper_state):
            paper_state["profit_lock_active"] = True
            paper_state.setdefault(
                "profit_lock_activated_at",
                datetime.utcnow().isoformat(timespec="seconds") + "Z",
            )
            save_state(paper_state)
        validation_status = strategy.get("validation_status")
        strategy_status = strategy.get("status")

        # Exploration samples are collected by the separate shadow cohort, whose
        # filter/score gates do not depend on this account's entry threshold. An
        # unvalidated experimental threshold therefore must never keep admitting
        # paper-account entries merely to manufacture more training trades.
        if validation_status != "validated" and strategy_status != "rolled_back":
            try:
                active_threshold = float(strategy.get("active_threshold", OPPORTUNITY_ENTRY_THRESHOLD))
            except (TypeError, ValueError, OverflowError):
                active_threshold = float("nan")
            if not math.isfinite(active_threshold) or active_threshold != LEGACY_BASELINE_ENTRY_THRESHOLD:
                strategy.update({
                    "active_threshold": LEGACY_BASELINE_ENTRY_THRESHOLD,
                    "rollback_threshold": LEGACY_BASELINE_ENTRY_THRESHOLD,
                    "candidate_threshold": None,
                    "status": "rolled_back",
                    "validation_status": "rolled_back",
                    "rollback_reason": "unvalidated_threshold_reset_to_baseline",
                })
                save_state(paper_state)
            elif strategy.get("candidate_threshold") is not None:
                strategy["candidate_threshold"] = None
                save_state(paper_state)
        # Whale-follow mode is the isolated account's default. Do not silently
        # fall back to momentum-only entries when the RPC provider is missing;
        # run_cycle will fail closed and record whale_data_unavailable instead.
        require_whale_data = os.environ.get("PAPER_REQUIRE_WHALE_ACCUMULATION", "1").strip() == "1"
        log(
            "[PAPER MODE] "
            f"threshold={strategy.get('active_threshold', OPPORTUNITY_ENTRY_THRESHOLD):g} "
            f"validation={strategy.get('validation_status', 'unknown')} "
            f"whale_accumulation_gate={'on' if require_whale_data else 'off'}"
        )
        return run_cycle(paper_state, require_whale_accumulation=require_whale_data)
    finally:
        os.chdir(previous_cwd)


if __name__ == "__main__":
    try:
        workspace = Path(__file__).resolve().parent
        previous_shadow_path_log = os.environ.get("SHADOW_PRICE_PATH_LOG")
        # Keep the historical account's remaining open shadow paths in its legacy
        # ledger. New research entries belong only to the isolated paper cohort.
        os.environ["SHADOW_PRICE_PATH_LOG"] = str(workspace / "shadow_price_paths.jsonl")
        try:
            run_cycle(research_shadow=False)
        finally:
            if previous_shadow_path_log is None:
                os.environ.pop("SHADOW_PRICE_PATH_LOG", None)
            else:
                os.environ["SHADOW_PRICE_PATH_LOG"] = previous_shadow_path_log
        paper_result = run_isolated_paper_cycle(workspace)
        log(
            "[PAPER ACCOUNT] independent cycle complete | "
            f"equity=${float(paper_result.get('equity', 0.0)):.2f} "
            f"positions={paper_result.get('open_positions', 0)} "
            f"trades={paper_result.get('trade_count', 'see paper ledger')}"
        )
    except Exception as exc:
        log(f"[ERROR] trading cycle failed: {type(exc).__name__}: {exc}")
        raise
