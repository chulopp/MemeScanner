"""
Smart Money Wallet Discovery Engine
====================================
Discovers "Smart Money" wallets on Solana by:
  Phase 1 — Collect runner tokens from DexScreener (≥2x return)
  Phase 2 — Trace early buyers (≤10 min after launch) via Helius
  Phase 3 — Qualify candidates:
             3a. SOL balance ≥ 50
             3b. Runner hit count ≥ 3 (across different tokens)
             3c. Negative control: pull wallet's full buy history,
                 classify each early-bought token as RUNNER/DEAD/NEUTRAL,
                 require ≥10 classifiable buys and runner ratio ≥15%
  Phase 4 — Sync QUALIFIED wallets → smart_money_profiles (SEED tier)

All decisions confirmed via grill session 2026-08-31.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from collections import defaultdict

import httpx
from rich.console import Console
from rich.progress import (
    Progress, SpinnerColumn, BarColumn,
    TextColumn, TimeElapsedColumn, MofNCompleteColumn
)
from rich.panel import Panel
from rich.table import Table

from src.config import settings
from src.database.client import db_manager
from src.database.models import SmartMoneyProfileModel
from src.utils.logger import logger

console = Console()

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class RunnerToken:
    token_address: str
    symbol: str
    chain_id: str
    created_at: Optional[datetime]
    peak_multiplier: float    # best price change seen (e.g. 3.5 = 350% / +2x)
    current_fdv_usd: float
    source: str = "dexscreener"


@dataclass
class EarlyBuyRecord:
    wallet_address: str
    token_address: str
    token_symbol: str
    buy_amount_sol: float
    entry_time_seconds: int      # seconds after token launch
    bought_at: Optional[datetime]
    entry_price_usd: float = 0.0


@dataclass
class TokenClassification:
    token_address: str
    token_symbol: str
    label: str                   # 'RUNNER' | 'DEAD' | 'NEUTRAL'
    current_price_usd: float = 0.0
    token_fdv_usd: float = 0.0


@dataclass
class WalletEvaluation:
    wallet_address: str
    sol_balance: float
    runner_hit_count: int        # from Phase 2 (runner tokens)
    dead_hit_count: int = 0      # from Phase 3 (wallet history)
    neutral_hit_count: int = 0
    total_early_buys: int = 0    # runner + dead (classifiable only)
    hit_ratio: float = 0.0
    status: str = "PENDING"      # 'QUALIFIED' | 'REJECTED'
    rejection_reason: str = ""
    history_hits: list[EarlyBuyRecord] = field(default_factory=list)


@dataclass
class DiscoveryConfig:
    max_runners: int = 5000
    min_runner_hits: int = 3
    min_hit_ratio: float = 0.15
    min_sol_balance: float = 50.0
    min_classifiable_buys: int = 10
    early_window_seconds: int = 600    # 10 minutes
    runner_threshold_multiplier: float = 2.0   # ≥2x = runner
    dead_threshold_multiplier: float = 0.1     # ≤0.1x = dead
    runner_fdv_threshold_usd: float = 20_000   # ≥$20k MC on pump.fun (4x launch)
    max_token_age_hours: float = 168.0         # Active runners within the last 7 days (1 week)
    batch_size: int = 50
    batch_delay_seconds: float = 1.0
    wallet_history_tx_limit: int = 100
    dexscreener_batch_size: int = 30           # max addresses per DexScreener call



# ---------------------------------------------------------------------------
# Phase 1: Runner Collector
# ---------------------------------------------------------------------------

class RunnerCollector:
    """
    Collects runner tokens from DexScreener.
    Strategy:
      1. GET /token-profiles/latest/v1  → paginate for token addresses
      2. GET /latest/dex/tokens/{addr1,addr2,...30} → check price & FDV
      3. Keep tokens with priceChange.h24 ≥ 100% (≥2x) OR fdv ≥ 100k
    """

    BASE_URL = "https://api.dexscreener.com"

    async def _fetch_latest_profiles(
        self,
        client: httpx.AsyncClient,
        page: int = 0,
    ) -> list[dict]:
        """Fetch a page of recently profiled token addresses from DexScreener."""
        try:
            resp = await client.get(
                f"{self.BASE_URL}/token-profiles/latest/v1",
                params={"page": page},
                timeout=15.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                # Returns list of objects with chainId and tokenAddress
                if isinstance(data, list):
                    return data
            return []
        except Exception as e:
            logger.debug(f"DexScreener profiles page {page} error: {e}")
            return []

    async def _classify_token_batch(
        self,
        client: httpx.AsyncClient,
        addresses: list[str],
    ) -> list[RunnerToken]:
        """Fetch pair data for up to 30 token addresses and classify runners."""
        if not addresses:
            return []
        addr_str = ",".join(addresses)
        try:
            resp = await client.get(
                f"{self.BASE_URL}/latest/dex/tokens/{addr_str}",
                timeout=15.0,
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            pairs = data.get("pairs") or []
            runners: dict[str, RunnerToken] = {}
            for pair in pairs:
                if pair.get("chainId") != "solana":
                    continue
                token_addr = pair.get("baseToken", {}).get("address", "")
                if not token_addr or token_addr in runners:
                    continue

                price_change = pair.get("priceChange") or {}
                h24 = price_change.get("h24") or 0.0
                h6 = price_change.get("h6") or 0.0
                fdv = (pair.get("fdv") or 0.0)
                symbol = pair.get("baseToken", {}).get("symbol", "UNKNOWN")

                # Parse creation time from pair.pairCreatedAt (epoch ms)
                created_at = None
                created_ms = pair.get("pairCreatedAt")
                if created_ms:
                    try:
                        created_at = datetime.fromtimestamp(
                            created_ms / 1000, tz=timezone.utc
                        )
                    except Exception:
                        pass

                best_change = max(h24, h6)   # best upside (for runner detection)
                worst_change = min(h24, h6)  # worst drop (for dead detection)
                multiplier = 1.0 + (best_change / 100.0) if best_change > 0 else 1.0

                is_runner = (
                    best_change >= 100.0         # ≥2x price change
                    or fdv >= 100_000            # significant FDV
                )
                if is_runner:
                    runners[token_addr] = RunnerToken(
                        token_address=token_addr,
                        symbol=symbol,
                        chain_id="solana",
                        created_at=created_at,
                        peak_multiplier=multiplier,
                        current_fdv_usd=fdv,
                    )
            return list(runners.values())
        except Exception as e:
            logger.debug(f"DexScreener classify batch error: {e}")
            return []

    EXCLUDED_MINTS = {
        "So11111111111111111111111111111111111111112",  # WSOL
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    }

    async def _fetch_pump_fun_fresh_runners(
        self,
        client: httpx.AsyncClient,
        config: DiscoveryConfig,
        seen_addresses: set[str],
    ) -> list[RunnerToken]:
        """
        Fetch fresh runner tokens directly from pump.fun frontend API.
        Only tokens created within config.max_token_age_hours (default 168h = 7 days)
        and with market cap >= $20k (≥4x from launch).
        """
        runners: list[RunnerToken] = []
        now_ms = time.time() * 1000
        max_age_ms = config.max_token_age_hours * 3600 * 1000

        # Scan across all 3 primary sorting methods up to offset 1500 (30 pages per sort)
        sort_modes = ["market_cap", "last_trade_timestamp", "created_timestamp"]
        for sort_mode in sort_modes:
            for offset in range(0, 1500, 50):
                url = (
                    f"https://frontend-api-v3.pump.fun/coins"
                    f"?offset={offset}&limit=50&sort={sort_mode}&order=DESC&includeNsfw=true"
                )
                try:
                    resp = await client.get(url, timeout=15.0)
                    if resp.status_code != 200:
                        break
                    coins = resp.json()
                    if not isinstance(coins, list) or not coins:
                        break

                    for c in coins:
                        mint = c.get("mint", "")
                        if not mint or mint in seen_addresses or mint in self.EXCLUDED_MINTS:
                            continue

                        created_ms = c.get("created_timestamp") or 0
                        age_ms = now_ms - created_ms
                        if age_ms < 0 or age_ms > max_age_ms:
                            continue

                        mcap = float(c.get("usd_market_cap") or 0.0)
                        is_complete = bool(c.get("complete"))

                        # Runner criteria: graduated or Mcap >= threshold ($20k)
                        if is_complete or mcap >= config.runner_fdv_threshold_usd:
                            created_at = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc) if created_ms else None
                            approx_multiplier = max(2.0, mcap / 5000.0)
                            runners.append(RunnerToken(
                                token_address=mint,
                                symbol=c.get("symbol") or "UNKNOWN",
                                chain_id="solana",
                                created_at=created_at,
                                peak_multiplier=approx_multiplier,
                                current_fdv_usd=mcap,
                                source="pump_fun_fresh",
                            ))
                            seen_addresses.add(mint)

                    await asyncio.sleep(0.15)
                except Exception as e:
                    logger.debug(f"Pump.fun fresh fetch error (sort={sort_mode}, offset={offset}): {e}")
                    break

        return runners

    async def _fetch_dex_fresh_boosts(
        self,
        client: httpx.AsyncClient,
        config: DiscoveryConfig,
        seen_addresses: set[str],
    ) -> list[RunnerToken]:
        """
        Fetch real-time boosted tokens on Solana from DexScreener,
        filtering strictly for fresh tokens <= config.max_token_age_hours.
        """
        candidate_addrs = set()
        endpoints = [
            f"{self.BASE_URL}/token-boosts/latest/v1",
            f"{self.BASE_URL}/token-boosts/top/v1",
            f"{self.BASE_URL}/token-profiles/latest/v1",
        ]
        for ep in endpoints:
            try:
                resp = await client.get(ep, timeout=15.0)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list):
                        for item in data:
                            if item.get("chainId") == "solana":
                                addr = item.get("tokenAddress", "")
                                if addr and addr not in seen_addresses and addr not in self.EXCLUDED_MINTS:
                                    candidate_addrs.add(addr)
            except Exception as e:
                logger.debug(f"DexScreener {ep} error: {e}")

        if not candidate_addrs:
            return []

        now_ms = time.time() * 1000
        max_age_ms = config.max_token_age_hours * 3600 * 1000
        fresh_runners: list[RunnerToken] = []

        unseen = list(candidate_addrs)
        for i in range(0, len(unseen), config.dexscreener_batch_size):
            batch = unseen[i: i + config.dexscreener_batch_size]
            classified = await self._classify_token_batch(client, batch)
            for r in classified:
                # Age filter: verify token is fresh <= max_token_age_hours
                if r.created_at:
                    token_age_ms = now_ms - (r.created_at.timestamp() * 1000)
                    if token_age_ms > max_age_ms:
                        continue  # Skip old tokens
                fresh_runners.append(r)
                seen_addresses.add(r.token_address)
            await asyncio.sleep(0.5)

        return fresh_runners

    async def collect(
        self,
        config: DiscoveryConfig,
        already_traced: set[str],
        progress: Progress,
    ) -> list[RunnerToken]:
        """
        Collect up to config.max_runners runner tokens from FRESH sources:
          1. Direct pump.fun API: fresh runners (<=48h old, graduated or mcap >= $60k)
          2. DexScreener live boosts & profiles: fresh Solana runners (<=48h old, >=2x return)
          3. Local Supabase backtest_tokens (runners already verified in DB)
        """
        task = progress.add_task(
            "[cyan]Phase 1: Collecting FRESH runners (<=48h active tokens)...",
            total=config.max_runners,
        )

        collected: dict[str, RunnerToken] = {}
        seen_addresses: set[str] = set(already_traced)

        async with httpx.AsyncClient(
            headers={"User-Agent": "MemeScanner/1.0"},
            follow_redirects=True,
        ) as client:
            # Source 1: Direct pump.fun API fresh runners (primary source for active smart money)
            if len(collected) < config.max_runners:
                pump_runners = await self._fetch_pump_fun_fresh_runners(client, config, seen_addresses)
                for r in pump_runners:
                    collected[r.token_address] = r
                    progress.advance(task)
                    if len(collected) >= config.max_runners:
                        break

            # Source 2: DexScreener fresh boosts (active trending runners)
            if len(collected) < config.max_runners:
                dex_runners = await self._fetch_dex_fresh_boosts(client, config, seen_addresses)
                for r in dex_runners:
                    if r.token_address not in collected:
                        collected[r.token_address] = r
                        progress.advance(task)
                        if len(collected) >= config.max_runners:
                            break

        # Source 3: Fallback / seed from local backtest runners if still needed
        if len(collected) < config.max_runners:
            try:
                db_rows = await db_manager.query(
                    "backtest_tokens",
                    select="token_address,symbol,label,label_return_pct,collected_at",
                    limit=3000,
                )
                for row in db_rows:
                    addr = row.get("token_address")
                    if not addr or addr in seen_addresses or addr in self.EXCLUDED_MINTS or addr in collected:
                        continue
                    ret = float(row.get("label_return_pct") or 0.0)
                    lbl = (row.get("label") or "").lower()
                    if lbl == "runner" or ret >= 100.0:
                        collected[addr] = RunnerToken(
                            token_address=addr,
                            symbol=row.get("symbol") or "UNKNOWN",
                            chain_id="solana",
                            created_at=None,
                            peak_multiplier=1.0 + (ret / 100.0) if ret > 0 else 2.0,
                            current_fdv_usd=100_000.0,
                            source="backtest_db",
                        )
                        seen_addresses.add(addr)
                        progress.advance(task)
                        if len(collected) >= config.max_runners:
                            break
            except Exception as e:
                logger.debug(f"DB runners fetch error: {e}")

        result = list(collected.values())[: config.max_runners]
        progress.update(task, completed=len(result))
        console.print(
            f"  ✓ Found [bold green]{len(result):,}[/bold green] fresh runner tokens"
        )
        return result



# ---------------------------------------------------------------------------
# Phase 2: Early Buyer Tracer
# ---------------------------------------------------------------------------

class EarlyBuyerTracer:
    """
    For each runner token, fetches parsed swap transactions via Helius
    and identifies wallets that bought within the early window (default 10 min).
    """

    HELIUS_TX_URL = "https://api.helius.xyz/v0/addresses/{address}/transactions"

    async def _fetch_early_buyers_for_token(
        self,
        client: httpx.AsyncClient,
        token: RunnerToken,
        config: DiscoveryConfig,
        semaphore: asyncio.Semaphore,
    ) -> list[EarlyBuyRecord]:
        """Fetch swap transactions for a token address and filter early buyers."""
        if not settings.helius_api_key:
            return []

        url = self.HELIUS_TX_URL.format(address=token.token_address)
        early_buyers: list[EarlyBuyRecord] = []
        token_launch_ts = token.created_at

        try:
            all_txs: list[dict] = []
            before_sig: Optional[str] = None
            max_pages = 5  # Up to 500 transactions backwards

            for _ in range(max_pages):
                params = {
                    "api-key": settings.helius_api_key,
                    "type": "SWAP",
                    "limit": 100,
                }
                if before_sig:
                    params["before"] = before_sig

                page_txs = None
                for attempt in range(4):
                    async with semaphore:
                        resp = await client.get(url, params=params, timeout=20.0)
                    if resp.status_code == 429:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    if resp.status_code == 200:
                        page_txs = resp.json()
                    break

                if not isinstance(page_txs, list) or not page_txs:
                    break

                all_txs.extend(page_txs)
                if len(page_txs) < 100:
                    # Reached genesis
                    break

                before_sig = page_txs[-1].get("signature")
                if token_launch_ts:
                    oldest_ts = page_txs[-1].get("timestamp")
                    if oldest_ts:
                        oldest_dt = datetime.fromtimestamp(oldest_ts, tz=timezone.utc)
                        if (oldest_dt - token_launch_ts).total_seconds() <= config.early_window_seconds:
                            break

                await asyncio.sleep(0.2)

            if not all_txs:
                return []

            # Anchor launch time to the earliest swap transaction on blockchain
            valid_timestamps = [t.get("timestamp") for t in all_txs if t.get("timestamp")]
            if valid_timestamps:
                token_launch_ts = datetime.fromtimestamp(min(valid_timestamps), tz=timezone.utc)

            for tx in all_txs:
                tx_timestamp = tx.get("timestamp")
                if not tx_timestamp:
                    continue

                tx_dt = datetime.fromtimestamp(tx_timestamp, tz=timezone.utc)

                if token_launch_ts:
                    entry_seconds = int((tx_dt - token_launch_ts).total_seconds())
                    if entry_seconds < 0 or entry_seconds > config.early_window_seconds:
                        continue
                else:
                    entry_seconds = 0

                fee_payer = tx.get("feePayer", "")
                if not fee_payer:
                    continue

                buy_sol = 0.0
                native_transfers = tx.get("nativeTransfers") or []
                for transfer in native_transfers:
                    if (transfer.get("toUserAccount") == token.token_address
                            or transfer.get("fromUserAccount") == fee_payer):
                        amount_lamports = transfer.get("amount", 0)
                        buy_sol = max(buy_sol, amount_lamports / 1e9)

                early_buyers.append(EarlyBuyRecord(
                    wallet_address=fee_payer,
                    token_address=token.token_address,
                    token_symbol=token.symbol,
                    buy_amount_sol=buy_sol,
                    entry_time_seconds=entry_seconds,
                    bought_at=tx_dt,
                    entry_price_usd=0.0,
                ))

            return early_buyers

        except Exception as e:
            logger.debug(f"Helius trace error for {token.token_address[:8]}: {e}")
            return []

    async def trace_all(
        self,
        runners: list[RunnerToken],
        config: DiscoveryConfig,
        progress: Progress,
    ) -> dict[str, list[EarlyBuyRecord]]:
        """
        Trace early buyers for all runner tokens.
        Returns: Dict[wallet_address → list of EarlyBuyRecord] across all runners.
        """
        task = progress.add_task(
            "[cyan]Phase 2: Tracing early buyers...",
            total=len(runners),
        )

        wallet_hits: dict[str, list[EarlyBuyRecord]] = defaultdict(list)
        all_hits_for_db: list[EarlyBuyRecord] = []
        semaphore = asyncio.Semaphore(2)  # Strict concurrency limit for Helius rate limits

        async with httpx.AsyncClient(
            headers={"User-Agent": "MemeScanner/1.0"},
        ) as client:
            for i in range(0, len(runners), config.batch_size):
                batch = runners[i: i + config.batch_size]
                tasks = [
                    self._fetch_early_buyers_for_token(client, token, config, semaphore)
                    for token in batch
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                batch_hits: list[EarlyBuyRecord] = []
                for result in results:
                    if isinstance(result, list):
                        for record in result:
                            wallet_hits[record.wallet_address].append(record)
                            batch_hits.append(record)
                            all_hits_for_db.append(record)

                if batch_hits:
                    await _save_hits_to_db(batch_hits, source="TRACE")

                progress.advance(task, len(batch))
                await asyncio.sleep(config.batch_delay_seconds)

        unique_wallets = len(wallet_hits)
        console.print(
            f"  ✓ Traced [bold]{len(runners):,}[/bold] tokens → "
            f"[bold green]{unique_wallets:,}[/bold green] unique early buyer wallets"
        )
        return dict(wallet_hits)


# ---------------------------------------------------------------------------
# Phase 3: Wallet Qualifier
# ---------------------------------------------------------------------------

class WalletQualifier:
    """
    Qualifies candidate wallets through a 3-step funnel:
      3a. SOL balance ≥ config.min_sol_balance
      3b. Runner hit count ≥ config.min_runner_hits
      3c. Negative control: pull full buy history, classify tokens,
          require ≥10 classifiable buys and runner ratio ≥15%
    """

    HELIUS_TX_URL = "https://api.helius.xyz/v0/addresses/{address}/transactions"
    DEXSCREENER_TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/{addresses}"

    async def _check_sol_balance(self, wallet_address: str) -> float:
        """Check SOL balance via Helius RPC getBalance."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    settings.helius_rpc_url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getBalance",
                        "params": [wallet_address],
                    },
                    timeout=10.0,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    lamports = data.get("result", {}).get("value", 0)
                    return lamports / 1e9
        except Exception as e:
            logger.debug(f"SOL balance check error for {wallet_address[:8]}: {e}")
        return 0.0

    async def _fetch_wallet_buy_history(
        self,
        client: httpx.AsyncClient,
        wallet_address: str,
        config: DiscoveryConfig,
    ) -> list[dict]:
        """Fetch 100 most recent swap transactions for a wallet."""
        if not settings.helius_api_key:
            return []

        url = self.HELIUS_TX_URL.format(address=wallet_address)
        params = {
            "api-key": settings.helius_api_key,
            "type": "SWAP",
            "limit": config.wallet_history_tx_limit,
        }
        try:
            resp = await client.get(url, params=params, timeout=20.0)
            if resp.status_code == 429:
                await asyncio.sleep(5.0)
                return []
            if resp.status_code == 200:
                data = resp.json()
                return data if isinstance(data, list) else []
        except Exception as e:
            logger.debug(f"Wallet history fetch error {wallet_address[:8]}: {e}")
        return []

    async def _classify_tokens_via_dexscreener(
        self,
        client: httpx.AsyncClient,
        token_addresses: list[str],
        config: DiscoveryConfig,
    ) -> dict[str, TokenClassification]:
        """
        Batch-classify token addresses via DexScreener.
        Returns dict[token_address → TokenClassification].
        """
        classifications: dict[str, TokenClassification] = {}
        if not token_addresses:
            return classifications

        # DexScreener allows up to 30 addresses per call
        for i in range(0, len(token_addresses), config.dexscreener_batch_size):
            batch = token_addresses[i: i + config.dexscreener_batch_size]
            addr_str = ",".join(batch)
            try:
                resp = await client.get(
                    self.DEXSCREENER_TOKENS_URL.format(addresses=addr_str),
                    timeout=15.0,
                )
                if resp.status_code != 200:
                    continue
                data = resp.json()
                pairs = data.get("pairs") or []

                # We only care about the best pair per token
                seen: set[str] = set()
                for pair in pairs:
                    if pair.get("chainId") != "solana":
                        continue
                    token_addr = pair.get("baseToken", {}).get("address", "")
                    if not token_addr or token_addr in seen:
                        continue
                    seen.add(token_addr)

                    price_change = pair.get("priceChange") or {}
                    h24 = price_change.get("h24") or 0.0
                    h6 = price_change.get("h6") or 0.0
                    fdv = pair.get("fdv") or 0.0
                    price_usd = float(pair.get("priceUsd") or 0.0)
                    symbol = pair.get("baseToken", {}).get("symbol", "UNKNOWN")

                    best_change = max(h24, h6)   # best upside (for runner detection)
                    worst_change = min(h24, h6)  # worst drop (for dead detection)

                    # RUNNER: ≥2x price change OR high FDV (still significant)
                    if best_change >= 100.0 or fdv >= config.runner_fdv_threshold_usd:
                        label = "RUNNER"
                    # DEAD: worst timeframe shows ≥90% drop (price collapsed)
                    elif worst_change <= -90.0:
                        label = "DEAD"
                    else:
                        label = "NEUTRAL"

                    classifications[token_addr] = TokenClassification(
                        token_address=token_addr,
                        token_symbol=symbol,
                        label=label,
                        current_price_usd=price_usd,
                        token_fdv_usd=fdv,
                    )

                await asyncio.sleep(0.3)
            except Exception as e:
                logger.debug(f"DexScreener classify batch error: {e}")

        return classifications

    async def _run_negative_control(
        self,
        wallet_address: str,
        known_runner_hits: list[EarlyBuyRecord],
        config: DiscoveryConfig,
    ) -> tuple[int, int, int, list[EarlyBuyRecord]]:
        """
        Pull wallet's full buy history and classify each early-bought token.
        Returns: (runner_count, dead_count, neutral_count, history_hits)
        """
        async with httpx.AsyncClient(
            headers={"User-Agent": "MemeScanner/1.0"},
        ) as client:
            txs = await self._fetch_wallet_buy_history(client, wallet_address, config)
            if not txs:
                return 0, 0, 0, []

            # Extract unique token addresses from wallet's swap history
            token_addresses: set[str] = set()
            tx_records: list[dict] = []

            for tx in txs:
                tx_timestamp = tx.get("timestamp")
                if not tx_timestamp:
                    continue

                # We need token address from token transfers
                token_transfers = tx.get("tokenTransfers") or []
                for transfer in token_transfers:
                    mint = transfer.get("mint", "")
                    if mint and mint not in ("So11111111111111111111111111111111111111112",):
                        # Non-SOL token — this is likely what they bought
                        token_addresses.add(mint)

                tx_records.append(tx)

            if not token_addresses:
                return 0, 0, 0, []

            # Classify all tokens in batch
            classifications = await self._classify_tokens_via_dexscreener(
                client, list(token_addresses), config
            )

            # Build history hits
            history_hits: list[EarlyBuyRecord] = []
            runner_set = {r.token_address for r in known_runner_hits}

            for tx in tx_records:
                tx_timestamp = tx.get("timestamp")
                if not tx_timestamp:
                    continue
                tx_dt = datetime.fromtimestamp(tx_timestamp, tz=timezone.utc)
                fee_payer = tx.get("feePayer", wallet_address)

                for transfer in (tx.get("tokenTransfers") or []):
                    mint = transfer.get("mint", "")
                    if not mint or mint == "So11111111111111111111111111111111111111112":
                        continue
                    # Skip tokens we already captured in Phase 2 runner tracing
                    if mint in runner_set:
                        continue

                    classification = classifications.get(mint)
                    if not classification:
                        continue  # Unknown token, skip

                    buy_sol = abs(transfer.get("tokenAmount", 0)) * 0.0  # approximate
                    history_hits.append(EarlyBuyRecord(
                        wallet_address=fee_payer,
                        token_address=mint,
                        token_symbol=classification.token_symbol,
                        buy_amount_sol=buy_sol,
                        entry_time_seconds=0,  # No launch time for arbitrary tokens
                        bought_at=tx_dt,
                        entry_price_usd=0.0,
                    ))

            # Count labels
            runner_count = sum(
                1 for h in history_hits
                if classifications.get(h.token_address, TokenClassification("", "", "NEUTRAL")).label == "RUNNER"
            )
            dead_count = sum(
                1 for h in history_hits
                if classifications.get(h.token_address, TokenClassification("", "", "NEUTRAL")).label == "DEAD"
            )
            neutral_count = len(history_hits) - runner_count - dead_count

            return runner_count, dead_count, neutral_count, history_hits

    async def qualify_all(
        self,
        wallet_hits: dict[str, list[EarlyBuyRecord]],
        config: DiscoveryConfig,
        already_evaluated: set[str],
        progress: Progress,
    ) -> list[WalletEvaluation]:
        """
        Run the full 3-step qualification funnel.
        Returns list of all WalletEvaluation (both QUALIFIED and REJECTED).
        """
        # ---- Step 3a: Runner hit count filter ----
        candidates_by_hits = {
            addr: hits
            for addr, hits in wallet_hits.items()
            if len(set(r.token_address for r in hits)) >= config.min_runner_hits
            and addr not in already_evaluated
        }

        console.print(
            f"\n  [bold]Step 3a:[/bold] Runner hit count ≥ {config.min_runner_hits}"
        )
        console.print(
            f"    [green]✓ {len(candidates_by_hits):,} / {len(wallet_hits):,}[/green] passed"
        )

        # ---- Step 3b: SOL balance filter ----
        task_bal = progress.add_task(
            f"[cyan]  Step 3b: SOL balance check (≥{config.min_sol_balance} SOL)...",
            total=len(candidates_by_hits),
        )
        candidates_with_balance: list[tuple[str, list[EarlyBuyRecord], float]] = []

        # Check balances in batches to avoid hammering RPC
        wallets = list(candidates_by_hits.items())
        for i in range(0, len(wallets), config.batch_size):
            batch = wallets[i: i + config.batch_size]
            balance_tasks = [self._check_sol_balance(addr) for addr, _ in batch]
            balances = await asyncio.gather(*balance_tasks, return_exceptions=True)

            for (addr, hits), balance in zip(batch, balances):
                if isinstance(balance, float) and balance >= config.min_sol_balance:
                    candidates_with_balance.append((addr, hits, balance))
                else:
                    # Save rejected wallet to DB
                    await _save_wallet_to_db(WalletEvaluation(
                        wallet_address=addr,
                        sol_balance=balance if isinstance(balance, float) else 0.0,
                        runner_hit_count=len(set(r.token_address for r in hits)),
                        status="REJECTED",
                        rejection_reason="low_balance",
                    ))
                progress.advance(task_bal)

            await asyncio.sleep(config.batch_delay_seconds)

        console.print(
            f"  [bold]Step 3b:[/bold] SOL balance ≥ {config.min_sol_balance} SOL"
        )
        console.print(
            f"    [green]✓ {len(candidates_with_balance):,} / {len(candidates_by_hits):,}[/green] passed"
        )

        # ---- Step 3c: Negative control (wallet history analysis) ----
        task_nc = progress.add_task(
            "[cyan]  Step 3c: Track record analysis (negative control)...",
            total=len(candidates_with_balance),
        )

        evaluations: list[WalletEvaluation] = []

        for addr, runner_hits, sol_balance in candidates_with_balance:
            runner_hit_count = len(set(r.token_address for r in runner_hits))

            try:
                nc_runner, nc_dead, nc_neutral, history_hits = await self._run_negative_control(
                    addr, runner_hits, config
                )
            except Exception as e:
                logger.debug(f"Negative control error for {addr[:8]}: {e}")
                nc_runner = nc_dead = nc_neutral = 0
                history_hits = []

            # Combine runner hits from Phase 2 with history classification
            total_runner = runner_hit_count + nc_runner  # Phase 2 runners count as RUNNER hits
            total_dead = nc_dead
            total_classifiable = total_runner + total_dead
            hit_ratio = total_runner / total_classifiable if total_classifiable > 0 else 0.0

            eval_result = WalletEvaluation(
                wallet_address=addr,
                sol_balance=sol_balance,
                runner_hit_count=total_runner,
                dead_hit_count=total_dead,
                neutral_hit_count=nc_neutral,
                total_early_buys=total_classifiable,
                hit_ratio=hit_ratio,
                history_hits=history_hits,
            )

            if total_classifiable < config.min_classifiable_buys:
                eval_result.status = "REJECTED"
                eval_result.rejection_reason = "low_sample"
            elif hit_ratio < config.min_hit_ratio:
                eval_result.status = "REJECTED"
                eval_result.rejection_reason = "low_ratio"
            else:
                eval_result.status = "QUALIFIED"

            evaluations.append(eval_result)

            # Checkpoint: save immediately
            await _save_wallet_to_db(eval_result)
            if history_hits:
                await _save_hits_to_db(
                    [EarlyBuyRecord(
                        wallet_address=h.wallet_address,
                        token_address=h.token_address,
                        token_symbol=h.token_symbol,
                        buy_amount_sol=h.buy_amount_sol,
                        entry_time_seconds=h.entry_time_seconds,
                        bought_at=h.bought_at,
                    ) for h in history_hits],
                    source="HISTORY",
                )

            progress.advance(task_nc)
            await asyncio.sleep(0.1)

        qualified = [e for e in evaluations if e.status == "QUALIFIED"]
        low_sample = [e for e in evaluations if e.rejection_reason == "low_sample"]
        low_ratio = [e for e in evaluations if e.rejection_reason == "low_ratio"]

        console.print(f"\n  [bold]Step 3c:[/bold] Track record analysis")
        console.print(
            f"    [yellow]⚠ {len(low_sample):,}[/yellow] rejected: insufficient sample (<{config.min_classifiable_buys} classifiable buys)"
        )
        console.print(
            f"    [yellow]⚠ {len(low_ratio):,}[/yellow] rejected: low runner ratio (<{config.min_hit_ratio:.0%})"
        )
        console.print(
            f"    [green]✓ {len(qualified):,}[/green] QUALIFIED"
        )

        return evaluations


# ---------------------------------------------------------------------------
# Phase 4: Sync to Scoring Engine
# ---------------------------------------------------------------------------

async def sync_to_smart_money_profiles(qualified: list[WalletEvaluation]) -> int:
    """
    Insert QUALIFIED wallets into smart_money_profiles as SEED tier
    so they are immediately usable by the OpportunityScoring engine.
    """
    count = 0
    for eval_result in qualified:
        profile = SmartMoneyProfileModel(
            wallet_address=eval_result.wallet_address,
            tier="SEED",
            is_active=True,
            total_trades_recorded=eval_result.total_early_buys,
            win_rate_pct=eval_result.hit_ratio * 100.0,
            source="AUTO_DISCOVERY",
            notes=(
                f"Discovered by discovery engine. "
                f"runner_hits={eval_result.runner_hit_count}, "
                f"dead_hits={eval_result.dead_hit_count}, "
                f"ratio={eval_result.hit_ratio:.2%}, "
                f"sol_balance={eval_result.sol_balance:.1f}"
            ),
        )
        ok = await db_manager.upsert_smart_money_wallet(profile)
        if ok:
            count += 1
    return count


# ---------------------------------------------------------------------------
# DB helper functions
# ---------------------------------------------------------------------------

async def _save_wallet_to_db(evaluation: WalletEvaluation) -> None:
    """Checkpoint: persist wallet evaluation result to smart_money_wallets."""
    data = {
        "wallet_address": evaluation.wallet_address,
        "sol_balance": evaluation.sol_balance,
        "runner_hit_count": evaluation.runner_hit_count,
        "dead_hit_count": evaluation.dead_hit_count,
        "neutral_hit_count": evaluation.neutral_hit_count,
        "total_early_buys": evaluation.total_early_buys,
        "hit_ratio": evaluation.hit_ratio,
        "status": evaluation.status,
        "rejection_reason": evaluation.rejection_reason,
        "last_evaluated_at": datetime.utcnow().isoformat(),
    }
    await db_manager.upsert_discovery_wallet(data)


async def _save_hits_to_db(hits: list[EarlyBuyRecord], source: str = "TRACE") -> None:
    """Checkpoint: persist early buy hits to smart_money_hits."""
    rows = [
        {
            "wallet_address": h.wallet_address,
            "token_address": h.token_address,
            "token_symbol": h.token_symbol or "",
            "token_label": "RUNNER",       # Phase 2 hits are always from runner tokens
            "source": source,
            "buy_amount_sol": h.buy_amount_sol,
            "entry_time_seconds": h.entry_time_seconds,
            "bought_at": h.bought_at.isoformat() if h.bought_at else None,
            "entry_price_usd": h.entry_price_usd,
        }
        for h in hits
    ]
    await db_manager.batch_upsert_discovery_hits(rows)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class DiscoveryOrchestrator:
    """
    Top-level coordinator for the Smart Money Discovery pipeline.
    Manages progress display, checkpoint/resume, and final reporting.
    """

    def __init__(self, config: DiscoveryConfig):
        self.config = config
        self.runner_collector = RunnerCollector()
        self.early_buyer_tracer = EarlyBuyerTracer()
        self.wallet_qualifier = WalletQualifier()

    async def run(self, resume: bool = False, dry_run: bool = False) -> list[WalletEvaluation]:
        """Execute the full discovery pipeline."""
        console.print(Panel(
            "[bold cyan]🔍 Smart Money Discovery Engine v1.0[/bold cyan]\n"
            f"Max runners: [bold]{self.config.max_runners:,}[/bold] | "
            f"Min hits: [bold]{self.config.min_runner_hits}[/bold] | "
            f"Min ratio: [bold]{self.config.min_hit_ratio:.0%}[/bold] | "
            f"Min SOL: [bold]{self.config.min_sol_balance}[/bold] | "
            f"Early window: [bold]{self.config.early_window_seconds // 60} min[/bold]",
            border_style="cyan",
        ))

        db_manager.connect()

        # Load checkpoint state for resume
        already_traced: set[str] = set()
        already_evaluated: set[str] = set()
        if resume:
            console.print("[dim]Loading checkpoint state (--resume)...[/dim]")
            already_traced = await db_manager.get_traced_token_addresses()
            already_evaluated = await db_manager.get_evaluated_wallet_addresses()
            console.print(
                f"  Resuming from: {len(already_traced):,} traced tokens, "
                f"{len(already_evaluated):,} evaluated wallets"
            )

        start_time = time.time()

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        ) as progress:

            # Phase 1: Collect runners
            runners = await self.runner_collector.collect(
                self.config, already_traced, progress
            )

            # Phase 2: Trace early buyers
            wallet_hits = await self.early_buyer_tracer.trace_all(
                runners, self.config, progress
            )

            # Phase 3: Qualify candidates
            console.print("\n[bold cyan]🧪 Phase 3: Qualifying candidates...[/bold cyan]")
            evaluations = await self.wallet_qualifier.qualify_all(
                wallet_hits, self.config, already_evaluated, progress
            )

        qualified = [e for e in evaluations if e.status == "QUALIFIED"]
        rejected = [e for e in evaluations if e.status == "REJECTED"]

        # Phase 4: Sync to scoring engine
        synced_count = 0
        if not dry_run and qualified:
            console.print("\n[bold cyan]🔄 Phase 4: Syncing to smart_money_profiles...[/bold cyan]")
            synced_count = await sync_to_smart_money_profiles(qualified)

        elapsed = time.time() - start_time
        minutes = int(elapsed // 60)
        seconds = int(elapsed % 60)

        # Final report
        table = Table(title="✅ Discovery Results", border_style="green")
        table.add_column("Metric", style="bold")
        table.add_column("Value", justify="right")
        table.add_row("Runners analyzed", f"{len(runners):,}")
        table.add_row("Unique early buyer wallets", f"{len(wallet_hits):,}")
        table.add_row("Candidates evaluated", f"{len(evaluations):,}")
        table.add_row("QUALIFIED wallets", f"[bold green]{len(qualified):,}[/bold green]")
        table.add_row("REJECTED wallets", f"[dim]{len(rejected):,}[/dim]")
        if qualified:
            avg_ratio = sum(e.hit_ratio for e in qualified) / len(qualified)
            best = max(qualified, key=lambda e: e.hit_ratio)
            table.add_row("Avg runner ratio (qualified)", f"{avg_ratio:.1%}")
            table.add_row("Best wallet ratio", f"{best.hit_ratio:.1%} ({best.wallet_address[:8]}...)")
        table.add_row("Synced to smart_money_profiles", f"{synced_count:,}")
        table.add_row("Total runtime", f"{minutes}m {seconds}s")

        console.print(table)

        if dry_run:
            console.print("[yellow]⚠️  DRY RUN — no data was written to Supabase[/yellow]")

        return evaluations
