import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database.client import db_manager
from src.paper_trading.portfolio_simulator import portfolio_simulator
from src.backtest.replay_engine import _offline_safety_check, _build_raw_token_event
from src.opportunity.scorer import opportunity_scorer

async def run_sweep():
    await db_manager.initialize()
    raw_tokens = await db_manager.query("backtest_tokens", filters={"label": "not.is.null"}, limit=5000)
    
    passed_safety = []
    for r in raw_tokens:
        p, _ = _offline_safety_check(r)
        if p:
            passed_safety.append(r)
    
    scored = []
    for r in passed_safety:
        ev = _build_raw_token_event(r)
        if ev:
            try:
                res = await opportunity_scorer.score_token(ev)
                score = res.opportunity_score
            except Exception:
                score = 0.0
            r_copy = dict(r)
            r_copy["opportunity_score"] = score
            scored.append(r_copy)
            
    print("\n" + "="*95)
    print(f"{'Threshold':<10} {'Signals':<9} {'Runners':<9} {'Dead':<9} {'Recall':<10} {'WinRate':<12} {'ROI (+1000TP)':<16} {'Final $':<12}")
    print("="*95)
    
    total_runners = sum(1 for s in scored if s.get("label") == "runner")
    
    for th in [25.0, 28.0, 30.0, 31.0, 32.0, 33.0, 35.0, 37.0, 40.0, 45.0, 50.0, 55.0, 60.0]:
        sigs = [s for s in scored if s["opportunity_score"] >= th]
        n_sig = len(sigs)
        if n_sig == 0:
            continue
        n_run = sum(1 for s in sigs if s.get("label") == "runner")
        n_dead = sum(1 for s in sigs if s.get("label") == "dead")
        recall = (n_run / total_runners) * 100.0 if total_runners > 0 else 0.0
        
        res1000 = portfolio_simulator.simulate_strategy(sigs, tp_target_pct=1000.0, sl_target_pct=-30.0, initial_balance=10.0, position_risk_pct=2.0)
        
        print(f"{th:<10.1f} {n_sig:<9} {n_run:<9} {n_dead:<9} {recall:<10.1f}% {res1000.win_rate_pct:<12.1f}% {res1000.roi_pct:<+16.1f}% ${res1000.final_balance:<12.2f}")
    print("="*95)

asyncio.run(run_sweep())