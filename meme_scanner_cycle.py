#!/usr/bin/env python3
"""
meme_scanner_cycle.py — v12-cycle-hardened

Single-cycle paper-trading scanner for GitHub Actions.
"""

import json
import os
import time
import csv
from datetime import datetime, timezone

import requests

VERSION = "v12-cycle-hardened"
STATE_FILE = "state.json"
TRADES_CSV = "trades_log.csv"
STARTING_EQUITY = 1000.0
POSITION_SIZE_USD = 100.0
MAX_CONCURRENT_POSITIONS = 3
TAKE_PROFIT_ACTIVATE = 0.08
TRAILING_DROP = 0.05
STOP_LOSS = 0.10
MAX_HOLD_SECONDS = 15 * 60
MIN_LIQUIDITY_USD = 2000
MIN_VOLUME_H1_USD = 500
MIN_LIQ_MCAP_RATIO = 0.10
MIN_BUY_SELL_RATIO = 1.3
MAX_HOLDER_CONCENTRATION = 0.50
WHALE_TOP_N = 20
WHALE_DUMP_THRESHOLD = 0.15
STOP_LOSS_COOLDOWN_SECONDS = 30 * 60
RPC_ENDPOINTS = [
    os.environ.get("HELIUS_RPC_URL", ""),
    "https://rpc.solanatracker.io/public",
    "https://rpc.nodeflare.app/solana/public",
    "https://solana-rpc.publicnode.com",
    "https://api.mainnet-beta.solana.com",
]
RPC_ENDPOINTS = list(dict.fromkeys(u for u in RPC_ENDPOINTS if u))
HEADERS = {"User-Agent": "meme-scanner-cycle/12"}
TIMEOUT = 6
HTTP_RETRIES = 3
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"equity": STARTING_EQUITY, "positions": {}, "blacklist": [], "cooldowns": {}, "trade_count": 0, "scan_count": 0, "version": VERSION}


def save_state(state):
    state["version"] = VERSION
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def append_trade(row):
    is_new = not os.path.exists(TRADES_CSV)
    with open(TRADES_CSV, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["timestamp", "mint", "symbol", "entry_price", "exit_price", "pnl_pct", "pnl_usd", "equity_after", "reason"])
        if is_new:
            w.writeheader()
        w.writerow(row)


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
    for url in ["https://api.dexscreener.com/token-boosts/latest/v1", "https://api.dexscreener.com/token-profiles/latest/v1"]:
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
        for attempt in range(1, 3):
            try:
                r = requests.post(endpoint, json=payload, headers=HEADERS, timeout=TIMEOUT)
                if r.status_code != 200:
                    log(f"  RPC {endpoint} -> HTTP {r.status_code}")
                    if r.status_code in RETRYABLE_STATUS and attempt == 1:
                        time.sleep(0.5)
                        continue
                    break
                body = r.json()
                if not isinstance(body, dict) or body.get("error"):
                    if isinstance(body, dict) and body.get("error"):
                        log(f"  RPC {endpoint} -> error {body['error'].get('code')}")
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
                    log(f"  RPC {endpoint} -> whale data OK ({len(balances)} accounts)")
                    return balances
                break
            except Exception as e:
                log(f"  RPC {endpoint} failed (attempt {attempt}/2): {e}")
                if attempt == 2:
                    break
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


def passes_market_filter(pair):
    liq = (pair.get("liquidity") or {}).get("usd") or 0
    vol_h1 = (pair.get("volume") or {}).get("h1") or 0
    mcap = pair.get("marketCap") or pair.get("fdv") or 0
    txns_m5 = (pair.get("txns") or {}).get("m5") or {}
    buys, sells = txns_m5.get("buys", 0), txns_m5.get("sells", 0)
    if liq < MIN_LIQUIDITY_USD:
        return False, f"low_liquidity:{liq}"
    if vol_h1 < MIN_VOLUME_H1_USD:
        return False, f"low_volume:{vol_h1}"
    if mcap and (liq / mcap) < MIN_LIQ_MCAP_RATIO:
        return False, f"low_liq_mcap_ratio:{liq/mcap:.3f}"
    if sells > 0 and (buys / sells) < MIN_BUY_SELL_RATIO:
        return False, f"weak_buy_pressure:{buys}/{sells}"
    if sells == 0 and buys == 0:
        return False, "no_recent_txns"
    return True, "ok"


def open_position(state, mint, pair, whale_balances):
    price = float(pair.get("priceUsd") or 0)
    if price <= 0 or whale_balances is None:
        return False
    symbol = (pair.get("baseToken") or {}).get("symbol", "?")
    liq = float((pair.get("liquidity") or {}).get("usd") or 0)
    state["positions"][mint] = {
        "symbol": symbol,
        "entry_price": price,
        "entry_time": time.time(),
        "amount_usd": POSITION_SIZE_USD,
        "peak_price": price,
        "trailing_active": False,
        "entry_liquidity": liq,
        "whale_baseline": whale_balances,
    }
    log(f"  OPENED {symbol} ({mint[:6]}...) @ ${price:.8f} liq=${liq:.0f}")
    return True


def close_position(state, mint, exit_price, reason):
    pos = state["positions"].pop(mint)
    entry_price = pos["entry_price"]
    pnl_pct = (exit_price - entry_price) / entry_price if entry_price else 0
    pnl_usd = pos["amount_usd"] * pnl_pct
    state["equity"] += pnl_usd
    state["trade_count"] += 1
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
        entry_liq = pos.get("entry_liquidity")
        if entry_liq > 0 and liq < entry_liq * 0.80:
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
                for addr, base_amt in baseline.items():
                    now_amt = current.get(addr, 0)
                    if base_amt > 0 and (base_amt - now_amt) / base_amt >= WHALE_DUMP_THRESHOLD:
                        close_position(state, mint, price, "whale_dump")
                        break
                if mint not in state["positions"]:
                    continue
        pnl_pct = (price - pos["entry_price"]) / pos["entry_price"]
        if price > pos["peak_price"]:
            pos["peak_price"] = price
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
    slots = MAX_CONCURRENT_POSITIONS - len(state["positions"])
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
        ok, why = passes_market_filter(pair)
        if not ok:
            continue
        rc = fetch_rugcheck(mint)
        ok, why = passes_security_filter(rc)
        if not ok:
            continue
        whale_balances = fetch_whale_balances(mint)
        if whale_balances is None:
            log(f"  skipped {mint[:6]}...: whale data unavailable")
            continue
        if open_position(state, mint, pair, whale_balances):
            slots -= 1


def main():
    state = load_state()
    state["scan_count"] = state.get("scan_count", 0) + 1
    log(f"=== {VERSION} cycle #{state['scan_count']} ===")
    manage_open_positions(state)
    look_for_entries(state)
    save_state(state)
    log(f"=== done | equity=${state['equity']:.2f} positions={len(state['positions'])} trades={state['trade_count']} ===")


if __name__ == "__main__":
    main()
