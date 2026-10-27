#!/usr/bin/env python3
"""Conservative, research-only quality/risk flags for observed DEX pair snapshots.

This module does not issue buy/sell signals, score wallet skill, or authorize trades.
Promotion/boost spend is reported as a disclosure flag only and never increases quality.
"""
import argparse
import json
import math
from pathlib import Path

MIN_LIQUIDITY_USD = 10_000.0
SEVERE_DRAWDOWN_PCT = -50.0
EXTREME_PUMP_PCT = 300.0
EXTREME_MOMENTUM_H1_PCT = 50.0
EXTREME_MOMENTUM_M5_PCT = 20.0
EXTREME_TURNOVER_RATIO = 20.0
SELL_PRESSURE_MIN_TXNS_H6 = 200
SELL_PRESSURE_RATIO = 1.5


def _number(value):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _txn_counts(value):
    if not isinstance(value, dict):
        return None, None
    buys, sells = _number(value.get("buys")), _number(value.get("sells"))
    if buys is None or sells is None or buys < 0 or sells < 0:
        return None, None
    return buys, sells


def screen_pair(record):
    """Return explainable risk flags for one normalized DEX pair snapshot."""
    record = record if isinstance(record, dict) else {}
    liquidity = _number(record.get("liquidity_usd"))
    price = _number(record.get("price_usd"))
    market_cap = _number(record.get("market_cap_usd"))
    volume_h6 = _number(record.get("volume_h6_usd"))
    volume_h24 = _number(record.get("volume_h24_usd"))
    change_m5 = _number(record.get("price_change_m5_pct"))
    change_h1 = _number(record.get("price_change_h1_pct"))
    change_h6 = _number(record.get("price_change_h6_pct"))
    change_h24 = _number(record.get("price_change_h24_pct"))
    buys_h6, sells_h6 = _txn_counts(record.get("txns_h6"))
    boosts = _number(record.get("boosts_active")) or 0.0

    flags = []
    score = 0

    def add(flag, points, explanation):
        nonlocal score
        flags.append({"code": flag, "points": points, "explanation": explanation})
        score += points

    if liquidity is None:
        add("missing_liquidity_data", 30, "Liquidity was unavailable; risk cannot be cleared from this snapshot.")
    elif liquidity < MIN_LIQUIDITY_USD:
        add("liquidity_below_floor", 35, f"Pool liquidity is ${liquidity:,.0f}, below the ${MIN_LIQUIDITY_USD:,.0f} research floor.")
        if liquidity < 5_000:
            add("very_thin_pool", 15, "A small order can represent a large share of the pool and move price sharply.")

    if price is None or price <= 0:
        add("missing_or_invalid_price", 25, "A positive finite USD price was not available.")

    if change_h24 is None:
        flags.append({"code": "missing_24h_change", "points": 0, "explanation": "No 24-hour price-change value; no momentum conclusion is drawn."})
    elif change_h24 <= SEVERE_DRAWDOWN_PCT:
        add("severe_24h_drawdown", 30, f"Observed 24-hour price change is {change_h24:.1f}% or worse.")
    elif change_h24 >= EXTREME_PUMP_PCT:
        add("extreme_24h_pump", 20, f"Observed 24-hour price change is +{change_h24:.1f}%; chase/mean-reversion risk is elevated.")

    if (change_h1 is not None and change_h1 >= EXTREME_MOMENTUM_H1_PCT) or (
        change_m5 is not None and change_m5 >= EXTREME_MOMENTUM_M5_PCT
    ):
        add("extreme_short_term_momentum", 10, "A rapid short-window price move increases slippage and reversal risk.")

    if buys_h6 is not None and sells_h6 is not None and buys_h6 + sells_h6 >= SELL_PRESSURE_MIN_TXNS_H6:
        if sells_h6 > 0 and buys_h6 / sells_h6 <= 1 / SELL_PRESSURE_RATIO:
            add("sell_pressure_h6", 15, f"Six-hour sell count ({sells_h6:.0f}) is at least {SELL_PRESSURE_RATIO:.1f}x buy count ({buys_h6:.0f}).")

    if liquidity is not None and liquidity > 0 and volume_h24 is not None and volume_h24 >= 0:
        turnover = volume_h24 / liquidity
        if turnover >= EXTREME_TURNOVER_RATIO:
            add("extreme_24h_turnover", 10, f"24-hour reported volume is {turnover:.1f}x current pool liquidity.")

    if liquidity is not None and liquidity > 0 and market_cap is not None and market_cap > 0:
        liquidity_ratio = liquidity / market_cap
        if liquidity_ratio < 0.02:
            add("weak_liquidity_vs_market_cap", 10, f"Pool liquidity is only {liquidity_ratio:.1%} of reported market cap.")

    # Paid promotion is not evidence of quality, demand, or wallet profitability.
    if boosts > 0:
        flags.append({"code": "promotion_not_quality_evidence", "points": 0, "explanation": f"DEX Screener reports {boosts:.0f} active boost units; promotion does not lower risk score."})

    score = min(100, score)
    if liquidity is None or price is None or price <= 0:
        risk_level = "insufficient_data"
    elif score >= 60:
        risk_level = "very_high"
    elif score >= 40:
        risk_level = "high"
    elif score >= 20:
        risk_level = "elevated"
    else:
        risk_level = "lower_observed_risk"

    return {
        "mint": record.get("mint"),
        "token_name": record.get("token_name") or record.get("base_token_name"),
        "token_symbol": record.get("token_symbol") or record.get("base_token_symbol"),
        "pair_address": record.get("pair_address"),
        "pair_url": record.get("pair_url"),
        "source": record.get("source", "unknown"),
        "observed_at": record.get("observed_at"),
        "price_usd": price,
        "liquidity_usd": liquidity,
        "market_cap_usd": market_cap,
        "volume_h6_usd": volume_h6,
        "volume_h24_usd": volume_h24,
        "price_change_m5_pct": change_m5,
        "price_change_h1_pct": change_h1,
        "price_change_h6_pct": change_h6,
        "price_change_h24_pct": change_h24,
        "txns_h6": {"buys": buys_h6, "sells": sells_h6} if buys_h6 is not None else None,
        "boosts_active": boosts,
        "risk_score": score,
        "risk_level": risk_level,
        "flags": flags,
        "eligible_for_entry": False,
        "research_only": True,
        "live_trading_authorized": False,
    }


def screen_report(provider_report):
    """Screen DexScreener records and report coverage without treating absence as safety."""
    provider_report = provider_report if isinstance(provider_report, dict) else {}
    records = provider_report.get("records", [])
    if not isinstance(records, list):
        records = []
    pairs = [screen_pair(row) for row in records
             if isinstance(row, dict) and str(row.get("source", "")).lower() == "dexscreener"]
    # Deduplicate overlapping requests without merging separate pools for the same token.
    deduped = []
    seen = set()
    for row in pairs:
        identity = row.get("pair_address") or (row.get("mint"), row.get("observed_at"))
        if identity in seen:
            continue
        seen.add(identity)
        deduped.append(row)
    deduped.sort(key=lambda row: (-row["risk_score"], row.get("liquidity_usd") or -1, row.get("token_symbol") or ""))
    levels = {level: sum(row["risk_level"] == level for row in deduped)
              for level in ("very_high", "high", "elevated", "lower_observed_risk", "insufficient_data")}
    return {
        "generated_at": provider_report.get("generated_at"),
        "status": "screened" if deduped else "no_dexscreener_records",
        "source_provider_status": provider_report.get("provider_status", []),
        "pairs_screened": len(deduped),
        "risk_level_counts": levels,
        "pairs": deduped,
        "research_only": True,
        "trade_signal": False,
        "live_trading_authorized": False,
        "limitations": [
            "This is a conservative descriptive risk screen, not a rug-pull detector or a buy/sell signal.",
            "Reported volume and market cap are third-party pool snapshots and can be noisy or manipulated.",
            "The number of DEX Screener boost units is disclosed but contributes no positive score.",
            "A low score is not proof of safety; holder distribution, mint/freeze controls, insider wallets, executable slippage, and verified wallet PnL require separate evidence.",
            "No transactions are signed or submitted; eligible_for_entry is always false.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="/tmp/market-data-connectors-report.json")
    parser.add_argument("--output", default="/tmp/token-quality-screen-report.json")
    args = parser.parse_args()
    try:
        payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {"records": [], "provider_status": [], "generated_at": None}
    report = screen_report(payload)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(encoded, encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("generated_at", "status", "pairs_screened", "risk_level_counts", "trade_signal", "live_trading_authorized")}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
