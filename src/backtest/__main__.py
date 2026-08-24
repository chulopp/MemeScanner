"""
CLI Entry Point — Fase 4 Backtest & Walk-Forward Cross Validation
Usage:
  python -m src.backtest collect-live [--target N] [--duration S]
  python -m src.backtest resolve      [--limit N] [--force]
  python -m src.backtest run          [--threshold F] [--limit N]
  python -m src.backtest optimize     [--iterations N] [--folds K]
  python -m src.backtest report       [--output PATH]
"""

import argparse
import asyncio
import sys
from typing import Optional

from src.database.client import db_manager
from src.utils.logger import logger


async def _cmd_collect_live(target: int, duration: Optional[int]) -> None:
    from src.backtest.data_collector import collect_live_tokens
    await db_manager.initialize()
    n = await collect_live_tokens(target=target, duration_seconds=duration)
    logger.info(f"Live collection complete: {n} tokens captured at T=0 creation.")


async def _cmd_resolve(limit: int, force: bool) -> None:
    from src.backtest.labeler import resolve_due_tokens
    await db_manager.initialize()
    stats = await resolve_due_tokens(limit=limit, force_all_unresolved=force)
    print(f"\n🎯 24h Outcome Resolution Summary:")
    print(f"  Total Checked: {stats['total_checked']}")
    print(f"  Resolved:      {stats['resolved']}")
    print(f"  Runners (≥2x): {stats['runners']}")
    print(f"  Dead (≤-70%):  {stats['dead']}")
    print(f"  Neutral:       {stats['neutral']}")
    print(f"  Pending:       {stats['pending']}")


async def _cmd_run(threshold: float, limit: int) -> None:
    from src.backtest.replay_engine import run_replay
    await db_manager.initialize()
    metrics = await run_replay(opportunity_threshold=threshold, limit=limit)
    print(f"\n📊 Single Replay Results (threshold={threshold}):")
    print(f"  Dataset:           {metrics.dataset_size} tokens")
    print(f"  Runners:           {metrics.runner_count}")
    print(f"  Dead:              {metrics.dead_count}")
    print(f"  Neutral:           {metrics.neutral_count}")
    print(f"  Passed Safety:     {metrics.total_passed_safety}")
    print(f"  Filter Precision:  {metrics.filter_precision:.1%}")
    print(f"  Opp Recall:        {metrics.opportunity_recall:.1%}")
    print(f"  EV per Trade:      {metrics.ev_per_trade:+.2f}%")
    print(f"  EV Positive:       {'✅' if metrics.ev_positive else '❌'}")


async def _cmd_optimize(iterations: int, folds: int, limit: int) -> None:
    from src.backtest.optimizer import run_bayesian_optimization, SKOPT_AVAILABLE
    if not SKOPT_AVAILABLE:
        print("❌ scikit-optimize not installed. Run: pip install scikit-optimize")
        sys.exit(1)
    await db_manager.initialize()
    result = await run_bayesian_optimization(n_calls=iterations, n_splits=folds, limit=limit)
    if "error" in result:
        print(f"❌ Optimization error: {result['error']}")
        return

    print(f"\n🏆 Walk-Forward Cross Validation Complete!")
    print(f"  Out-of-Sample (OOS) EV/Trade:    {result.get('oos_ev_per_trade', 0):+.2f}%")
    print(f"  Out-of-Sample (OOS) Precision:   {result.get('oos_filter_precision', 0):.1%}")
    print(f"  Out-of-Sample (OOS) Recall:      {result.get('oos_opportunity_recall', 0):.1%}")
    print(f"  Optimal Parameters:              {result.get('best_params', {})}")
    print(f"  OOS EV Positive:                 {'✅ YA' if result.get('is_ev_positive_oos') else '❌ TIDAK'}")


async def _cmd_report(output: str) -> None:
    from src.backtest.reporter import generate_report
    await db_manager.initialize()
    path = await generate_report(output_path=output)
    if path:
        print(f"📄 Report saved to: {path}")
    else:
        print("⚠️  No data to report. Run backtest/optimize first.")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m src.backtest",
        description="MemeScanner Fase 4 — Rigorous Backtest & Walk-Forward Cross Validation CLI"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # collect / collect-live
    p_collect = subparsers.add_parser("collect-live", aliases=["collect"], help="Stream live token births from WebSocket at T=0")
    p_collect.add_argument("--target", type=int, default=200, help="Target token count (default: 200)")
    p_collect.add_argument("--duration", type=int, default=None, help="Max duration in seconds")

    # resolve / label
    p_resolve = subparsers.add_parser("resolve", aliases=["label"], help="Resolve 24h outcomes for mature tokens")
    p_resolve.add_argument("--limit", type=int, default=500, help="Max tokens to resolve (default: 500)")
    p_resolve.add_argument("--force", action="store_true", help="Force resolution even if 24h not fully elapsed")

    # run
    p_run = subparsers.add_parser("run", help="Run baseline replay simulation")
    p_run.add_argument("--threshold", type=float, default=60.0, help="Opportunity score threshold (default: 60.0)")
    p_run.add_argument("--limit", type=int, default=500, help="Max tokens to replay (default: 500)")

    # optimize
    p_opt = subparsers.add_parser("optimize", help="Run 5-Fold Walk-Forward Bayesian Parameter Optimization")
    p_opt.add_argument("--iterations", type=int, default=30, help="Number of Bayesian evaluations (default: 30)")
    p_opt.add_argument("--folds", type=int, default=5, help="Number of Walk-Forward folds (default: 5)")
    p_opt.add_argument("--limit", type=int, default=500, help="Max tokens to evaluate (default: 500)")

    # report
    p_rep = subparsers.add_parser("report", help="Generate Markdown DoD report")
    p_rep.add_argument("--output", type=str, default="backtest_results/report.md", help="Output file path")

    args = parser.parse_args()

    if args.command in ["collect-live", "collect"]:
        asyncio.run(_cmd_collect_live(args.target, args.duration))
    elif args.command in ["resolve", "label"]:
        asyncio.run(_cmd_resolve(args.limit, args.force))
    elif args.command == "run":
        asyncio.run(_cmd_run(args.threshold, args.limit))
    elif args.command == "optimize":
        asyncio.run(_cmd_optimize(args.iterations, args.folds, args.limit))
    elif args.command == "report":
        asyncio.run(_cmd_report(args.output))


if __name__ == "__main__":
    main()
