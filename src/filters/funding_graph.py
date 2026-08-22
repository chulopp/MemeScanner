import asyncio
import time
from datetime import datetime
from typing import Optional, Any
from pydantic import BaseModel, Field
import httpx

from src.config import settings
from src.utils.known_wallets import is_known_cex_or_system
from src.utils.solana_rpc import solana_rpc
from src.utils.logger import logger


class FundingHopInfo(BaseModel):
    funder_address: Optional[str] = None
    funding_timestamp: Optional[datetime] = None
    amount_sol: float = 0.0
    signature: Optional[str] = None
    is_known_cex: bool = False


class FundingTraceNode(BaseModel):
    wallet_address: str
    hop1: Optional[FundingHopInfo] = None
    hop2: Optional[FundingHopInfo] = None
    token_holding_amount: float = 0.0
    token_holding_pct: float = 0.0


class DisjointSet:
    """Union-Find Disjoint Set data structure with path compression."""

    def __init__(self, elements: list[str]):
        self.parent = {el: el for el in elements}
        self.rank = {el: 0 for el in elements}

    def find(self, i: str) -> str:
        if self.parent[i] == i:
            return i
        self.parent[i] = self.find(self.parent[i])
        return self.parent[i]

    def union(self, i: str, j: str) -> bool:
        root_i = self.find(i)
        root_j = self.find(j)
        if root_i != root_j:
            if self.rank[root_i] < self.rank[root_j]:
                self.parent[root_i] = root_j
            elif self.rank[root_i] > self.rank[root_j]:
                self.parent[root_j] = root_i
            else:
                self.parent[root_j] = root_i
                self.rank[root_i] += 1
            return True
        return False


class FundingGraphTracer:
    """2-Hop SOL Funding Graph Engine using Helius Enhanced API + RPC Fallback."""

    def __init__(self):
        self._http_client: Optional[httpx.AsyncClient] = None
        self._semaphore = asyncio.Semaphore(settings.rpc_concurrency_limit)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=10.0)
        return self._http_client

    async def _find_earliest_inbound_transfer_helius(self, wallet_address: str) -> Optional[FundingHopInfo]:
        """Fetches parsed transactions via Helius Enhanced API to find earliest inbound SOL transfer."""
        if not settings.helius_api_key:
            return None

        url = f"https://api.helius.xyz/v0/addresses/{wallet_address}/transactions"
        params = {
            "api-key": settings.helius_api_key,
            "type": "TRANSFER",
            "limit": 50
        }

        async with self._semaphore:
            try:
                client = await self._get_client()
                response = await client.get(url, params=params)
                if response.status_code != 200:
                    logger.debug(f"Helius Enhanced API HTTP {response.status_code} for {wallet_address[:8]}...")
                    return None

                data = response.json()
                if not isinstance(data, list) or not data:
                    return None

                # Enhanced API returns newest to oldest; search from end (oldest)
                for tx in reversed(data):
                    native_transfers = tx.get("nativeTransfers", [])
                    for transfer in native_transfers:
                        recipient = transfer.get("toUserAccount")
                        sender = transfer.get("fromUserAccount")
                        amount_lamports = transfer.get("amount", 0)

                        if recipient == wallet_address and sender and sender != wallet_address:
                            timestamp = tx.get("timestamp")
                            dt = datetime.utcfromtimestamp(timestamp) if timestamp else None
                            amount_sol = amount_lamports / 1_000_000_000.0

                            return FundingHopInfo(
                                funder_address=sender,
                                funding_timestamp=dt,
                                amount_sol=amount_sol,
                                signature=tx.get("signature"),
                                is_known_cex=is_known_cex_or_system(sender)
                            )
            except Exception as e:
                logger.debug(f"Helius Enhanced API error for {wallet_address[:8]}: {e}")

        return None

    async def _find_earliest_inbound_transfer_rpc(self, wallet_address: str) -> Optional[FundingHopInfo]:
        """Fallback method using standard Solana JSON-RPC to inspect oldest transaction."""
        try:
            signatures = await solana_rpc.get_signatures_for_address(wallet_address, limit=50)
            if not signatures:
                return None

            # Oldest signature in batch
            oldest_sig_item = signatures[-1]
            sig = oldest_sig_item.get("signature")
            block_time = oldest_sig_item.get("blockTime")
            if not sig:
                return None

            tx_res = await solana_rpc._rpc_call(
                "getTransaction",
                [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
            )
            if not tx_res:
                return None

            meta = tx_res.get("meta", {})
            pre_balances = meta.get("preBalances", [])
            post_balances = meta.get("postBalances", [])
            transaction = tx_res.get("transaction", {})
            message = transaction.get("message", {})
            account_keys = message.get("accountKeys", [])

            # Extract signer (fee payer / sender)
            if account_keys:
                first_acc = account_keys[0]
                sender = first_acc.get("pubkey") if isinstance(first_acc, dict) else str(first_acc)
                if sender and sender != wallet_address:
                    dt = datetime.utcfromtimestamp(block_time) if block_time else None
                    # Estimate SOL transferred from balance change
                    amount_sol = 0.0
                    for idx, acc in enumerate(account_keys):
                        pub = acc.get("pubkey") if isinstance(acc, dict) else str(acc)
                        if pub == wallet_address and idx < len(post_balances) and idx < len(pre_balances):
                            diff = (post_balances[idx] - pre_balances[idx]) / 1_000_000_000.0
                            if diff > 0:
                                amount_sol = diff
                            break

                    return FundingHopInfo(
                        funder_address=sender,
                        funding_timestamp=dt,
                        amount_sol=amount_sol,
                        signature=sig,
                        is_known_cex=is_known_cex_or_system(sender)
                    )
        except Exception as e:
            logger.debug(f"RPC fallback funding trace error for {wallet_address[:8]}: {e}")

        return None

    async def get_funder_hop(self, wallet_address: str) -> Optional[FundingHopInfo]:
        """Attempts Helius Enhanced API first; falls back to standard RPC."""
        if is_known_cex_or_system(wallet_address):
            return None

        # 1. Primary: Helius Enhanced Transactions
        funder = await self._find_earliest_inbound_transfer_helius(wallet_address)
        if funder:
            return funder

        # 2. Fallback: Standard RPC
        return await self._find_earliest_inbound_transfer_rpc(wallet_address)

    async def trace_wallet_node(self, wallet_address: str, holding_amount: float = 0.0, holding_pct: float = 0.0) -> FundingTraceNode:
        """Traces Hop 1 (Funder) and Hop 2 (Grandfunder) for a given wallet."""
        node = FundingTraceNode(
            wallet_address=wallet_address,
            token_holding_amount=holding_amount,
            token_holding_pct=holding_pct
        )

        # Hop 1
        hop1 = await self.get_funder_hop(wallet_address)
        node.hop1 = hop1

        # Hop 2 (only if Hop 1 exists and is not a known CEX)
        if hop1 and hop1.funder_address and not hop1.is_known_cex:
            hop2 = await self.get_funder_hop(hop1.funder_address)
            node.hop2 = hop2

        return node

    async def trace_wallets_batch(self, wallet_holdings: dict[str, float], total_supply: float) -> list[FundingTraceNode]:
        """Traces a batch of wallets concurrently."""
        tasks = []
        for wallet, amount in wallet_holdings.items():
            pct = (amount / total_supply * 100.0) if total_supply > 0 else 0.0
            tasks.append(self.trace_wallet_node(wallet, amount, pct))

        results = await asyncio.gather(*tasks, return_exceptions=False)
        return list(results)

    def analyze_clusters(
        self,
        nodes: list[FundingTraceNode]
    ) -> tuple[list[dict], float, list[dict]]:
        """
        Groups nodes into Sybil clusters based on:
        1. Direct Funding (W1 directly funded W2)
        2. Shared Hop-1 Funder (W1 and W2 funded by same non-CEX parent)
        3. Shared Hop-2 Grandfunder (W1 and W2 have same non-CEX root source)

        Returns:
            (clusters, max_cluster_supply_pct, relationships_to_persist)
        """
        wallets = [n.wallet_address for n in nodes]
        dset = DisjointSet(wallets)
        relationships: list[dict] = []

        # Compare every pair (W_i, W_j)
        for i in range(len(nodes)):
            for j in range(i + 1, len(nodes)):
                n1 = nodes[i]
                n2 = nodes[j]
                w1, w2 = n1.wallet_address, n2.wallet_address

                matched = False
                rel_type = None
                hop_dist = 0
                shared_funder = None
                confidence = 0.0

                # 1. Direct funding
                if n1.hop1 and n1.hop1.funder_address == w2:
                    matched = True
                    rel_type = "DIRECT_FUNDING"
                    hop_dist = 1
                    shared_funder = w2
                    confidence = 0.95
                elif n2.hop1 and n2.hop1.funder_address == w1:
                    matched = True
                    rel_type = "DIRECT_FUNDING"
                    hop_dist = 1
                    shared_funder = w1
                    confidence = 0.95

                # 2. Shared Hop 1 Funder
                elif (
                    n1.hop1 and n2.hop1 and
                    n1.hop1.funder_address == n2.hop1.funder_address and
                    not n1.hop1.is_known_cex and
                    not n2.hop1.is_known_cex
                ):
                    matched = True
                    rel_type = "SHARED_FUNDER_HOP1"
                    hop_dist = 1
                    shared_funder = n1.hop1.funder_address
                    # Higher confidence if funded within 24h
                    confidence = 0.90
                    if n1.hop1.funding_timestamp and n2.hop1.funding_timestamp:
                        time_diff = abs((n1.hop1.funding_timestamp - n2.hop1.funding_timestamp).total_seconds())
                        if time_diff < 86400:  # < 24 hours
                            confidence = 0.98

                # 3. Shared Hop 2 Grandfunder
                elif (
                    n1.hop2 and n2.hop2 and
                    n1.hop2.funder_address == n2.hop2.funder_address and
                    not n1.hop2.is_known_cex and
                    not n2.hop2.is_known_cex
                ):
                    matched = True
                    rel_type = "SHARED_FUNDER_HOP2"
                    hop_dist = 2
                    shared_funder = n1.hop2.funder_address
                    confidence = 0.80

                if matched and rel_type:
                    dset.union(w1, w2)
                    relationships.append({
                        "wallet_a": w1,
                        "wallet_b": w2,
                        "relationship_type": rel_type,
                        "hop_distance": hop_dist,
                        "shared_funding_sol": max(
                            n1.hop1.amount_sol if n1.hop1 else 0.0,
                            n2.hop1.amount_sol if n2.hop1 else 0.0
                        ),
                        "confidence_score": confidence,
                        "shared_funder": shared_funder
                    })

        # Group wallets by disjoint set root
        clusters_map: dict[str, list[FundingTraceNode]] = {}
        for node in nodes:
            root = dset.find(node.wallet_address)
            clusters_map.setdefault(root, []).append(node)

        clusters_result = []
        max_cluster_supply_pct = 0.0

        for root, members in clusters_map.items():
            # Only count as cluster if >= 2 wallets or single wallet holding significant supply
            total_supply_pct = sum(m.token_holding_pct for m in members)
            total_tokens = sum(m.token_holding_amount for m in members)

            if len(members) >= 2 or total_supply_pct >= 5.0:
                if len(members) >= 2 and total_supply_pct > max_cluster_supply_pct:
                    max_cluster_supply_pct = total_supply_pct

                clusters_result.append({
                    "root": root,
                    "wallets_count": len(members),
                    "wallets": [m.wallet_address for m in members],
                    "total_supply_pct": total_supply_pct,
                    "total_tokens": total_tokens
                })

        return clusters_result, max_cluster_supply_pct, relationships

    async def close(self):
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()


funding_tracer = FundingGraphTracer()
