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
        deployer_address: Optional[str] = None,
        deployer_initial_buy: float = 0.0
    ) -> dict[str, float]:
        """
        Extracts candidate early buyers & top holders:
        1. Top largest token accounts via getTokenLargestAccounts
        2. Resolves each ATA to owner wallet address
        3. Adds deployer address if provided (with actual initial buy or 0.0)
        """
        wallet_holdings: dict[str, float] = {}

        # 1. Fetch Top 8 largest token accounts
        try:
            largest_accounts = await solana_rpc.get_token_largest_accounts(mint_address)
            candidate_atas = largest_accounts[:8]

            # Resolve ATA -> owner wallet concurrently
            resolve_tasks = []
            amounts = []
            raw_addrs = []
            for acc in candidate_atas:
                addr = acc.get("address")
                if not addr:
                    continue
                ui_amount = acc.get("uiAmount")
                amount_str = acc.get("amount", "0")
                amount = float(ui_amount) if ui_amount is not None else float(amount_str)

                raw_addrs.append(addr)
                amounts.append(amount)
                resolve_tasks.append(solana_rpc.get_token_account_owner(addr))

            if resolve_tasks:
                owners = await asyncio.gather(*resolve_tasks, return_exceptions=True)
                for raw_addr, amount, owner_res in zip(raw_addrs, amounts, owners):
                    owner = owner_res if isinstance(owner_res, str) and owner_res else raw_addr
                    # Aggregate holding if multiple ATAs resolve to same owner
                    wallet_holdings[owner] = wallet_holdings.get(owner, 0.0) + amount
        except Exception as e:
            logger.debug(f"Error fetching/resolving largest accounts for {mint_address[:8]}: {e}")

        # 2. Add deployer if known and not already in wallet_holdings
        if deployer_address and deployer_address not in wallet_holdings:
            wallet_holdings[deployer_address] = deployer_initial_buy

        return wallet_holdings

    async def evaluate_token_bundling(
        self,
        mint_address: str,
        total_supply: float = 1_000_000_000.0,
        deployer_address: Optional[str] = None,
        deployer_initial_buy: float = 0.0
    ) -> BundlingResult:
        """
        Evaluates a token for Block-0 bundling and 2-Hop Sybil clusters.
        """
        # Guard against 0 or negative total_supply
        total_supply = max(total_supply, 1.0)

        # Step 1: Extract candidate wallets and their holdings
        wallet_holdings = await self.extract_early_buyers_and_top_holders(
            mint_address=mint_address,
            total_supply=total_supply,
            deployer_address=deployer_address,
            deployer_initial_buy=deployer_initial_buy
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
