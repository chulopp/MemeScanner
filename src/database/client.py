import asyncio
from typing import Optional, Any
from supabase import create_client, Client
from src.config import settings
from src.database.models import (
    TokenModel,
    FilterResultModel,
    WalletModel,
    WalletRelationshipModel,
    MetricSnapshotModel,
    SmartMoneyProfileModel
)
from src.utils.logger import logger


class DatabaseManager:
    """Async Database Manager interfacing with Supabase PostgreSQL."""

    def __init__(self):
        self._client: Optional[Client] = None
        self._connected = False
        self._in_memory_tokens: dict[str, dict] = {}
        self._in_memory_filters: dict[str, dict] = {}
        self._in_memory_wallets: dict[str, dict] = {}
        self._in_memory_snapshots: list[dict] = []
        self._in_memory_smart_money: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    def connect(self):
        effective_key = settings.effective_supabase_key
        if settings.supabase_url and effective_key:
            try:
                self._client = create_client(settings.supabase_url, effective_key)
                self._connected = True
                logger.info("Connected to Supabase client successfully (using service_role/configured key).")
            except Exception as e:
                logger.warning(f"Failed to connect to Supabase: {e}. Using in-memory fallback.")
                self._connected = False
        else:
            logger.info("No Supabase credentials provided. Using in-memory database fallback.")

    async def upsert_wallet(self, wallet: WalletModel) -> bool:
        """Upsert wallet deployer record."""
        data = {
            "wallet_address": wallet.wallet_address,
            "first_seen": wallet.first_seen.isoformat(),
            "reputation_score": wallet.reputation_score,
            "rug_count_history": wallet.rug_count_history,
            "total_tokens_launched": wallet.total_tokens_launched,
            "tags": wallet.tags
        }

        if self._connected and self._client:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    lambda: self._client.table("wallets").upsert(data).execute()
                )
                return True
            except Exception as e:
                logger.warning(f"Supabase upsert wallet error: {e}")

        async with self._lock:
            self._in_memory_wallets[wallet.wallet_address] = data
            return True

    async def get_wallet(self, wallet_address: str) -> Optional[dict]:
        """Fetch wallet data."""
        if self._connected and self._client:
            try:
                loop = asyncio.get_running_loop()
                response = await loop.run_in_executor(
                    None,
                    lambda: self._client.table("wallets").select("*").eq("wallet_address", wallet_address).execute()
                )
                if response.data:
                    return response.data[0]
            except Exception as e:
                logger.debug(f"Supabase get wallet error: {e}")

        async with self._lock:
            return self._in_memory_wallets.get(wallet_address)

    async def insert_token(self, token: TokenModel) -> bool:
        """Insert or update token record."""
        # Ensure deployer wallet exists first if provided
        if token.deployer_wallet_address:
            await self.upsert_wallet(WalletModel(
                wallet_address=token.deployer_wallet_address,
                total_tokens_launched=1
            ))

        data = {
            "token_address": token.token_address,
            "symbol": token.symbol,
            "name": token.name,
            "deployer_wallet_address": token.deployer_wallet_address,
            "launch_timestamp": token.launch_timestamp.isoformat(),
            "launch_venue": token.launch_venue,
            "status": token.status,
            "initial_metadata": token.initial_metadata
        }

        if self._connected and self._client:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    lambda: self._client.table("tokens").upsert(data).execute()
                )
                return True
            except Exception as e:
                logger.warning(f"Supabase insert token error: {e}")

        async with self._lock:
            self._in_memory_tokens[token.token_address] = data
            return True

    async def update_token_status(self, token_address: str, status: str) -> bool:
        """Update token status (e.g. PASSED_SAFETY, REJECTED)."""
        if self._connected and self._client:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    lambda: self._client.table("tokens").update({"status": status}).eq("token_address", token_address).execute()
                )
                return True
            except Exception as e:
                logger.warning(f"Supabase update token status error: {e}")

        async with self._lock:
            if token_address in self._in_memory_tokens:
                self._in_memory_tokens[token_address]["status"] = status
            return True

    async def insert_filter_result(self, result: FilterResultModel) -> bool:
        """Insert filter check result."""
        data = {
            "token_address": result.token_address,
            "checked_at": result.checked_at.isoformat(),
            "mint_authority_renounced": result.mint_authority_renounced,
            "freeze_authority_renounced": result.freeze_authority_renounced,
            "lp_locked_or_burned": result.lp_locked_or_burned,
            "lp_lock_pct": result.lp_lock_pct,
            "top10_holder_pct": result.top10_holder_pct,
            "honeypot_check_passed": result.honeypot_check_passed,
            "dev_holding_pct": result.dev_holding_pct,
            "sniper_bundle_pct": result.sniper_bundle_pct,
            "instant_scalp_flags_count": result.instant_scalp_flags_count,
            "filter_pass": result.filter_pass,
            "rejection_reason": result.rejection_reason,
            "raw_check_data": result.raw_check_data
        }

        if self._connected and self._client:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    lambda: self._client.table("filter_results").insert(data).execute()
                )
                return True
            except Exception as e:
                logger.warning(f"Supabase insert filter result error: {e}")

        async with self._lock:
            self._in_memory_filters[result.token_address] = data
            return True

    async def batch_insert_relationships(self, relationships: list[WalletRelationshipModel]) -> bool:
        """Batch insert detected wallet funding relationships."""
        if not relationships:
            return True

        rows = [
            {
                "wallet_a": r.wallet_a,
                "wallet_b": r.wallet_b,
                "relationship_type": r.relationship_type,
                "hop_distance": r.hop_distance,
                "detected_at": r.detected_at.isoformat(),
                "shared_funding_sol": r.shared_funding_sol,
                "confidence_score": r.confidence_score
            }
            for r in relationships
        ]

        if self._connected and self._client:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    lambda: self._client.table("wallet_relationships").insert(rows).execute()
                )
                return True
            except Exception as e:
                logger.warning(f"Supabase batch insert wallet_relationships error: {e}")

        return True

    async def insert_metric_snapshot(self, snapshot: MetricSnapshotModel) -> bool:
        """Insert an opportunity metric snapshot into Supabase/in-memory."""
        data = {
            "token_address": snapshot.token_address,
            "snapshot_at": snapshot.snapshot_at.isoformat(),
            "opportunity_score": snapshot.opportunity_score,
            "score_vol_velocity": snapshot.score_vol_velocity,
            "score_smart_money": snapshot.score_smart_money,
            "score_global_fee": snapshot.score_global_fee,
            "score_holder_curve": snapshot.score_holder_curve,
            "score_social_meta": snapshot.score_social_meta,
            "market_cap_usd": snapshot.market_cap_usd,
            "liquidity_usd": snapshot.liquidity_usd,
            "volume_5m_usd": snapshot.volume_5m_usd,
            "buy_tx_count_5m": snapshot.buy_tx_count_5m,
            "sell_tx_count_5m": snapshot.sell_tx_count_5m,
            "net_buy_pressure_ratio": snapshot.net_buy_pressure_ratio,
            "global_priority_fees_sol": snapshot.global_priority_fees_sol,
            "bonding_curve_pct": snapshot.bonding_curve_pct,
            "unique_holders_count": snapshot.unique_holders_count,
            "weights_used": snapshot.weights_used,
            "active_components": snapshot.active_components,
            "raw_metrics": snapshot.raw_metrics
        }

        if self._connected and self._client:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    lambda: self._client.table("metric_snapshots").insert(data).execute()
                )
                return True
            except Exception as e:
                logger.warning(f"Supabase insert metric_snapshot error: {e}")

        async with self._lock:
            self._in_memory_snapshots.append(data)
            return True

    async def upsert_smart_money_wallet(self, profile: SmartMoneyProfileModel) -> bool:
        """Upsert a smart money profile."""
        data = {
            "wallet_address": profile.wallet_address,
            "tier": profile.tier,
            "is_active": profile.is_active,
            "first_added": profile.first_added.isoformat(),
            "last_active_at": profile.last_active_at.isoformat(),
            "total_trades_recorded": profile.total_trades_recorded,
            "total_volume_sol": profile.total_volume_sol,
            "net_realized_profit_sol": profile.net_realized_profit_sol,
            "win_rate_pct": profile.win_rate_pct,
            "profit_factor": profile.profit_factor,
            "source": profile.source,
            "notes": profile.notes
        }

        if self._connected and self._client:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    lambda: self._client.table("smart_money_profiles").upsert(data).execute()
                )
                return True
            except Exception as e:
                logger.warning(f"Supabase upsert smart_money_profiles error: {e}")

        async with self._lock:
            self._in_memory_smart_money[profile.wallet_address] = data
            return True

    async def batch_upsert_smart_money_wallets(self, profiles: list[SmartMoneyProfileModel]) -> bool:
        """Batch upsert multiple smart money profiles."""
        if not profiles:
            return True

        rows = [
            {
                "wallet_address": p.wallet_address,
                "tier": p.tier,
                "is_active": p.is_active,
                "first_added": p.first_added.isoformat(),
                "last_active_at": p.last_active_at.isoformat(),
                "total_trades_recorded": p.total_trades_recorded,
                "total_volume_sol": p.total_volume_sol,
                "net_realized_profit_sol": p.net_realized_profit_sol,
                "win_rate_pct": p.win_rate_pct,
                "profit_factor": p.profit_factor,
                "source": p.source,
                "notes": p.notes
            }
            for p in profiles
        ]

        if self._connected and self._client:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    lambda: self._client.table("smart_money_profiles").upsert(rows).execute()
                )
                return True
            except Exception as e:
                logger.warning(f"Supabase batch upsert smart_money_profiles error: {e}")

        async with self._lock:
            for r in rows:
                self._in_memory_smart_money[r["wallet_address"]] = r
            return True

    async def get_smart_money_wallets(self, active_only: bool = True) -> list[dict]:
        """Fetch list of smart money wallets from Supabase / in-memory."""
        if self._connected and self._client:
            try:
                loop = asyncio.get_running_loop()
                query = self._client.table("smart_money_profiles").select("*")
                if active_only:
                    query = query.eq("is_active", True)
                response = await loop.run_in_executor(
                    None,
                    lambda: query.execute()
                )
                if response.data:
                    return response.data
            except Exception as e:
                logger.debug(f"Supabase get_smart_money_wallets error: {e}")

        async with self._lock:
            if active_only:
                return [w for w in self._in_memory_smart_money.values() if w.get("is_active", True)]
            return list(self._in_memory_smart_money.values())

    # ------------------------------------------------------------------ #
    # Generic CRUD helpers (used by backtest modules)                      #
    # ------------------------------------------------------------------ #

    async def query(
        self,
        table: str,
        filters: Optional[dict[str, str]] = None,
        select: str = "*",
        limit: int = 1000
    ) -> list[dict]:
        """
        Generic query — fetches rows from any Supabase table.
        `filters` is a dict of {column: "operator.value"} e.g. {"label": "not.is.null"}.
        Falls back to empty list on error or no connection.
        """
        if self._connected and self._client:
            try:
                loop = asyncio.get_running_loop()

                def _fetch_page(offset: int, chunk_size: int):
                    q = self._client.table(table).select(select)
                    if filters:
                        for col, val in filters.items():
                            if val.startswith("not.is.null"):
                                q = q.not_.is_(col, "null")
                            elif val.startswith("is.null"):
                                q = q.is_(col, "null")
                            elif val.startswith("eq."):
                                q = q.eq(col, val[3:])
                            elif val.startswith("not."):
                                q = q.neq(col, val[4:])
                            elif val.startswith("gte."):
                                q = q.gte(col, val[4:])
                            elif val.startswith("lte."):
                                q = q.lte(col, val[4:])
                    q = q.range(offset, offset + chunk_size - 1)
                    return q.execute()

                all_rows = []
                remaining = limit
                offset = 0

                while remaining > 0:
                    chunk_size = min(remaining, 1000)
                    response = await loop.run_in_executor(None, lambda o=offset, c=chunk_size: _fetch_page(o, c))
                    data = response.data or []
                    if not data:
                        break
                    all_rows.extend(data)
                    offset += len(data)
                    remaining -= len(data)
                    if len(data) < chunk_size:
                        break  # Reached end of table

                return all_rows
            except Exception as e:
                logger.debug(f"Supabase generic query error on '{table}': {e}")
        return []


    async def insert(self, table: str, row: dict) -> bool:
        """Generic insert for any table. Returns True on success."""
        if self._connected and self._client:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    lambda: self._client.table(table).insert(row).execute()
                )
                return True
            except Exception as e:
                logger.debug(f"Supabase insert error on '{table}': {e}")
        return False

    async def upsert(self, table: str, row: dict, on_conflict: str = "id") -> bool:
        """Generic upsert for any table."""
        if self._connected and self._client:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    lambda: self._client.table(table).upsert(row, on_conflict=on_conflict).execute()
                )
                return True
            except Exception as e:
                logger.debug(f"Supabase upsert error on '{table}': {e}")
        return False

    async def update(self, table: str, updates: dict, filters: dict) -> bool:
        """Generic update for any table. filters: {column: 'eq.value'}."""
        if self._connected and self._client:
            try:
                loop = asyncio.get_running_loop()

                def _run():
                    q = self._client.table(table).update(updates)
                    for col, val in filters.items():
                        if val.startswith("eq."):
                            q = q.eq(col, val[3:])
                    return q.execute()

                await loop.run_in_executor(None, _run)
                return True
            except Exception as e:
                logger.debug(f"Supabase update error on '{table}': {e}")
        return False

    async def initialize(self) -> None:
        """Alias to ensure DB is connected (for CLI entry points)."""
        if not self._connected:
            self.connect()

    # ------------------------------------------------------------------ #
    # Smart Money Discovery pipeline methods                               #
    # ------------------------------------------------------------------ #

    async def upsert_discovery_wallet(self, data: dict) -> bool:
        """
        Upsert a wallet evaluation result into smart_money_wallets.
        Used as checkpoint during the qualification pipeline.
        """
        if self._connected and self._client:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    lambda: self._client.table("smart_money_wallets")
                        .upsert(data, on_conflict="wallet_address")
                        .execute()
                )
                return True
            except Exception as e:
                logger.debug(f"Supabase upsert smart_money_wallets error: {e}")
        # In-memory fallback (key on wallet_address)
        async with self._lock:
            self._in_memory_smart_money[data.get("wallet_address", "")] = data
        return True

    async def batch_upsert_discovery_hits(self, rows: list[dict]) -> bool:
        """
        Batch upsert early buy hits into smart_money_hits.
        Deduplicates in-memory by (wallet_address, token_address) to prevent
        Postgres 'ON CONFLICT DO UPDATE command cannot affect row a second time' error.
        """
        if not rows:
            return True
        # Deduplicate within batch
        unique_map = {}
        for r in rows:
            key = (r.get("wallet_address"), r.get("token_address"))
            if key[0] and key[1]:
                unique_map[key] = r
        deduped_rows = list(unique_map.values())
        if not deduped_rows:
            return True

        if self._connected and self._client:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    lambda: self._client.table("smart_money_hits")
                        .upsert(deduped_rows, on_conflict="wallet_address,token_address")
                        .execute()
                )
                return True
            except Exception as e:
                logger.debug(f"Supabase batch upsert smart_money_hits error: {e}")
        return True

    async def get_traced_token_addresses(self) -> set[str]:
        """
        Returns the set of token_address values already present in smart_money_hits.
        Used by --resume flag to skip tokens already traced.
        """
        if self._connected and self._client:
            try:
                loop = asyncio.get_running_loop()
                resp = await loop.run_in_executor(
                    None,
                    lambda: self._client.table("smart_money_hits")
                        .select("token_address")
                        .eq("source", "TRACE")
                        .execute()
                )
                if resp.data:
                    return {row["token_address"] for row in resp.data}
            except Exception as e:
                logger.debug(f"Supabase get_traced_token_addresses error: {e}")
        return set()

    async def get_evaluated_wallet_addresses(self) -> set[str]:
        """
        Returns the set of wallet_address values already evaluated in smart_money_wallets.
        Used by --resume flag to skip wallets already qualified/rejected.
        """
        if self._connected and self._client:
            try:
                loop = asyncio.get_running_loop()
                resp = await loop.run_in_executor(
                    None,
                    lambda: self._client.table("smart_money_wallets")
                        .select("wallet_address")
                        .neq("status", "PENDING")
                        .execute()
                )
                if resp.data:
                    return {row["wallet_address"] for row in resp.data}
            except Exception as e:
                logger.debug(f"Supabase get_evaluated_wallet_addresses error: {e}")
        return set()


db_manager = DatabaseManager()

