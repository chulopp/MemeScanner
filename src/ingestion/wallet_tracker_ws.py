"""
Wallet Tracker WebSocket Listener — Pintu B: Wallet-First Ingestion Stream

Connects to Helius Enhanced WebSocket (transactionSubscribe) to monitor every
on-chain BUY transaction from Smart Money wallets tracked in `smart_money_profiles`.

Flow:
  1. On startup, load all active wallets from Supabase (is_active=True).
  2. Open Helius WebSocket, subscribe to transaction notifications for each tracked wallet.
  3. For each incoming transaction:
     a. Parse swap events to detect BUY direction (SOL -> Token).
     b. Apply conviction gate: SOL spent >= MIN_CONVICTION_SOL (0.5 SOL).
     c. Emit a RawTokenEvent with source="WALLET_TRACKER" and Smart Money attribution.
     d. Downstream: event flows into the standard Safety Filter pipeline (same as Pintu A).
  4. Periodically re-sync wallet list from DB (WALLET_SYNC_INTERVAL_S) to catch new wallets
     without restarting the process.
"""

import asyncio
import json
from datetime import datetime, timezone
from typing import Callable, Awaitable, Optional

import httpx
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from src.config import settings
from src.database.client import db_manager
from src.ingestion.schemas import RawTokenEvent
from src.utils.logger import logger

# Minimum SOL spent by a Smart Money wallet to qualify as a conviction signal
MIN_CONVICTION_SOL = 0.5

# How often to re-sync the active wallet list from Supabase (seconds)
WALLET_SYNC_INTERVAL_S = 60

# Known non-token mints to skip (WSOL, native placeholders)
_SKIP_MINTS = {
    "So11111111111111111111111111111111111111112",
    "So11111111111111111111111111111111111111111",
}

# Max wallets to subscribe per WebSocket connection (Helius limit guidance)
MAX_SUBSCRIPTIONS = 100

TokenCallback = Callable[[RawTokenEvent], Awaitable[None]]


class WalletTrackerListener:
    """
    Real-time Helius WebSocket listener for tracked Smart Money wallet transactions.

    Emits RawTokenEvent with source='WALLET_TRACKER' whenever a tracked wallet
    executes a qualifying buy (>= 0.5 SOL into a non-native token).
    """

    def __init__(self, on_token_callback: TokenCallback):
        self.on_token_callback = on_token_callback
        self._tracked_wallets: set[str] = set()
        self._running = False
        self._listen_task: Optional[asyncio.Task] = None
        self._sync_task: Optional[asyncio.Task] = None
        self._ws_url = settings.helius_ws_url if settings.helius_ws_url else f"wss://mainnet.helius-rpc.com/?api-key={settings.helius_api_key}"

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def start(self):
        """Start the listener and periodic wallet sync."""
        self._running = True
        # Initial wallet load before starting listener
        await self._sync_wallets()
        if not self._tracked_wallets:
            logger.warning(
                "[WalletTracker] No active Smart Money wallets found in DB. "
                "Listener will retry after wallet sync."
            )
        self._listen_task = asyncio.create_task(self._listen_loop())
        self._sync_task = asyncio.create_task(self._periodic_wallet_sync())
        logger.info(
            f"[WalletTracker] Started. Monitoring {len(self._tracked_wallets)} Smart Money wallet(s)."
        )

    async def stop(self):
        """Gracefully stop listener and sync tasks."""
        self._running = False
        for task in (self._listen_task, self._sync_task):
            if task and not task.done():
                task.cancel()
        logger.info("[WalletTracker] Stopped.")

    # ------------------------------------------------------------------
    # WebSocket listener loop
    # ------------------------------------------------------------------

    async def _listen_loop(self):
        backoff = 1.0
        while self._running:
            if not self._tracked_wallets:
                await asyncio.sleep(5)
                continue
            try:
                await self._connect_and_listen()
                backoff = 1.0  # Reset on clean disconnect
            except (ConnectionClosed, WebSocketException, OSError) as e:
                logger.warning(f"[WalletTracker] WebSocket disconnected: {e}. Reconnecting in {backoff:.0f}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[WalletTracker] Unexpected error: {e}. Reconnecting in {backoff:.0f}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _connect_and_listen(self):
        """Open WebSocket, subscribe to tracked wallets, then listen for messages."""
        logger.info(f"[WalletTracker] Connecting to Helius WS...")
        async with websockets.connect(
            self._ws_url,
            ping_interval=20,
            ping_timeout=20,
            open_timeout=30,
        ) as ws:
            # Subscribe to all tracked wallets (batch, up to MAX_SUBSCRIPTIONS)
            wallets_to_sub = list(self._tracked_wallets)[:MAX_SUBSCRIPTIONS]
            for wallet in wallets_to_sub:
                subscribe_payload = json.dumps({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "transactionSubscribe",
                    "params": [
                        {"mentions": [wallet]},
                        {
                            "commitment": "confirmed",
                            "encoding": "jsonParsed",
                            "transactionDetails": "full",
                            "showRewards": False,
                            "maxSupportedTransactionVersion": 0,
                        },
                    ],
                })
                await ws.send(subscribe_payload)

            logger.info(
                f"[WalletTracker] Subscribed to {len(wallets_to_sub)} wallet(s) via Helius WS."
            )

            async for raw_msg in ws:
                if not self._running:
                    break
                try:
                    msg = json.loads(raw_msg)
                    # Skip subscription confirmations (they have an 'id' key, not 'method')
                    if "id" in msg and "result" in msg:
                        continue
                    await self._handle_message(msg)
                except json.JSONDecodeError:
                    continue
                except Exception as e:
                    logger.debug(f"[WalletTracker] Error processing message: {e}")

    # ------------------------------------------------------------------
    # Message parsing
    # ------------------------------------------------------------------

    async def _handle_message(self, msg: dict):
        """
        Parse a Helius WebSocket transaction notification.
        Detect BUY swaps (SOL -> Token) and apply conviction gate.
        """
        params = msg.get("params", {})
        result = params.get("result", {})
        value = result.get("value", {})

        # Extract top-level metadata
        tx_signature = value.get("signature", "")
        account_keys_raw = (
            value.get("transaction", {})
            .get("message", {})
            .get("accountKeys", [])
        )
        account_keys = [
            (k["pubkey"] if isinstance(k, dict) else k)
            for k in account_keys_raw
        ]

        # Find which tracked wallet is involved
        triggered_wallet: Optional[str] = None
        for acct in account_keys:
            if acct in self._tracked_wallets:
                triggered_wallet = acct
                break

        if not triggered_wallet:
            return

        # Extract native SOL balance changes to detect direction (BUY = SOL net decrease)
        pre_balances = value.get("meta", {}).get("preBalances", [])
        post_balances = value.get("meta", {}).get("postBalances", [])
        inner_instructions = value.get("meta", {}).get("innerInstructions", [])
        post_token_balances = value.get("meta", {}).get("postTokenBalances", [])
        pre_token_balances = value.get("meta", {}).get("preTokenBalances", [])

        # Find wallet's index in account keys
        try:
            wallet_idx = account_keys.index(triggered_wallet)
        except ValueError:
            return

        # Calculate net SOL change for this wallet
        if wallet_idx < len(pre_balances) and wallet_idx < len(post_balances):
            sol_change = (post_balances[wallet_idx] - pre_balances[wallet_idx]) / 1e9
        else:
            return

        # BUY = wallet SOL decreased (net negative SOL change, at least MIN_CONVICTION_SOL)
        sol_spent = -sol_change
        if sol_spent < MIN_CONVICTION_SOL:
            return  # Not a conviction buy (could be sell, or dust)

        # Find the token that was received (net positive token balance change for this wallet)
        token_mint: Optional[str] = None
        token_amount: float = 0.0

        for post_bal in post_token_balances:
            owner = post_bal.get("owner")
            if owner != triggered_wallet:
                continue
            mint = post_bal.get("mint", "")
            if mint in _SKIP_MINTS:
                continue
            # Find pre balance for this mint
            pre_amount = 0.0
            for pre_bal in pre_token_balances:
                if pre_bal.get("mint") == mint and pre_bal.get("owner") == triggered_wallet:
                    pre_amount = float(
                        pre_bal.get("uiTokenAmount", {}).get("uiAmount", 0) or 0
                    )
                    break
            post_amount = float(
                post_bal.get("uiTokenAmount", {}).get("uiAmount", 0) or 0
            )
            gained = post_amount - pre_amount
            if gained > 0:
                token_mint = mint
                token_amount = gained
                break

        if not token_mint:
            return

        logger.info(
            f"[WalletTracker] CONVICTION BUY detected! "
            f"Wallet: {triggered_wallet[:8]}... | Token: {token_mint[:8]}... | "
            f"SOL Spent: {sol_spent:.3f} SOL | Tx: {tx_signature[:12]}..."
        )

        # Fetch minimal token metadata for downstream pipeline
        token_metadata = await self._fetch_token_metadata(token_mint)
        if not token_metadata:
            logger.debug(f"[WalletTracker] Could not fetch metadata for {token_mint[:8]}, skipping.")
            return

        # Build RawTokenEvent with Pintu B attribution
        now_utc = datetime.now(tz=timezone.utc)
        try:
            event = RawTokenEvent(
                token_address=token_mint,
                symbol=token_metadata.get("symbol", "UNKNOWN")[:20],
                name=token_metadata.get("name", "Unknown Token")[:60],
                deployer_wallet_address=token_metadata.get("deployer"),
                launch_venue=token_metadata.get("launch_venue", "pump_fun"),
                launch_timestamp=now_utc,
                initial_buy_amount=token_amount,
                total_supply=token_metadata.get("total_supply", 1_000_000_000.0),
                initial_sol_liquidity=token_metadata.get("initial_sol_liquidity", 0.0),
                bonding_curve_address=token_metadata.get("bonding_curve_address"),
                pool_address=token_metadata.get("pool_address"),
                raw_payload={"wallet_tracker_raw": True, "tx_signature": tx_signature},
                # Pintu B attribution
                source="WALLET_TRACKER",
                triggered_by_wallet=triggered_wallet,
                triggered_by_wallet_sol_spent=sol_spent,
                triggered_by_tx_signature=tx_signature,
            )
        except Exception as e:
            logger.debug(f"[WalletTracker] Failed to build RawTokenEvent for {token_mint[:8]}: {e}")
            return

        # Fire the downstream callback (Safety Filter pipeline -> Paper Trading)
        await self.on_token_callback(event)

    # ------------------------------------------------------------------
    # Token metadata fetcher (lightweight via Helius DAS)
    # ------------------------------------------------------------------

    async def _fetch_token_metadata(self, mint: str) -> Optional[dict]:
        """
        Fetch minimal metadata for a token via Helius getAsset DAS API.
        Returns dict with symbol, name, deployer, launch_venue, etc.
        """
        url = f"https://mainnet.helius-rpc.com/?api-key={settings.helius_api_key}"
        payload = {
            "jsonrpc": "2.0",
            "id": "wallet-tracker-meta",
            "method": "getAsset",
            "params": {"id": mint},
        }
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code != 200:
                    return None
                data = resp.json().get("result", {})
                if not data:
                    return None

                content = data.get("content", {})
                metadata = content.get("metadata", {})
                token_info = data.get("token_info", {})
                authorities = data.get("authorities", [])

                # Infer launch venue from mint suffix
                launch_venue = "pump_fun" if mint.endswith("pump") else "raydium"

                deployer = None
                for auth in authorities:
                    if auth.get("scopes") and "full" in auth.get("scopes", []):
                        deployer = auth.get("address")
                        break

                return {
                    "symbol": metadata.get("symbol", "UNKNOWN"),
                    "name": metadata.get("name", "Unknown Token"),
                    "deployer": deployer,
                    "launch_venue": launch_venue,
                    "total_supply": float(token_info.get("supply", 1_000_000_000)),
                    "initial_sol_liquidity": 30.0 if launch_venue == "pump_fun" else 0.0,
                    "bonding_curve_address": None,
                    "pool_address": None,
                }
        except Exception as e:
            logger.debug(f"[WalletTracker] Metadata fetch error for {mint[:8]}: {e}")
            return None

    # ------------------------------------------------------------------
    # Wallet list sync
    # ------------------------------------------------------------------

    async def _sync_wallets(self):
        """Load active Smart Money wallets from Supabase into self._tracked_wallets."""
        try:
            wallets_data = await db_manager.get_smart_money_wallets(active_only=True)
            new_set = {w["wallet_address"] for w in wallets_data if w.get("wallet_address")}
            added = new_set - self._tracked_wallets
            removed = self._tracked_wallets - new_set
            self._tracked_wallets = new_set
            if added or removed:
                logger.info(
                    f"[WalletTracker] Wallet list synced: {len(self._tracked_wallets)} active "
                    f"(+{len(added)} added, -{len(removed)} removed)."
                )
        except Exception as e:
            logger.warning(f"[WalletTracker] Wallet sync error: {e}")

    async def _periodic_wallet_sync(self):
        """Periodically re-sync tracked wallet list from DB."""
        while self._running:
            try:
                await asyncio.sleep(WALLET_SYNC_INTERVAL_S)
                await self._sync_wallets()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[WalletTracker] Periodic sync error: {e}")
