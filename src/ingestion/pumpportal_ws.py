"""
PumpPortal Unified WebSocket Client — Pintu A + B dalam Satu Koneksi

Menggabungkan dua aliran data via satu WebSocket ke PumpPortal:
  1. subscribeNewToken  (Free)   — stream token baru lahir di Pump.fun
  2. subscribeAccountTrade (Metered, 0.01 SOL/10k events) — stream BUY/SELL
     dari 32 Smart Money wallet yang kita pantau

Arsitektur Single Connection:
  - PumpPortal melarang >1 koneksi WS simultan dari API key yang sama.
    Seluruh subscription (new token + wallet trades) dikirim lewat satu koneksi.
  - Dispatch oleh field `txType`: tidak ada txType → new token event.
    txType == 'buy' / 'sell' → wallet trade event.

Smart Money Buy Cache:
  - Saat Smart Money wallet beli token X, dicatat ke SMART_MONEY_BUYS dict (in-memory).
  - TTL 10 menit per token untuk menghindari memory bloat.
  - delayed_evaluator.py membaca cache ini di T+2 dan pass sebagai candidate_wallets ke scorer.

Graceful Degradation:
  - Kalau PUMPPORTAL_API_KEY kosong, hanya subscribe subscribeNewToken (free mode).
  - Tidak ada error — bot tetap jalan seperti biasa, Smart Money scoring via weight redistribution.
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Callable, Awaitable, Optional

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from src.config import settings
from src.database.client import db_manager
from src.ingestion.schemas import RawTokenEvent
from src.utils.logger import logger


# ──────────────────────────────────────────────────────────────────────────────
# Smart Money Buy Cache (in-memory, module-level)
# ──────────────────────────────────────────────────────────────────────────────

# Menyimpan wallet smart money yang membeli token tertentu dalam 10 menit terakhir.
# key: token_address (str)
# value: set of wallet_address (str)
SMART_MONEY_BUYS: dict[str, set[str]] = {}
_SMART_MONEY_TIMESTAMPS: dict[str, float] = {}  # last_write per token
_CACHE_TTL_SECONDS = 600  # 10 menit


def record_smart_money_buy(token_address: str, wallet_address: str) -> None:
    """Catat bahwa Smart Money wallet membeli token ini."""
    if token_address not in SMART_MONEY_BUYS:
        SMART_MONEY_BUYS[token_address] = set()
    SMART_MONEY_BUYS[token_address].add(wallet_address)
    _SMART_MONEY_TIMESTAMPS[token_address] = time.time()


def get_smart_money_buyers(token_address: str) -> list[str]:
    """
    Ambil list wallet Smart Money yang membeli token ini (dalam TTL 10 menit).
    Returns empty list jika tidak ada.
    """
    buyers = SMART_MONEY_BUYS.get(token_address, set())
    return list(buyers)


def prune_smart_money_cache() -> None:
    """Hapus cache entries yang sudah expired (> 10 menit)."""
    cutoff = time.time() - _CACHE_TTL_SECONDS
    expired = [k for k, ts in _SMART_MONEY_TIMESTAMPS.items() if ts < cutoff]
    for k in expired:
        SMART_MONEY_BUYS.pop(k, None)
        _SMART_MONEY_TIMESTAMPS.pop(k, None)
    if expired:
        logger.debug(f"[PumpPortal] Pruned {len(expired)} expired Smart Money cache entries.")


# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

# Minimum SOL spent oleh Smart Money wallet agar dianggap conviction buy
MIN_CONVICTION_SOL = 0.5

# Interval sync wallet list dari Supabase (seconds)
WALLET_SYNC_INTERVAL_S = 300  # 5 menit

# Interval prune in-memory cache (seconds)
CACHE_PRUNE_INTERVAL_S = 300  # 5 menit

# Callback type aliases
NewTokenCallback = Callable[[RawTokenEvent], Awaitable[None]]
WalletTradeCallback = Callable[[dict], Awaitable[None]]


# ──────────────────────────────────────────────────────────────────────────────
# Unified Client Class
# ──────────────────────────────────────────────────────────────────────────────

class PumpPortalUnifiedClient:
    """
    Single WebSocket connection ke PumpPortal yang menangani:
      1. New token events (subscribeNewToken) → on_new_token callback
      2. Smart Money wallet trades (subscribeAccountTrade) → cache update + on_wallet_trade callback

    Jika PUMPPORTAL_API_KEY tidak dikonfigurasi, hanya subscribeNewToken yang aktif (free mode).
    """

    def __init__(
        self,
        on_new_token: NewTokenCallback,
        on_wallet_trade: Optional[WalletTradeCallback] = None,
    ):
        self.on_new_token = on_new_token
        self.on_wallet_trade = on_wallet_trade
        self._tracked_wallets: set[str] = set()
        self._running = False
        self._listen_task: Optional[asyncio.Task] = None
        self._sync_task: Optional[asyncio.Task] = None
        self._prune_task: Optional[asyncio.Task] = None

        # Build WS URL — tambahkan API key jika ada
        api_key = settings.pumpportal_api_key
        base_url = settings.pumpportal_ws_url
        if api_key:
            self._ws_url = f"{base_url}?api-key={api_key}"
            self._has_api_key = True
        else:
            self._ws_url = base_url
            self._has_api_key = False

    # ── Public Interface ───────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start unified listener, wallet sync, dan cache prune tasks."""
        self._running = True

        # Load active smart money wallets on startup
        await self._sync_wallets()

        if self._has_api_key:
            logger.info(
                f"[PumpPortal] API key configured. Running in FULL mode "
                f"(subscribeNewToken + subscribeAccountTrade for {len(self._tracked_wallets)} wallets)."
            )
        else:
            logger.info(
                "[PumpPortal] No API key found. Running in FREE mode "
                "(subscribeNewToken only). Smart Money scoring via weight redistribution."
            )

        self._listen_task = asyncio.create_task(self._listen_loop())
        self._sync_task = asyncio.create_task(self._periodic_wallet_sync())
        self._prune_task = asyncio.create_task(self._periodic_cache_prune())

    async def stop(self) -> None:
        """Gracefully stop all tasks."""
        self._running = False
        for task in (self._listen_task, self._sync_task, self._prune_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        logger.info("[PumpPortal] Unified client stopped.")

    # ── WebSocket Listener Loop ───────────────────────────────────────────────

    async def _listen_loop(self) -> None:
        """Main reconnect loop dengan exponential backoff."""
        backoff = 1.0
        while self._running:
            try:
                await self._connect_and_listen()
                backoff = 1.0
            except (ConnectionClosed, WebSocketException, OSError) as e:
                logger.warning(
                    f"[PumpPortal] WebSocket disconnected: {e}. Reconnecting in {backoff:.0f}s..."
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(
                    f"[PumpPortal] Unexpected error: {e}. Reconnecting in {backoff:.0f}s..."
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _connect_and_listen(self) -> None:
        """Buka satu WebSocket connection, subscribe, dan listen sampai putus."""
        logger.info(f"[PumpPortal] Connecting to {self._ws_url.split('?')[0]}...")
        async with websockets.connect(
            self._ws_url,
            ping_interval=20,
            ping_timeout=20,
            open_timeout=30,
        ) as ws:
            # Subscribe 1: New token events (always free)
            await ws.send(json.dumps({"method": "subscribeNewToken"}))

            # Subscribe 2: Smart Money wallet trades (metered, only if API key configured)
            if self._has_api_key and self._tracked_wallets:
                wallet_list = list(self._tracked_wallets)
                await ws.send(json.dumps({
                    "method": "subscribeAccountTrade",
                    "keys": wallet_list,
                }))
                logger.info(
                    f"[PumpPortal] Subscribed to subscribeNewToken + "
                    f"subscribeAccountTrade ({len(wallet_list)} wallets)."
                )
            else:
                logger.info("[PumpPortal] Subscribed to subscribeNewToken.")

            async for raw_msg in ws:
                if not self._running:
                    break
                try:
                    data = json.loads(raw_msg)
                    await self._handle_message(data)
                except json.JSONDecodeError:
                    continue
                except Exception as e:
                    logger.debug(f"[PumpPortal] Error processing message: {e}")

    # ── Message Dispatch ──────────────────────────────────────────────────────

    async def _handle_message(self, data: dict) -> None:
        """
        Dispatch incoming PumpPortal WebSocket message ke handler yang tepat.

        New token event (dari subscribeNewToken):
          - Tidak punya field 'txType' (atau txType == 'create')
          - Punya field 'mint'

        Wallet trade event (dari subscribeAccountTrade):
          - Punya field 'txType' == 'buy' | 'sell'
          - Punya field 'mint' dan 'traderPublicKey'
        """
        tx_type = data.get("txType")
        mint = data.get("mint")

        if not mint:
            return  # Pesan konfirmasi subscription atau unknown format

        if tx_type in ("buy", "sell"):
            # ── Wallet Trade Event ──
            await self._handle_wallet_trade(data, tx_type, mint)
        else:
            # ── New Token Event ──
            await self._handle_new_token(data, mint)

    async def _handle_new_token(self, data: dict, mint: str) -> None:
        """Handle token creation event dari subscribeNewToken."""
        # Filter: Pump.fun token address harus berakhir dengan 'pump'
        if not mint.endswith("pump"):
            logger.debug(f"[PumpPortal] Skipping non-pump mint: {mint[:8]}")
            return

        # Tolak token dengan dev snipe > 20% supply awal
        initial_buy = float(data.get("initialBuy", 0.0))
        if initial_buy > 200_000_000.0:
            logger.debug(
                f"[PumpPortal] Skipping {mint[:8]} — suspicious dev initial buy "
                f"({initial_buy:,.0f} tokens > 20% supply)"
            )
            return

        try:
            event = RawTokenEvent(
                token_address=mint,
                symbol=data.get("symbol", "UNKNOWN"),
                name=data.get("name", "Unknown Token"),
                deployer_wallet_address=data.get("traderPublicKey"),
                launch_venue="pump_fun",
                launch_timestamp=datetime.now(tz=timezone.utc),
                initial_buy_amount=initial_buy,
                total_supply=1_000_000_000.0,
                initial_sol_liquidity=float(data.get("vSolInBondingCurve", 30.0)),
                bonding_curve_address=data.get("bondingCurveKey"),
                raw_payload=data,
                source="NEW_PAIR",
            )
            asyncio.create_task(self.on_new_token(event))
        except Exception as e:
            logger.debug(f"[PumpPortal] Failed to build RawTokenEvent for {mint[:8]}: {e}")

    async def _handle_wallet_trade(self, data: dict, tx_type: str, mint: str) -> None:
        """Handle wallet trade event dari subscribeAccountTrade."""
        trader = data.get("traderPublicKey", "")

        # Hanya proses jika wallet yang trade adalah salah satu Smart Money wallet yang kita track
        if not trader or trader not in self._tracked_wallets:
            return

        if tx_type == "buy":
            sol_amount = float(data.get("solAmount", 0.0))

            # Conviction gate: minimum 0.5 SOL agar tidak noise dust transaction
            if sol_amount < MIN_CONVICTION_SOL:
                logger.debug(
                    f"[PumpPortal] Smart Money buy below conviction threshold: "
                    f"{trader[:8]}... spent {sol_amount:.4f} SOL on {mint[:8]} (< {MIN_CONVICTION_SOL} SOL)"
                )
                return

            # Catat ke in-memory cache untuk diambil di T+2 scoring
            record_smart_money_buy(mint, trader)

            logger.info(
                f"💎 [SmartMoney] {trader[:8]}... bought ${data.get('symbol', '?')} "
                f"({mint[:8]}...) — {sol_amount:.3f} SOL → cached for T+2 scoring"
            )

        # Fire optional on_wallet_trade callback (e.g., untuk Pintu B direct entry)
        if self.on_wallet_trade:
            try:
                await self.on_wallet_trade(data)
            except Exception as e:
                logger.debug(f"[PumpPortal] on_wallet_trade callback error: {e}")

    # ── Wallet List Sync ──────────────────────────────────────────────────────

    async def _sync_wallets(self) -> None:
        """Load active Smart Money wallets dari Supabase ke self._tracked_wallets."""
        try:
            wallets_data = await db_manager.get_smart_money_wallets(active_only=True)
            new_set = {w["wallet_address"] for w in wallets_data if w.get("wallet_address")}
            added = new_set - self._tracked_wallets
            removed = self._tracked_wallets - new_set
            self._tracked_wallets = new_set
            if added or removed:
                logger.info(
                    f"[PumpPortal] Smart Money wallet list synced: "
                    f"{len(self._tracked_wallets)} active (+{len(added)} added, -{len(removed)} removed)."
                )
        except Exception as e:
            logger.warning(f"[PumpPortal] Wallet sync error: {e}")

    async def _periodic_wallet_sync(self) -> None:
        """Periodically re-sync tracked wallet list dari DB."""
        while self._running:
            try:
                await asyncio.sleep(WALLET_SYNC_INTERVAL_S)
                await self._sync_wallets()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[PumpPortal] Periodic wallet sync error: {e}")

    async def _periodic_cache_prune(self) -> None:
        """Periodically prune expired entries dari Smart Money buy cache."""
        while self._running:
            try:
                await asyncio.sleep(CACHE_PRUNE_INTERVAL_S)
                prune_smart_money_cache()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[PumpPortal] Cache prune error: {e}")
