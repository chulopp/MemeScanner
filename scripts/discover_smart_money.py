#!/usr/bin/env python
"""
scripts/discover_smart_money.py
================================
CLI entry point for the Smart Money Wallet Discovery pipeline.

Usage:
    python scripts/discover_smart_money.py [OPTIONS]

Options:
    --max-runners INT          Number of runner tokens to scrape (default: 5000)
    --min-hits INT             Minimum runner hits to be a candidate (default: 3)
    --min-entry-sol FLOAT      Minimum SOL per buy entry (default: 1.0 SOL)
    --min-trades INT           Minimum trades in last 90d (default: 20)
    --lineage-min-trades INT   Relaxed trades in last 90d for SM-lineage wallets (default: 5)
    --early-window INT         Seconds after launch considered "early" (default: 600)
    --max-age-hours FLOAT      Max token age in hours (default: 168.0 = 7 days)
    --dry-run                  Do not write to database
    --resume                   Resume from last checkpoint
    --verbose                  Enable verbose debug logging
    --batch-size INT           Tokens per API batch (default: 50)
"""

import argparse
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.discovery.smart_money_discovery import DiscoveryConfig, DiscoveryOrchestrator
from src.utils.logger import logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Smart Money Wallet Discovery — MemeScanner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--max-runners", type=int, default=5000,
        help="Number of runner tokens to scrape from DexScreener (default: 5000)"
    )
    parser.add_argument(
        "--min-hits", type=int, default=3,
        help="Minimum distinct runner hits to be a candidate (default: 3)"
    )
    parser.add_argument(
        "--min-entry-sol", type=float, default=1.0,
        help="Minimum SOL per early buy entry to qualify as high-conviction (default: 1.0 SOL)"
    )
    parser.add_argument(
        "--min-trades", type=int, default=20,
        help="Minimum trade count in last 90 days for new wallets (default: 20)"
    )
    parser.add_argument(
        "--lineage-min-trades", type=int, default=5,
        help="Relaxed trade count in last 90 days for wallets with SM lineage (default: 5)"
    )
    parser.add_argument(
        "--early-window", type=int, default=600,
        help="Seconds after token launch considered 'early' (default: 600 = 10 min)"
    )
    parser.add_argument(
        "--max-age-hours", type=float, default=168.0,
        help="Maximum age of runner tokens in hours (default: 168.0 = 7 days)"
    )
    parser.add_argument(
        "--batch-size", type=int, default=50,
        help="Number of tokens per processing batch (default: 50)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run pipeline but do NOT write results to database"
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Resume from last checkpoint"
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Enable verbose debug logging"
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    if args.verbose:
        import logging
        logging.basicConfig(level=logging.DEBUG)

    config = DiscoveryConfig(
        max_runners=args.max_runners,
        min_runner_hits=args.min_hits,
        min_entry_sol=args.min_entry_sol,
        min_trades_90d=args.min_trades,
        lineage_min_trades_90d=args.lineage_min_trades,
        early_window_seconds=args.early_window,
        max_token_age_hours=args.max_age_hours,
        batch_size=args.batch_size,
    )

    orchestrator = DiscoveryOrchestrator(config)
    await orchestrator.run(resume=args.resume, dry_run=args.dry_run)


if __name__ == "__main__":
    asyncio.run(main())

