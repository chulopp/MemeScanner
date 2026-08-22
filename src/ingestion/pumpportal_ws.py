import asyncio
import json
from typing import Callable, Awaitable, Optional
import websockets
from src.config import settings
from src.ingestion.schemas import RawTokenEvent
from src.utils.logger import logger


class PumpPortalListener:
    """Real-time WebSocket listener for new tokens on Pump.fun via PumpPortal."""

    def __init__(self, on_token_callback: Callable[[RawTokenEvent], Awaitable[None]]):
        self.ws_url = settings.pumpportal_ws_url
        self.on_token_callback = on_token_callback
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._listen_loop())
        logger.info("PumpPortal WebSocket listener started.")

    async def _listen_loop(self):
        backoff = 1.0
        while self._running:
            try:
                logger.info(f"Connecting to PumpPortal WS: {self.ws_url}...")
                async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=20) as ws:
                    # Subscribe to new token creation events
                    subscribe_payload = json.dumps({"method": "subscribeNewToken"})
                    await ws.send(subscribe_payload)
                    logger.info("Subscribed to Pump.fun new tokens stream (subscribeNewToken).")
                    backoff = 1.0  # Reset backoff on successful connection

                    async for message in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(message)
                            # Parse token event
                            mint = data.get("mint")
                            if mint:
                                event = RawTokenEvent(
                                    token_address=mint,
                                    symbol=data.get("symbol", "UNKNOWN"),
                                    name=data.get("name", "Unknown Token"),
                                    deployer_wallet_address=data.get("traderPublicKey"),
                                    launch_venue="pump_fun",
                                    initial_buy_amount=float(data.get("initialBuy", 0.0)),
                                    total_supply=1_000_000_000.0,
                                    initial_sol_liquidity=float(data.get("vSolInBondingCurve", 30.0)),
                                    bonding_curve_address=data.get("bondingCurveKey"),
                                    raw_payload=data
                                )
                                asyncio.create_task(self.on_token_callback(event))
                        except Exception as parse_err:
                            logger.debug(f"Error parsing PumpPortal message: {parse_err}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"PumpPortal WS disconnected: {e}. Reconnecting in {backoff:.1f}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)

    async def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("PumpPortal WebSocket listener stopped.")
