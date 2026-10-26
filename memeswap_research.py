"""Read-only MemeSwap Solana token risk enrichment for shadow research.

This module only retrieves a public token profile. It never requests quotes,
builds transactions, signs, or submits trades. Keep unknown source values as
None so downstream strategy research cannot silently treat missing risk data
as a clean bill of health.
"""
import math
import os
import time
from urllib.parse import quote

import requests


DEFAULT_API_BASE = "https://memeswap.fun/api"
REQUEST_TIMEOUT_SECONDS = 2.0
TRUE_VALUES = {"1", "true", "yes", "y"}
FALSE_VALUES = {"0", "false", "no", "n"}


def is_enabled():
    """Enrichment is opt-in outside scheduled paper workflow environments."""
    return str(os.environ.get("MEMESWAP_RESEARCH_ENABLED", "0")).strip().lower() in TRUE_VALUES


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _first_value(*values):
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _finite_number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _source_boolean(value):
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        if float(value) == 1.0:
            return 1.0
        if float(value) == 0.0:
            return 0.0
        return None
    text = str(value or "").strip().lower()
    if text in TRUE_VALUES:
        return 1.0
    if text in FALSE_VALUES:
        return 0.0
    return None


def _age_seconds(timestamp_ms, now_seconds):
    timestamp = _finite_number(timestamp_ms)
    if timestamp is None or timestamp <= 0:
        return None
    # Source timestamps are milliseconds since Unix epoch. Do not synthesize
    # freshness when a source timestamp is missing or malformed.
    return max(0.0, float(now_seconds) - timestamp / 1000.0)


def normalize_token_profile(payload, expected_mint, now=None):
    """Flatten observed profile/risk values; return None for wrong-chain/mint data."""
    if not isinstance(payload, dict) or not str(expected_mint or "").strip():
        return None

    row = _mapping(payload.get("row"))
    profile = _mapping(payload.get("profile"))
    market = _mapping(profile.get("market"))
    holders = _mapping(profile.get("holders"))
    row_holders = _mapping(row.get("holders"))
    row_security = _mapping(row.get("security"))
    security = _mapping(_first_value(profile.get("security"), row_security))
    profile_lp = _mapping(profile.get("lp"))
    row_lp = _mapping(row.get("lp"))
    row_fake = _mapping(row.get("fake"))
    profile_fake = _mapping(profile.get("fake"))

    chain = str(_first_value(payload.get("chain"), row.get("chain"), profile.get("chain")) or "").strip().lower()
    mint = str(_first_value(
        payload.get("token"), row.get("address"), row.get("token"),
        profile.get("token"), profile.get("address"),
    ) or "").strip()
    # Solana base58 addresses are case-sensitive. The request must match the
    # response exactly; symbol-only fallback is intentionally forbidden.
    if chain != "solana" or mint != str(expected_mint).strip():
        return None

    now_seconds = time.time() if now is None else float(now)
    price_timestamp = _first_value(
        row.get("priceTs"), profile.get("priceTs"), market.get("priceTs"),
        row.get("metaTs"),
    )
    profile_timestamp = _first_value(
        profile.get("updatedAt"), row.get("profileTs"), payload.get("updatedAt"),
        profile.get("profileTs"),
    )

    def number_from(*values):
        return _finite_number(_first_value(*values))

    def bool_from(*values):
        return _source_boolean(_first_value(*values))

    # Source fields can be absent. A missing field stays None and later causes
    # that particular risk filter to fail closed.
    return {
        "memeswap_available": 1.0,
        "memeswap_price_age_seconds": _age_seconds(price_timestamp, now_seconds),
        "memeswap_profile_age_seconds": _age_seconds(profile_timestamp, now_seconds),
        "memeswap_top1_pct": number_from(holders.get("top1"), row_holders.get("top1"), row.get("top1")),
        "memeswap_top5_pct": number_from(holders.get("top5"), row_holders.get("top5"), row.get("top5")),
        "memeswap_top10_pct": number_from(holders.get("top10"), row_holders.get("top10"), row.get("top10")),
        "memeswap_top20_pct": number_from(holders.get("top20"), row_holders.get("top20"), row.get("top20")),
        "memeswap_lp_locked_pct": number_from(
            profile.get("lpLockedPct"), row.get("lpLockedPct"),
            profile_lp.get("lockedPct"), row_lp.get("lockedPct"),
        ),
        "memeswap_lp_locked": bool_from(
            profile_lp.get("locked"), row_lp.get("locked"),
        ),
        "memeswap_fake_score": number_from(
            profile_fake.get("score"), row_fake.get("score"),
        ),
        "memeswap_honeypot": bool_from(security.get("honeypot")),
        "memeswap_mintable": bool_from(security.get("mintable")),
        "memeswap_buy_tax_value": number_from(security.get("buyTax")),
        "memeswap_sell_tax_value": number_from(security.get("sellTax")),
        "memeswap_gate_pass": bool_from(_first_value(row.get("gatePass"), profile.get("gatePass"))),
    }


def fetch_token_profile(mint, now=None, enabled=None, session=None):
    """Fetch one read-only public token profile with a short timeout and no retries."""
    if enabled is None:
        enabled = is_enabled()
    mint = str(mint or "").strip()
    if not enabled or not mint:
        return None

    api_base = str(os.environ.get("MEMESWAP_API_BASE", DEFAULT_API_BASE)).rstrip("/")
    url = f"{api_base}/token/solana/{quote(mint, safe='')}"
    client = session if session is not None else requests
    try:
        response = client.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError, TypeError, OverflowError):
        return None
    return normalize_token_profile(payload, mint, now=now)
