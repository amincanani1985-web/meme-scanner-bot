"""Read-only RugCheck token risk enrichment. Never executes transactions."""
import math
import os
import time
from datetime import datetime
from urllib.parse import quote
import requests

RUGCHECK_REPORT_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
TRUE_VALUES = {"1", "true", "yes", "y"}


def is_enabled():
    return str(os.environ.get("RUGCHECK_RESEARCH_ENABLED", "0")).strip().lower() in TRUE_VALUES


def _num(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _active(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)) and value in (0, 1):
        return float(value)
    return 1.0


def _active_field(mapping, key):
    """Treat an explicit JSON null authority as disabled, but absent fields as unknown."""
    if not isinstance(mapping, dict) or key not in mapping:
        return None
    if mapping[key] is None:
        return 0.0
    return _active(mapping[key])


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def normalize_token_report(payload, expected_mint, now=None):
    if not isinstance(payload, dict) or not str(expected_mint or "").strip():
        return None
    if str(payload.get("mint") or "").strip() != str(expected_mint).strip():
        return None
    token = _mapping(payload.get("token"))
    ext = _mapping(payload.get("token_extensions"))
    token_meta = _mapping(payload.get("tokenMeta"))
    risks = payload.get("risks")
    risks = risks if isinstance(risks, list) else None
    holders = payload.get("topHolders")
    holders = [x for x in holders if isinstance(x, dict)] if isinstance(holders, list) else None
    markets = payload.get("markets")
    markets = [x for x in markets if isinstance(x, dict)] if isinstance(markets, list) else None
    holder_pct = [_num(x.get("pct")) for x in holders] if holders is not None else None
    if holder_pct is not None:
        holder_pct = [x for x in holder_pct if x is not None]
    risk_levels = [str(x.get("level") or "").lower() for x in risks if isinstance(x, dict)] if risks is not None else None
    locked_pct, locked_usd = [], []
    if markets is not None:
        for market in markets:
            lp = _mapping(market.get("lp"))
            p, u = _num(lp.get("lpLockedPct")), _num(lp.get("lpLockedUSD"))
            if p is not None: locked_pct.append(p)
            if u is not None: locked_usd.append(u)
    detected = _timestamp(payload.get("detectedAt"))
    now = time.time() if now is None else float(now)
    full = (
        holders is not None and len(holder_pct or []) >= 20
        and risks is not None and bool(markets)
        and _num(payload.get("totalHolders")) is not None
        and all(k in token for k in ("mintAuthority", "freezeAuthority"))
        and "mintCloseAuthority" in ext
        and "tokenMetadata" in ext
        and "mutable" in token_meta
    )
    return {
        "rugcheck_available": 1.0,
        "rugcheck_risk_data_available": 1.0 if risks is not None and "rugged" in payload else 0.0,
        "rugcheck_full_risk_screen_available": 1.0 if full else 0.0,
        "rugcheck_mint_authority_active": _active_field(token, "mintAuthority"),
        "rugcheck_freeze_authority_active": _active_field(token, "freezeAuthority"),
        "rugcheck_mint_close_authority_active": _active_field(ext, "mintCloseAuthority"),
        "rugcheck_metadata_mutable": _active_field(token_meta, "mutable"),
        "rugcheck_holder_count": _num(payload.get("totalHolders")),
        "rugcheck_top1_pct": sum(holder_pct[:1]) if holder_pct is not None else None,
        "rugcheck_top10_pct": sum(holder_pct[:10]) if holder_pct is not None else None,
        "rugcheck_top20_pct": sum(holder_pct[:20]) if holder_pct is not None else None,
        "rugcheck_insider_pct": sum((_num(h.get("pct")) or 0.0) for h in holders if h.get("insider") is True) if holders is not None else None,
        "rugcheck_lp_locked_pct": max(locked_pct) if locked_pct else None,
        "rugcheck_lp_locked_usd": max(locked_usd) if locked_usd else None,
        "rugcheck_warning_risk_count": float(sum(x in {"warn", "warning"} for x in risk_levels)) if risk_levels is not None else None,
        "rugcheck_danger_risk_count": float(sum(x == "danger" for x in risk_levels)) if risk_levels is not None else None,
        "rugcheck_critical_risk_count": float(sum(x == "critical" for x in risk_levels)) if risk_levels is not None else None,
        "rugcheck_rugged": 1.0 if payload.get("rugged") is True else (0.0 if payload.get("rugged") is False else None),
        "rugcheck_score_normalised": _num(payload.get("score_normalised")),
        "rugcheck_report_age_seconds": max(0.0, now - detected) if detected is not None else None,
    }


def fetch_token_report(mint, timeout=4.0):
    if not is_enabled() or not str(mint or "").strip():
        return None
    try:
        response = requests.get(
            RUGCHECK_REPORT_URL.format(mint=quote(str(mint).strip(), safe="")),
            headers={"Accept": "application/json"},
            timeout=max(0.1, min(float(timeout), 8.0)),
        )
        response.raise_for_status()
        return normalize_token_report(response.json(), mint)
    except (requests.RequestException, ValueError, TypeError, OverflowError):
        return None
