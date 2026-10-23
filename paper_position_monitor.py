"""Monitor existing isolated paper positions without discovering or opening trades."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import meme_scanner_cycle as scanner


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
        scanner.manage_open_positions(state)
        scanner.manage_shadow_positions(state)
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
        }
    finally:
        os.chdir(previous_cwd)


if __name__ == "__main__":
    print(json.dumps(run(), sort_keys=True))
