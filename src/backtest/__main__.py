"""
CLI Entry Point — Fase 4 Backtest
Usage:
  python -m src.backtest collect [--target N]
  python -m src.backtest label   [--limit N]
  python -m src.backtest run     [--threshold F] [--limit N]
  python -m src.backtest optimize [--iterations N] [--limit N]
  python -m src.backtest report  [--output PATH]
"""

import argparse
import asyncio
import sys

from src.database.client import db_manager
from src.utils.logger import logger


async def _cmd_collect(target: int) -> None:
    from src.backtest.data_collector import collect_historical_tokens
    await db_manager.initialize()
    n = await collect_historical_tokens(target=target)
    logger.info(f"Collection done: {n} tokens stored.")


async def _cmd_label(limit: int) -> None:
    from src.backtest.labeler import label_backtest_tokens
    await db_manager.initialize()
    stats = await label_backtest_tokens(limit=limit)
    print(f"\n🏷️  Labeling Summary:")
    print(f"  Total:   {stats['total']}")
    print(f"  Labeled: {stats['labeled']}")
    print(f"  Skipped: {stats['skipped']}")
    print(f"  Runners: {stats['runners']}")
    print(f"  Dead:    {stats['dead']}")
    print(f"  Neutral: {stats['neutral']}")


async def _cmd_run(threshold: float, limit: int) -> None:
    from src.backtest.replay_engine import run_replay
    await db_manager.initialize()
    metrics = await run_replay(opportunity_threshold=threshold, limit=limit)
    print(f"\n📊 Backtest Results (threshold={threshold}):")
    print(f"  Dataset:           {metrics.dataset_size} tokens")
    print(f"  Runners:           {metrics.runner_count}")
    print(f"  Dead:              {metrics.dead_count}")
    print(f"  Neutral:           {metrics.neutral_count}")
    print(f"  Passed Safety:     {metrics.total_passed_safety}")
    print(f"  Filter Precision:  {metrics.filter_precision:.1%}")
    print(f"  Opp Recall:        {metrics.opportunity_recall:.1%}")
    print(f"  EV per Trade:      {metrics.ev_per_trade:+.2f}%")
    print(f"  EV Positive:       {'✅' if metrics.ev_positive else '❌'}")


async def _cmd_optimize(iterations: int, limit: int) -> None:
    from src.backtest.optimizer import run_bayesian_optimization, SKOPT_AVAILABLE
    if not SKOPT_AVAILABLE:
        print("❌ scikit-optimize not installed. Run: pip install scikit-optimize")
        sys.exit(1)
    await db_manager.initialize()
    result = await run_bayesian_optimization(n_calls=iterations, limit=limit)
    print(f"\n🎯 Optimization Complete!")
    print(f"  Best EV/Trade:       {result.get('best_ev_per_trade', 0):+.2f}%")
    print(f"  Best Filter Prec:    {result.get('best_filter_precision', 0):.1%}")
    print(f"  Total Evaluations:   {result.get('total_evaluations', 0)}")
    print(f"  Best Params:         {result.get('best_params', {})}")


async def _cmd_report(output: str) -> None:
    from src.backtest.reporter import generate_report
    await db_manager.initialize()
    path = await generate_report(output_path=output)
    if path:
        print(f"📄 Report saved to: {path}")
    else:
        print("⚠️  No data to report. Run backtest first.")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m src.backtest",
        description="MemeScanner Fase 4 — Backtest CLI"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # collect
    p_collect = subparsers.add_parser("collect", help="Collect historical tokens from DexScreener")
    p_collect.add_argument("--target", type=int, default=300, help="Target token count (default: 300)")

    # label
    p_label = subparsers.add_parser("label", help="Label tokens by 24h price action")
    p_label.add_argument("--limit", type=int, default=500, help="Max tokens to label (default: 500)")

    # run
    p_run = subparsers.add_parser("run", help="Run single backtest with current parameters")
    p_run.add_argument("--threshold", type=float, default=60.0, help="Opportunity score threshold (HYPOTHESIS_INIT: 60)")
    p_run.add_argument("--limit", type=int, default=500, help="Max tokens to replay (default: 500)")

    # optimize
    p_opt = subparsers.add_parser("optimize", help="Run Bayesian parameter optimization")
    p_opt.add_argument("--iterations", type=int, default=50, help="Number of Bayesian evaluations (default: 50)")
    p_opt.add_argument("--limit", type=int, default=500, help="Max tokens per evaluation (default: 500)")

    # report
    p_rep = subparsers.add_parser("report", help="Generate Markdown DoD report")
    p_rep.add_argument("--output", type=str, default="backtest_results/report.md", help="Output file path")

    args = parser.parse_args()

    if args.command == "collect":
        asyncio.run(_cmd_collect(args.target))
    elif args.command == "label":
        asyncio.run(_cmd_label(args.limit))
    elif args.command == "run":
        asyncio.run(_cmd_run(args.threshold, args.limit))
    elif args.command == "optimize":
        asyncio.run(_cmd_optimize(args.iterations, args.limit))
    elif args.command == "report":
        asyncio.run(_cmd_report(args.output))


if __name__ == "__main__":
    main()
