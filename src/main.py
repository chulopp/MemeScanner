import argparse
import asyncio
import signal
import sys
from typing import Optional

# Ensure UTF-8 output on Windows console
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from src.config import settings
from src.ingestion.manager import IngestionManager
from src.ingestion.schemas import RawTokenEvent
from src.filters.pipeline import filter_pipeline
from src.filters.schemas import SafetyCheckResult
from src.filters.funding_graph import funding_tracer
from src.opportunity.smart_money import smart_money_engine
from src.database.client import db_manager
from src.database.models import SmartMoneyProfileModel
from src.utils.price_feed import price_feed
from src.utils.logger import logger, console, print_token_table, mask_url
from src.utils.solana_rpc import solana_rpc
from src.paper_trading.outcome_worker import outcome_worker
from src.paper_trading import price_fetcher as pt_price_fetcher


class MemeScannerApp:
    """Main application orchestrator for Solana Meme Coin Safety & Signal Bot."""

    def __init__(self, smoke_test: bool = False, duration: int = 60):
        self.smoke_test = smoke_test
        self.duration = duration
        self.processed_tokens: list[dict] = []
        self.ingestion_manager = IngestionManager(self._on_token_ingested)
        self._shutdown_event = asyncio.Event()
        self._evaluator_task: Optional[asyncio.Task] = None

    async def _on_token_ingested(self, event: RawTokenEvent):
        """Callback invoked whenever a new token is ingested."""
        # Route to safety filter pipeline
        result: SafetyCheckResult = await filter_pipeline.process_token(event)

        # Collect for summary reporting
        self.processed_tokens.append({
            "symbol": event.symbol,
            "name": event.name,
            "token_address": event.token_address,
            "launch_venue": event.launch_venue,
            "status": "PASSED_SAFETY" if result.filter_pass else "REJECTED",
            "opportunity_score": result.opportunity_score,
            "dev_holding_pct": result.dev_holding_pct,
            "sniper_bundle_pct": result.sniper_bundle_pct,
            "lp_lock_pct": result.lp_lock_pct,
            "instant_scalp_flags_count": result.instant_scalp_flags_count,
            "rejection_reason": result.rejection_reason
        })

    async def _periodic_smart_money_evaluator(self):
        """Background periodic worker evaluating wallet promotion & demotion every 24h."""
        while not self._shutdown_event.is_set():
            try:
                wallets_data = await db_manager.get_smart_money_wallets(active_only=False)
                if wallets_data:
                    promoted = 0
                    demoted = 0
                    for w in wallets_data:
                        try:
                            profile = SmartMoneyProfileModel(**w)
                            original_tier = profile.tier
                            evaluated = await smart_money_engine.evaluate_promotion_and_demotion(profile)
                            if evaluated.tier != original_tier:
                                await db_manager.upsert_smart_money_wallet(evaluated)
                                if evaluated.tier == "ACTIVE":
                                    promoted += 1
                                elif evaluated.tier == "DEMOTED":
                                    demoted += 1
                        except Exception as e:
                            logger.debug(f"Error evaluating wallet profile: {e}")

                    if promoted > 0 or demoted > 0:
                        logger.info(f"🔄 Smart Money Evaluator: {promoted} promoted to ACTIVE, {demoted} demoted.")
            except Exception as loop_err:
                logger.debug(f"Smart money evaluator loop error: {loop_err}")

            # Run every 24 hours (or stop on shutdown)
            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=86400.0)
            except asyncio.TimeoutError:
                pass

    async def run(self):
        console.print("\n[bold cyan]========================================================[/bold cyan]")
        console.print("[bold yellow]Solana Meme Coin Safety & Signal Bot (Fase 0, 1, 2 & 3)[/bold yellow]")
        console.print(f"[dim]Helius RPC: {mask_url(settings.helius_rpc_url)}[/dim]")
        console.print(f"[dim]PumpPortal WS: {settings.pumpportal_ws_url}[/dim]")
        console.print("[bold cyan]========================================================[/bold cyan]\n")

        # Start background periodic smart money evaluator
        self._evaluator_task = asyncio.create_task(self._periodic_smart_money_evaluator())

        # Start Fase 5 outcome worker (APScheduler)
        try:
            await outcome_worker.start()
            logger.info("📡 Fase 5 Paper Trading outcome worker started.")
        except Exception as ow_err:
            logger.warning(f"Outcome worker start skipped: {ow_err}")

        # Start ingestion listeners
        await self.ingestion_manager.start()

        if self.smoke_test:
            logger.info(f"Running in SMOKE TEST mode for {self.duration} seconds...")
            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=float(self.duration))
            except asyncio.TimeoutError:
                logger.info("Smoke test duration completed.")
        else:
            logger.info("Bot running live. Press Ctrl+C to stop.")
            await self._shutdown_event.wait()

        # Stop ingestion and cleanup
        await self.shutdown()

    async def shutdown(self):
        logger.info("Shutting down MemeScanner...")
        self._shutdown_event.set()
        if self._evaluator_task and not self._evaluator_task.done():
            self._evaluator_task.cancel()
        await self.ingestion_manager.stop()
        await funding_tracer.close()
        await solana_rpc.close()
        await price_feed.close()
        try:
            await outcome_worker.stop()
            await pt_price_fetcher.close()
        except Exception:
            pass

        # Print summary table if any tokens were processed
        if self.processed_tokens:
            console.print("\n")
            print_token_table(self.processed_tokens)
            passed_count = sum(1 for t in self.processed_tokens if t["status"] == "PASSED_SAFETY")
            rejected_count = sum(1 for t in self.processed_tokens if t["status"] == "REJECTED")
            console.print(
                f"\n[bold]Total Ingested: {len(self.processed_tokens)} | "
                f"[green]Passed: {passed_count}[/green] | "
                f"[red]Rejected: {rejected_count}[/red][/bold]\n"
            )
        else:
            logger.info("No tokens were ingested during this window.")


def main():
    parser = argparse.ArgumentParser(description="Solana Meme Coin Safety & Ingestion Bot")
    parser.add_argument("--smoke-test", action="store_true", help="Run in smoke test mode for a fixed duration")
    parser.add_argument("--duration", type=int, default=30, help="Duration in seconds for smoke test (default: 30)")
    args = parser.parse_args()

    app = MemeScannerApp(smoke_test=args.smoke_test, duration=args.duration)

    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        logger.info("Process interrupted by user.")
    except Exception as e:
        logger.exception(f"Fatal error in main application: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
