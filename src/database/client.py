import asyncio
from typing import Optional, Any
from supabase import create_client, Client
from src.config import settings
from src.database.models import TokenModel, FilterResultModel, WalletModel, WalletRelationshipModel
from src.utils.logger import logger


class DatabaseManager:
    """Async Database Manager interfacing with Supabase PostgreSQL."""

    def __init__(self):
        self._client: Optional[Client] = None
        self._connected = False
        self._in_memory_tokens: dict[str, dict] = {}
        self._in_memory_filters: dict[str, dict] = {}
        self._in_memory_wallets: dict[str, dict] = {}
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


db_manager = DatabaseManager()
