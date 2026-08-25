"""
CLI Entry Point — Fase 5 Paper Trading
Usage:
  python -m src.paper_trading status    — Show active signal tracking status
  python -m src.paper_trading evaluate  [--window 24h]  — Run evaluation report
"""

import argparse
import asyncio

from src.database.client import db_manager
from src.utils.logger import logger


async def _cmd_status() -> None:
    """Show current paper trading status: total signals, pending, resolved."""
    await db_manager.initialize()

    all_signals = await db_manager.query("paper_signals", limit=5000)
    if not all_signals:
        print("\nℹ️  No paper trading signals recorded yet.")
        print("   Start the bot with: python src/main.py")
        return

    signals = [s for s in all_signals if not s.get("is_baseline")]
    baselines = [s for s in all_signals if s.get("is_baseline")]
    resolved = [s for s in signals if s.get("resolved_24h")]
    pending = [s for s in signals if not s.get("resolved_24h")]

    print(f"\n📊 Paper Trading Status")
    print(f"{'─'*40}")
    print(f"  Total Signals:    {len(signals)}")
    print(f"  Total Baselines:  {len(baselines)}")
    print(f"  Fully Resolved:   {len(resolved)}")
    print(f"  Pending (Active): {len(pending)}")
    print(f"{'─'*40}")

    if pending:
        print(f"\n  🔄 Active Signals (awaiting resolution):")
        for s in pending[:10]:
            score = s.get("opportunity_score", 0)
            sym = s.get("symbol", "?")
            mint = s.get("token_address", "")[:12]
            r5 = "✅" if s.get("resolved_5m") else "⏳"
            r15 = "✅" if s.get("resolved_15m") else "⏳"
            r1h = "✅" if s.get("resolved_1h") else "⏳"
            r4h = "✅" if s.get("resolved_4h") else "⏳"
            r24h = "✅" if s.get("resolved_24h") else "⏳"
            print(f"    ${sym:8s} ({mint}...) Score:{score:.0f} | 5m:{r5} 15m:{r15} 1h:{r1h} 4h:{r4h} 24h:{r24h}")

        if len(pending) > 10:
            print(f"    ... and {len(pending) - 10} more")

    print()


async def _cmd_evaluate(window: str) -> None:
    """Run evaluation report."""
    from src.paper_trading.evaluator import evaluate_paper_trading
    await db_manager.initialize()
    await evaluate_paper_trading(window=window)


async def _cmd_portfolio(balance: float, risk_pct: float, source: str, apply_filter: bool = True, threshold: float = 60.0) -> None:
    """Run Virtual Portfolio & Multi-Exit Simulation."""

    from src.paper_trading.portfolio_simulator import portfolio_simulator
    from src.backtest.replay_engine import _offline_safety_check, _build_raw_token_event
    from src.opportunity.scorer import opportunity_scorer
    await db_manager.initialize()

    signals: list[dict] = []
    if source == "backtest":
        raw_tokens = await db_manager.query("backtest_tokens", filters={"label": "not.is.null"}, limit=1000)
        if apply_filter:
            for row in raw_tokens:
                passed, _ = _offline_safety_check(row)
                if passed:
                    event = _build_raw_token_event(row)
                    if event:
                        try:
                            score_res = await opportunity_scorer.score_token(event)
                            if score_res.opportunity_score >= threshold:
                                signals.append(row)
                        except Exception:
                            signals.append(row)
        else:
            signals = raw_tokens
    else:
        # Default to paper signals
        paper_sigs = await db_manager.query("paper_signals", limit=1000)
        outcomes = await db_manager.query("signal_outcomes", limit=5000)
        
        # Merge outcomes into signals
        outcomes_by_sig = {}
        for o in outcomes:
            sid = o.get("signal_id")
            if sid not in outcomes_by_sig:
                outcomes_by_sig[sid] = {}
            w = o.get("time_window")
            outcomes_by_sig[sid][f"return_{w}"] = o.get("return_pct", 0.0)
            outcomes_by_sig[sid][f"peak_{w}"] = o.get("ath_return_pct", 0.0)
            outcomes_by_sig[sid]["ath_return_pct"] = max(outcomes_by_sig[sid].get("ath_return_pct", 0.0), o.get("ath_return_pct", 0.0))
            outcomes_by_sig[sid]["mae_pct"] = min(outcomes_by_sig[sid].get("mae_pct", 0.0), o.get("mae_pct", 0.0))
            outcomes_by_sig[sid]["status"] = o.get("status")

        for s in paper_sigs:
            if s.get("is_baseline"):
                continue
            sig_copy = dict(s)
            sig_id = s.get("id")
            if sig_id in outcomes_by_sig:
                sig_copy.update(outcomes_by_sig[sig_id])
            signals.append(sig_copy)

    if not signals:
        print(f"\nℹ️  No qualified trade signals found in '{source}' (threshold={threshold}).")
        print("   Semua token disaring oleh safety filter atau skor di bawah threshold.")
        return

    milestones = portfolio_simulator.calculate_milestones(signals)
    matrix_results = portfolio_simulator.run_matrix_simulation(
        signals=signals,
        initial_balance=balance,
        position_risk_pct=risk_pct
    )

    report_str = portfolio_simulator.render_cli_report(
        matrix_results=matrix_results,
        milestones=milestones,
        initial_balance=balance,
        position_risk_pct=risk_pct
    )
    print("\n" + report_str + "\n")



def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m src.paper_trading",
        description="MemeScanner Fase 5 & 6 — Paper Trading & Portfolio Simulator CLI"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # status
    subparsers.add_parser("status", help="Show current paper trading status")

    # evaluate
    p_eval = subparsers.add_parser("evaluate", help="Run performance evaluation report")
    p_eval.add_argument("--window", type=str, default="24h", choices=["5m", "15m", "1h", "4h", "24h"],
                        help="Outcome window to evaluate (default: 24h)")

    # portfolio
    p_port = subparsers.add_parser("portfolio", help="Run Virtual Portfolio & Multi-Exit Strategy Simulator")
    p_port.add_argument("--balance", type=float, default=10.0, help="Initial capital in USD (default: 10.0)")
    p_port.add_argument("--risk", type=float, default=2.0, help="Position sizing risk % per trade (default: 2.0)")
    p_port.add_argument("--source", type=str, default="paper", choices=["paper", "backtest"],
                        help="Data source: 'paper' or 'backtest' (default: paper)")
    p_port.add_argument("--threshold", type=float, default=60.0, help="Opportunity score threshold when using backtest source (default: 60.0)")
    p_port.add_argument("--unfiltered", action="store_true", help="Simulate blind trading without safety filters")


    args = parser.parse_args()

    if args.command == "status":
        asyncio.run(_cmd_status())
    elif args.command == "evaluate":
        asyncio.run(_cmd_evaluate(args.window))
    elif args.command == "portfolio":
        apply_filter = not args.unfiltered
        asyncio.run(_cmd_portfolio(args.balance, args.risk, args.source, apply_filter=apply_filter, threshold=args.threshold))



if __name__ == "__main__":
    main()

