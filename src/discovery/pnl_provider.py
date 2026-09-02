"""
P&L Provider — Smart Money Discovery Phase 3
=============================================
Multi-tier realized P&L fetcher for wallet qualification.

Tier 1: Vybe Network API  (free, pre-indexed, returns 170+ metrics per wallet)
Tier 2: Helius swap history reconstruction (gratis, manual, slower)

Usage:
    provider = PnLProvider()
    result = await provider.get_wallet_pnl("WALLET_ADDRESS")
    if result.is_successful and result.realized_pnl_90d_sol > 0 and result.realized_pnl_30d_sol > 0:
        # wallet is profitable — qualified
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

from src.config import settings
from src.utils.logger import logger


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class PnLResult:
    wallet_address: str
    realized_pnl_90d_sol: float = 0.0   # SOL realized profit last 90 days
    realized_pnl_30d_sol: float = 0.0   # SOL realized profit last 30 days
    total_trades_90d: int = 0            # Trade count last 90 days
    total_volume_sol: float = 0.0        # Total SOL traded volume
    win_rate_pct: float = 0.0            # % profitable trades (0-100)
    provider: str = "unknown"            # 'vybe' | 'helius_manual' | 'unknown'
    is_successful: bool = False          # False = all providers failed


# ---------------------------------------------------------------------------
# Tier 1: Vybe Network
# ---------------------------------------------------------------------------

class _VybePnLFetcher:
    """
    Fetches realized P&L from Vybe Network free API.
    Endpoint: GET /v1/wallet/{address}/pnl
    Sign up free: https://vybenetwork.com → API Dashboard → generate key
    Set env: VYBE_API_KEY=<key>
    """

    REQUEST_DELAY = 0.5   # seconds between requests (free tier courtesy delay)

    async def fetch(self, wallet: str, client: httpx.AsyncClient) -> Optional[PnLResult]:
        if not settings.vybe_api_key:
            return None

        url = f"{settings.vybe_base_url}/v1/wallet/{wallet}/pnl"
        headers = {"X-API-Key": settings.vybe_api_key}

        try:
            resp = await client.get(url, headers=headers, timeout=15.0)

            if resp.status_code == 429:
                logger.debug(f"[PnL/Vybe] Rate limited for {wallet[:8]} — backing off 5s")
                await asyncio.sleep(5.0)
                return None

            if resp.status_code == 404:
                # Wallet not yet indexed by Vybe — not an error
                logger.debug(f"[PnL/Vybe] Wallet {wallet[:8]} not indexed")
                return None

            if resp.status_code != 200:
                logger.debug(f"[PnL/Vybe] HTTP {resp.status_code} for {wallet[:8]}")
                return None

            data = resp.json()

            # Handle both snake_case and camelCase field names
            pnl_90d = float(
                data.get("realizedPnl90d") or
                data.get("realized_pnl_90d") or
                (data.get("realizedPnl") or {}).get("90d") or 0
            )
            pnl_30d = float(
                data.get("realizedPnl30d") or
                data.get("realized_pnl_30d") or
                (data.get("realizedPnl") or {}).get("30d") or 0
            )
            trades_90d = int(
                data.get("totalTrades90d") or
                data.get("total_trades_90d") or
                data.get("totalTrades") or 0
            )
            volume = float(
                data.get("totalVolumeSol") or
                data.get("total_volume_sol") or
                data.get("totalVolume") or 0
            )
            win_rate = float(data.get("winRate") or data.get("win_rate") or 0)
            # Normalise: Vybe may return 0-1 ratio or 0-100 pct
            if 0 < win_rate <= 1.0:
                win_rate = win_rate * 100.0

            await asyncio.sleep(self.REQUEST_DELAY)

            return PnLResult(
                wallet_address=wallet,
                realized_pnl_90d_sol=pnl_90d,
                realized_pnl_30d_sol=pnl_30d,
                total_trades_90d=trades_90d,
                total_volume_sol=volume,
                win_rate_pct=win_rate,
                provider="vybe",
                is_successful=True,
            )

        except Exception as e:
            logger.debug(f"[PnL/Vybe] Exception for {wallet[:8]}: {e}")
            return None


# ---------------------------------------------------------------------------
# Tier 2: Helius manual swap reconstruction
# ---------------------------------------------------------------------------

class _HeliusManualPnLFetcher:
    """
    Reconstructs realized P&L from Helius Enhanced API swap transactions.

    Logic:
      SOL spent  (nativeTransfers FROM wallet in SWAP tx) = costs
      SOL received (nativeTransfers TO wallet in SWAP tx) = proceeds
      pnl_window = sum(proceeds) - sum(costs) for that time window

    Approximate but accurate enough for profitability gate.
    Does NOT track individual token P&L — just net SOL flow.
    """

    HELIUS_TX_URL = "https://api.helius.xyz/v0/addresses/{address}/transactions"
    PAGE_LIMIT = 100
    MAX_PAGES = 5    # up to 500 swap txs — sufficient for 90d signal

    async def fetch(self, wallet: str, client: httpx.AsyncClient) -> Optional[PnLResult]:
        if not settings.helius_api_key:
            return None

        url = self.HELIUS_TX_URL.format(address=wallet)
        params = {"api-key": settings.helius_api_key, "type": "SWAP", "limit": self.PAGE_LIMIT}

        cutoff_90d = datetime.now(tz=timezone.utc) - timedelta(days=90)
        cutoff_30d = datetime.now(tz=timezone.utc) - timedelta(days=30)

        all_txs: list[dict] = []
        before_sig: Optional[str] = None

        try:
            for _ in range(self.MAX_PAGES):
                p = dict(params)
                if before_sig:
                    p["before"] = before_sig

                resp = await client.get(url, params=p, timeout=20.0)
                if resp.status_code == 429:
                    await asyncio.sleep(5.0)
                    break
                if resp.status_code != 200:
                    break

                page = resp.json()
                if not isinstance(page, list) or not page:
                    break

                all_txs.extend(page)
                before_sig = page[-1].get("signature")

                oldest_ts = page[-1].get("timestamp", 0)
                if oldest_ts:
                    oldest_dt = datetime.fromtimestamp(oldest_ts, tz=timezone.utc)
                    if oldest_dt < cutoff_90d:
                        break

                await asyncio.sleep(0.3)

        except Exception as e:
            logger.debug(f"[PnL/Helius] Fetch error for {wallet[:8]}: {e}")
            return None

        if not all_txs:
            return None

        sol_in_90d = sol_out_90d = 0.0
        sol_in_30d = sol_out_30d = 0.0
        trades_90d = winning_trades = 0
        total_volume = 0.0

        for tx in all_txs:
            ts = tx.get("timestamp")
            if not ts:
                continue
            tx_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            if tx_dt < cutoff_90d:
                continue

            native_transfers = tx.get("nativeTransfers") or []
            tx_out = tx_in = 0.0

            for nt in native_transfers:
                amount_sol = nt.get("amount", 0) / 1e9
                if nt.get("fromUserAccount") == wallet:
                    tx_out += amount_sol
                elif nt.get("toUserAccount") == wallet:
                    tx_in += amount_sol

            trades_90d += 1
            total_volume += tx_out
            if tx_in > tx_out:
                winning_trades += 1

            sol_in_90d += tx_in
            sol_out_90d += tx_out

            if tx_dt >= cutoff_30d:
                sol_in_30d += tx_in
                sol_out_30d += tx_out

        win_rate = (winning_trades / trades_90d * 100.0) if trades_90d > 0 else 0.0

        return PnLResult(
            wallet_address=wallet,
            realized_pnl_90d_sol=sol_in_90d - sol_out_90d,
            realized_pnl_30d_sol=sol_in_30d - sol_out_30d,
            total_trades_90d=trades_90d,
            total_volume_sol=total_volume,
            win_rate_pct=win_rate,
            provider="helius_manual",
            is_successful=True,
        )


# ---------------------------------------------------------------------------
# Public PnLProvider (tiered)
# ---------------------------------------------------------------------------

class PnLProvider:
    """
    Tiered P&L provider:
      Tier 1: Vybe Network (fast, pre-indexed, free)
      Tier 2: Helius manual reconstruction (fallback, slower, free)
    """

    def __init__(self) -> None:
        self._vybe = _VybePnLFetcher()
        self._helius = _HeliusManualPnLFetcher()

    async def get_wallet_pnl(self, wallet: str) -> PnLResult:
        """Fetch P&L for a single wallet, with automatic tier fallback."""
        async with httpx.AsyncClient(headers={"User-Agent": "MemeScanner/1.0"}) as client:
            result = await self._vybe.fetch(wallet, client)
            if result and result.is_successful:
                logger.debug(
                    f"[PnL] {wallet[:8]} via Vybe: "
                    f"90d={result.realized_pnl_90d_sol:+.2f} SOL, "
                    f"30d={result.realized_pnl_30d_sol:+.2f} SOL, "
                    f"trades={result.total_trades_90d}"
                )
                return result

            result = await self._helius.fetch(wallet, client)
            if result and result.is_successful:
                logger.debug(
                    f"[PnL] {wallet[:8]} via Helius: "
                    f"90d={result.realized_pnl_90d_sol:+.2f} SOL, "
                    f"30d={result.realized_pnl_30d_sol:+.2f} SOL, "
                    f"trades={result.total_trades_90d}"
                )
                return result

        logger.debug(f"[PnL] All tiers failed for {wallet[:8]}")
        return PnLResult(wallet_address=wallet, is_successful=False)

    async def get_wallets_pnl_batch(
        self,
        wallets: list[str],
        concurrency: int = 3,
    ) -> dict[str, PnLResult]:
        """Fetch P&L for multiple wallets with bounded concurrency."""
        semaphore = asyncio.Semaphore(concurrency)
        results: dict[str, PnLResult] = {}

        async def _one(w: str) -> None:
            async with semaphore:
                results[w] = await self.get_wallet_pnl(w)

        await asyncio.gather(*[_one(w) for w in wallets], return_exceptions=True)
        return results


# Singleton
pnl_provider = PnLProvider()
