"""Solana largest-holder snapshots and conservative whale-dump heuristics.

A holder balance decline is not proof of a sale (tokens can be transferred). The
heuristic only flags a dump when a tracked owner's balance falls materially AND
the market shows sell pressure or non-positive short-term price momentum.
"""
import math
import os

import requests

WHALE_DUMP_THRESHOLD = 0.20
HTTP_TIMEOUT_SECONDS = 8


def helius_rpc_url():
    """Return the configured Helius RPC endpoint, or an empty string."""
    url = (os.environ.get("HELIUS_RPC_URL") or "").strip()
    if url:
        return url
    key = (os.environ.get("HELIUS_API_KEY") or "").strip()
    if key:
        return f"https://mainnet.helius-rpc.com/?api-key={key}"
    return ""


def helius_configured():
    return bool(helius_rpc_url())


def detect_whale_balance_drops(previous, current, threshold=WHALE_DUMP_THRESHOLD):
    """Return owners whose observed balance fell by threshold or more.

    Only owners present in both snapshots are compared. Missing owners are not
    treated as zero because a holder may simply have fallen out of the top-20 list.
    """
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return []
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        return []
    if not math.isfinite(threshold) or threshold <= 0 or threshold >= 1:
        return []

    dropped = []
    for owner in previous.keys() & current.keys():
        try:
            old = float(previous[owner])
            new = float(current[owner])
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(old) or not math.isfinite(new) or old <= 0 or new < 0:
            continue
        if (old - new) / old >= threshold:
            dropped.append(owner)
    return sorted(dropped)


def confirmed_whale_dump(previous, current, pair_data, threshold=WHALE_DUMP_THRESHOLD):
    """Require a large holder drop plus independent market sell-pressure evidence."""
    if not detect_whale_balance_drops(previous, current, threshold):
        return False
    if not isinstance(pair_data, dict):
        return False
    try:
        txns = pair_data.get("txns") or {}
        m5 = txns.get("m5") or {}
        buys = float(m5.get("buys"))
        sells = float(m5.get("sells"))
        price_change = float((pair_data.get("priceChange") or {}).get("m5"))
    except (TypeError, ValueError, AttributeError, OverflowError):
        return False
    if not all(math.isfinite(v) and v >= 0 for v in (buys, sells)):
        return False
    if not math.isfinite(price_change):
        return False
    return sells > buys or price_change <= 0


def fetch_whale_balances(mint, rpc_url=None):
    """Fetch top token accounts from Solana RPC and aggregate by wallet owner.

    Returns a {wallet_owner: ui_token_balance} snapshot, or None when the provider
    is unavailable or the response cannot be trusted. Uses two bounded RPC calls.
    """
    url = rpc_url or helius_rpc_url()
    if not url or not mint:
        return None
    try:
        response = requests.post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts",
                  "params": [mint, {"commitment": "confirmed"}]},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        accounts = ((payload.get("result") or {}).get("value") or [])
        if payload.get("error") or not isinstance(accounts, list) or not accounts:
            return None
        addresses = [a.get("address") for a in accounts if isinstance(a, dict) and a.get("address")]
        if not addresses:
            return None

        response = requests.post(
            url,
            json={"jsonrpc": "2.0", "id": 2, "method": "getMultipleAccounts",
                  "params": [addresses, {"encoding": "jsonParsed", "commitment": "confirmed"}]},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        values = ((payload.get("result") or {}).get("value") or [])
        if payload.get("error") or not isinstance(values, list):
            return None

        balances = {}
        for account in values:
            try:
                info = account["data"]["parsed"]["info"]
                owner = info["owner"]
                raw = info["tokenAmount"].get("uiAmountString")
                amount = float(raw if raw is not None else info["tokenAmount"]["uiAmount"])
            except (TypeError, ValueError, KeyError, AttributeError):
                continue
            if not owner or not math.isfinite(amount) or amount < 0:
                continue
            balances[owner] = balances.get(owner, 0.0) + amount
        return balances or None
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        return None
