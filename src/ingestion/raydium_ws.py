import asyncio
import json
from typing import Callable, Awaitable, Optional
import websockets
from src.config import settings
from src.ingestion.schemas import RawTokenEvent
from src.utils.solana_rpc import solana_rpc
from src.utils.logger import logger

RAYDIUM_AMM_V4_PROGRAM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
RAYDIUM_CPMM_PROGRAM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
WSOL_MINT = "So11111111111111111111111111111111111111112"


class RaydiumListener:
    """Real-time WebSocket listener for new Raydium AMM pools via Helius WebSocket."""

    def __init__(self, on_token_callback: Callable[[RawTokenEvent], Awaitable[None]]):
        self.ws_url = settings.helius_ws_url
        self.on_token_callback = on_token_callback
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        if not settings.helius_api_key:
            logger.warning("No Helius API Key configured. Raydium WebSocket listener skipped.")
            return

        self._running = True
        self._task = asyncio.create_task(self._listen_loop())
        logger.info("Raydium AMM WebSocket listener started.")

    async def _listen_loop(self):
        backoff = 1.0
        while self._running:
            try:
                logger.info(f"Connecting to Helius WS for Raydium pool detection...")
                async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=20) as ws:
                    # Subscribe to Raydium AMM V4 logs
                    subscribe_payload = json.dumps({
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "logsSubscribe",
                        "params": [
                            {"mentions": [RAYDIUM_AMM_V4_PROGRAM]},
                            {"commitment": "confirmed"}
                        ]
                    })
                    await ws.send(subscribe_payload)
                    logger.info("Subscribed to Raydium AMM logsSubscribe stream.")
                    backoff = 1.0

                    async for message in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(message)
                            params = data.get("params", {})
                            result = params.get("result", {})
                            value = result.get("value", {})
                            logs = value.get("logs", [])
                            signature = value.get("signature")

                            # Check if logs indicate a pool initialization
                            is_init = any(
                                "initialize2" in log.lower() or "init_pc_amount" in log.lower() or "instruction: initialize" in log.lower()
                                for log in logs
                            )

                            if is_init and signature:
                                asyncio.create_task(self._process_raydium_init(signature))

                        except Exception as parse_err:
                            logger.debug(f"Error parsing Raydium log message: {parse_err}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Raydium Helius WS disconnected: {e}. Reconnecting in {backoff:.1f}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)

    async def _process_raydium_init(self, signature: str):
        """Fetches transaction details to extract the token mint and pool metadata."""
        try:
            tx_res = await solana_rpc._rpc_call(
                "getTransaction",
                [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
            )
            if not tx_res:
                return

            tx = tx_res.get("transaction", {})
            message = tx.get("message", {})
            account_keys = message.get("accountKeys", [])

            # Extract mints from postTokenBalances
            meta = tx_res.get("meta", {})
            post_token_balances = meta.get("postTokenBalances", [])

            candidate_mints = []
            for b in post_token_balances:
                mint = b.get("mint")
                if mint and mint != WSOL_MINT and mint not in candidate_mints:
                    candidate_mints.append(mint)

            if not candidate_mints and account_keys:
                # Fallback to checking account keys for token mints
                for acc in account_keys:
                    pub = acc.get("pubkey") if isinstance(acc, dict) else acc
                    if pub and pub not in [RAYDIUM_AMM_V4_PROGRAM, WSOL_MINT]:
                        candidate_mints.append(pub)

            if candidate_mints:
                target_mint = candidate_mints[0]
                # Fetch basic mint info
                mint_info = await solana_rpc.get_mint_info(target_mint)
                signer = account_keys[0].get("pubkey") if isinstance(account_keys[0], dict) else str(account_keys[0])

                event = RawTokenEvent(
                    token_address=target_mint,
                    symbol="RAY_TOKEN",
                    name="Raydium New Pair",
                    deployer_wallet_address=signer,
                    launch_venue="raydium",
                    total_supply=float(mint_info.get("supply", 1_000_000_000)),
                    raw_payload={"signature": signature, "mint_info": mint_info}
                )
                await self.on_token_callback(event)

        except Exception as e:
            logger.debug(f"Error processing Raydium pool initialization {signature}: {e}")

    async def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Raydium AMM WebSocket listener stopped.")
