import asyncio
from datetime import datetime, timedelta
from typing import Optional, Any
from src.config import settings
from src.database.client import db_manager
from src.database.models import SmartMoneyProfileModel
from src.utils.logger import logger


class SmartMoneyMatchResult:
    def __init__(
        self,
        score: float,
        matched_wallets_count: int,
        matched_wallets: list[str],
        total_tracked_wallets: int,
        is_successful: bool = True,
        raw_data: Optional[dict[str, Any]] = None
    ):
        self.score = score
        self.matched_wallets_count = matched_wallets_count
        self.matched_wallets = matched_wallets
        self.total_tracked_wallets = total_tracked_wallets
        self.is_successful = is_successful
        self.raw_data = raw_data or {}


class SmartMoneyEngine:
    """
    Smart Money Profiling Engine.
    Tracks high-conviction wallet accumulation and manages auto-promotion/demotion.
    """

    def __init__(self):
        self._cached_wallets: set[str] = set()
        self._last_cache_refresh: Optional[datetime] = None
        self._cache_ttl_seconds = 300  # 5 minutes

    async def _ensure_cache(self):
        """Refreshes active smart money wallet cache if expired or empty."""
        now = datetime.utcnow()
        if (
            not self._cached_wallets
            or not self._last_cache_refresh
            or (now - self._last_cache_refresh).total_seconds() > self._cache_ttl_seconds
        ):
            wallets_data = await db_manager.get_smart_money_wallets(active_only=True)
            self._cached_wallets = {w["wallet_address"] for w in wallets_data if "wallet_address" in w}
            self._last_cache_refresh = now

    async def evaluate_token_smart_money(
        self,
        candidate_wallet_addresses: list[str]
    ) -> SmartMoneyMatchResult:
        """
        Compares candidate early buyers/holders against the active Smart Money registry.
        """
        try:
            await self._ensure_cache()
            tracked_count = len(self._cached_wallets)

            if not candidate_wallet_addresses or tracked_count == 0:
                return SmartMoneyMatchResult(
                    score=0.0,
                    matched_wallets_count=0,
                    matched_wallets=[],
                    total_tracked_wallets=tracked_count,
                    is_successful=True,
                    raw_data={"matched_wallets": []}
                )

            # Find matching wallets
            matched = [addr for addr in candidate_wallet_addresses if addr in self._cached_wallets]
            match_count = len(matched)

            # Score mapping [HIPOTESIS_AWAL]:
            # 0 smart money: 0 score
            # 1 smart money: 40 score (early single conviction)
            # 2 smart money: 75 score (strong multi-wallet accumulation)
            # 3+ smart money: 100 score (high-conviction alpha convergence)
            if match_count == 0:
                score = 0.0
            elif match_count == 1:
                score = 40.0
            elif match_count == 2:
                score = 75.0
            else:
                score = 100.0

            if match_count > 0:
                logger.info(
                    f"💎 [bold cyan]Smart Money Detected[/bold cyan]: {match_count} smart wallets "
                    f"accumulated token ({', '.join([m[:8] + '...' for m in matched])})"
                )

            return SmartMoneyMatchResult(
                score=score,
                matched_wallets_count=match_count,
                matched_wallets=matched,
                total_tracked_wallets=tracked_count,
                is_successful=True,
                raw_data={
                    "matched_wallets": matched,
                    "match_count": match_count,
                    "total_tracked": tracked_count
                }
            )
        except Exception as e:
            logger.debug(f"Error evaluating smart money matches: {e}")
            return SmartMoneyMatchResult(
                score=0.0,
                matched_wallets_count=0,
                matched_wallets=[],
                total_tracked_wallets=len(self._cached_wallets),
                is_successful=False,
                raw_data={"error": str(e)}
            )

    async def evaluate_promotion_and_demotion(self, wallet_profile: SmartMoneyProfileModel) -> SmartMoneyProfileModel:
        """
        Evaluates a wallet for auto-promotion to ACTIVE or demotion based on performance & activity.
        Criteria: >= 20 trades, Net Profit > 15 SOL, Profit Factor > 1.8.
        """
        now = datetime.utcnow()
        trades = wallet_profile.total_trades_recorded
        net_profit = wallet_profile.net_realized_profit_sol
        pf = wallet_profile.profit_factor
        days_inactive = (now - wallet_profile.last_active_at).days

        # Demotion Check: Inactive for >= 14 days or Profit Factor < 1.0
        if days_inactive >= settings.smart_money_demotion_days or pf < 1.0:
            wallet_profile.tier = "DEMOTED"
            wallet_profile.is_active = False
            wallet_profile.notes = f"{wallet_profile.notes} | Demoted on {now.strftime('%Y-%m-%d')} (inactive {days_inactive}d / PF {pf:.2f})"
            return wallet_profile

        # Auto-Promotion Check
        if (
            trades >= settings.smart_money_min_trades
            and net_profit >= settings.smart_money_min_net_profit_sol
            and pf >= settings.smart_money_min_profit_factor
        ):
            wallet_profile.tier = "ACTIVE"
            wallet_profile.is_active = True
            wallet_profile.source = "AUTO_PROMOTED"

        return wallet_profile


smart_money_engine = SmartMoneyEngine()
