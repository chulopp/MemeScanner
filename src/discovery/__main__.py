#!/usr/bin/env python
"""
src/discovery/__main__.py
==========================
CLI entry point for Smart Money Discovery Engine.

Commands:
    python -m src.discovery run [OPTIONS]
    python -m src.discovery test-pnl --wallet <ADDRESS>
    python -m src.discovery test-lineage --wallet <ADDRESS>
    python -m src.discovery audit-wallet <ADDRESS> [--limit N]
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from rich.console import Console
from rich.table import Table

console = Console()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Smart Money Wallet Discovery Engine — MemeScanner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # -----------------------------------------------------------------------
    # Command: run
    # -----------------------------------------------------------------------
    run_parser = subparsers.add_parser("run", help="Run the discovery pipeline")
    run_parser.add_argument(
        "--max-runners", type=int, default=5000,
        help="Number of runner tokens to scrape from DexScreener (default: 5000)"
    )
    run_parser.add_argument(
        "--min-hits", type=int, default=3,
        help="Minimum distinct runner hits to be a candidate (default: 3)"
    )
    run_parser.add_argument(
        "--min-entry-sol", type=float, default=1.0,
        help="Minimum SOL per early buy entry to qualify as high-conviction (default: 1.0 SOL)"
    )
    run_parser.add_argument(
        "--min-trades", type=int, default=20,
        help="Minimum trade count in last 90 days for new wallets (default: 20)"
    )
    run_parser.add_argument(
        "--lineage-min-trades", type=int, default=5,
        help="Relaxed trade count in last 90 days for wallets with SM lineage (default: 5)"
    )
    run_parser.add_argument(
        "--early-window", type=int, default=600,
        help="Seconds after token launch considered 'early' (default: 600 = 10 min)"
    )
    run_parser.add_argument(
        "--max-age-hours", type=float, default=168.0,
        help="Maximum age of runner tokens in hours (default: 168.0 = 7 days)"
    )
    run_parser.add_argument(
        "--batch-size", type=int, default=50,
        help="Tokens per batch (default: 50)"
    )
    run_parser.add_argument(
        "--dry-run", action="store_true",
        help="Run pipeline but do NOT write results to database"
    )
    run_parser.add_argument(
        "--resume", action="store_true",
        help="Resume from checkpoint (skip already evaluated wallets and traced tokens)"
    )
    run_parser.add_argument(
        "--verbose", action="store_true",
        help="Enable verbose debug logging"
    )

    # -----------------------------------------------------------------------
    # Command: test-pnl
    # -----------------------------------------------------------------------
    pnl_parser = subparsers.add_parser("test-pnl", help="Test P&L lookup on a single wallet")
    pnl_parser.add_argument("--wallet", type=str, required=True, help="Wallet address to test")

    # -----------------------------------------------------------------------
    # Command: test-lineage
    # -----------------------------------------------------------------------
    lin_parser = subparsers.add_parser("test-lineage", help="Test funding lineage check on a single wallet")
    lin_parser.add_argument("--wallet", type=str, required=True, help="Wallet address to check")

    # -----------------------------------------------------------------------
    # Command: audit-wallet
    # -----------------------------------------------------------------------
    audit_parser = subparsers.add_parser(
        "audit-wallet",
        help="Audit a Smart Money wallet's full on-chain trade history (BUY/SELL reconstruction)"
    )
    audit_parser.add_argument("wallet", type=str, help="Solana wallet address to audit")
    audit_parser.add_argument(
        "--limit", type=int, default=100,
        help="Max transactions to fetch from Helius (default: 100)"
    )

    return parser.parse_args()


async def _cmd_run(args: argparse.Namespace) -> None:
    from src.discovery.smart_money_discovery import DiscoveryConfig, DiscoveryOrchestrator

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


async def _cmd_test_pnl(wallet: str) -> None:
    from src.discovery.pnl_provider import pnl_provider

    console.print(f"\n[bold cyan]🔍 Testing P&L fetch for:[/bold cyan] [yellow]{wallet}[/yellow]")
    res = await pnl_provider.get_wallet_pnl(wallet)

    table = Table(title="Wallet P&L Result", show_header=True)
    table.add_column("Metric", style="bold")
    table.add_column("Value")

    table.add_row("Address", res.wallet_address)
    table.add_row("Provider", res.provider)
    table.add_row("Status", "[green]SUCCESS[/green]" if res.is_successful else "[red]FAILED[/red]")
    table.add_row("Realized PnL (90d)", f"{res.realized_pnl_90d_sol:+.2f} SOL")
    table.add_row("Realized PnL (30d)", f"{res.realized_pnl_30d_sol:+.2f} SOL")
    table.add_row("Total Trades (90d)", str(res.total_trades_90d))
    table.add_row("Total Volume (SOL)", f"{res.total_volume_sol:.2f} SOL")
    table.add_row("Win Rate", f"{res.win_rate_pct:.1f}%")

    console.print(table)


async def _cmd_test_lineage(wallet: str) -> None:
    from src.discovery.lineage_checker import lineage_checker
    from src.database.client import db_manager

    await db_manager.initialize()
    known = await db_manager.get_active_smart_money_addresses()

    console.print(f"\n[bold cyan]🔗 Checking Lineage for:[/bold cyan] [yellow]{wallet}[/yellow]")
    console.print(f"Loaded [bold]{len(known)}[/bold] known active smart money addresses from database.")

    res = await lineage_checker.check(wallet, known)

    table = Table(title="Lineage Check Result", show_header=True)
    table.add_column("Metric", style="bold")
    table.add_column("Value")

    table.add_row("Address", res.wallet_address)
    table.add_row("Has SM Lineage", "[green]YES[/green]" if res.funded_by_known_smart_money else "[yellow]NO[/yellow]")
    table.add_row("Parent Wallet", res.lineage_parent_wallet or "None")
    table.add_row("Hop Distance", str(res.lineage_hop_distance))

    console.print(table)


async def _cmd_audit_wallet(wallet: str, limit: int) -> None:
    from src.discovery.wallet_replay_audit import audit_wallet, print_audit_report

    report = await audit_wallet(wallet, limit)
    print_audit_report(report)


def main() -> None:
    args = parse_args()

    if getattr(args, "verbose", False):
        import logging
        logging.basicConfig(level=logging.DEBUG)

    if args.command == "run":
        asyncio.run(_cmd_run(args))
    elif args.command == "test-pnl":
        asyncio.run(_cmd_test_pnl(args.wallet))
    elif args.command == "test-lineage":
        asyncio.run(_cmd_test_lineage(args.wallet))
    elif args.command == "audit-wallet":
        asyncio.run(_cmd_audit_wallet(args.wallet, args.limit))


if __name__ == "__main__":
    main()
