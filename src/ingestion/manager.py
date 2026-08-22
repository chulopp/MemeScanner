import asyncio
from typing import Callable, Awaitable, Optional
from src.ingestion.schemas import RawTokenEvent
from src.ingestion.pumpportal_ws import PumpPortalListener
from src.ingestion.raydium_ws import RaydiumListener
from src.utils.redis_client import redis_manager
from src.database.client import db_manager
from src.database.models import TokenModel
from src.utils.logger import logger


class IngestionManager:
    """Orchestrates Pump.fun and Raydium listeners with Redis deduplication and DB logging."""

    def __init__(self, on_token_event: Optional[Callable[[RawTokenEvent], Awaitable[None]]] = None):
        self.on_token_event = on_token_event
        self.pump_listener = PumpPortalListener(self._handle_raw_token)
        self.raydium_listener = RaydiumListener(self._handle_raw_token)
        self._running = False

    async def _handle_raw_token(self, event: RawTokenEvent):
        """Deduplicates token and records to database, then triggers filter pipeline."""
        is_new = await redis_manager.check_and_set_token(event.token_address)
        if not is_new:
            logger.debug(f"Duplicate token ignored: {event.token_address}")
            return

        logger.info(
            f"🎯 New Token Ingested: [{event.launch_venue.upper()}] "
            f"{event.symbol} ({event.name}) | Mint: {event.token_address}"
        )

        # Save to database
        token_record = TokenModel(
            token_address=event.token_address,
            symbol=event.symbol,
            name=event.name,
            deployer_wallet_address=event.deployer_wallet_address,
            launch_venue=event.launch_venue,
            initial_metadata=event.raw_payload
        )
        await db_manager.insert_token(token_record)

        # Forward to filter callback if registered
        if self.on_token_event:
            try:
                await self.on_token_event(event)
            except Exception as e:
                logger.error(f"Error in token processing callback for {event.token_address}: {e}")

    async def start(self):
        self._running = True
        await redis_manager.connect()
        db_manager.connect()
        await self.pump_listener.start()
        await self.raydium_listener.start()
        logger.info("Ingestion Manager running (Pump.fun & Raydium parallel streams).")

    async def stop(self):
        self._running = False
        await self.pump_listener.stop()
        await self.raydium_listener.stop()
        await redis_manager.close()
        logger.info("Ingestion Manager stopped.")
