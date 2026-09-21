#!/usr/bin/env python3
"""
meme_scanner_cycle.py — v11 (GitHub Actions single-cycle edition)

Runs ONE cycle: load state -> manage open positions -> look for new
entries -> save state -> append closed trades to CSV. Designed to be
invoked repeatedly by a GitHub Actions cron schedule (e.g. every 10
minutes), with state.json and trades_log.csv committed back to the
repo between runs so progress survives across runs.

Consolidates decisions from v4 -> v10.2 of this project:
  - Discovery via DexScreener token-boosts/token-profiles (chainId=solana),
    NOT RugCheck new_tokens (which mostly returns tokens still on the
    pump.fun bonding curve with no DexScreener pair).
  - RugCheck security check: freezeAuthority/mintAuthority explicit,
    risks[] danger/high reject, holder concentration reject (top10 > 50%),
    score read from score_normalised (not the raw score).
  - Whale distribution check: top 20 holders' ACTUAL balances (not rank)
    fetched via Solana RPC; a >=15% real balance drop vs. the balance
    recorded at entry triggers an emergency exit (this avoids the
    v8.3 false-positive bug where a holder merely dropping out of the
    top-N was misread as a 100% sell).
  - Trailing stop (activate at +8%, trail 5% off the peak) instead of a
    flat take-profit.
  - Blacklist/cooldown: liquidity_drained exits blacklist the mint
    permanently (this session); stop_loss exits get a 30-minute cooldown.
  - Paper trading only. No real funds, no private keys, nothing is ever
    submitted on-chain.
"""

import json
import os
import sys
import time
import csv
from datetime import datetime, timezone

import requests

VERSION = "v11-cycle"

# ---------------------------------------------------------------- config --
STATE_FILE = "state.json"
TRADES_CSV = "trades_log.csv"

STARTING_EQUITY = 1000.0
POSITION_SIZE_USD = 100.0
MAX_CONCURRENT_POSITIONS = 3

TAKE_PROFIT_ACTIVATE = 0.08     # start trailing once +8%
TRAILING_DROP = 0.05            # exit if price falls 5% off the peak
STOP_LOSS = 0.10                # hard stop -10%
MAX_HOLD_SECONDS = 15 * 60      # force-close after 15 minutes

MIN_LIQUIDITY_USD = 2000
MIN_VOLUME_H1_USD = 500
MIN_LIQ_MCAP_RATIO = 0.10
MIN_BUY_SELL_RATIO = 1.3
MAX_HOLDER_CONCENTRATION = 0.50   # reject if top10 holders > 50% of supply

WHALE_TOP_N = 20
WHALE_DUMP_THRESHOLD = 0.15       # >=15% real balance drop = distribution
STOP_LOSS_COOLDOWN_SECONDS = 30 * 60

RPC_ENDPOINTS = [
    os.environ.get("HELIUS_RPC_URL", ""),          # optional, tried first if set
    "https://rpc.ankr.com/solana",
    "https://api.mainnet-beta.solana.com",
]
RPC_ENDPOINTS = [u for u in RPC_ENDPOINTS if u]

HEADERS = {"User-Agent": "meme-scanner-cycle/11"}
TIMEOUT = 10


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ------------------------------------------------------------ state I/O --
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {
        "equity": STARTING_EQUITY,
        "positions": {},
        "blacklist": [],
        "cooldowns": {},
        "trade_count": 0,
        "scan_count": 0,
        "version": VERSION,
    }


def save_state(state):
    state["version"] = VERSION
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def append_trade(row):
    is_new = not os.path.exists(TRADES_CSV)
    with open(TRADES_CSV, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "timestamp", "mint", "symbol", "entry_price", "exit_price",
            "pnl_pct", "pnl_usd", "equity_after", "reason",
        ])
        if is_new:
            w.writeheader()
        w.writerow(row)


# ------------------------------------------------------------- fetchers --
def safe_get_json(url, params=None):
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=TIMEOUT)
        if r.status_code != 200:
            log(f"  GET {url} -> HTTP {r.status_code}")
            return None
        return r.json()
    except Exception as e:
        log(f"  GET {url} failed: {e}")
        return None


def fetch_dex_pair(mint):
    data = safe_get_json(f"https://api.dexscreener.com/latest/dex/tokens/{mint}")
    if not data or not data.get("pairs"):
        return None
    solana_pairs = [p for p in data["pairs"] if p.get("chainId") == "solana"]
    if not solana_pairs:
        return None
    # pick the highest-liquidity pair
    solana_pairs.sort(key=lambda p: (p.get("liquidity") or {}).get("usd", 0), reverse=True)
    return solana_pairs[0]


def discover_candidates():
    """Discovery via DexScreener boosted/profile feeds, filtered to Solana."""
    mints = set()
    for url in [
        "https://api.dexscreener.com/token-boosts/latest/v1",
        "https://api.dexscreener.com/token-profiles/latest/v1",
    ]:
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
    """Top-N holder balances via Solana RPC. Returns {address: ui_amount} or None."""
    for endpoint in RPC_ENDPOINTS:
        try:
            payload = {
                "jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts",
                "params": [mint],
            }
            r = requests.post(endpoint, json=payload, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code != 200:
                log(f"  RPC {endpoint} -> HTTP {r.status_code}")
                continue
            result = r.json().get("result", {}).get("value", [])
            if not result:
                continue
            balances = {}
            for acc in result[:WHALE_TOP_N]:
                balances[acc["address"]] = float(acc.get("uiAmount") or 0)
            return balances
        except Exception as e:
            log(f"  RPC {endpoint} failed: {e}")
            continue
    return None


# --------------------------------------------------------------- filters --
def passes_security_filter(rc_report):
    if not rc_report:
        return False, "no_rugcheck_data"

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
    # RugCheck reports pct as 0-100
    if total_pct > MAX_HOLDER_CONCENTRATION * 100:
        return False, f"holder_concentration:{total_pct:.1f}%"

    return True, "ok"


def passes_market_filter(pair):
    liq = (pair.get("liquidity") or {}).get("usd") or 0
    vol_h1 = (pair.get("volume") or {}).get("h1") or 0
    mcap = pair.get("marketCap") or pair.get("fdv") or 0
    txns_m5 = (pair.get("txns") or {}).get("m5") or {}
    buys = txns_m5.get("buys", 0)
    sells = txns_m5.get("sells", 0)

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


# ------------------------------------------------------------- position ---
def open_position(state, mint, pair, whale_balances):
    price = float(pair.get("priceUsd") or 0)
    if price <= 0:
        return False
    symbol = (pair.get("baseToken") or {}).get("symbol", "?")
    state["positions"][mint] = {
        "symbol": symbol,
        "entry_price": price,
        "entry_time": time.time(),
        "amount_usd": POSITION_SIZE_USD,
        "peak_price": price,
        "trailing_active": False,
        "whale_baseline": whale_balances or {},
    }
    log(f"  OPENED {symbol} ({mint[:6]}...) @ ${price:.8f}")
    return True


def close_position(state, mint, exit_price, reason):
    pos = state["positions"].pop(mint)
    entry_price = pos["entry_price"]
    pnl_pct = (exit_price - entry_price) / entry_price if entry_price else 0
    pnl_usd = pos["amount_usd"] * pnl_pct
    state["equity"] += pnl_usd
    state["trade_count"] += 1

    append_trade({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mint": mint,
        "symbol": pos["symbol"],
        "entry_price": entry_price,
        "exit_price": exit_price,
        "pnl_pct": round(pnl_pct * 100, 2),
        "pnl_usd": round(pnl_usd, 2),
        "equity_after": round(state["equity"], 2),
        "reason": reason,
    })

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
            # can't get a price. Force-close after 2x max_hold using last known price.
            age = time.time() - pos["entry_time"]
            if age > 2 * MAX_HOLD_SECONDS:
                close_position(state, mint, pos["peak_price"], "no_price_data_forced_close")
                state["blacklist"].append(mint)
            else:
                log(f"  {pos['symbol']}: no price data ({age/60:.1f}min since entry)")
            continue

        price = float(pair.get("priceUsd") or 0)
        if price <= 0:
            continue

        liq = (pair.get("liquidity") or {}).get("usd") or 0
        entry_liq = pos.get("entry_liquidity", liq)
        pos.setdefault("entry_liquidity", liq)

        # emergency exit: liquidity rug
        if entry_liq and liq < entry_liq * 0.80:
            close_position(state, mint, price, "liquidity_drained")
            continue

        # whale distribution check (real balances, not rank)
        baseline = pos.get("whale_baseline") or {}
        if baseline:
            current = fetch_whale_balances(mint)
            if current:
                dumped = False
                for addr, base_amt in baseline.items():
                    now_amt = current.get(addr, 0)
                    if base_amt > 0 and (base_amt - now_amt) / base_amt >= WHALE_DUMP_THRESHOLD:
                        dumped = True
                        break
                if dumped:
                    close_position(state, mint, price, "whale_dump")
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

        log(f"  {pos['symbol']}: ${price:.8f} ({pnl_pct*100:+.1f}%) "
            f"{'[trailing]' if pos['trailing_active'] else ''}")


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
        if mint in state["positions"]:
            continue
        if mint in state["blacklist"]:
            continue
        if mint in state["cooldowns"]:
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
            log(f"  rejected {mint[:6]}...: {why}")
            continue

        whale_balances = fetch_whale_balances(mint)
        if open_position(state, mint, pair, whale_balances):
            slots -= 1


# ------------------------------------------------------------------ main --
def run_cycle():
    log(f"=== {VERSION} — cycle start ===")
    state = load_state()
    state["scan_count"] = state.get("scan_count", 0) + 1

    log(f"equity=${state['equity']:.2f} open_positions={len(state['positions'])} "
        f"trades_so_far={state['trade_count']} scan#{state['scan_count']}")

    manage_open_positions(state)
    look_for_entries(state)

    save_state(state)
    log(f"=== cycle done — equity=${state['equity']:.2f} ===")


if __name__ == "__main__":
    run_cycle()
