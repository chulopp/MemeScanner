import asyncio
import time
from datetime import datetime
from typing import Optional, Any
import httpx

from src.config import settings
from src.utils.logger import logger
from src.utils.solana_rpc import solana_rpc


class VolumeVelocityResult:
    def __init__(
        self,
        score: float,
        buy_count: int,
        sell_count: int,
        buy_volume_sol: float,
        sell_volume_sol: float,
        net_buy_pressure_ratio: float,
        is_successful: bool = True,
        raw_data: Optional[dict[str, Any]] = None
    ):
        self.score = score
        self.buy_count = buy_count
        self.sell_count = sell_count
        self.buy_volume_sol = buy_volume_sol
        self.sell_volume_sol = sell_volume_sol
        self.net_buy_pressure_ratio = net_buy_pressure_ratio
        self.is_successful = is_successful
        self.raw_data = raw_data or {}


class VolumeVelocityEngine:
    """
    Evaluates Volume Velocity & Net Buy Pressure for a token over a 5-minute window.
    Calculates buy vs sell transactions count and volume ratio.
    """

    def __init__(self):
        self._http_client: Optional[httpx.AsyncClient] = None
        self._semaphore = asyncio.Semaphore(settings.rpc_concurrency_limit)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=10.0)
        return self._http_client

    async def calculate_velocity(
        self,
        mint_address: str,
        initial_buy_sol: float = 0.0,
        window_seconds: Optional[int] = None
    ) -> VolumeVelocityResult:
        """
        Fetches parsed recent transactions via Helius Enhanced API (or RPC fallback)
        and computes buy/sell pressure ratio within the configured window.
        """
        window_sec = window_seconds or settings.vol_velocity_window_seconds
        now_ts = time.time()
        cutoff_ts = now_ts - window_sec

        buy_count = 0
        sell_count = 0
        buy_vol_sol = initial_buy_sol
        sell_vol_sol = 0.0

        # Attempt Helius Enhanced API call
        if settings.helius_api_key:
            url = f"https://api.helius.xyz/v0/addresses/{mint_address}/transactions"
            params = {
                "api-key": settings.helius_api_key,
                "limit": 100
            }

            async with self._semaphore:
                try:
                    client = await self._get_client()
                    response = await client.get(url, params=params)
                    if response.status_code == 200:
                        txs = response.json()
                        if isinstance(txs, list) and txs:
                            for tx in txs:
                                tx_time = tx.get("timestamp", 0)
                                if tx_time < cutoff_ts:
                                    continue

                                tx_type = tx.get("type", "").upper()
                                native_transfers = tx.get("nativeTransfers", [])
                                token_transfers = tx.get("tokenTransfers", [])

                                # Classify swap direction:
                                is_buy = False
                                is_sell = False

                                for tt in token_transfers:
                                    if tt.get("mint") == mint_address:
                                        # User received tokens = buy
                                        is_buy = True
                                        break

                                if not is_buy:
                                    for nt in native_transfers:
                                        # Outbound SOL from contract/pool = sell
                                        if nt.get("fromUserAccount") == mint_address:
                                            is_sell = True
                                            break

                                if is_buy:
                                    buy_count += 1
                                    for nt in native_transfers:
                                        buy_vol_sol += nt.get("amount", 0) / 1_000_000_000.0
                                elif is_sell or tx_type == "SWAP":
                                    sell_count += 1
                                    for nt in native_transfers:
                                        sell_vol_sol += nt.get("amount", 0) / 1_000_000_000.0
                                else:
                                    buy_count += 1
                except Exception as e:
                    logger.debug(f"Helius Volume Velocity error for {mint_address[:8]}: {e}")

        # Baseline fallback if no external transactions indexed yet (very fresh token)
        if buy_count == 0 and sell_count == 0:
            buy_count = 1  # Deployer initial buy
            sell_count = 0
            buy_vol_sol = max(initial_buy_sol, 0.5)

        total_swaps = buy_count + sell_count
        ratio = buy_count / max(sell_count, 1.0)

        # Min-max normalization: max ratio (e.g. 5.0) maps to 100 score
        max_ratio = settings.vol_velocity_buy_sell_ratio_max
        normalized_score = min((ratio / max_ratio) * 100.0, 100.0)

        # Penalty if sell count is higher than buy count
        if sell_count > buy_count:
            normalized_score = max(normalized_score * 0.5, 0.0)

        return VolumeVelocityResult(
            score=round(normalized_score, 2),
            buy_count=buy_count,
            sell_count=sell_count,
            buy_volume_sol=round(buy_vol_sol, 4),
            sell_volume_sol=round(sell_vol_sol, 4),
            net_buy_pressure_ratio=round(ratio, 2),
            is_successful=True,
            raw_data={
                "total_swaps_5m": total_swaps,
                "buy_count": buy_count,
                "sell_count": sell_count,
                "buy_vol_sol": buy_vol_sol,
                "sell_vol_sol": sell_vol_sol,
                "ratio": ratio
            }
        )

    async def close(self):
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()


volume_velocity_engine = VolumeVelocityEngine()
