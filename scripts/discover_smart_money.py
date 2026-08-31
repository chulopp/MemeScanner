#!/usr/bin/env python
"""
scripts/discover_smart_money.py
================================
CLI entry point for the Smart Money Wallet Discovery pipeline.

Usage:
    python scripts/discover_smart_money.py [OPTIONS]

Options:
    --max-runners INT    Number of runner tokens to scrape (default: 5000)
    --min-hits INT       Minimum runner hits to be a candidate (default: 3)
    --min-ratio FLOAT    Minimum runner/total ratio (default: 0.15)
    --min-sol FLOAT      Minimum SOL balance (default: 50)
    --min-sample INT     Min classifiable early buys for negative control (default: 10)
    --early-window INT   Seconds after launch considered "early" (default: 600)
    --dry-run            Do not write to database
    --resume             Resume from last checkpoint (skip already-traced tokens)
    --verbose            Enable verbose debug logging
    --batch-size INT     Tokens per API batch (default: 50)

Example:
    python scripts/discover_smart_money.py --max-runners 5000 --resume
    python scripts/discover_smart_money.py --max-runners 50 --dry-run --verbose
"""

import argparse
import asyncio
import sys
import os

# Ensure project root is on path
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
        "--min-ratio", type=float, default=0.15,
        help="Minimum runner-to-classifiable ratio (default: 0.15 = 15%%)"
    )
    parser.add_argument(
        "--min-sol", type=float, default=50.0,
        help="Minimum SOL balance (default: 50)"
    )
    parser.add_argument(
        "--min-sample", type=int, default=10,
        help="Minimum classifiable early buys for negative control (default: 10)"
    )
    parser.add_argument(
        "--early-window", type=int, default=600,
        help="Seconds after token launch considered 'early' (default: 600 = 10 min)"
    )
    parser.add_argument(
        "--max-age-hours", type=float, default=168.0,
        help="Maximum age of runner tokens in hours (default: 168.0 = 7 days / 1 week)"
    )
    parser.add_argument(
        "--batch-size", type=int, default=50,
        help="Number of tokens per processing batch (default: 50)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run pipeline but do NOT write results to Supabase"
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Resume from last checkpoint (skip already-traced tokens/wallets)"
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
        min_hit_ratio=args.min_ratio,
        min_sol_balance=args.min_sol,
        min_classifiable_buys=args.min_sample,
        early_window_seconds=args.early_window,
        max_token_age_hours=args.max_age_hours,
        batch_size=args.batch_size,
    )

    orchestrator = DiscoveryOrchestrator(config)
    await orchestrator.run(resume=args.resume, dry_run=args.dry_run)


if __name__ == "__main__":
    asyncio.run(main())
