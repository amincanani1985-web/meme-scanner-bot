"""Read-only RugCheck Solana token-risk enrichment for paper research.

The public report endpoint returns cached on-chain analysis. Report age and field
coverage are explicit; missing fields are never interpreted as zero-risk.
This module never submits transactions or interacts with swap endpoints.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
import os

import requests


RUGCHECK_REPORT_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
REQUEST_TIMEOUT_SECONDS = 8.0


def is_enabled() -> bool:
    """Whether optional public risk enrichment is enabled."""
    value = str(os.environ.get("RUGCHECK_RESEARCH_ENABLED", "1")).strip().lower()
    return value not in {"0", "false", "no", "off", "disabled"}


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _bool_number(value):
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if value is None:
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        if float(value) in (0.0, 1.0):
            return float(value)
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in {"true", "1", "yes"}:
            return 1.0
        if cleaned in {"false", "0", "no", ""}:
            return 0.0
    return None


def _timestamp_seconds(value):
    """Parse epoch seconds/milliseconds or an ISO-8601 timestamp."""
    if value is None:
        return None
    number = _number(value)
    if number is not None:
        if number > 100_000_000_000:
            number /= 1000.0
        return number if number > 0 else None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        result = parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _active_flag(obj, key):
    if not isinstance(obj, dict) or key not in obj:
        return None
    value = obj.get(key)
    if value is None:
        return 0.0
    if isinstance(value, str) and value.strip().lower() in {"", "null", "none", "false", "0", "no"}:
        return 0.0
    return 1.0


def _valid_pct(value):
    number = _number(value)
    if number is None or number < 0 or number > 100:
        return None
    return number


def normalize_token_report(payload, expected_mint, now=None):
    """Normalize a full report, rejecting mismatched mints and keeping unknowns null."""
    if not isinstance(payload, dict) or not isinstance(expected_mint, str) or not expected_mint.strip():
        return None
    mint = payload.get("mint")
    if not isinstance(mint, str) or mint.strip() != expected_mint.strip():
        return None

    now_seconds = _number(now)
    if now_seconds is None:
        now_seconds = datetime.now(timezone.utc).timestamp()

    token = payload.get("token") if isinstance(payload.get("token"), dict) else {}
    extensions = payload.get("token_extensions") if isinstance(payload.get("token_extensions"), dict) else {}
    token_meta = payload.get("tokenMeta") if isinstance(payload.get("tokenMeta"), dict) else {}
    extension_meta = extensions.get("tokenMetadata") if isinstance(extensions.get("tokenMetadata"), dict) else {}
    risks = payload.get("risks") if isinstance(payload.get("risks"), list) else None
    holders = payload.get("topHolders") if isinstance(payload.get("topHolders"), list) else None
    markets = payload.get("markets") if isinstance(payload.get("markets"), list) else None

    detected_at = _timestamp_seconds(payload.get("detectedAt"))
    report_age = max(0.0, now_seconds - detected_at) if detected_at is not None else None

    holder_pcts, insider_pcts = [], []
    if holders is not None:
        for holder in holders:
            if not isinstance(holder, dict):
                continue
            pct = _valid_pct(holder.get("pct"))
            if pct is None:
                continue
            holder_pcts.append(pct)
            if holder.get("insider") is True:
                insider_pcts.append(pct)
    holder_pcts.sort(reverse=True)
    top1 = max(holder_pcts) if holder_pcts else None
    top10 = min(100.0, sum(holder_pcts[:10])) if len(holder_pcts) >= 10 else None
    top20 = min(100.0, sum(holder_pcts[:20])) if len(holder_pcts) >= 20 else None
    insider_pct = min(100.0, sum(insider_pcts)) if holder_pcts else None

    normalized_risks = [risk for risk in risks or [] if isinstance(risk, dict)]
    levels = [str(risk.get("level") or "").strip().lower() for risk in normalized_risks]
    warning_count = sum(level in {"warn", "warning"} for level in levels)
    danger_count = sum(level == "danger" for level in levels)
    critical_count = sum(level == "critical" for level in levels)
    high_count = sum(level in {"high", "danger", "critical"} for level in levels)
    copycat_count = sum("copycat" in str(risk.get("name") or "").lower() for risk in normalized_risks)

    lp_markets = []
    for market in markets or []:
        if not isinstance(market, dict):
            continue
        lp = market.get("lp")
        if not isinstance(lp, dict):
            continue
        locked_pct = _valid_pct(lp.get("lpLockedPct"))
        locked_usd = _number(lp.get("lpLockedUSD"))
        if locked_pct is not None:
            lp_markets.append((locked_usd if locked_usd is not None else -1.0, locked_pct, locked_usd))
    lp_markets.sort(key=lambda item: item[0], reverse=True)
    lp_locked_pct = lp_markets[0][1] if lp_markets else None
    lp_locked_usd = lp_markets[0][2] if lp_markets else None

    rugged = _bool_number(payload.get("rugged")) if "rugged" in payload else None
    mint_authority_active = _active_flag(token, "mintAuthority")
    freeze_authority_active = _active_flag(token, "freezeAuthority")
    close_authority_active = _active_flag(extensions, "mintCloseAuthority")
    metadata_mutable = _bool_number(token_meta.get("mutable")) if "mutable" in token_meta else None
    if metadata_mutable is None and "authority" in extension_meta:
        metadata_mutable = _active_flag(extension_meta, "authority")

    transfer_fee = payload.get("transferFee") if isinstance(payload.get("transferFee"), dict) else {}
    transfer_fee_pct = _number(transfer_fee.get("pct"))
    transfer_fee_authority_active = _active_flag(transfer_fee, "authority")

    total_holders = _number(payload.get("totalHolders"))
    total_market_liquidity = _number(payload.get("totalMarketLiquidity"))
    total_stable_liquidity = _number(payload.get("totalStableLiquidity"))
    raw_score = _number(payload.get("score"))
    normalized_score = _number(payload.get("score_normalised"))
    if normalized_score is None:
        normalized_score = _number(payload.get("score_normalized"))

    result = {
        "rugcheck_available": 1.0,
        "rugcheck_risk_data_available": 1.0 if (
            token or risks is not None or holders is not None or markets is not None
        ) else 0.0,
        "rugcheck_report_age_seconds": report_age,
        "rugcheck_score_raw": raw_score,
        "rugcheck_score_normalised": normalized_score,
        "rugcheck_risk_count": float(len(normalized_risks)) if risks is not None else None,
        "rugcheck_warning_risk_count": float(warning_count) if risks is not None else None,
        "rugcheck_danger_risk_count": float(danger_count) if risks is not None else None,
        "rugcheck_critical_risk_count": float(critical_count) if risks is not None else None,
        "rugcheck_high_risk_count": float(high_count) if risks is not None else None,
        "rugcheck_copycat_risk_count": float(copycat_count) if risks is not None else None,
        "rugcheck_mint_authority_active": mint_authority_active,
        "rugcheck_freeze_authority_active": freeze_authority_active,
        "rugcheck_mint_close_authority_active": close_authority_active,
        "rugcheck_metadata_mutable": metadata_mutable,
        "rugcheck_transfer_fee_pct": transfer_fee_pct,
        "rugcheck_transfer_fee_authority_active": transfer_fee_authority_active,
        "rugcheck_top1_pct": top1,
        "rugcheck_top10_pct": top10,
        "rugcheck_top20_pct": top20,
        "rugcheck_insider_pct": insider_pct,
        "rugcheck_holder_count": total_holders,
        "rugcheck_lp_locked_pct": lp_locked_pct,
        "rugcheck_lp_locked_usd": lp_locked_usd,
        "rugcheck_total_market_liquidity_usd": total_market_liquidity,
        "rugcheck_total_stable_liquidity_usd": total_stable_liquidity,
        "rugcheck_rugged": rugged,
    }

    # Coverage is a data-quality fact, not a safety verdict.
    result["rugcheck_full_risk_screen_available"] = 1.0 if all((
        detected_at is not None,
        token and "mintAuthority" in token,
        token and "freezeAuthority" in token,
        "mintCloseAuthority" in extensions,
        metadata_mutable is not None,
        risks is not None,
        holders is not None and len(holder_pcts) >= 20 and top10 is not None and top20 is not None,
        lp_locked_pct is not None,
        total_holders is not None,
        rugged is not None,
    )) else 0.0
    return result


def fetch_token_report(mint, timeout=REQUEST_TIMEOUT_SECONDS):
    """Fetch one public read-only full report; transport and schema errors fail closed."""
    if not is_enabled() or not isinstance(mint, str) or not mint.strip():
        return None
    timeout_value = _number(timeout)
    if timeout_value is None or timeout_value <= 0:
        timeout_value = REQUEST_TIMEOUT_SECONDS
    timeout_value = min(timeout_value, REQUEST_TIMEOUT_SECONDS)
    try:
        response = requests.get(
            RUGCHECK_REPORT_URL.format(mint=mint.strip()),
            timeout=timeout_value,
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError, TypeError, OverflowError):
        return None
    return normalize_token_report(payload, mint.strip())
