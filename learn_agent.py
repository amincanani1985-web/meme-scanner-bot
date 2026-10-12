#!/usr/bin/env python3
"""Adaptive bounded strategy-learning engine with walk-forward validation and rollback."""
import csv, json, math
from pathlib import Path

MIN_TRADES=30
CANDIDATE_THRESHOLDS=(80.0,82.5,85.0,87.5,90.0)
MIN_CANDIDATE_SAMPLES=8
ROLLBACK_WINDOW=10
ROLLBACK_WIN_RATE=0.30
ROLLBACK_AVG_PNL=-2.0
MIN_VALIDATION_EDGE=0.25
BASELINE_THRESHOLD=80.0

def load_observations(path="strategy_observations.csv"):
    """Load complete, finite observations; never invent scores for legacy trades."""
    p = Path(path)
    if not p.exists():
        return []
    rows = []
    with p.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                timestamp = str(row.get("timestamp", "")).strip()
                score = float(row["score"])
                pnl_pct = float(row["pnl_pct"])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if not timestamp or not math.isfinite(score) or not math.isfinite(pnl_pct):
                continue
            if not 0.0 <= score <= 100.0:
                continue
            rows.append({"timestamp": timestamp, "score": score, "pnl_pct": pnl_pct, "reason": row.get("reason", "")})
    return sorted(rows, key=lambda item: item["timestamp"])

def _metrics(rows,threshold):
    s=[r for r in rows if r["score"]>=threshold]
    if not s: return {"n":0,"wins":0,"win_rate":0.0,"avg_pnl":0.0,"profit_factor":0.0}
    wins=[r for r in s if r["pnl_pct"]>0]; losses=[r for r in s if r["pnl_pct"]<0]
    gw=sum(r["pnl_pct"] for r in wins); gl=abs(sum(r["pnl_pct"] for r in losses))
    return {"n":len(s),"wins":len(wins),"win_rate":len(wins)/len(s),"avg_pnl":sum(r["pnl_pct"] for r in s)/len(s),"profit_factor":gw/gl if gl else (999.0 if gw else 0.0)}

def walk_forward(observations,threshold,train_ratio=0.7):
    rows=sorted(observations,key=lambda r:r["timestamp"])
    if len(rows)<MIN_TRADES: return {"eligible":False,"reason":f"insufficient_history:{len(rows)}/{MIN_TRADES}","threshold":threshold}
    split=max(1,int(len(rows)*train_ratio)); train=rows[:split]; val=rows[split:]
    tm=_metrics(train,threshold); vm=_metrics(val,threshold)
    return {"eligible":vm["n"]>=MIN_CANDIDATE_SAMPLES,"threshold":threshold,"train":tm,"validation":vm}

def choose_candidate(observations,current_threshold=BASELINE_THRESHOLD):
    results=[walk_forward(observations,t) for t in CANDIDATE_THRESHOLDS]
    eligible=[r for r in results if r["eligible"]]
    if not eligible: return {"action":"hold","threshold":current_threshold,"reason":"no_candidate_has_enough_validation_samples","candidates":results}
    baseline=next((r for r in eligible if r["threshold"]==current_threshold),None)
    base_avg=baseline["validation"]["avg_pnl"] if baseline else -999.0
    best=max(eligible,key=lambda r:(r["validation"]["avg_pnl"],r["validation"]["win_rate"],r["validation"]["profit_factor"]))
    if best["threshold"]!=current_threshold and best["validation"]["avg_pnl"]>=base_avg+MIN_VALIDATION_EDGE and best["validation"]["avg_pnl"]>0:
        return {"action":"activate","threshold":best["threshold"],"reason":"walk_forward_candidate_beats_baseline","selected":best,"candidates":results}
    return {"action":"hold","threshold":current_threshold,"reason":"no_validated_candidate_beats_baseline","selected":best,"candidates":results}

def should_rollback(observations,current_threshold):
    rows=[r for r in observations if r["score"]>=current_threshold][-ROLLBACK_WINDOW:]
    if len(rows)<ROLLBACK_WINDOW: return False,"rollback_window_not_full"
    m=_metrics(rows,current_threshold)
    return (m["win_rate"]<ROLLBACK_WIN_RATE or m["avg_pnl"]<=ROLLBACK_AVG_PNL),f"active_strategy:win_rate={m['win_rate']:.2f},avg_pnl={m['avg_pnl']:.2f}"

def adaptive_update_state(state,observations):
    if not isinstance(state, dict):
        state = {}
    strategy = state.get("strategy")
    if not isinstance(strategy, dict):
        strategy = {}
        state["strategy"] = strategy
    strategy.setdefault("active_threshold", BASELINE_THRESHOLD)
    strategy.setdefault("candidate_threshold", None)
    strategy.setdefault("status", "baseline")
    strategy.setdefault("last_update_trade", 0)
    strategy.setdefault("rollback_threshold", BASELINE_THRESHOLD)

    # Normalize malformed thresholds before any arithmetic. Invalid persisted
    # values fall back to the safe baseline instead of crashing the scheduled job.
    try:
        active = float(strategy.get("active_threshold", BASELINE_THRESHOLD))
        if not math.isfinite(active):
            raise ValueError("non-finite active threshold")
    except (TypeError, ValueError, OverflowError):
        active = BASELINE_THRESHOLD
        strategy["active_threshold"] = BASELINE_THRESHOLD
    try:
        rollback_threshold = float(strategy.get("rollback_threshold", BASELINE_THRESHOLD))
        if not math.isfinite(rollback_threshold):
            raise ValueError("non-finite rollback threshold")
    except (TypeError, ValueError, OverflowError):
        rollback_threshold = BASELINE_THRESHOLD
        strategy["rollback_threshold"] = BASELINE_THRESHOLD

    if (strategy.get("status") == "waiting_for_history"
        and strategy.get("candidate_threshold") is None
        and active < BASELINE_THRESHOLD):
        strategy["active_threshold"] = BASELINE_THRESHOLD
        strategy["rollback_threshold"] = BASELINE_THRESHOLD
        strategy["baseline_migrated_from"] = active
        active = BASELINE_THRESHOLD

    current = active
    strategy["history_rows"] = len(observations)
    strategy["required_rows"] = MIN_TRADES
    if len(observations) < MIN_TRADES:
        strategy["status"] = "waiting_for_history"
        strategy["validation_status"] = "insufficient_history"
        strategy["candidate_threshold"] = None
        return state
    rollback, reason = should_rollback(observations, current)
    if current != BASELINE_THRESHOLD and rollback:
        strategy.update({"active_threshold": BASELINE_THRESHOLD, "candidate_threshold": None, "status": "rolled_back", "validation_status": "rolled_back", "rollback_threshold": BASELINE_THRESHOLD, "rollback_reason": reason})
        return state
    rec = choose_candidate(observations, current)
    strategy["candidate_threshold"] = rec.get("threshold")
    strategy["validation"] = rec
    strategy["last_validation_trade"] = state.get("trade_count", 0)
    if rec.get("reason") == "no_candidate_has_enough_validation_samples":
        strategy["status"] = "waiting_for_validation"
        strategy["validation_status"] = "insufficient_threshold_samples"
        strategy["candidate_threshold"] = None
        return state
    if rec.get("action") == "activate" and rec["threshold"] != current:
        strategy.update({"active_threshold": float(rec["threshold"]), "status": "candidate_activated", "validation_status": "validated", "activated_at_trade": state.get("trade_count", 0), "last_update_trade": state.get("trade_count", 0), "rollback_threshold": BASELINE_THRESHOLD, "activation_reason": rec.get("reason")})
    else:
        strategy["validation_status"] = "evaluated_no_improvement"
        if current == BASELINE_THRESHOLD:
            strategy["status"] = "baseline"
    return state

def run(state_path="state.json",observations_path="strategy_observations.csv",apply=False):
    obs=load_observations(observations_path)
    state=json.loads(Path(state_path).read_text()) if Path(state_path).exists() else {"trade_count":0}
    updated=adaptive_update_state(state,obs)
    if apply: Path(state_path).write_text(json.dumps(updated,indent=2)+"\n")
    return {"observations":len(obs),"strategy":updated.get("strategy",{}),"applied":apply}

if __name__=="__main__":
    import argparse
    ap=argparse.ArgumentParser(); ap.add_argument("--state",default="state.json"); ap.add_argument("--observations",default="strategy_observations.csv"); ap.add_argument("--apply",action="store_true")
    a=ap.parse_args(); print(json.dumps(run(a.state,a.observations,a.apply),indent=2))
