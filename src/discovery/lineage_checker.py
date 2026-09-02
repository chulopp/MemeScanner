"""
Smart Money Lineage Checker — Phase 2b
========================================
Checks whether a newly-seen candidate wallet's funding origin (1-hop or 2-hop)
traces back to a wallet already qualified in smart_money_profiles.

If a match is found:
  - Tag wallet as funded_by_known_smart_money = True
  - Record lineage_parent_wallet = the matching known wallet address
  - Apply a relaxed trade count threshold (lineage_min_trades_90d) in Phase 3b

Reuses FundingGraphTracer from src/filters/funding_graph.py (no new API calls).
The tracer uses Helius Enhanced API (Tier 1) with RPC fallback (Tier 2) and
automatically skips known CEX/system wallets.

Design decision: known_smart_wallets is loaded ONCE per run from Supabase.
Wallets newly qualified in the SAME run do NOT contribute to lineage discovery
until the next run. This is intentional — ensures deterministic pipeline order.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.filters.funding_graph import FundingGraphTracer
from src.utils.logger import logger


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class LineageCheckResult:
    wallet_address: str
    funded_by_known_smart_money: bool = False
    lineage_parent_wallet: Optional[str] = None    # address of the known smart money funder
    lineage_hop_distance: int = 0                  # 0 = no lineage, 1 = direct, 2 = grandparent


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------

class SmartMoneyLineageChecker:
    """
    Phase 2b: Funding Lineage Check.

    For each candidate wallet, traces its funding origin up to 2 hops
    and checks whether any funder is in the known_smart_wallets set.

    Usage:
        checker = SmartMoneyLineageChecker()
        known = await db_manager.get_active_smart_money_addresses()   # load once
        result = await checker.check("WALLET_ADDRESS", known)
        if result.funded_by_known_smart_money:
            # use lineage_min_trades_90d instead of min_trades_90d
    """

    def __init__(self) -> None:
        self._tracer = FundingGraphTracer()

    async def check(
        self,
        wallet: str,
        known_smart_wallets: set[str],
    ) -> LineageCheckResult:
        """
        Trace funding origin of wallet and check for smart money lineage.

        Args:
            wallet: candidate wallet address to check
            known_smart_wallets: set of wallet addresses in smart_money_profiles (is_active=True)
                                 Loaded once per discovery run from Supabase.
        Returns:
            LineageCheckResult with lineage details.
        """
        result = LineageCheckResult(wallet_address=wallet)

        if not known_smart_wallets:
            return result  # nothing to match against

        try:
            node = await self._tracer.trace_wallet_node(wallet)

            # Hop 1: direct funder
            if node.hop1 and node.hop1.funder_address:
                funder1 = node.hop1.funder_address
                if funder1 in known_smart_wallets:
                    result.funded_by_known_smart_money = True
                    result.lineage_parent_wallet = funder1
                    result.lineage_hop_distance = 1
                    logger.debug(
                        f"[Lineage] {wallet[:8]} ← direct fund from known SM {funder1[:8]} "
                        f"({node.hop1.amount_sol:.2f} SOL, is_cex={node.hop1.is_known_cex})"
                    )
                    return result

                # Hop 2: funder's funder (only if hop1 is not a CEX)
                if not node.hop1.is_known_cex and node.hop2 and node.hop2.funder_address:
                    funder2 = node.hop2.funder_address
                    if funder2 in known_smart_wallets:
                        result.funded_by_known_smart_money = True
                        result.lineage_parent_wallet = funder2
                        result.lineage_hop_distance = 2
                        logger.debug(
                            f"[Lineage] {wallet[:8]} ← 2-hop from known SM {funder2[:8]} "
                            f"(via {funder1[:8]})"
                        )
                        return result

        except Exception as e:
            logger.debug(f"[Lineage] Trace error for {wallet[:8]}: {e}")

        return result

    async def check_batch(
        self,
        wallets: list[str],
        known_smart_wallets: set[str],
    ) -> dict[str, LineageCheckResult]:
        """
        Check lineage for multiple wallets. Results keyed by wallet address.
        NOTE: trace_wallet_node makes Helius API calls — run sequentially
        or with light concurrency to respect rate limits.
        """
        results: dict[str, LineageCheckResult] = {}
        for wallet in wallets:
            results[wallet] = await self.check(wallet, known_smart_wallets)
        return results


# Singleton
lineage_checker = SmartMoneyLineageChecker()
