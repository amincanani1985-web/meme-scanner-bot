#!/usr/bin/env python3
"""
meme_scanner_cycle.py — v13-entry-hardened

Single-cycle paper-trading scanner for GitHub Actions.
"""

import json
import os
import time
import csv
from datetime import datetime, timezone

import requests

try:
    from learn_agent import adaptive_update_state, load_observations
except Exception:
    adaptive_update_state = None
    load_observations = lambda path="strategy_observations.csv": []

VERSION = "v16-adaptive-self-correcting"
STATE_FILE = "state.json"
TRADES_CSV = "trades_log.csv"
STARTING_EQUITY = 1000.0
POSITION_SIZE_USD = 100.0
MAX_POSITION_EQUITY_FRACTION = 0.05
MIN_ENTRY_EQUITY_USD = 200.0
MAX_CONCURRENT_POSITIONS = 8
TAKE_PROFIT_ACTIVATE = 0.06
TRAILING_DROP = 0.04
STOP_LOSS = 0.12
HARD_STOP_LOSS = 0.15
ENTRY_CONFIRMATIONS = 2
ENTRY_CONFIRMATION_WINDOW_SECONDS = 15 * 60
ENTRY_SLIPPAGE_BPS = 50
EXIT_SLIPPAGE_BPS = 100
MAX_IMPACT_FRACTION = 0.01
LEARNING_OBSERVATIONS_CSV = "strategy_observations.csv"
MAX_HOLD_SECONDS = 15 * 60
MIN_LIQUIDITY_USD = 20000
LIQUIDITY_EMERGENCY_USD = 10000
LIQUIDITY_DRAIN_RATIO = 0.50
LIQUIDITY_DRAIN_CONFIRMATIONS = 2
MIN_VOLUME_H1_USD = 3000
MIN_LIQ_MCAP_RATIO = 0.25
MIN_BUY_SELL_RATIO = 1.5
MIN_PRICE_CHANGE_M5_PCT = 1.0
MAX_HOLDER_CONCENTRATION = 0.45
WHALE_TOP_N = 20
WHALE_DUMP_THRESHOLD = 0.15
STOP_LOSS_COOLDOWN_SECONDS = 30 * 60
TARGET_TRADES = 200
MAX_PORTFOLIO_EXPOSURE_FRACTION = 0.20
OPPORTUNITY_ENTRY_THRESHOLD = 65.0
ADAPTIVE_MIN_TRADES = 20
ADAPTIVE_UPDATE_EVERY_TRADES = 5
LEARNING_MIN_TRADES = 10
LEARNING_ALPHA = 0.20
HELIUS_RPC_URL = os.environ.get("HELIUS_RPC_URL", "").strip()
HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "").strip()
HELIUS_API_ENDPOINT = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}" if HELIUS_API_KEY else ""
RPC_ENDPOINTS = [
    HELIUS_RPC_URL,
    HELIUS_API_ENDPOINT,
    "https://rpc.solanatracker.io/public",
    "https://rpc.ankr.com/solana",
    "https://solana-rpc.publicnode.com",
    "https://api.mainnet.solana.com",
    "https://api.mainnet-beta.solana.com",
]
RPC_ENDPOINTS = list(dict.fromkeys(u for u in RPC_ENDPOINTS if u))
HEADERS = {"User-Agent": "meme-scanner-cycle/13"}
TIMEOUT = 5
RPC_TIMEOUT = 4
RPC_MAX_RETRIES = 1
HTTP_RETRIES = 3
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"equity": STARTING_EQUITY, "positions": {}, "blacklist": [], "cooldowns": {}, "trade_count": 0, "scan_count": 0, "version": VERSION, "candidate_observations": {}, "strategy": {"active_threshold": OPPORTUNITY_ENTRY_THRESHOLD, "candidate_threshold": None, "status": "baseline", "last_update_trade": 0, "rollback_threshold": OPPORTUNITY_ENTRY_THRESHOLD}}


def save_state(state):
    state["version"] = VERSION
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def append_trade(row):
    is_new = not os.path.exists(TRADES_CSV)
    with open(TRADES_CSV, "a", newline="") as f:
        fields = ["timestamp", "mint", "symbol", "entry_price", "exit_price", "pnl_pct", "pnl_usd", "equity_after", "reason"]
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if is_new:
            w.writeheader()
        w.writerow(row)

def append_learning_observation(pos, pnl_pct, reason):
    is_new = not os.path.exists(LEARNING_OBSERVATIONS_CSV)
    fields = ["timestamp", "score", "pnl_pct", "reason"]
    with open(LEARNING_OBSERVATIONS_CSV, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if is_new:
            w.writeheader()
        w.writerow({"timestamp": datetime.now(timezone.utc).isoformat(), "score": round(float(pos.get("signal_score") or 0), 4), "pnl_pct": round(pnl_pct * 100, 4), "reason": reason})

def get_entry_threshold(state):
    strategy = state.get("strategy") or {}
    try:
        threshold = float(strategy.get("active_threshold", OPPORTUNITY_ENTRY_THRESHOLD))
    except (TypeError, ValueError):
        threshold = OPPORTUNITY_ENTRY_THRESHOLD
    return max(55.0, min(70.0, threshold))

def candidate_confirmation(state, mint, score):
    now = time.time()
    observations = state.setdefault("candidate_observations", {})
    item = observations.get(mint) or {"hits": 0, "last_seen": 0.0, "last_score": 0.0}
    if now - float(item.get("last_seen") or 0) > ENTRY_CONFIRMATION_WINDOW_SECONDS:
        item = {"hits": 0, "last_seen": 0.0, "last_score": 0.0}
    previous_score = float(item.get("last_score") or 0.0)
    if item["hits"] > 0 and score < max(OPPORTUNITY_ENTRY_THRESHOLD, previous_score - 5.0):
        item = {"hits": 0, "last_seen": now, "last_score": 0.0}
    item["hits"] = int(item.get("hits") or 0) + 1
    item["last_seen"] = now
    item["last_score"] = round(score, 2)
    observations[mint] = item
    return item["hits"] >= ENTRY_CONFIRMATIONS


def safe_get_json(url, params=None):
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=TIMEOUT)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, (dict, list)):
                    return data
                log(f"  GET {url} -> invalid JSON shape")
                return None
            log(f"  GET {url} -> HTTP {r.status_code}")
            if r.status_code not in RETRYABLE_STATUS or attempt == HTTP_RETRIES:
                return None
        except Exception as e:
            log(f"  GET {url} failed (attempt {attempt}/{HTTP_RETRIES}): {e}")
            if attempt == HTTP_RETRIES:
                return None
        time.sleep(0.5 * attempt)
    return None


def fetch_dex_pair(mint):
    data = safe_get_json(f"https://api.dexscreener.com/latest/dex/tokens/{mint}")
    if not data or not data.get("pairs"):
        return None
    pairs = [p for p in data["pairs"] if p.get("chainId") == "solana"]
    if not pairs:
        return None
    pairs.sort(key=lambda p: (p.get("liquidity") or {}).get("usd", 0), reverse=True)
    return pairs[0]


def discover_candidates():
    mints = set()
    for url in ["https://api.dexscreener.com/token-boosts/latest/v1", "https://api.dexscreener.com/token-boosts/top/v1", "https://api.dexscreener.com/token-profiles/latest/v1"]:
        data = safe_get_json(url)
        if not data:
            continue
        items = data if isinstance(data, list) else data.get("items", [])
        for item in items:
            if item.get("chainId") == "solana" and item.get("tokenAddress"):
                mints.add(item["tokenAddress"])
    return list(mints)


def fetch_rugcheck(mint):
    return safe_get_json(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report")


def fetch_whale_balances(mint):
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts", "params": [mint, {"commitment": "confirmed"}]}
    for endpoint in RPC_ENDPOINTS:
        for attempt in range(1, RPC_MAX_RETRIES + 1):
            try:
                r = requests.post(endpoint, json=payload, headers=HEADERS, timeout=RPC_TIMEOUT)
                if r.status_code != 200:
                    log(f"  RPC endpoint #{RPC_ENDPOINTS.index(endpoint)+1} -> HTTP {r.status_code}")
                    break
                body = r.json()
                if not isinstance(body, dict) or body.get("error"):
                    if isinstance(body, dict) and body.get("error"):
                        log(f"  RPC endpoint #{RPC_ENDPOINTS.index(endpoint)+1} -> error {body['error'].get('code')}")
                    break
                result = (body.get("result") or {}).get("value") or []
                if not result:
                    break
                balances = {}
                for acc in result[:WHALE_TOP_N]:
                    address = acc.get("address")
                    if not address:
                        continue
                    raw = acc.get("uiAmount")
                    if raw is None:
                        raw = float(acc.get("amount") or 0) / (10 ** int(acc.get("decimals") or 0))
                    balances[address] = float(raw or 0)
                if balances:
                    log(f"  RPC endpoint #{RPC_ENDPOINTS.index(endpoint)+1} -> whale data OK ({len(balances)} accounts)")
                    return balances
                break
            except Exception as e:
                log(f"  RPC endpoint #{RPC_ENDPOINTS.index(endpoint)+1} failed (attempt {attempt}/{RPC_MAX_RETRIES}): {e}")
                if attempt == 2:
                    break
    rc_report = fetch_rugcheck(mint)
    holders = (rc_report or {}).get("topHolders") or []
    fallback = {}
    for holder in holders[:WHALE_TOP_N]:
        address = holder.get("address") or holder.get("owner")
        pct = holder.get("pct")
        if address and pct is not None:
            fallback[address] = float(pct)
    if fallback:
        log(f"  Rugcheck holder snapshot -> fallback OK ({len(fallback)} accounts)")
        return fallback
    return None


def passes_security_filter(rc_report):
    if not rc_report:
        return False, "no_rugcheck_data"
    if "mintAuthority" not in rc_report or "freezeAuthority" not in rc_report:
        return False, "incomplete_authority_data"
    if rc_report.get("mintAuthority") not in (None, "", "11111111111111111111111111111111"):
        return False, "mint_authority_not_renounced"
    if rc_report.get("freezeAuthority") not in (None, "", "11111111111111111111111111111111"):
        return False, "freeze_authority_active"
    for risk in (rc_report.get("risks") or []):
        level = (risk.get("level") or "").lower()
        if level in ("danger", "high"):
            return False, f"rugcheck_risk:{risk.get('name', level)}"
    score = rc_report.get("score_normalised")
    if score is None:
        score = rc_report.get("score")
    if score is not None and score > 5000:
        return False, f"risk_score_too_high:{score}"
    holders = rc_report.get("topHolders") or []
    total_pct = sum((h.get("pct") or 0) for h in holders[:10])
    if total_pct > MAX_HOLDER_CONCENTRATION * 100:
        return False, f"holder_concentration:{total_pct:.1f}%"
    return True, "ok"


def score_market_opportunity(pair, learning=None):
    liq = float((pair.get("liquidity") or {}).get("usd") or 0)
    vol_h1 = float((pair.get("volume") or {}).get("h1") or 0)
    mcap = float(pair.get("marketCap") or pair.get("fdv") or 0)
    txns = (pair.get("txns") or {}).get("m5") or {}
    buys = float(txns.get("buys") or 0)
    sells = float(txns.get("sells") or 0)
    total = buys + sells
    pc = pair.get("priceChange") or {}
    m5 = float(pc.get("m5") or 0)
    h1 = float(pc.get("h1") or 0)
    liq_mcap = liq / mcap if mcap > 0 else 0
    buy_ratio = buys / sells if sells > 0 else (2.5 if buys > 0 else 0)
    vol_liq = vol_h1 / liq if liq > 0 else 0
    components = {
        "liquidity": min(20.0, 20.0 * min(liq / 20000.0, 1.0)),
        "volume": min(20.0, 20.0 * min(vol_liq / 2.0, 1.0)),
        "momentum": min(15.0, max(0.0, 7.5 + m5 * 2.0)) + min(5.0, max(0.0, h1 * 0.5)),
        "buy_pressure": min(15.0, max(0.0, 7.5 + (buy_ratio - 1.0) * 5.0)),
        "activity": min(10.0, total / 4.0),
        "liq_mcap": min(10.0, max(0.0, liq_mcap * 40.0)),
        "stability": 10.0 if -5.0 <= h1 <= 50.0 else 4.0,
    }
    score = sum(components.values())
    if learning and learning.get("trades", 0) >= LEARNING_MIN_TRADES:
        bucket = str(min(90, max(0, int(score // 10) * 10)))
        stats = (learning.get("buckets") or {}).get(bucket) or {}
        n = stats.get("n", 0)
        if n >= 3:
            score += ((stats.get("wins", 0) / n) - 0.5) * 12.0
    return max(0.0, min(100.0, score)), components


def passes_market_filter(pair, learning=None, threshold=None):
    liq = (pair.get("liquidity") or {}).get("usd") or 0
    vol_h1 = (pair.get("volume") or {}).get("h1") or 0
    mcap = pair.get("marketCap") or pair.get("fdv") or 0
    txns_m5 = (pair.get("txns") or {}).get("m5") or {}
    buys, sells = txns_m5.get("buys", 0), txns_m5.get("sells", 0)
    price_change_m5 = (pair.get("priceChange") or {}).get("m5") or 0
    price_change_h1 = (pair.get("priceChange") or {}).get("h1") or 0
    if liq < MIN_LIQUIDITY_USD:
        return False, f"low_liquidity:{liq}"
    if vol_h1 < MIN_VOLUME_H1_USD:
        return False, f"low_volume:{vol_h1}"
    if sells == 0 and buys == 0:
        return False, "no_recent_txns"
    if mcap > 0 and liq / mcap < MIN_LIQ_MCAP_RATIO:
        return False, f"low_liq_mcap_ratio:{liq / mcap:.2f}"
    total_m5 = buys + sells
    if total_m5 >= 4 and buys / max(sells, 1) < MIN_BUY_SELL_RATIO:
        return False, f"weak_buy_pressure:{buys}/{sells}"
    if price_change_m5 < MIN_PRICE_CHANGE_M5_PCT or price_change_h1 <= 0:
        return False, f"weak_momentum:m5={price_change_m5:.2f},h1={price_change_h1:.2f}"
    score, _ = score_market_opportunity(pair, learning or {})
    active_threshold = float(threshold if threshold is not None else OPPORTUNITY_ENTRY_THRESHOLD)
    if score < active_threshold:
        return False, f"opportunity_score:{score:.1f}"
    return True, f"opportunity_score:{score:.1f}"


def open_position(state, mint, pair, whale_balances, signal_score=0.0, signal_components=None):
    price = float(pair.get("priceUsd") or 0)
    if price <= 0 or whale_balances is None:
        return False
    if state.get("equity", 0) < MIN_ENTRY_EQUITY_USD:
        return False
    symbol = (pair.get("baseToken") or {}).get("symbol", "?")
    liq = float((pair.get("liquidity") or {}).get("usd") or 0)
    state["positions"][mint] = {
        "symbol": symbol,
        "entry_price": price * (1.0 + ENTRY_SLIPPAGE_BPS / 10000.0),
        "market_entry_price": price,
        "entry_time": time.time(),
        "amount_usd": round(min(POSITION_SIZE_USD, state["equity"] * MAX_POSITION_EQUITY_FRACTION), 2),
        "estimated_impact": round(min(MAX_IMPACT_FRACTION, ((min(POSITION_SIZE_USD, state["equity"] * MAX_POSITION_EQUITY_FRACTION) / max(liq, 1.0)) ** 0.5) * 0.005), 6),
        "peak_price": price,
        "trailing_active": False,
        "entry_liquidity": liq,
        "liquidity_drain_hits": 0,
        "whale_baseline": whale_balances,
        "whale_dump_hits": 0,
        "signal_score": round(signal_score, 2),
        "signal_components": signal_components or {},
    }
    log(f"  OPENED {symbol} ({mint[:6]}...) @ ${price:.8f} liq=${liq:.0f}")
    return True


def update_learning(state, pos, pnl_pct):
    learning = state.setdefault("learning", {"trades": 0, "wins": 0, "buckets": {}})
    score = float(pos.get("signal_score") or 0)
    bucket = str(min(90, max(0, int(score // 10) * 10)))
    stats = learning.setdefault("buckets", {}).setdefault(bucket, {"n": 0, "wins": 0, "avg_pnl": 0.0})
    stats["n"] += 1
    if pnl_pct > 0:
        learning["wins"] += 1
        stats["wins"] += 1
    stats["avg_pnl"] = round(stats["avg_pnl"] + LEARNING_ALPHA * (pnl_pct * 100.0 - stats["avg_pnl"]), 4)
    learning["trades"] += 1


def liquidity_drain_detected(pos, liquidity):
    """Require persistent liquidity loss; allow an emergency absolute floor."""
    entry_liq = float(pos.get("entry_liquidity") or 0)
    if entry_liq <= 0 or liquidity <= 0:
        return False
    if liquidity <= LIQUIDITY_EMERGENCY_USD:
        return True
    if liquidity >= entry_liq * LIQUIDITY_DRAIN_RATIO:
        pos["liquidity_drain_hits"] = 0
        return False
    hits = int(pos.get("liquidity_drain_hits") or 0) + 1
    pos["liquidity_drain_hits"] = hits
    return hits >= LIQUIDITY_DRAIN_CONFIRMATIONS


def close_position(state, mint, exit_price, reason):
    pos = state["positions"].pop(mint)
    entry_price = pos["entry_price"]
    impact = float(pos.get("estimated_impact") or 0.0)
    effective_exit = exit_price * max(0.0, 1.0 - EXIT_SLIPPAGE_BPS / 10000.0 - impact)
    pnl_pct = (effective_exit - entry_price) / entry_price if entry_price else 0
    pnl_usd = pos["amount_usd"] * pnl_pct
    state["equity"] += pnl_usd
    state["trade_count"] += 1
    update_learning(state, pos, pnl_pct)
    append_learning_observation(pos, pnl_pct, reason)
    append_trade({"timestamp": datetime.now(timezone.utc).isoformat(), "mint": mint, "symbol": pos["symbol"], "entry_price": entry_price, "exit_price": exit_price, "pnl_pct": round(pnl_pct * 100, 2), "pnl_usd": round(pnl_usd, 2), "equity_after": round(state["equity"], 2), "reason": reason})
    if reason == "liquidity_drained":
        if mint not in state["blacklist"]:
            state["blacklist"].append(mint)
        log(f"  CLOSED {pos['symbol']} reason={reason} pnl={pnl_pct*100:.1f}% -> BLACKLISTED")
    elif reason == "stop_loss":
        state["cooldowns"][mint] = time.time() + STOP_LOSS_COOLDOWN_SECONDS
        log(f"  CLOSED {pos['symbol']} reason={reason} pnl={pnl_pct*100:.1f}% -> 30min cooldown")
    else:
        log(f"  CLOSED {pos['symbol']} reason={reason} pnl={pnl_pct*100:.1f}%")


def manage_open_positions(state):
    for mint in list(state["positions"].keys()):
        pos = state["positions"][mint]
        pair = fetch_dex_pair(mint)
        if pair is None:
            age = time.time() - pos["entry_time"]
            if age > 2 * MAX_HOLD_SECONDS:
                close_position(state, mint, pos["entry_price"], "no_price_data_forced_close")
                if mint not in state["blacklist"]:
                    state["blacklist"].append(mint)
            else:
                log(f"  {pos['symbol']}: no price data ({age/60:.1f}min since entry)")
            continue
        price = float(pair.get("priceUsd") or 0)
        if price <= 0:
            continue
        liq = float((pair.get("liquidity") or {}).get("usd") or 0)
        if pos.get("entry_liquidity") is None:
            pos["entry_liquidity"] = liq
            log(f"  {pos['symbol']}: initialized legacy entry liquidity=${liq:.0f}")
        if liquidity_drain_detected(pos, liq):
            close_position(state, mint, price, "liquidity_drained")
            continue
        baseline = pos.get("whale_baseline") or {}
        if not baseline:
            current = fetch_whale_balances(mint)
            if current is None:
                log(f"  {pos['symbol']}: whale baseline unavailable; whale check deferred")
            else:
                pos["whale_baseline"] = current
                baseline = current
                log(f"  {pos['symbol']}: initialized legacy whale baseline ({len(current)} accounts)")
        if baseline:
            current = fetch_whale_balances(mint)
            if current is None:
                log(f"  {pos['symbol']}: whale data unavailable; skipping whale check")
            else:
                dumped = False
                for addr, base_amt in baseline.items():
                    now_amt = current.get(addr, 0)
                    if base_amt > 0 and (base_amt - now_amt) / base_amt >= WHALE_DUMP_THRESHOLD:
                        dumped = True
                        break
                if dumped:
                    pos["whale_dump_hits"] = int(pos.get("whale_dump_hits") or 0) + 1
                else:
                    pos["whale_dump_hits"] = 0
                if int(pos.get("whale_dump_hits") or 0) >= 2:
                    close_position(state, mint, price, "whale_dump")
                    continue
        pnl_pct = (price - pos["entry_price"]) / pos["entry_price"]
        if price > pos["peak_price"]:
            pos["peak_price"] = price
        if pnl_pct <= -HARD_STOP_LOSS:
            close_position(state, mint, price, "hard_stop_loss")
            continue
        if pnl_pct <= -STOP_LOSS:
            close_position(state, mint, price, "stop_loss")
            continue
        if pnl_pct >= TAKE_PROFIT_ACTIVATE:
            pos["trailing_active"] = True
        if pos["trailing_active"]:
            drop_from_peak = (pos["peak_price"] - price) / pos["peak_price"]
            if drop_from_peak >= TRAILING_DROP:
                close_position(state, mint, price, "trailing_stop")
                continue
        age = time.time() - pos["entry_time"]
        if age >= MAX_HOLD_SECONDS:
            close_position(state, mint, price, "max_hold")
            continue
        log(f"  {pos['symbol']}: ${price:.8f} ({pnl_pct*100:+.1f}%) {'[trailing]' if pos['trailing_active'] else ''}")


def look_for_entries(state):
    if state.get("trade_count", 0) >= TARGET_TRADES:
        log(f"  target reached ({TARGET_TRADES}), no new entries")
        return
    max_exposure_slots = int((state.get("equity", 0) * MAX_PORTFOLIO_EXPOSURE_FRACTION) / max(POSITION_SIZE_USD, 1.0))
    max_exposure_slots = max(1, max_exposure_slots)
    slots = min(MAX_CONCURRENT_POSITIONS - len(state["positions"]), max_exposure_slots - len(state["positions"]), TARGET_TRADES - state.get("trade_count", 0))
    if slots <= 0:
        log("  max concurrent positions reached, skipping discovery")
        return
    now = time.time()
    for mint, until in list(state["cooldowns"].items()):
        if now >= until:
            del state["cooldowns"][mint]
    candidates = discover_candidates()
    log(f"  discovered {len(candidates)} candidate mint(s)")
    for mint in candidates:
        if slots <= 0:
            break
        if mint in state["positions"] or mint in state["blacklist"] or mint in state["cooldowns"]:
            continue
        pair = fetch_dex_pair(mint)
        if pair is None:
            continue
        score, components = score_market_opportunity(pair, state.get("learning") or {})
        active_threshold = get_entry_threshold(state)
        if score < active_threshold:
            log(f"  skipped {mint[:6]}...: opportunity_score:{score:.1f}")
            continue
        ok, why = passes_market_filter(pair, state.get("learning") or {}, active_threshold)
        if not ok:
            log(f"  skipped {mint[:6]}...: {why}")
            continue
        if not candidate_confirmation(state, mint, score):
            log(f"  skipped {mint[:6]}...: awaiting_entry_confirmation")
            continue
        rc = fetch_rugcheck(mint)
        ok, why = passes_security_filter(rc)
        if not ok:
            log(f"  skipped {mint[:6]}...: {why}")
            continue
        whale_balances = fetch_whale_balances(mint)
        if whale_balances is None:
            log(f"  skipped {mint[:6]}...: whale data unavailable")
            continue
        if open_position(state, mint, pair, whale_balances, score, components):
            slots -= 1


def main():
    state = load_state()
    state.setdefault("strategy", {"active_threshold": OPPORTUNITY_ENTRY_THRESHOLD, "candidate_threshold": None, "status": "baseline", "last_update_trade": 0, "rollback_threshold": OPPORTUNITY_ENTRY_THRESHOLD})
    state.setdefault("candidate_observations", {})
    if adaptive_update_state is not None:
        try:
            observations = load_observations(LEARNING_OBSERVATIONS_CSV)
            strategy = state.get("strategy") or {}
            last_update = int(strategy.get("last_update_trade", 0) or 0)
            if state.get("trade_count", 0) >= ADAPTIVE_MIN_TRADES and state.get("trade_count", 0) - last_update >= ADAPTIVE_UPDATE_EVERY_TRADES:
                adaptive_update_state(state, observations)
                log(f"  adaptive strategy: threshold={get_entry_threshold(state):.1f} status={state.get('strategy', {}).get('status')}")
        except Exception as e:
            log(f"  adaptive learning skipped: {e}")
    state["scan_count"] = state.get("scan_count", 0) + 1
    log(f"=== {VERSION} cycle #{state['scan_count']} ===")
    manage_open_positions(state)
    look_for_entries(state)
    save_state(state)
    log(f"=== done | equity=${state['equity']:.2f} positions={len(state['positions'])} trades={state['trade_count']}/{TARGET_TRADES} ===")
    if state["trade_count"] >= TARGET_TRADES:
        log(f"=== TARGET REACHED: {TARGET_TRADES} paper trades completed ===")


if __name__ == "__main__":
    main()
