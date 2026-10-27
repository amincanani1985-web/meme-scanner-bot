#!/usr/bin/env python3
"""Read-only multi-provider market-data collection for Solana meme-token research.

Providers are optional. Missing credentials or provider failures are reported as
unavailable and never converted into empty/negative trading evidence. This module
does not generate signals, sign transactions, or submit orders.
"""
import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

TIMEOUT = 10
MAX_MINTS = 5
DEXSCREENER_BASE = "https://api.dexscreener.com"
BIRDEYE_BASE = "https://public-api.birdeye.so"
DUNE_BASE = "https://api.dune.com/api/v1"


def _json_request(session, method, url, **kwargs):
    response = session.request(method, url, timeout=TIMEOUT, **kwargs)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, (dict, list)):
        raise ValueError("provider returned unexpected JSON type")
    return payload


def _extract_mints(*paths, limit=MAX_MINTS):
    found = []
    for path in paths:
        if not path:
            continue
        try:
            state = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        candidates = []
        if isinstance(state, dict):
            candidates.extend([state.get("mint"), state.get("token_mint"), state.get("token_address")])
            for key in ("tokens", "positions", "open_positions"):
                value = state.get(key)
                if isinstance(value, dict):
                    candidates.extend(value.keys())
                    for item in value.values():
                        if isinstance(item, dict):
                            candidates.extend([item.get("mint"), item.get("token_mint"), item.get("token_address")])
                elif isinstance(value, list):
                    candidates.extend(value)
            wallets = state.get("wallets")
            if isinstance(wallets, dict):
                for wallet_state in wallets.values():
                    if not isinstance(wallet_state, dict):
                        continue
                    candidates.extend([wallet_state.get("mint"), wallet_state.get("token_mint"), wallet_state.get("token_address")])
                    for field in ("normalized_swaps", "recent_events", "events"):
                        events = wallet_state.get(field)
                        if isinstance(events, list):
                            for event in events:
                                if isinstance(event, dict):
                                    candidates.extend([event.get("mint"), event.get("token_mint"), event.get("token_address")])
        for candidate in candidates:
            if isinstance(candidate, dict):
                candidate = candidate.get("mint") or candidate.get("address") or candidate.get("token_address")
            if isinstance(candidate, str):
                candidate = candidate.strip()
                if 32 <= len(candidate) <= 64 and candidate not in found:
                    found.append(candidate)
                    if len(found) >= limit:
                        return found
    return found


def _provider_result(name, configured, fn):
    if not configured:
        return {"provider": name, "status": "not_configured", "records": [], "error": None}
    try:
        data = fn()
        return {"provider": name, "status": "ok", "records": data, "error": None}
    except Exception as exc:  # provider boundary: one provider must not hide other evidence
        return {"provider": name, "status": "error", "records": [], "error": f"{type(exc).__name__}: {exc}"[:300]}


def fetch_dexscreener(session, mint):
    payload = _json_request(session, "GET", f"{DEXSCREENER_BASE}/latest/dex/tokens/{mint}")
    pairs = payload.get("pairs") if isinstance(payload, dict) else None
    if not isinstance(pairs, list):
        return []
    output = []
    for pair in pairs:
        if not isinstance(pair, dict) or str(pair.get("chainId", "")).lower() != "solana":
            continue
        output.append({
            "mint": mint,
            "pair_address": pair.get("pairAddress"),
            "dex_id": pair.get("dexId"),
            "base_mint": (pair.get("baseToken") or {}).get("address"),
            "quote_mint": (pair.get("quoteToken") or {}).get("address"),
            "price_usd": pair.get("priceUsd"),
            "liquidity_usd": (pair.get("liquidity") or {}).get("usd"),
            "volume_h1_usd": (pair.get("volume") or {}).get("h1"),
            "price_change_m5_pct": (pair.get("priceChange") or {}).get("m5"),
            "txns_m5": pair.get("txns", {}).get("m5") if isinstance(pair.get("txns"), dict) else None,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "source": "dexscreener",
        })
    return output


def fetch_birdeye(session, mint, api_key):
    payload = _json_request(
        session, "GET", f"{BIRDEYE_BASE}/defi/v3/token/txs",
        headers={"X-API-KEY": api_key, "x-chain": "solana"},
        params={"address": mint, "sort_by": "block_time", "sort_type": "desc", "limit": 20},
    )
    body = payload.get("data", {}) if isinstance(payload, dict) else {}
    items = body.get("items", []) if isinstance(body, dict) else []
    if not isinstance(items, list):
        return []
    output = []
    for item in items:
        if not isinstance(item, dict):
            continue
        output.append({
            "mint": mint,
            "signature": item.get("txHash") or item.get("tx_hash") or item.get("signature"),
            "timestamp": item.get("blockUnixTime") or item.get("block_time"),
            "side": item.get("side") or item.get("txType"),
            "owner": item.get("owner") or item.get("ownerAddress") or item.get("signer"),
            "source_event": item,
            "source": "birdeye",
        })
    return output


def fetch_dune_saved_query(session, api_key, query_id):
    started = _json_request(
        session, "POST", f"{DUNE_BASE}/query/{query_id}/execute",
        headers={"X-DUNE-API-KEY": api_key, "Content-Type": "application/json"},
        json={"performance": "medium"},
    )
    execution_id = started.get("execution_id") if isinstance(started, dict) else None
    if not execution_id:
        raise ValueError("Dune did not return an execution_id")
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        status = _json_request(
            session, "GET", f"{DUNE_BASE}/execution/{execution_id}/status",
            headers={"X-DUNE-API-KEY": api_key},
        )
        state = status.get("state") if isinstance(status, dict) else None
        if state == "QUERY_STATE_COMPLETED":
            result = _json_request(
                session, "GET", f"{DUNE_BASE}/execution/{execution_id}/results",
                headers={"X-DUNE-API-KEY": api_key},
            )
            rows = ((result.get("result") or {}).get("rows") or []) if isinstance(result, dict) else []
            return rows if isinstance(rows, list) else []
        if state in {"QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED"}:
            raise RuntimeError(f"Dune query ended with {state}")
        time.sleep(2)
    raise TimeoutError("Dune query did not finish within 45 seconds")


def collect(mints=None, swaps_path="whale_copy_state.json", balances_path="whale_watch_state.json",
            session=None, environ=None):
    env = os.environ if environ is None else environ
    session = session or requests.Session()
    mints = list(dict.fromkeys(mints or _extract_mints(swaps_path, balances_path)))[:MAX_MINTS]
    providers = []

    providers.append(_provider_result(
        "dexscreener", bool(mints),
        lambda: [record for mint in mints for record in fetch_dexscreener(session, mint)],
    ))
    birdeye_key = (env.get("BIRDEYE_API_KEY") or "").strip()
    providers.append(_provider_result(
        "birdeye", bool(birdeye_key and mints),
        lambda: [record for mint in mints for record in fetch_birdeye(session, mint, birdeye_key)],
    ))
    dune_key = (env.get("DUNE_API_KEY") or "").strip()
    dune_query = (env.get("DUNE_SOLANA_TRADES_QUERY_ID") or "").strip()
    providers.append(_provider_result(
        "dune", bool(dune_key and dune_query),
        lambda: fetch_dune_saved_query(session, dune_key, dune_query),
    ))
    helius_configured = bool((env.get("HELIUS_API_KEY") or "").strip() or (env.get("HELIUS_RPC_URL") or "").strip())
    providers.append({
        "provider": "helius",
        "status": "configured_existing_pipeline" if helius_configured else "not_configured",
        "records": [],
        "error": None,
        "note": "Wallet transaction parsing remains in whale_copy_paper.py; avoid double-counting its events.",
    })
    nansen_configured = bool((env.get("NANSEN_API_KEY") or "").strip())
    providers.append({
        "provider": "nansen",
        "status": "credential_present_adapter_pending" if nansen_configured else "not_configured",
        "records": [],
        "error": None,
        "note": "No Nansen API calls are made until the exact subscribed endpoint and response contract are configured.",
    })

    all_records = []
    for provider in providers:
        records = provider.get("records")
        if isinstance(records, list):
            all_records.extend(records)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "network": "solana",
        "research_only": True,
        "live_trading_authorized": False,
        "trade_signal": False,
        "mints_requested": mints,
        "provider_status": providers,
        "records": all_records,
        "limitations": [
            "Missing credentials are not treated as evidence of zero activity.",
            "Birdeye and Dune require credentials; Dune additionally requires a saved query ID.",
            "DexScreener is public and can return no pair data for a mint.",
            "Helius is reported as configured here; transaction ingestion is owned by the existing pipeline.",
            "Nansen remains explicitly uncalled until its exact API contract is configured.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--swaps", default="whale_copy_state.json")
    parser.add_argument("--balances", default="whale_watch_state.json")
    parser.add_argument("--mints", default="", help="Comma-separated Solana mint addresses (optional)")
    parser.add_argument("--output", default="/tmp/market-data-connectors-report.json")
    args = parser.parse_args()
    mints = [item.strip() for item in args.mints.split(",") if item.strip()] or None
    report = collect(mints=mints, swaps_path=args.swaps, balances_path=args.balances)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    summary = {key: report[key] for key in ("generated_at", "mints_requested", "provider_status")}
    print(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
