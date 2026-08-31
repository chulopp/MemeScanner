import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.database.client import db_manager
from src.backtest.cross_validation import split_time_series_folds
from src.backtest.replay_engine import _build_raw_token_event, _offline_safety_check, load_p80_priority_fee_from_supabase, compute_trade_cost, CostModelConfig
from src.backtest.metrics import BacktestSignal, compute_metrics
from src.opportunity.scorer import OpportunityScorer

async def main():
    print('Connecting to DB...')
    await db_manager.initialize()

    rows = await db_manager.query(
        'backtest_tokens',
        filters={'label': 'not.is.null'},
        limit=5000
    )
    print(f'Loaded {len(rows)} tokens.')

    p80_fee_sol = await load_p80_priority_fee_from_supabase()
    cost_config = CostModelConfig(priority_fee_sol=p80_fee_sol)
    scorer = OpportunityScorer()

    sorted_tokens = sorted(rows, key=lambda x: str(x.get('listed_at') or x.get('collected_at') or ''))
    
    # Pre-score all tokens in parallel or sequential with progress
    print('Pre-scoring tokens...')
    scored_signals = []
    
    # To be fast, let's process in batches
    batch_size = 50
    for i in range(0, len(sorted_tokens), batch_size):
        batch = sorted_tokens[i:i+batch_size]
        tasks = []
        for row in batch:
            event = _build_raw_token_event(row)
            if not event:
                continue
            label = row.get('label', 'neutral')
            label_return_pct = row.get('label_return_pct', 0.0) or 0.0
            liquidity_usd = row.get('liquidity_usd', 0.0) or 0.0
            passed_safety, rejection_reason = _offline_safety_check(row)
            trade_cost = compute_trade_cost(liquidity_usd, cost_config)
            
            async def score_one(e, row_data, p_safe, r_reason, t_cost, lbl, ret, liq):
                opp_score = 0.0
                if p_safe:
                    try:
                        res = await scorer.score_token(e)
                        opp_score = res.opportunity_score
                    except Exception:
                        pass
                return {
                    'row': row_data,
                    'signal': BacktestSignal(
                        token_address=e.token_address,
                        symbol=e.symbol,
                        passed_safety=p_safe,
                        opportunity_score=opp_score,
                        label=lbl,
                        label_return_pct=ret,
                        liquidity_usd=liq,
                        total_cost_pct=t_cost.total_cost_pct,
                        rejection_reason=r_reason
                    )
                }
            tasks.append(score_one(event, row, passed_safety, rejection_reason, trade_cost, label, label_return_pct, liquidity_usd))
        
        batch_results = await asyncio.gather(*tasks)
        scored_signals.extend(batch_results)
        print(f'Scored {len(scored_signals)}/{len(sorted_tokens)} tokens...')

    # Now split folds
    n_splits = 5
    n = len(scored_signals)
    step = n / float(n_splits)
    folds = []
    for k in range(1, n_splits):
        train_end = int(k * step)
        test_end = int((k + 1) * step) if k < n_splits - 1 else n
        train_data = scored_signals[:train_end]
        test_data = scored_signals[train_end:test_end]
        folds.append((train_data, test_data))

    print('\nTesting thresholds from 25.0 to 75.0 (step 2.5)...')
    print(f"{'Thresh':<8} {'ActiveFolds':<12} {'Avg OOS Recall':<16} {'Avg OOS Prec':<14} {'Avg OOS EV':<12} {'EV Positive':<12} {'Status'}")
    print('-' * 85)

    sweet_spots = []
    for thresh in [float(x) for x in range(25, 75, 2)]:
        # Evaluate each fold
        fold_recalls = []
        fold_precs = []
        fold_evs = []
        active_folds = 0
        
        for idx, (train_set, test_set) in enumerate(folds, 1):
            test_signals = [item['signal'] for item in test_set]
            metrics = compute_metrics(test_signals, opportunity_threshold=thresh)
            
            test_runners = sum(1 for s in test_signals if s.label == 'runner')
            if test_runners > 0:
                active_folds += 1
                fold_recalls.append(metrics.opportunity_recall)
            
            fold_precs.append(metrics.filter_precision)
            fold_evs.append(metrics.ev_per_trade)
            
        avg_rec = sum(fold_recalls) / len(fold_recalls) if fold_recalls else 0.0
        avg_prec = sum(fold_precs) / len(fold_precs) if fold_precs else 0.0
        avg_ev = sum(fold_evs) / len(fold_evs) if fold_evs else 0.0
        
        is_pos = avg_ev > 0
        passes = (avg_rec >= 0.50) and (avg_prec >= 0.50) and is_pos and (active_folds >= 1)
        status = 'PASS ALL GATES' if passes else 'FAIL'
        
        if passes:
            sweet_spots.append((thresh, avg_rec, avg_prec, avg_ev))
            
        print(f"{thresh:<8.1f} {active_folds:<12} {avg_rec:<16.1%} {avg_prec:<14.1%} {avg_ev:<+12.2f}% {str(is_pos):<12} {status}")

    print('-' * 85)
    if sweet_spots:
        print('\nSweet spots found:')
        for s in sweet_spots:
            print(f"  Threshold {s[0]:.1f} -> Recall: {s[1]:.1%}, Precision: {s[2]:.1%}, EV: {s[3]:+.2f}%")
        best = max(sweet_spots, key=lambda x: (x[3], x[1]))
        print(f"\nRecommended optimal threshold: {best[0]:.1f} (EV={best[3]:+.2f}%, Recall={best[1]:.1%})")
    else:
        print('\nNo threshold passed all gates with baseline weights. Need parameter search.')

if __name__ == '__main__':
    asyncio.run(main())
