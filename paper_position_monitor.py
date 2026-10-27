"""Monitor existing isolated paper positions without discovering or opening trades."""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import meme_scanner_cycle as scanner
import whale_shadow_capture


def run(root_dir=None):
    """Refresh exit decisions for current paper positions only; never scan candidates."""
    root = Path(root_dir or Path(__file__).resolve().parent).resolve()
    paper_dir = root / "paper_account"
    if not paper_dir.is_dir():
        raise FileNotFoundError(f"paper account directory not found: {paper_dir}")

    previous_cwd = Path.cwd()
    try:
        os.chdir(paper_dir)
        scanner.PAIR_CACHE.clear()
        state = scanner.load_state()
        scanner.reconcile_learning_from_trade_log(state)
        trades_before = int(state.get("trade_count", 0))
        # Prefetch every current position in documented batches to avoid one HTTP
        # request per token during the high-frequency exit-monitor pass.
        capture_candidates = whale_shadow_capture.monitored_mints(root, now=time.time())
        monitored_mints = list(dict.fromkeys([
            *state.get("positions", {}).keys(),
            *state.get("shadow_positions", {}).keys(),
            *capture_candidates,
        ]))
        scanner.fetch_dex_pairs(monitored_mints)
        scanner.manage_open_positions(state)
        scanner.manage_shadow_positions(state)
        capture_result = whale_shadow_capture.sample(
            root,
            quote_fetcher=scanner.fetch_dex_pair,
            now=time.time(),
            candidate_mints=capture_candidates,
        )
        risk = scanner.update_risk_control(state)
        monitored_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        state["last_position_monitor"] = monitored_at
        scanner.save_state(state)
        return {
            "mode": "monitor_only",
            "timestamp": monitored_at,
            "closed_trades": max(0, int(state.get("trade_count", 0)) - trades_before),
            "equity": round(float(state.get("equity", 0.0)), 6),
            "open_positions": len(state.get("positions", {})),
            "shadow_positions": len(state.get("shadow_positions", {})),
            "risk_status": risk.get("status", "unknown"),
            "whale_capture_active_paths": capture_result.get("active_paths", 0),
            "whale_capture_started": capture_result.get("new_paths_started", 0),
            "whale_capture_completed": capture_result.get("paths_completed", 0),
            "whale_capture_rejected": capture_result.get("paths_rejected", 0),
            "whale_capture_quote_failures": capture_result.get("quotes_failed", 0),
        }
    finally:
        os.chdir(previous_cwd)


if __name__ == "__main__":
    print(json.dumps(run(), sort_keys=True))
