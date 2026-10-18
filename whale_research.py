#!/usr/bin/env python3
"""Research-only Solana holder behavior monitor. Never places or recommends trades."""
import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import meme_scanner_cycle as scanner
from whale_monitor import fetch_whale_balances, helius_configured

MAX_WATCH_TOKENS = 8
MAX_STORED_MINTS = 100
MAX_STORED_WALLETS = 500
MAX_RECENT_EVENTS = 20
DROP_THRESHOLD = 0.20
EVENTS_PATH = "whale_events.csv"


def _market_sell_pressure(pair_data):
    if not isinstance(pair_data, dict):
        return False
    try:
        m5 = (pair_data.get("txns") or {}).get("m5") or {}
        buys = float(m5.get("buys"))
        sells = float(m5.get("sells"))
        price_change = float((pair_data.get("priceChange") or {}).get("m5"))
    except (TypeError, ValueError, AttributeError, OverflowError):
        return False
    if not all(math.isfinite(value) and value >= 0 for value in (buys, sells)):
        return False
    return math.isfinite(price_change) and (sells > buys or price_change <= 0)


def record_balance_changes(state, mint, previous, current, pair_data, timestamp=None):
    """Persist repeated owner-balance drops with independent market context.

    A balance decrease is not necessarily a sale. The confirmation label means
    only that the same observation also had sell-heavy transactions or nonpositive
    short-term price movement; transaction-level swap verification is separate.
    """
    if not isinstance(state, dict):
        raise TypeError("state must be a dictionary")
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return []
    if not isinstance(state.get("wallets"), dict):
        state["wallets"] = {}
    if not isinstance(state.get("events"), list):
        state["events"] = []
    observed_at = timestamp or datetime.now(timezone.utc).isoformat()
    market_confirmed = _market_sell_pressure(pair_data)
    events = []
    for owner in sorted(previous.keys() & current.keys()):
        try:
            old = float(previous[owner])
            new = float(current[owner])
        except (TypeError, ValueError, OverflowError):
            continue
        if (not math.isfinite(old) or not math.isfinite(new)
                or old <= 0 or new < 0):
            continue
        drop_fraction = (old - new) / old
        if drop_fraction < DROP_THRESHOLD:
            continue
        event = {
            "timestamp": observed_at,
            "mint": str(mint),
            "owner": str(owner),
            "drop_pct": round(drop_fraction * 100.0, 4),
            "market_confirmed": bool(market_confirmed),
        }
        events.append(event)
        wallet = state["wallets"].setdefault(str(owner), {
            "observed_balance_drops": 0,
            "confirmed_sell_pressure_events": 0,
            "tokens_seen": [],
            "recent_events": [],
        })
        wallet["observed_balance_drops"] = int(wallet.get("observed_balance_drops", 0)) + 1
        if market_confirmed:
            wallet["confirmed_sell_pressure_events"] = int(wallet.get("confirmed_sell_pressure_events", 0)) + 1
        tokens = wallet.setdefault("tokens_seen", [])
        if mint not in tokens:
            tokens.append(str(mint))
            del tokens[:-50]
        recent = wallet.setdefault("recent_events", [])
        recent.append(event)
        del recent[:-MAX_RECENT_EVENTS]
        wallet["last_event_at"] = observed_at
        state["events"].append(event)
    del state["events"][:-200]
    if len(state["wallets"]) > MAX_STORED_WALLETS:
        ranked = sorted(state["wallets"].items(),
                        key=lambda item: (item[1].get("confirmed_sell_pressure_events", 0),
                                          item[1].get("observed_balance_drops", 0),
                                          item[1].get("last_event_at", "")))
        for owner, _ in ranked[:len(state["wallets"]) - MAX_STORED_WALLETS]:
            state["wallets"].pop(owner, None)
    return events


def _load_state(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            payload.setdefault("snapshots", {})
            payload.setdefault("wallets", {})
            payload.setdefault("events", [])
            return payload
    except (OSError, ValueError, TypeError):
        pass
    return {"snapshots": {}, "wallets": {}, "events": []}


def _append_events(events, path=EVENTS_PATH):
    if not events:
        return
    file_path = Path(path)
    exists = file_path.exists() and file_path.stat().st_size > 0
    with file_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "timestamp", "mint", "owner", "drop_pct", "market_confirmed"
        ])
        if not exists:
            writer.writeheader()
        writer.writerows(events)


def run(state_path="whale_watch_state.json"):
    """Observe a bounded set of liquid candidates; never changes trading state."""
    if not helius_configured():
        return {"status": "helius_not_configured", "scanned": 0,
                "snapshots_updated": 0, "events": 0, "confirmed_events": 0,
                "top_wallets": []}
    state = _load_state(state_path)
    candidates = []
    for mint in scanner.fetch_candidate_mints():
        pair = scanner.fetch_dex_pair(mint)
        if not isinstance(pair, dict):
            continue
        try:
            liquidity = float((pair.get("liquidity") or {}).get("usd") or 0)
            price = float(pair.get("priceUsd") or 0)
        except (TypeError, ValueError, AttributeError, OverflowError):
            continue
        if not math.isfinite(liquidity) or liquidity < 20000 or not math.isfinite(price) or price <= 0:
            continue
        score, _ = scanner.score_shadow_opportunity(pair)
        candidates.append((float(score), mint, pair))
    candidates.sort(key=lambda item: item[0], reverse=True)

    all_events = []
    updated = 0
    watched = candidates[:MAX_WATCH_TOKENS]
    for _, mint, pair in watched:
        balances = fetch_whale_balances(mint)
        if not balances:
            continue
        previous_record = state["snapshots"].get(mint, {})
        previous = previous_record.get("balances", {}) if isinstance(previous_record, dict) else {}
        if previous:
            all_events.extend(record_balance_changes(
                state, mint, previous, balances, pair
            ))
        state["snapshots"][mint] = {
            "balances": balances,
            "symbol": (pair.get("baseToken") or {}).get("symbol", "?"),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        updated += 1
    if len(state["snapshots"]) > MAX_STORED_MINTS:
        ordered = sorted(state["snapshots"].items(),
                         key=lambda item: item[1].get("updated_at", ""))
        for mint, _ in ordered[:len(state["snapshots"]) - MAX_STORED_MINTS]:
            state["snapshots"].pop(mint, None)
    Path(state_path).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    _append_events(all_events)
    top_wallets = sorted(
        ({"owner": owner, "observed_drops": int(data.get("observed_balance_drops", 0)),
          "confirmed_sell_pressure_events": int(data.get("confirmed_sell_pressure_events", 0)),
          "tokens_seen": len(data.get("tokens_seen", []))}
         for owner, data in state["wallets"].items()),
        key=lambda item: (item["confirmed_sell_pressure_events"], item["observed_drops"]),
        reverse=True,
    )[:5]
    return {
        "status": "ok",
        "scanned": len(watched),
        "snapshots_updated": updated,
        "events": len(all_events),
        "confirmed_events": sum(1 for event in all_events if event["market_confirmed"]),
        "tracked_wallets": len(state["wallets"]),
        "top_wallets": top_wallets,
        "research_only": True,
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
