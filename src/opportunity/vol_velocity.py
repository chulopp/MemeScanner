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
        provider_used: str = "helius",
        is_successful: bool = True,
        raw_data: Optional[dict[str, Any]] = None
    ):
        self.score = score
        self.buy_count = buy_count
        self.sell_count = sell_count
        self.buy_volume_sol = buy_volume_sol
        self.sell_volume_sol = sell_volume_sol
        self.net_buy_pressure_ratio = net_buy_pressure_ratio
        self.provider_used = provider_used
        self.is_successful = is_successful
        self.raw_data = raw_data or {}


class VolumeVelocityEngine:
    """
    Multi-Tier Volume Velocity & Net Buy Pressure Scoring Engine:
    - Tier 1 (Primary): Helius Enhanced Transactions API
    - Tier 2 (Fallback): DexScreener Public API (no key required, 5m aggregated stats)
    - Tier 3 (Fallback): Standard Solana RPC getSignaturesForAddress
    """

    def __init__(self):
        self._http_client: Optional[httpx.AsyncClient] = None
        self._semaphore = asyncio.Semaphore(settings.rpc_concurrency_limit)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=10.0)
        return self._http_client

    async def _fetch_from_helius(
        self,
        mint_address: str,
        cutoff_ts: float,
        initial_buy_sol: float
    ) -> Optional[dict]:
        """Tier 1: Fetches parsed on-chain transactions via Helius Enhanced API."""
        if not settings.helius_api_key:
            return None

        url = f"https://api.helius.xyz/v0/addresses/{mint_address}/transactions"
        params = {
            "api-key": settings.helius_api_key,
            "limit": 100
        }

        async with self._semaphore:
            client = await self._get_client()
            response = await asyncio.wait_for(
                client.get(url, params=params),
                timeout=8.0
            )

            if response.status_code == 429:
                logger.warning(f"⚠️ Helius Enhanced API Rate Limited (HTTP 429) for {mint_address[:8]}...")
                return None
            elif response.status_code != 200:
                logger.debug(f"Helius HTTP {response.status_code} for {mint_address[:8]}...")
                return None

            txs = response.json()
            if not isinstance(txs, list):
                return None

            buy_count = 0
            sell_count = 0
            buy_vol_sol = initial_buy_sol
            sell_vol_sol = 0.0

            for tx in txs:
                tx_time = tx.get("timestamp", 0)
                if tx_time < cutoff_ts:
                    continue

                events = tx.get("events", {})
                swap_event = events.get("swap") if isinstance(events, dict) else None
                fee_payer = tx.get("feePayer", "")
                token_transfers = tx.get("tokenTransfers", [])
                native_transfers = tx.get("nativeTransfers", [])

                is_buy = False
                is_sell = False
                sol_amount = 0.0

                # 1. Check structured swap event if parsed by Helius
                if isinstance(swap_event, dict):
                    token_outputs = swap_event.get("tokenOutputs", [])
                    token_inputs = swap_event.get("tokenInputs", [])
                    native_input = swap_event.get("nativeInput")
                    native_output = swap_event.get("nativeOutput")

                    if any(to.get("mint") == mint_address for to in token_outputs):
                        is_buy = True
                        if native_input and isinstance(native_input, dict):
                            sol_amount = native_input.get("amount", 0) / 1_000_000_000.0
                    elif any(ti.get("mint") == mint_address for ti in token_inputs):
                        is_sell = True
                        if native_output and isinstance(native_output, dict):
                            sol_amount = native_output.get("amount", 0) / 1_000_000_000.0

                # 2. Fallback to transfer stream analysis
                if not is_buy and not is_sell and token_transfers:
                    mint_transfers = [tt for tt in token_transfers if tt.get("mint") == mint_address]
                    for tt in mint_transfers:
                        to_user = tt.get("toUserAccount")
                        from_user = tt.get("fromUserAccount")

                        if to_user and (to_user == fee_payer or fee_payer in [nt.get("fromUserAccount") for nt in native_transfers]):
                            is_buy = True
                            break
                        elif from_user and (from_user == fee_payer or fee_payer in [nt.get("toUserAccount") for nt in native_transfers]):
                            is_sell = True
                            break

                    for nt in native_transfers:
                        if nt.get("amount"):
                            sol_amount += nt.get("amount", 0) / 1_000_000_000.0

                if is_buy:
                    buy_count += 1
                    buy_vol_sol += sol_amount
                elif is_sell:
                    sell_count += 1
                    sell_vol_sol += sol_amount

            return {
                "buy_count": buy_count,
                "sell_count": sell_count,
                "buy_vol_sol": buy_vol_sol,
                "sell_vol_sol": sell_vol_sol,
                "provider": "helius"
            }

    async def _fetch_from_dexscreener(
        self,
        mint_address: str,
        initial_buy_sol: float
    ) -> Optional[dict]:
        """Tier 2: Free fallback via DexScreener public token endpoint."""
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint_address}"
        async with self._semaphore:
            client = await self._get_client()
            response = await asyncio.wait_for(
                client.get(url),
                timeout=5.0
            )
            if response.status_code != 200:
                return None

            data = response.json()
            pairs = data.get("pairs")
            if not pairs or not isinstance(pairs, list):
                return None

            buy_count = 0
            sell_count = 0
            vol_5m_usd = 0.0

            for pair in pairs:
                txns = pair.get("txns", {})
                m5 = txns.get("m5", {})
                buy_count += int(m5.get("buys", 0))
                sell_count += int(m5.get("sells", 0))
                vol_data = pair.get("volume", {})
                vol_5m_usd += float(vol_data.get("m5", 0.0))

            # Estimate SOL volume assuming ~$180 baseline
            estimated_sol_vol = vol_5m_usd / 180.0 if vol_5m_usd > 0 else initial_buy_sol

            logger.info(f"🔄 DexScreener fallback active for {mint_address[:8]}... (5m Buys: {buy_count}, Sells: {sell_count})")
            return {
                "buy_count": buy_count,
                "sell_count": sell_count,
                "buy_vol_sol": max(estimated_sol_vol * (buy_count / max(buy_count + sell_count, 1)), initial_buy_sol),
                "sell_vol_sol": estimated_sol_vol * (sell_count / max(buy_count + sell_count, 1)),
                "provider": "dexscreener"
            }

    async def _fetch_from_rpc_signatures(
        self,
        mint_address: str,
        cutoff_ts: float,
        initial_buy_sol: float
    ) -> Optional[dict]:
        """Tier 3: Fallback using standard Solana JSON-RPC signatures count."""
        try:
            sigs = await solana_rpc.get_signatures_for_address(mint_address, limit=50)
            if not sigs:
                return None

            recent_tx_count = sum(1 for s in sigs if (s.get("blockTime") or 0) >= cutoff_ts)
            if recent_tx_count == 0:
                recent_tx_count = len(sigs)

            # In early pump curves, most initial txs are buys
            est_buys = max(int(recent_tx_count * 0.7), 1)
            est_sells = max(recent_tx_count - est_buys, 0)

            logger.info(f"🔄 Standard RPC fallback active for {mint_address[:8]}... (Recent txs: {recent_tx_count})")
            return {
                "buy_count": est_buys,
                "sell_count": est_sells,
                "buy_vol_sol": initial_buy_sol,
                "sell_vol_sol": 0.0,
                "provider": "solana_rpc"
            }
        except Exception as e:
            logger.debug(f"RPC fallback failed for {mint_address[:8]}: {e}")
            return None

    async def calculate_velocity(
        self,
        mint_address: str,
        initial_buy_sol: float = 0.0,
        window_seconds: Optional[int] = None
    ) -> VolumeVelocityResult:
        """
        Fetches parsed recent transactions and computes buy/sell pressure ratio.
        Executes Multi-Tier Fallback: Helius -> DexScreener -> Solana RPC.
        """
        window_sec = window_seconds or settings.vol_velocity_window_seconds
        now_ts = time.time()
        cutoff_ts = now_ts - window_sec

        result_data: Optional[dict] = None
        provider_used = "none"

        # Tier 1: Try Helius Enhanced API
        if settings.helius_api_key:
            try:
                result_data = await self._fetch_from_helius(mint_address, cutoff_ts, initial_buy_sol)
                if result_data:
                    provider_used = "helius"
            except Exception as e:
                logger.debug(f"Tier 1 Helius error: {e}")

        # Tier 2: Try DexScreener Fallback
        if not result_data:
            try:
                result_data = await self._fetch_from_dexscreener(mint_address, initial_buy_sol)
                if result_data:
                    provider_used = "dexscreener"
            except Exception as e:
                logger.debug(f"Tier 2 DexScreener fallback error: {e}")

        # Tier 3: Try Standard Solana RPC Fallback
        if not result_data:
            try:
                result_data = await self._fetch_from_rpc_signatures(mint_address, cutoff_ts, initial_buy_sol)
                if result_data:
                    provider_used = "solana_rpc"
            except Exception as e:
                logger.debug(f"Tier 3 RPC fallback error: {e}")

        # If data was obtained from any provider
        if result_data:
            buy_count = result_data["buy_count"]
            sell_count = result_data["sell_count"]
            buy_vol_sol = result_data["buy_vol_sol"]
            sell_vol_sol = result_data["sell_vol_sol"]

            if buy_count == 0 and sell_count == 0:
                if initial_buy_sol > 0:
                    buy_count = 1
                    buy_vol_sol = initial_buy_sol
                    ratio = 1.0
                    normalized_score = 25.0
                else:
                    ratio = 0.0
                    normalized_score = 0.0
            else:
                ratio = buy_count / max(sell_count, 1.0)
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
                provider_used=provider_used,
                is_successful=True,
                raw_data={
                    "total_swaps_5m": buy_count + sell_count,
                    "buy_count": buy_count,
                    "sell_count": sell_count,
                    "buy_vol_sol": buy_vol_sol,
                    "sell_vol_sol": sell_vol_sol,
                    "ratio": ratio,
                    "provider": provider_used
                }
            )

        # Baseline fallback for brand-new token with dev initial buy
        if initial_buy_sol > 0:
            return VolumeVelocityResult(
                score=25.0,
                buy_count=1,
                sell_count=0,
                buy_volume_sol=initial_buy_sol,
                sell_volume_sol=0.0,
                net_buy_pressure_ratio=1.0,
                provider_used="baseline_initial_buy",
                is_successful=True,
                raw_data={"note": "Initial launch liquidity baseline"}
            )

        # If all providers failed and no initial buy known -> mark unavailable so weight redistributes
        logger.warning(f"⚠️ All Volume Velocity providers failed for {mint_address[:8]}... Setting component unavailable.")
        return VolumeVelocityResult(
            score=0.0,
            buy_count=0,
            sell_count=0,
            buy_volume_sol=0.0,
            sell_volume_sol=0.0,
            net_buy_pressure_ratio=0.0,
            provider_used="none",
            is_successful=False,
            raw_data={"error": "All providers failed; weight dynamically redistributed"}
        )

    async def close(self):
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()


volume_velocity_engine = VolumeVelocityEngine()
