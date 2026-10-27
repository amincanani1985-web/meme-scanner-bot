#!/usr/bin/env python3
"""Explainable, research-only analysis of tracked Solana whale swap behaviour.

This module describes observed histories; it does not infer intent with certainty,
select live trades, sign transactions, or treat balance changes as verified swaps.
It uses only normalized swap records already collected by whale_copy_paper.py.
"""
import argparse
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

VALID_QUOTES = {"SOL", "USDC", "USDT"}
CONSENSUS_WINDOW_SECONDS = 300.0
FOLLOW_WINDOW_SECONDS = 900.0
MIN_PROFILE_SWAPS = 8
MIN_QUALIFIED_CLOSED_TRADES = 8
MIN_QUALIFIED_WIN_RATE = 0.55
MIN_QUALIFIED_PROFIT_FACTOR = 1.25
MIN_QUALIFIED_MEAN_NET_PNL = 0.5


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _load_json(path):
    if not path:
        return {}
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _normalize_swaps(swap_state):
    """Validate and deduplicate only well-formed wallet-side swap observations."""
    wallets = swap_state.get("wallets") if isinstance(swap_state, dict) else {}
    if not isinstance(wallets, dict):
        return [], 0
    events, rejected = [], 0
    seen_signatures = set()
    for owner, record in wallets.items():
        if not isinstance(record, dict):
            continue
        swaps = record.get("normalized_swaps", [])
        if not isinstance(swaps, list):
            continue
        for raw in swaps:
            if not isinstance(raw, dict):
                rejected += 1
                continue
            timestamp = _number(raw.get("timestamp"))
            amount = _number(raw.get("token_amount"))
            quote_amount = _number(raw.get("quote_amount"))
            side = raw.get("side")
            mint = str(raw.get("mint") or "").strip()
            quote = str(raw.get("quote_symbol") or "").upper()
            signature = str(raw.get("signature") or "").strip()
            wallet = str(raw.get("wallet") or owner).strip()
            if (timestamp is None or timestamp <= 0 or amount is None or amount <= 0
                    or quote_amount is None or quote_amount <= 0 or side not in ("buy", "sell")
                    or not mint or quote not in VALID_QUOTES or not wallet):
                rejected += 1
                continue
            # The same signature may contain multiple tracked wallet-side swaps.
            # Deduplicate repeated records per wallet, not globally by transaction.
            signature_key = (signature, wallet)
            if signature and signature_key in seen_signatures:
                continue
            if signature:
                seen_signatures.add(signature_key)
            events.append({
                "wallet": wallet,
                "mint": mint,
                "timestamp": timestamp,
                "side": side,
                "token_amount": amount,
                "quote_amount": quote_amount,
                "quote_symbol": quote,
                "signature": signature,
            })
    events.sort(key=lambda event: (event["timestamp"], event["signature"], event["wallet"]))
    return events, rejected


def _counter_values(mapping):
    return {key: round(value, 8) for key, value in sorted(mapping.items())}


def _hold_durations(events):
    """Estimate holding intervals by FIFO token inventory; no price/PnL inference."""
    lots_by_mint = defaultdict(list)
    durations = []
    matched_units = 0.0
    for event in events:
        mint = event["mint"]
        amount = event["token_amount"]
        if event["side"] == "buy":
            lots_by_mint[mint].append({
                "remaining": amount, "timestamp": event["timestamp"],
            })
            continue
        remaining = amount
        lots = lots_by_mint[mint]
        while remaining > 1e-12 and lots:
            lot = lots[0]
            matched = min(remaining, lot["remaining"])
            durations.append(max(0.0, event["timestamp"] - lot["timestamp"]))
            matched_units += matched
            remaining -= matched
            lot["remaining"] -= matched
            if lot["remaining"] <= 1e-12:
                lots.pop(0)
    return durations, matched_units


def _copy_paper_qualified(record, metrics):
    """Reuse strict realised-history gates; behavioural patterns alone never qualify."""
    if not isinstance(record, dict) or not isinstance(metrics, dict):
        return False
    if not record.get("history_complete", False) or record.get("history_truncated", False):
        return False
    closed = _number(metrics.get("closed_trades"))
    win_rate = _number(metrics.get("win_rate"))
    profit_factor = _number(metrics.get("profit_factor"))
    mean_pnl = _number(metrics.get("mean_net_pnl_pct"))
    return bool(
        closed is not None and closed >= MIN_QUALIFIED_CLOSED_TRADES
        and win_rate is not None and win_rate >= MIN_QUALIFIED_WIN_RATE
        and profit_factor is not None and profit_factor >= MIN_QUALIFIED_PROFIT_FACTOR
        and mean_pnl is not None and mean_pnl >= MIN_QUALIFIED_MEAN_NET_PNL
    )


def _wallet_profile(wallet, record, events, leadership):
    events = sorted(events, key=lambda event: (event["timestamp"], event["signature"]))
    buys = [event for event in events if event["side"] == "buy"]
    sells = [event for event in events if event["side"] == "sell"]
    total = len(events)
    buy_share = len(buys) / total if total else 0.0
    sell_share = len(sells) / total if total else 0.0
    buy_notionals, sell_notionals = defaultdict(float), defaultdict(float)
    for event in buys:
        buy_notionals[event["quote_symbol"]] += event["quote_amount"]
    for event in sells:
        sell_notionals[event["quote_symbol"]] += event["quote_amount"]

    intervals = [
        events[i]["timestamp"] - events[i - 1]["timestamp"]
        for i in range(1, len(events))
        if events[i]["timestamp"] > events[i - 1]["timestamp"]
    ]
    durations, matched_units = _hold_durations(events)
    metrics = record.get("metrics") if isinstance(record.get("metrics"), dict) else {}
    history_complete = bool(record.get("history_complete", False))
    history_truncated = bool(record.get("history_truncated", False))

    if total < 4:
        style = "insufficient_observations"
    elif len(buys) >= 3 and buy_share >= 0.70:
        style = "accumulator"
    elif len(sells) >= 3 and sell_share >= 0.60:
        style = "distributor"
    elif len(buys) >= 2 and len(sells) >= 2:
        style = "round_trip_or_rotating"
    elif buy_share >= 0.60:
        style = "buy_side_bias"
    elif sell_share >= 0.60:
        style = "sell_side_bias"
    else:
        style = "mixed_flow"

    tags = [style]
    gaps_under_3s = sum(gap < 3.0 for gap in intervals)
    if len(intervals) >= 7 and statistics.median(intervals) < 3.0 and gaps_under_3s / len(intervals) >= 0.80:
        tags.append("high_frequency_possible_bot")
    # Repeated adds to the same token can be a ladder-style entry, but are not
    # proof of intent or profitability.
    for mint in {event["mint"] for event in buys}:
        token_buys = [event for event in buys if event["mint"] == mint]
        consecutive_adds = 1
        for i in range(1, len(token_buys)):
            gap = token_buys[i]["timestamp"] - token_buys[i - 1]["timestamp"]
            if 5.0 <= gap <= 300.0:
                consecutive_adds += 1
                if consecutive_adds >= 3:
                    tags.append("ladder_buyer")
                    break
            else:
                consecutive_adds = 1
        if "ladder_buyer" in tags:
            break

    if history_complete and not history_truncated and total >= 30 and _number(metrics.get("closed_trades") or 0) >= 8:
        confidence = "high"
    elif history_complete and not history_truncated and total >= 12:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "wallet": wallet,
        "observed_style": style,
        "behavior_tags": list(dict.fromkeys(tags)),
        "confidence": confidence,
        "history_complete": history_complete,
        "history_truncated": history_truncated,
        "swap_count": total,
        "unique_tokens": len({event["mint"] for event in events}),
        "buy_count": len(buys),
        "sell_count": len(sells),
        "buy_share": round(buy_share, 4),
        "sell_share": round(sell_share, 4),
        "buy_notional_by_quote": _counter_values(buy_notionals),
        "sell_notional_by_quote": _counter_values(sell_notionals),
        "median_intertrade_seconds": round(statistics.median(intervals), 3) if intervals else None,
        "median_observed_hold_seconds": round(statistics.median(durations), 3) if durations else None,
        "matched_sell_inventory_units": round(matched_units, 8),
        "observed_leader_count": leadership.get("leaders", 0),
        "observed_follower_count": leadership.get("followers", 0),
        "realized_performance": {
            "closed_trades": int(_number(metrics.get("closed_trades")) or 0),
            "win_rate": round(_number(metrics.get("win_rate")) or 0.0, 4),
            "profit_factor": round(_number(metrics.get("profit_factor")) or 0.0, 4),
            "mean_net_pnl_pct": round(_number(metrics.get("mean_net_pnl_pct")) or 0.0, 4),
        },
        "copy_paper_qualified": _copy_paper_qualified(record, metrics),
    }


def _token_flow(mint, events):
    events = sorted(events, key=lambda event: (event["timestamp"], event["signature"], event["wallet"]))
    buys = [event for event in events if event["side"] == "buy"]
    sells = [event for event in events if event["side"] == "sell"]
    last_timestamp = max((event["timestamp"] for event in events), default=0.0)
    latest = [event for event in events
              if last_timestamp - CONSENSUS_WINDOW_SECONDS <= event["timestamp"] <= last_timestamp]
    latest_buyers = {event["wallet"] for event in latest if event["side"] == "buy"}
    latest_sellers = {event["wallet"] for event in latest if event["side"] == "sell"}
    buy_notionals, sell_notionals = defaultdict(float), defaultdict(float)
    for event in buys:
        buy_notionals[event["quote_symbol"]] += event["quote_amount"]
    for event in sells:
        sell_notionals[event["quote_symbol"]] += event["quote_amount"]

    # Count unique wallets in each trailing five-minute window. This distinguishes
    # a real cluster of observed owners from repeated swaps by a single address.
    max_buyers = max_sellers = 0
    left = 0
    for right, event in enumerate(events):
        while events[left]["timestamp"] < event["timestamp"] - CONSENSUS_WINDOW_SECONDS:
            left += 1
        window = events[left:right + 1]
        max_buyers = max(max_buyers, len({e["wallet"] for e in window if e["side"] == "buy"}))
        max_sellers = max(max_sellers, len({e["wallet"] for e in window if e["side"] == "sell"}))

    if len(latest_buyers) >= 3 and len(latest_buyers) > len(latest_sellers):
        label, crowding = "multi_wallet_buy_consensus", True
    elif len(latest_sellers) >= 3 and len(latest_sellers) >= len(latest_buyers):
        label, crowding = "multi_wallet_distribution", False
    elif latest_buyers or latest_sellers:
        label, crowding = "mixed_or_early_flow", False
    else:
        label, crowding = "no_recent_observed_swaps", False

    return {
        "mint": mint,
        "observed_swaps": len(events),
        "unique_wallets": len({event["wallet"] for event in events}),
        "unique_buyers": len({event["wallet"] for event in buys}),
        "unique_sellers": len({event["wallet"] for event in sells}),
        "buy_events": len(buys),
        "sell_events": len(sells),
        "buy_notional_by_quote": _counter_values(buy_notionals),
        "sell_notional_by_quote": _counter_values(sell_notionals),
        "latest_observed_timestamp": last_timestamp or None,
        "latest_window_seconds": CONSENSUS_WINDOW_SECONDS,
        "latest_window_unique_buyers": len(latest_buyers),
        "latest_window_unique_sellers": len(latest_sellers),
        "max_five_minute_unique_buyers": max_buyers,
        "max_five_minute_unique_sellers": max_sellers,
        "flow_label": label,
        "crowding_risk": crowding,
        "trade_signal": False,
    }



def _wallet_relationships(by_mint):
    """Find repeated close-in-time token entries within tracked histories.

    These are temporal overlaps, not proof that addresses share an owner or
    coordinated intent. Equal-time buys are not assigned an invented leader.
    """
    pairs = defaultdict(lambda: {"shared_tokens": set(), "lags": []})
    for mint, events in by_mint.items():
        first_buy = {}
        for event in events:
            if event["side"] != "buy":
                continue
            first_buy[event["wallet"]] = min(
                first_buy.get(event["wallet"], event["timestamp"]), event["timestamp"]
            )
        rows = sorted(first_buy.items(), key=lambda item: (item[1], item[0]))
        for index, (leader, leader_time) in enumerate(rows):
            for follower, follower_time in rows[index + 1:]:
                lag = follower_time - leader_time
                if lag > FOLLOW_WINDOW_SECONDS:
                    break
                if lag <= 0:
                    continue
                edge = pairs[(leader, follower)]
                edge["shared_tokens"].add(mint)
                edge["lags"].append(lag)

    relationships = []
    for (leader, follower), info in pairs.items():
        tokens = sorted(info["shared_tokens"])
        lags = info["lags"]
        relationships.append({
            "earlier_observed_wallet": leader,
            "later_observed_wallet": follower,
            "shared_token_count": len(tokens),
            "shared_tokens": tokens[:10],
            "mean_lead_lag_seconds": round(statistics.mean(lags), 3) if lags else None,
            "min_lead_lag_seconds": round(min(lags), 3) if lags else None,
            "max_lead_lag_seconds": round(max(lags), 3) if lags else None,
            "relationship_label": "repeated_timing_overlap" if len(tokens) >= 2 else "single_token_timing_overlap",
            "coordination_proven": False,
        })
    relationships.sort(key=lambda item: (
        item["shared_token_count"], -float(item["mean_lead_lag_seconds"] or 0),
        item["earlier_observed_wallet"], item["later_observed_wallet"]
    ), reverse=True)
    return relationships


def analyze_behavior(swap_state, watch_state=None, now=None):
    """Summarize observed wallet styles and multi-wallet token flows without trade actions."""
    events, rejected = _normalize_swaps(swap_state)
    by_wallet = defaultdict(list)
    by_mint = defaultdict(list)
    records = swap_state.get("wallets", {}) if isinstance(swap_state, dict) else {}
    leadership = defaultdict(lambda: {"leaders": 0, "followers": 0})

    for event in events:
        by_wallet[event["wallet"]].append(event)
        by_mint[event["mint"]].append(event)

    # The first wallet observed in the tracked set is a relative observation, not
    # proof it was the market-wide first buyer. Repeated buys by one wallet count once.
    for mint, token_events in by_mint.items():
        first_by_wallet = {}
        for event in token_events:
            if event["side"] != "buy":
                continue
            first_by_wallet[event["wallet"]] = min(
                first_by_wallet.get(event["wallet"], event["timestamp"]), event["timestamp"]
            )
        first_rows = sorted(first_by_wallet.items(), key=lambda item: (item[1], item[0]))
        if not first_rows:
            continue
        earliest = first_rows[0][1]
        co_leaders = [wallet for wallet, ts in first_rows if ts == earliest]
        if len(co_leaders) == 1:
            leadership[co_leaders[0]]["leaders"] += 1
        for wallet, timestamp in first_rows:
            if timestamp > earliest and timestamp - earliest <= FOLLOW_WINDOW_SECONDS:
                leadership[wallet]["followers"] += 1

    wallet_profiles = []
    all_wallet_ids = set(str(wallet) for wallet in records.keys()) | set(by_wallet.keys())
    for wallet in all_wallet_ids:
        record = records.get(wallet, {})
        profile = _wallet_profile(wallet, record if isinstance(record, dict) else {},
                                  by_wallet.get(wallet, []), leadership[wallet])
        wallet_profiles.append(profile)

    wallet_profiles.sort(key=lambda item: (
        item["copy_paper_qualified"], item["confidence"] == "high",
        item["swap_count"], item["unique_tokens"], item["wallet"]
    ), reverse=True)
    token_flows = [_token_flow(mint, token_events) for mint, token_events in by_mint.items()]
    token_flows.sort(key=lambda item: (
        item["crowding_risk"], item["latest_window_unique_buyers"],
        item["unique_wallets"], item["observed_swaps"]
    ), reverse=True)
    relationships = _wallet_relationships(by_mint)
    relationship_counts = defaultdict(int)
    for relationship in relationships:
        relationship_counts[relationship["earlier_observed_wallet"]] += relationship["shared_token_count"]
        relationship_counts[relationship["later_observed_wallet"]] += relationship["shared_token_count"]
    for profile in wallet_profiles:
        profile["temporal_overlap_score"] = relationship_counts.get(profile["wallet"], 0)
        if profile["observed_follower_count"] >= 2:
            profile["behavior_tags"].append("repeated_observed_follower")
        if (profile["median_observed_hold_seconds"] is not None
                and profile["median_observed_hold_seconds"] <= 120
                and profile["matched_sell_inventory_units"] > 0):
            profile["behavior_tags"].append("short_horizon_trade_pattern")
        if (profile["unique_tokens"] >= 3 and profile["buy_count"] >= 3
                and profile["sell_count"] >= 3):
            profile["behavior_tags"].append("multi_token_rotator")

    all_metrics = [item["realized_performance"] for item in wallet_profiles
                   if item["realized_performance"]["closed_trades"] > 0]
    watch_wallets = watch_state.get("wallets", {}) if isinstance(watch_state, dict) else {}
    stats = {
        "tracked_wallet_records": len(all_wallet_ids),
        "wallets_with_valid_swaps": len(by_wallet),
        "wallets_with_complete_history": sum(
            1 for item in wallet_profiles if item["history_complete"] and not item["history_truncated"]
        ),
        "wallets_with_realized_outcomes": len(all_metrics),
        "wallets_meeting_copy_paper_gates": sum(1 for item in wallet_profiles if item["copy_paper_qualified"]),
        "tokens_with_observed_swaps": len(by_mint),
        "wallet_balance_watch_records": len(watch_wallets) if isinstance(watch_wallets, dict) else 0,
        "swap_events_used": len(events),
        "rejected_malformed_or_unsupported_events": rejected,
        "multi_wallet_buy_consensus_tokens": sum(1 for item in token_flows
                                                  if item["flow_label"] == "multi_wallet_buy_consensus"),
        "multi_wallet_distribution_tokens": sum(1 for item in token_flows
                                                 if item["flow_label"] == "multi_wallet_distribution"),
        "repeated_timing_overlap_pairs": sum(1 for item in relationships
                                               if item["shared_token_count"] >= 2),
    }
    return {
        "status": "ok" if events else "insufficient_swap_history",
        "mode": "explainable_wallet_behaviour_and_token_flow_research",
        "research_only": True,
        "live_trading_authorized": False,
        "copy_policy_changed": False,
        "leadership_scope": "first observed among tracked wallet histories only",
        "consensus_scope": "unique tracked wallet addresses in the trailing window of observed swaps only",
        "consensus_window_seconds": CONSENSUS_WINDOW_SECONDS,
        "follower_window_seconds": FOLLOW_WINDOW_SECONDS,
        "statistics": stats,
        "wallet_profiles": wallet_profiles[:50],
        "token_flows": token_flows[:50],
        "wallet_relationships": relationships[:100],
        "interpretation_limits": [
            "Observed wallet histories may be incomplete; missing transactions can change timing and inferred style.",
            "First observed buyer means first in this tracked dataset, not first on the market.",
            "Balance changes are not treated as swaps; transfer attribution remains unresolved.",
            "Quote notionals stay separated by SOL, USDC and USDT and are not added across currencies.",
            "Behavior classifications are descriptive research, not proof of intent, future return, or a trade recommendation.",
            "Repeated same-token timing overlap does not establish common ownership or coordination.",
            "This module never changes entry thresholds or authorizes live trading.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--swaps", default="whale_copy_state.json")
    parser.add_argument("--balances", default="whale_watch_state.json")
    parser.add_argument("--output")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    report = analyze_behavior(_load_json(args.swaps), _load_json(args.balances))
    limit = max(1, min(int(args.limit), 200))
    report["wallet_profiles"] = report.get("wallet_profiles", [])[:limit]
    report["token_flows"] = report.get("token_flows", [])[:limit]
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
    if args.output:
        Path(args.output).write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
