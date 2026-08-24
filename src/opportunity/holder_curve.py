"""
Holder Curve Engine — Fase 3 Opportunity Scoring
Evaluates bonding curve progression and unique holder accumulation.
Formula Hypothesis: Score = (0.50 * BondingProgressScore) + (0.50 * HolderDistributionScore)
"""

import asyncio
from typing import Optional, Any
import httpx

from src.config import settings
from src.ingestion.schemas import RawTokenEvent
from src.utils.logger import logger
from src.utils.solana_rpc import solana_rpc


class HolderCurveResult:
    def __init__(
        self,
        score: float,
        bonding_curve_pct: float,
        unique_holders_count: int,
        top_holder_concentration_pct: float = 0.0,
        provider_used: str = "on_chain",
        is_successful: bool = True,
        raw_data: Optional[dict[str, Any]] = None
    ):
        self.score = score
        self.bonding_curve_pct = bonding_curve_pct
        self.unique_holders_count = unique_holders_count
        self.top_holder_concentration_pct = top_holder_concentration_pct
        self.provider_used = provider_used
        self.is_successful = is_successful
        self.raw_data = raw_data or {}


class HolderCurveEngine:
    """
    Evaluates:
    1. Bonding curve progression (0 - 100% towards Pump.fun graduation / Raydium status)
    2. Number of active unique holders & holder distribution
    """

    def __init__(self):
        self._http_client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=6.0)
        return self._http_client

    def _calculate_bonding_score(self, bonding_pct: float) -> float:
        """
        Maps bonding curve % to opportunity score [HYPOTHESIS_INIT].
        Early accumulation is solid, high progress towards graduation gives maximum score.
        """
        if bonding_pct >= 85.0:
            return 100.0  # HYPOTHESIS_INIT: Near graduation / breakout momentum
        elif bonding_pct >= 50.0:
            return 90.0   # HYPOTHESIS_INIT: Solid half-way momentum
        elif bonding_pct >= 15.0:
            return 65.0   # HYPOTHESIS_INIT: Healthy accumulation phase
        elif bonding_pct > 0.0:
            return 30.0   # HYPOTHESIS_INIT: Very early infancy
        return 15.0

    def _calculate_holders_score(self, holder_count: int) -> float:
        """
        Maps unique holder count to score [HYPOTHESIS_INIT].
        Penalizes single-entity tokens, rewards organic holder spread.
        """
        if holder_count > 40:
            return 100.0  # HYPOTHESIS_INIT
        elif holder_count >= 16:
            return 85.0   # HYPOTHESIS_INIT
        elif holder_count >= 6:
            return 60.0   # HYPOTHESIS_INIT
        elif holder_count >= 2:
            return 35.0   # HYPOTHESIS_INIT
        return 10.0       # HYPOTHESIS_INIT: 1 or 0 holders (dev only)

    async def evaluate_holder_curve(
        self,
        event: RawTokenEvent,
        candidate_wallets: Optional[list[str]] = None
    ) -> HolderCurveResult:
        """
        Evaluates holder curve score for a given token event.
        """
        mint_address = event.token_address
        raw_payload = event.raw_payload or {}

        # 1. Determine Bonding Curve Progress %
        bonding_pct = 0.0
        provider = "payload"

        if event.launch_venue == "raydium":
            # Already launched/graduated to AMM
            bonding_pct = 100.0
        else:
            # Pump.fun venue calculation
            # Try to read directly from raw_payload (PumpPortal trade or new token event)
            if "progress" in raw_payload:
                try:
                    bonding_pct = float(raw_payload["progress"])
                except (ValueError, TypeError):
                    bonding_pct = 0.0
            elif "vSolInBondingCurve" in raw_payload:
                try:
                    v_sol = float(raw_payload["vSolInBondingCurve"])
                    # Pump.fun virtual curve starts around 30 SOL and completes at 115 SOL (~85 SOL delta)
                    bonding_pct = max(0.0, min(100.0, ((v_sol - 30.0) / 85.0) * 100.0))
                except (ValueError, TypeError):
                    bonding_pct = 0.0
            elif event.initial_sol_liquidity > 0:
                bonding_pct = max(0.0, min(100.0, (event.initial_sol_liquidity / 85.0) * 100.0))
            elif event.initial_buy_amount > 0 and event.total_supply > 0:
                bonding_pct = max(0.0, min(100.0, (event.initial_buy_amount / event.total_supply) * 100.0))

        # 2. Determine Unique Holder Count & Distribution
        holder_count = 1
        top_conc_pct = 0.0

        # Start with candidate wallets if provided from bundling/funding trace
        if candidate_wallets:
            holder_count = max(holder_count, len(set(candidate_wallets)))

        # Query top holders on-chain via Solana RPC
        try:
            largest_accs = await solana_rpc.get_token_largest_accounts(mint_address)
            if largest_accs:
                provider = "on_chain"
                active_accs = [acc for acc in largest_accs if float(acc.get("uiAmount", 0) or 0) > 0]
                holder_count = max(holder_count, len(active_accs))

                if event.total_supply > 0 and active_accs:
                    top_bal = float(active_accs[0].get("uiAmount", 0) or 0)
                    top_conc_pct = (top_bal / event.total_supply) * 100.0
        except Exception as rpc_err:
            logger.debug(f"RPC largest accounts check failed for {mint_address[:8]}: {rpc_err}")

        # Fallback to DexScreener if holder count is still 1 and not yet determined
        if holder_count <= 1:
            try:
                client = await self._get_client()
                resp = await client.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint_address}")
                if resp.status_code == 200:
                    data = resp.json()
                    pairs = data.get("pairs") or []
                    if pairs:
                        provider = "dexscreener"
                        # DexScreener doesn't always have exact holders, but gives pair presence
                        txns = pairs[0].get("txns", {}).get("h24", {})
                        buys = txns.get("buys", 0)
                        if buys > 0:
                            holder_count = max(holder_count, min(buys, 20))
            except Exception as ds_err:
                logger.debug(f"Dexscreener fallback failed for holder curve: {ds_err}")

        # 3. Calculate Component Scores & Aggregate
        score_bonding = self._calculate_bonding_score(bonding_pct)
        score_holders = self._calculate_holders_score(holder_count)

        # Composite score [HYPOTHESIS_INIT: 50% bonding progress + 50% holder distribution]
        final_score = round((0.50 * score_bonding) + (0.50 * score_holders), 2)

        return HolderCurveResult(
            score=final_score,
            bonding_curve_pct=round(bonding_pct, 2),
            unique_holders_count=holder_count,
            top_holder_concentration_pct=round(top_conc_pct, 2),
            provider_used=provider,
            is_successful=True,
            raw_data={
                "score_bonding": score_bonding,
                "score_holders": score_holders,
                "bonding_pct": bonding_pct,
                "holder_count": holder_count
            }
        )

    async def close(self):
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()


holder_curve_engine = HolderCurveEngine()
