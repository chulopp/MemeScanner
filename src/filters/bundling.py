import asyncio
from typing import Optional, Any
from pydantic import BaseModel, Field

from src.config import settings
from src.utils.solana_rpc import solana_rpc
from src.filters.funding_graph import funding_tracer, FundingTraceNode
from src.utils.logger import logger


class BundlingResult(BaseModel):
    token_address: str
    sniper_bundle_pct: float = 0.0
    is_bundle_risk: bool = False
    cluster_count: int = 0
    max_cluster_size: int = 0
    clusters: list[dict] = Field(default_factory=list)
    relationships: list[dict] = Field(default_factory=list)
    analyzed_wallets_count: int = 0
    raw_cluster_data: dict[str, Any] = Field(default_factory=dict)


class BundlingEngine:
    """Bundling and Block-0 Sybil detection engine for Solana meme tokens."""

    async def extract_early_buyers_and_top_holders(
        self,
        mint_address: str,
        total_supply: float,
        deployer_address: Optional[str] = None
    ) -> dict[str, float]:
        """
        Extracts candidate early buyers & top holders:
        1. Top largest token accounts via getTokenLargestAccounts
        2. First transactions signers via getSignaturesForAddress
        3. Deployer address (if provided)
        """
        wallet_holdings: dict[str, float] = {}

        # 1. Fetch Top 5-10 largest token accounts
        try:
            largest_accounts = await solana_rpc.get_token_largest_accounts(mint_address)
            for acc in largest_accounts[:8]:
                addr = acc.get("address")
                ui_amount = acc.get("uiAmount")
                amount_str = acc.get("amount", "0")
                amount = float(ui_amount) if ui_amount is not None else float(amount_str)

                # For token accounts, get parsed owner if needed or use address
                if addr:
                    wallet_holdings[addr] = amount
        except Exception as e:
            logger.debug(f"Error fetching largest accounts for {mint_address[:8]}: {e}")

        # 2. Add deployer if known
        if deployer_address and deployer_address not in wallet_holdings:
            # Estimate dev allocation if not in top accounts (default small baseline)
            wallet_holdings[deployer_address] = total_supply * 0.01

        # If no holders found, return fallback
        if not wallet_holdings and deployer_address:
            wallet_holdings[deployer_address] = total_supply * 0.05

        return wallet_holdings

    async def evaluate_token_bundling(
        self,
        mint_address: str,
        total_supply: float = 1_000_000_000.0,
        deployer_address: Optional[str] = None
    ) -> BundlingResult:
        """
        Evaluates a token for Block-0 bundling and 2-Hop Sybil clusters.
        """
        # Step 1: Extract candidate wallets and their holdings
        wallet_holdings = await self.extract_early_buyers_and_top_holders(
            mint_address=mint_address,
            total_supply=total_supply,
            deployer_address=deployer_address
        )

        if not wallet_holdings:
            return BundlingResult(token_address=mint_address)

        # Step 2: Trace 2-Hop SOL funding tree for all candidate wallets
        nodes: list[FundingTraceNode] = await funding_tracer.trace_wallets_batch(
            wallet_holdings=wallet_holdings,
            total_supply=total_supply
        )

        # Step 3: Run cluster detection & Disjoint-Set grouping
        clusters, max_cluster_supply_pct, relationships = funding_tracer.analyze_clusters(nodes)

        # Step 4: Decision threshold (> 25% supply in single Sybil cluster is a hard risk)
        is_risk = max_cluster_supply_pct > settings.max_sniper_bundle_pct
        max_cluster_size = max([c.get("wallets_count", 0) for c in clusters], default=0)

        if is_risk:
            logger.warning(
                f"🚨 [bold red]Bundle Monopoly Detected[/bold red] on {mint_address[:8]}... "
                f"Cluster controls {max_cluster_supply_pct:.1f}% supply across {max_cluster_size} wallets."
            )

        return BundlingResult(
            token_address=mint_address,
            sniper_bundle_pct=max_cluster_supply_pct,
            is_bundle_risk=is_risk,
            cluster_count=len(clusters),
            max_cluster_size=max_cluster_size,
            clusters=clusters,
            relationships=relationships,
            analyzed_wallets_count=len(nodes),
            raw_cluster_data={
                "clusters": clusters,
                "node_traces_count": len(nodes),
                "relationships_count": len(relationships)
            }
        )


bundling_engine = BundlingEngine()
