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


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m src.paper_trading",
        description="MemeScanner Fase 5 — Paper Trading CLI"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # status
    subparsers.add_parser("status", help="Show current paper trading status")

    # evaluate
    p_eval = subparsers.add_parser("evaluate", help="Run performance evaluation report")
    p_eval.add_argument("--window", type=str, default="24h", choices=["5m", "15m", "1h", "4h", "24h"],
                        help="Outcome window to evaluate (default: 24h)")

    args = parser.parse_args()

    if args.command == "status":
        asyncio.run(_cmd_status())
    elif args.command == "evaluate":
        asyncio.run(_cmd_evaluate(args.window))


if __name__ == "__main__":
    main()
