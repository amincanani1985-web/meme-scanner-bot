#!/usr/bin/env python3
"""Offline entry-strategy backtest and chronological walk-forward validator."""
import argparse, csv, json
from pathlib import Path

THRESHOLDS=(60.0,62.5,65.0,67.5,70.0)

def load(path):
    p=Path(path)
    if not p.exists(): return []
    out=[]
    with p.open(newline="") as f:
        for r in csv.DictReader(f):
            try: out.append({"timestamp":r["timestamp"],"score":float(r["score"]),"pnl_pct":float(r["pnl_pct"])})
            except (KeyError,TypeError,ValueError): pass
    return sorted(out,key=lambda x:x["timestamp"])

def metrics(rows,t):
    s=[r for r in rows if r["score"]>=t]
    wins=[r for r in s if r["pnl_pct"]>0]
    losses=[r for r in s if r["pnl_pct"]<0]
    gw=sum(r["pnl_pct"] for r in wins); gl=abs(sum(r["pnl_pct"] for r in losses))
    return {"n":len(s),"win_rate":len(wins)/len(s) if s else 0,"avg_pnl":sum(r["pnl_pct"] for r in s)/len(s) if s else 0,"profit_factor":gw/gl if gl else (999 if gw else 0)}

def run(rows):
    if len(rows)<20: return {"status":"insufficient_history","rows":len(rows),"thresholds":{}}
    split=max(1,int(len(rows)*0.7)); train=rows[:split]; val=rows[split:]
    result={}
    for t in THRESHOLDS:
        result[str(t)]={"train":metrics(train,t),"validation":metrics(val,t)}
    return {"status":"ok","rows":len(rows),"train_rows":len(train),"validation_rows":len(val),"thresholds":result}

if __name__=="__main__":
    ap=argparse.ArgumentParser(); ap.add_argument("--observations",default="strategy_observations.csv")
    args=ap.parse_args(); print(json.dumps(run(load(args.observations)),indent=2))
