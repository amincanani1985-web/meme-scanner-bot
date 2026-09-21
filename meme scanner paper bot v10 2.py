#!/usr/bin/env python3
"""
meme_scanner_paper_bot_v10_2.py

Solana meme-coin PAPER trading bot (no real money — simulated account only).

Pipeline:
  1. Discover candidate tokens from DexScreener (boosted / profiled tokens, Solana only)
  2. Pull market data (price, liquidity, volume, buy/sell ratio) from DexScreener
  3. Security check via RugCheck (normalised risk score, risks[] array,
     freeze/mint authority, holder concentration)
  4. Whale wallet check via Solana RPC (top holder balances; distribution = exit signal)
  5. Enter/exit paper positions with a trailing stop, hard stop-loss, and max-hold timeout
  6. Blacklist / cooldown tokens that burned us before
  7. Checkpoint progress to disk (survives restarts) + write every closed trade to CSV
  8. Tiny HTTP health endpoint so this can run as a Render Web Service (free tier
     needs a bound port + something an uptime pinger can hit)

Run modes:
  python meme_scanner_paper_bot_v10_2.py             -> live paper-trading loop
  python meme_scanner_paper_bot_v10_2.py --selftest   -> offline logic test with fake
                                                          data, no network calls at all
"""

import os
import sys
import json
import time
import csv
import random
import threading
import http.server
import socketserver
from datetime import datetime, timedelta

VERSION = "v10.2"
print(f"=== meme_scanner_paper_bot {VERSION} starting ===", flush=True)

try:
    import requests
except ImportError:
    print("Missing dependency: pip install requests")
    sys.exit(1)

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "").strip()  # optional, big reliability boost

STARTING_EQUITY = 1000.0
PAPER_POSITION_SIZE_USD = 50.0

POLL_SECONDS = 10
MAX_TRADES = 100
MAX_RUNTIME_MINUTES = 240        # safety cap regardless of trade count
MAX_HOLD_MINUTES = 15

TAKE_PROFIT_ACTIVATE_PCT = 0.08  # trailing stop arms once we're up this much
TRAILING_STOP_PCT = 0.05         # then exit on a pullback of this much from the peak
STOP_LOSS_PCT = 0.10

MIN_LIQUIDITY_USD = 2000
MIN_VOLUME_H1_USD = 500
MIN_LIQUIDITY_TO_MCAP_RATIO = 0.10
MIN_BUY_SELL_RATIO_M5 = 1.3
MAX_HOLDER_CONCENTRATION = 0.50   # reject if top10 holders own more than this fraction

WHALE_TOP_N = 20                 # max supported by getTokenLargestAccounts
WHALE_DROP_PCT = 0.15            # a baseline holder's balance dropping this much = distribution

LIQUIDITY_DRAIN_EXIT_DROP = 0.20  # emergency exit if liquidity falls this much since entry

STOP_LOSS_COOLDOWN_MINUTES = 30  # cooldown (not permanent ban) after a stop-loss exit

CHECKPOINT_FILE = "checkpoint.json"
TRADES_CSV_FILE = "trades_log.csv"

DEXSCREENER_BOOSTS_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
DEXSCREENER_PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_PAIRS_URL = "https://api.dexscreener.com/latest/dex/tokens/{address}"

RUGCHECK_REPORT_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"

PUBLIC_RPCS = [
    "https://api.mainnet-beta.solana.com",
    "https://rpc.ankr.com/solana",
    "https://solana-api.projectserum.com",
]

HEALTH_PORT = int(os.environ.get("PORT", "10000"))  # Render injects PORT


# ----------------------------------------------------------------------------
# TINY HEALTH SERVER (so Render's free web-service tier has something to ping)
# ----------------------------------------------------------------------------

class _HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"meme_scanner_paper_bot alive\n")

    def log_message(self, format, *args):
        pass  # keep the console clean, real logs come from the bot itself


def start_health_server():
    try:
        with socketserver.TCPServer(("0.0.0.0", HEALTH_PORT), _HealthHandler) as httpd:
            httpd.serve_forever()
    except Exception as e:
        print(f"[health server] failed to start on port {HEALTH_PORT}: {e}", flush=True)


# ----------------------------------------------------------------------------
# RPC HELPERS (Helius first if configured, then public fallbacks, 429-aware)
# ----------------------------------------------------------------------------

def get_rpc_endpoints():
    endpoints = []
    if HELIUS_API_KEY:
        endpoints.append(f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}")
    endpoints.extend(PUBLIC_RPCS)
    return endpoints


def rpc_call(method, params, timeout=10):
    """Try each RPC endpoint in order until one answers without a 429/error."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    last_err = None
    for url in get_rpc_endpoints():
        try:
            r = requests.post(url, json=payload, timeout=timeout)
            if r.status_code == 429:
                last_err = f"429 rate-limited on {url}"
                continue
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                last_err = f"RPC error from {url}: {data['error']}"
                continue
            return data.get("result")
        except Exception as e:
            last_err = f"{url} -> {e}"
            continue
    print(f"[rpc] all endpoints failed: {last_err}", flush=True)
    return None


def test_rpc_connectivity():
    result = rpc_call("getHealth", [])
    ok = result == "ok" or result is not None
    print(f"[rpc] connectivity test: {'OK' if ok else 'FAILED'} ({result})", flush=True)
    return ok


def get_token_largest_accounts(mint):
    result = rpc_call("getTokenLargestAccounts", [mint])
    if not result or "value" not in result:
        return []
    return result["value"][:WHALE_TOP_N]


def get_token_account_balance(address):
    result = rpc_call("getTokenAccountBalance", [address])
    if not result or "value" not in result:
        return None
    try:
        return float(result["value"]["uiAmountString"])
    except (KeyError, TypeError, ValueError):
        return None


# ----------------------------------------------------------------------------
# WHALE TRACKER
# v8.4 fix: never rely on "did this address drop out of the top-N ranking" —
# that produces false positives when a whale is simply overtaken by another
# buyer, not actually selling. Instead we pin down the baseline addresses at
# entry and re-query their *actual* balance directly.
# ----------------------------------------------------------------------------

class WhaleTracker:
    def __init__(self):
        # mint -> {address: baseline_balance}
        self.baselines = {}

    def establish_baseline(self, mint, rpc_fn=get_token_largest_accounts, bal_fn=get_token_account_balance):
        accounts = rpc_fn(mint)
        baseline = {}
        for acc in accounts:
            addr = acc.get("address")
            amount = None
            try:
                amount = float(acc["uiAmountString"])
            except (KeyError, TypeError, ValueError):
                amount = bal_fn(addr) if addr else None
            if addr and amount is not None:
                baseline[addr] = amount
        if not baseline:
            print(f"[whale] {mint}: no_data establishing baseline", flush=True)
        self.baselines[mint] = baseline
        return baseline

    def check_distribution(self, mint, bal_fn=get_token_account_balance):
        """Returns (is_dumping: bool, detail: str|None)."""
        baseline = self.baselines.get(mint)
        if not baseline:
            return False, None
        for addr, base_amount in baseline.items():
            if base_amount <= 0:
                continue
            current = bal_fn(addr)
            if current is None:
                continue  # no_data, don't false-trigger
            drop = (base_amount - current) / base_amount
            if drop >= WHALE_DROP_PCT:
                return True, f"{addr} balance dropped {drop*100:.1f}% ({base_amount}->{current})"
        return False, None

    def forget(self, mint):
        self.baselines.pop(mint, None)


# ----------------------------------------------------------------------------
# DATA FETCHERS
# ----------------------------------------------------------------------------

def fetch_candidate_mints():
    """Boosted + profiled tokens from DexScreener, Solana only."""
    mints = set()
    for url in (DEXSCREENER_BOOSTS_URL, DEXSCREENER_PROFILES_URL):
        try:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            for item in r.json():
                if item.get("chainId") == "solana" and item.get("tokenAddress"):
                    mints.add(item["tokenAddress"])
        except Exception as e:
            print(f"[dexscreener] fetch failed for {url}: {e}", flush=True)
    return list(mints)


def fetch_pair_data(mint):
    """Best (highest-liquidity) Solana pair for a mint, normalized to the fields we need."""
    try:
        r = requests.get(DEXSCREENER_PAIRS_URL.format(address=mint), timeout=10)
        r.raise_for_status()
        pairs = r.json().get("pairs") or []
        sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
        if not sol_pairs:
            return None
        best = max(sol_pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0) or 0)
        txns_m5 = (best.get("txns") or {}).get("m5") or {}
        return {
            "mint": mint,
            "symbol": (best.get("baseToken") or {}).get("symbol", "?"),
            "price_usd": float(best.get("priceUsd") or 0),
            "liquidity_usd": (best.get("liquidity") or {}).get("usd", 0) or 0,
            "volume_h1_usd": (best.get("volume") or {}).get("h1", 0) or 0,
            "market_cap_usd": best.get("marketCap") or best.get("fdv") or 0,
            "buys_m5": txns_m5.get("buys", 0) or 0,
            "sells_m5": txns_m5.get("sells", 0) or 0,
        }
    except Exception as e:
        print(f"[dexscreener] pair data failed for {mint}: {e}", flush=True)
        return None


def fetch_rugcheck_report(mint):
    try:
        r = requests.get(RUGCHECK_REPORT_URL.format(mint=mint), timeout=10)
        r.raise_for_status()
        data = r.json()
        top_holders = data.get("topHolders") or []
        top10_pct = sum(h.get("pct", 0) for h in top_holders[:10]) / 100.0
        return {
            "score_normalised": data.get("score_normalised", data.get("score", 0)),
            "risks": [r.get("level", "").lower() for r in (data.get("risks") or [])],
            "freeze_authority": data.get("freezeAuthority"),
            "mint_authority": data.get("mintAuthority"),
            "top10_holder_pct": top10_pct,
        }
    except Exception as e:
        print(f"[rugcheck] report failed for {mint}: {e}", flush=True)
        return None


# ----------------------------------------------------------------------------
# FILTER LOGIC
# ----------------------------------------------------------------------------

def passes_filters(pair, rug):
    if pair is None or rug is None:
        return False, "missing_data"
    if pair["liquidity_usd"] < MIN_LIQUIDITY_USD:
        return False, f"liquidity {pair['liquidity_usd']:.0f} < {MIN_LIQUIDITY_USD}"
    if pair["volume_h1_usd"] < MIN_VOLUME_H1_USD:
        return False, f"volume_h1 {pair['volume_h1_usd']:.0f} < {MIN_VOLUME_H1_USD}"
    if pair["market_cap_usd"] > 0:
        ratio = pair["liquidity_usd"] / pair["market_cap_usd"]
        if ratio < MIN_LIQUIDITY_TO_MCAP_RATIO:
            return False, f"liq/mcap {ratio:.2f} < {MIN_LIQUIDITY_TO_MCAP_RATIO}"
    sells = max(pair["sells_m5"], 1)
    if pair["buys_m5"] / sells < MIN_BUY_SELL_RATIO_M5:
        return False, f"buy/sell {pair['buys_m5']}/{pair['sells_m5']} < {MIN_BUY_SELL_RATIO_M5}"
    if rug["freeze_authority"]:
        return False, "freeze_authority set"
    if rug["mint_authority"]:
        return False, "mint_authority set"
    if any(level in ("danger", "high") for level in rug["risks"]):
        return False, f"rugcheck risk flags: {rug['risks']}"
    if rug["top10_holder_pct"] > MAX_HOLDER_CONCENTRATION:
        return False, f"holder concentration {rug['top10_holder_pct']*100:.0f}% > {MAX_HOLDER_CONCENTRATION*100:.0f}%"
    return True, "ok"


# ----------------------------------------------------------------------------
# PAPER BOT
# ----------------------------------------------------------------------------

class PaperBot:
    def __init__(self, fetch_candidates=fetch_candidate_mints, fetch_pair=fetch_pair_data,
                 fetch_rug=fetch_rugcheck_report, whale=None):
        self.equity = STARTING_EQUITY
        self.positions = {}      # mint -> position dict
        self.blacklist = {}      # mint -> None (permanent) or datetime (cooldown until)
        self.closed_trades = 0
        self.start_time = datetime.utcnow()
        self.whale = whale or WhaleTracker()
        self.fetch_candidates = fetch_candidates
        self.fetch_pair = fetch_pair
        self.fetch_rug = fetch_rug
        self._ensure_csv_header()
        self.load_checkpoint()

    # ---- persistence ----

    def _ensure_csv_header(self):
        if not os.path.exists(TRADES_CSV_FILE):
            with open(TRADES_CSV_FILE, "w", newline="") as f:
                csv.writer(f).writerow(
                    ["timestamp", "mint", "symbol", "entry_price", "exit_price",
                     "pnl_pct", "reason", "equity_after"]
                )

    def save_checkpoint(self):
        data = {
            "equity": self.equity,
            "positions": self.positions,
            "blacklist": {k: (v.isoformat() if isinstance(v, datetime) else None)
                          for k, v in self.blacklist.items()},
            "closed_trades": self.closed_trades,
        }
        with open(CHECKPOINT_FILE, "w") as f:
            json.dump(data, f, default=str)

    def load_checkpoint(self):
        if not os.path.exists(CHECKPOINT_FILE):
            return
        try:
            with open(CHECKPOINT_FILE) as f:
                data = json.load(f)
            self.equity = data.get("equity", self.equity)
            self.positions = data.get("positions", {})
            self.closed_trades = data.get("closed_trades", 0)
            for k, v in (data.get("blacklist") or {}).items():
                self.blacklist[k] = datetime.fromisoformat(v) if v else None
            print(f"[checkpoint] resumed: equity=${self.equity:.2f}, "
                  f"{len(self.positions)} open, {self.closed_trades} closed", flush=True)
        except Exception as e:
            print(f"[checkpoint] failed to load, starting fresh: {e}", flush=True)

    def log_trade(self, mint, symbol, entry_price, exit_price, reason):
        pnl_pct = (exit_price - entry_price) / entry_price if entry_price else 0
        pnl_usd = PAPER_POSITION_SIZE_USD * pnl_pct
        self.equity += pnl_usd
        self.closed_trades += 1
        with open(TRADES_CSV_FILE, "a", newline="") as f:
            csv.writer(f).writerow(
                [datetime.utcnow().isoformat(), mint, symbol, entry_price, exit_price,
                 f"{pnl_pct*100:.2f}", reason, f"{self.equity:.2f}"]
            )
        print(f"[trade closed] {symbol} {reason} pnl={pnl_pct*100:.1f}% "
              f"equity=${self.equity:.2f}", flush=True)

    # ---- blacklist / cooldown ----

    def is_blocked(self, mint):
        if mint not in self.blacklist:
            return False
        until = self.blacklist[mint]
        if until is None:
            return True  # permanent
        if datetime.utcnow() < until:
            return True
        del self.blacklist[mint]  # cooldown expired
        return False

    def block(self, mint, permanent=False, cooldown_minutes=None):
        if permanent:
            self.blacklist[mint] = None
        elif cooldown_minutes:
            self.blacklist[mint] = datetime.utcnow() + timedelta(minutes=cooldown_minutes)

    # ---- core loop pieces ----

    def try_enter(self, mint):
        if mint in self.positions or self.is_blocked(mint):
            return
        pair = self.fetch_pair(mint)
        rug = self.fetch_rug(mint)
        ok, reason = passes_filters(pair, rug)
        if not ok:
            print(f"[scan] reject {mint[:8]}: {reason}", flush=True)
            return
        self.whale.establish_baseline(mint)
        dumping, detail = self.whale.check_distribution(mint)
        if dumping:
            print(f"[scan] reject {mint[:8]}: whale already distributing ({detail})", flush=True)
            return
        self.positions[mint] = {
            "symbol": pair["symbol"],
            "entry_price": pair["price_usd"],
            "entry_liquidity": pair["liquidity_usd"],
            "peak_price": pair["price_usd"],
            "entry_time": datetime.utcnow().isoformat(),
            "trailing_armed": False,
        }
        print(f"[enter] {pair['symbol']} ({mint[:8]}) @ ${pair['price_usd']:.8f}", flush=True)

    def try_exit(self, mint):
        pos = self.positions.get(mint)
        if not pos:
            return
        pair = self.fetch_pair(mint)
        entry_time = datetime.fromisoformat(pos["entry_time"])
        held_minutes = (datetime.utcnow() - entry_time).total_seconds() / 60.0

        if pair is None:
            pos["missing_ticks"] = pos.get("missing_ticks", 0) + 1
            minutes_missing = pos["missing_ticks"] * POLL_SECONDS / 60.0
            print(f"[warn] {pos['symbol']}: no price data for ~{minutes_missing:.1f} min", flush=True)
            if held_minutes > MAX_HOLD_MINUTES * 2:
                self._close(mint, pos["entry_price"], "force_close_no_data")
                self.block(mint, permanent=True)
            return
        pos["missing_ticks"] = 0

        price = pair["price_usd"]
        if price > pos["peak_price"]:
            pos["peak_price"] = price

        pnl = (price - pos["entry_price"]) / pos["entry_price"]

        # whale dump check
        dumping, detail = self.whale.check_distribution(mint)
        if dumping:
            self._close(mint, price, "whale_dump")
            self.block(mint, permanent=True)
            return

        # liquidity drain emergency exit
        if pos["entry_liquidity"] > 0:
            liq_drop = (pos["entry_liquidity"] - pair["liquidity_usd"]) / pos["entry_liquidity"]
            if liq_drop >= LIQUIDITY_DRAIN_EXIT_DROP:
                self._close(mint, price, "liquidity_drained")
                self.block(mint, permanent=True)
                return

        # trailing stop
        if pnl >= TAKE_PROFIT_ACTIVATE_PCT:
            pos["trailing_armed"] = True
        if pos["trailing_armed"]:
            drop_from_peak = (pos["peak_price"] - price) / pos["peak_price"]
            if drop_from_peak >= TRAILING_STOP_PCT:
                self._close(mint, price, "trailing_stop")
                return

        # hard stop loss
        if pnl <= -STOP_LOSS_PCT:
            self._close(mint, price, "stop_loss")
            self.block(mint, cooldown_minutes=STOP_LOSS_COOLDOWN_MINUTES)
            return

        # max hold timeout
        if held_minutes >= MAX_HOLD_MINUTES:
            self._close(mint, price, "max_hold")
            return

    def _close(self, mint, exit_price, reason):
        pos = self.positions.pop(mint, None)
        if not pos:
            return
        self.log_trade(mint, pos["symbol"], pos["entry_price"], exit_price, reason)
        self.whale.forget(mint)

    # ---- main scan cycle ----

    def scan_once(self):
        for mint in list(self.positions.keys()):
            self.try_exit(mint)
        if self.closed_trades < MAX_TRADES:
            for mint in self.fetch_candidates():
                self.try_enter(mint)
        self.save_checkpoint()

    def should_stop(self):
        elapsed_minutes = (datetime.utcnow() - self.start_time).total_seconds() / 60.0
        if self.closed_trades >= MAX_TRADES:
            print(f"[stop] reached {MAX_TRADES} closed trades", flush=True)
            return True
        if elapsed_minutes >= MAX_RUNTIME_MINUTES:
            print(f"[stop] reached max runtime of {MAX_RUNTIME_MINUTES} min", flush=True)
            return True
        return False

    def run(self):
        test_rpc_connectivity()
        scan_count = 0
        while not self.should_stop():
            scan_count += 1
            print(f"--- scan #{scan_count} | equity=${self.equity:.2f} | "
                  f"open={len(self.positions)} | closed={self.closed_trades} ---", flush=True)
            try:
                self.scan_once()
            except Exception as e:
                print(f"[error] scan failed: {e}", flush=True)
            time.sleep(POLL_SECONDS)
        print(f"=== finished: equity=${self.equity:.2f}, "
              f"{self.closed_trades} trades closed ===", flush=True)


# ----------------------------------------------------------------------------
# SELF-TEST (no network calls — synthetic data only)
# ----------------------------------------------------------------------------

def _selftest():
    print("Running offline self-test (no network calls)...")
    passed = 0
    failed = 0

    def check(name, cond):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  OK   {name}")
        else:
            failed += 1
            print(f"  FAIL {name}")

    # 1. filter logic: a good token passes
    good_pair = {"liquidity_usd": 5000, "volume_h1_usd": 2000, "market_cap_usd": 20000,
                 "buys_m5": 10, "sells_m5": 5, "price_usd": 0.001, "symbol": "GOOD"}
    good_rug = {"score_normalised": 5, "risks": [], "freeze_authority": None,
                "mint_authority": None, "top10_holder_pct": 0.3}
    ok, _ = passes_filters(good_pair, good_rug)
    check("good token passes filters", ok)

    # 2. filter logic: low liquidity rejected
    bad_pair = dict(good_pair, liquidity_usd=500)
    ok, reason = passes_filters(bad_pair, good_rug)
    check("low liquidity rejected", not ok and "liquidity" in reason)

    # 3. freeze authority rejected
    bad_rug = dict(good_rug, freeze_authority="Some111Authority")
    ok, reason = passes_filters(good_pair, bad_rug)
    check("freeze authority rejected", not ok and "freeze" in reason)

    # 4. holder concentration rejected
    bad_rug2 = dict(good_rug, top10_holder_pct=0.9)
    ok, reason = passes_filters(good_pair, bad_rug2)
    check("holder concentration rejected", not ok and "concentration" in reason)

    # 5. RugCheck danger risk rejected
    bad_rug3 = dict(good_rug, risks=["danger"])
    ok, reason = passes_filters(good_pair, bad_rug3)
    check("danger risk flag rejected", not ok and "risk" in reason)

    # 6. WhaleTracker: real drop detected, ranking-shuffle NOT a false positive
    wt = WhaleTracker()
    fake_accounts = [{"address": "AAA", "uiAmountString": "1000"},
                      {"address": "BBB", "uiAmountString": "500"}]
    wt.establish_baseline("MINT1", rpc_fn=lambda m: fake_accounts)
    # simulate AAA actually selling 20% (real drop)
    balances = {"AAA": 800.0, "BBB": 500.0}
    dumping, detail = wt.check_distribution("MINT1", bal_fn=lambda addr: balances.get(addr))
    check("real 20% drop flagged as dumping", dumping)

    balances_stable = {"AAA": 1000.0, "BBB": 500.0}
    dumping2, _ = wt.check_distribution("MINT1", bal_fn=lambda addr: balances_stable.get(addr))
    check("stable balances not flagged", not dumping2)

    # missing data should never false-trigger
    dumping3, _ = wt.check_distribution("MINT1", bal_fn=lambda addr: None)
    check("missing balance data does not false-trigger", not dumping3)

    # 7. PaperBot trailing stop logic
    bot = PaperBot(fetch_candidates=lambda: [], fetch_pair=lambda m: None,
                    fetch_rug=lambda m: None, whale=WhaleTracker())
    bot.positions["MINTX"] = {
        "symbol": "TESTX", "entry_price": 1.0, "entry_liquidity": 1000,
        "peak_price": 1.0, "entry_time": datetime.utcnow().isoformat(),
        "trailing_armed": False, "missing_ticks": 0,
    }
    # price rises 10% -> trailing should arm, not yet exit
    bot.fetch_pair = lambda m: {"price_usd": 1.10, "liquidity_usd": 1000, "symbol": "TESTX"}
    bot.whale.check_distribution = lambda m, **kw: (False, None)
    bot.try_exit("MINTX")
    check("trailing arms at +10%% profit", bot.positions.get("MINTX", {}).get("trailing_armed") is True)

    # price pulls back 6% from peak (1.10 -> 1.034) -> should exit via trailing stop
    bot.fetch_pair = lambda m: {"price_usd": 1.034, "liquidity_usd": 1000, "symbol": "TESTX"}
    equity_before = bot.equity
    bot.try_exit("MINTX")
    check("trailing stop exits on pullback", "MINTX" not in bot.positions and bot.equity != equity_before)

    # 8. stop loss + cooldown
    bot.positions["MINTY"] = {
        "symbol": "TESTY", "entry_price": 1.0, "entry_liquidity": 1000,
        "peak_price": 1.0, "entry_time": datetime.utcnow().isoformat(),
        "trailing_armed": False, "missing_ticks": 0,
    }
    bot.fetch_pair = lambda m: {"price_usd": 0.89, "liquidity_usd": 1000, "symbol": "TESTY"}
    bot.try_exit("MINTY")
    check("stop loss exits at -10%%+", "MINTY" not in bot.positions)
    check("stop loss applies cooldown, not permanent ban", bot.blacklist.get("MINTY") is not None)
    check("cooldown blocks re-entry immediately", bot.is_blocked("MINTY"))

    # 9. liquidity drain -> permanent blacklist
    bot.positions["MINTZ"] = {
        "symbol": "TESTZ", "entry_price": 1.0, "entry_liquidity": 1000,
        "peak_price": 1.0, "entry_time": datetime.utcnow().isoformat(),
        "trailing_armed": False, "missing_ticks": 0,
    }
    bot.fetch_pair = lambda m: {"price_usd": 1.0, "liquidity_usd": 700, "symbol": "TESTZ"}
    bot.try_exit("MINTZ")
    check("liquidity drain force-exits", "MINTZ" not in bot.positions)
    check("liquidity drain -> permanent block", bot.blacklist.get("MINTZ", "missing") is None)

    # 10. force-close after missing price data for 2x max_hold
    bot.positions["MINTW"] = {
        "symbol": "TESTW", "entry_price": 1.0, "entry_liquidity": 1000,
        "peak_price": 1.0,
        "entry_time": (datetime.utcnow() - timedelta(minutes=MAX_HOLD_MINUTES * 2 + 1)).isoformat(),
        "trailing_armed": False, "missing_ticks": 0,
    }
    bot.fetch_pair = lambda m: None
    bot.try_exit("MINTW")
    check("force-close after prolonged missing price data", "MINTW" not in bot.positions)
    check("force-closed mint gets permanently blocked", bot.blacklist.get("MINTW", "missing") is None)

    # 11. checkpoint round-trip
    tmp_ckpt = "selftest_checkpoint.json"
    global CHECKPOINT_FILE
    old_ckpt = CHECKPOINT_FILE
    CHECKPOINT_FILE = tmp_ckpt
    bot.save_checkpoint()
    bot2 = PaperBot(fetch_candidates=lambda: [], fetch_pair=lambda m: None,
                     fetch_rug=lambda m: None, whale=WhaleTracker())
    check("checkpoint restores equity", abs(bot2.equity - bot.equity) < 0.01)
    os.remove(tmp_ckpt)
    if os.path.exists(TRADES_CSV_FILE):
        os.remove(TRADES_CSV_FILE)
    CHECKPOINT_FILE = old_ckpt

    print(f"\nSelf-test done: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


# ----------------------------------------------------------------------------
# ENTRY POINT
# ----------------------------------------------------------------------------

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        # health server for Render's free web-service tier (uptime pinger target)
        threading.Thread(target=start_health_server, daemon=True).start()
        bot = PaperBot()
        bot.run()
