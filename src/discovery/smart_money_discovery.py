"""
Smart Money Wallet Discovery Engine
====================================
Discovers "Smart Money" wallets on Solana by:
  Phase 1  — Collect runner tokens from DexScreener (≥2x return)
  Phase 2  — Trace early buyers (≤10 min after launch) via Helius
               SIZE GATE: skip entries with buy_sol < min_entry_sol (1 SOL)
  Phase 2b — Funding Lineage Check: check if wallet was funded (1-2 hops)
               by a wallet already in smart_money_profiles
  Phase 3a — Runner hit count ≥ min_runner_hits (3)
  Phase 3b — Vybe trade count gate: ≥ min_trades_90d (20) for new wallets;
               lineage wallets get relaxed threshold (lineage_min_trades_90d = 5)
  Phase 3c — Vybe P&L gate: realized_pnl_90d > 0 AND realized_pnl_30d > 0
               (Helius manual reconstruction as fallback)
  Phase 4  — Sync QUALIFIED wallets → smart_money_profiles (SEED tier)
               Fields populated: net_realized_profit_sol, total_volume_sol,
               win_rate_pct, lineage_parent_wallet

Redesigned 2026-09-01 — fixes Bug #3, removes flawed negative control,
replaces SOL balance check with Vybe P&L and trade count.
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
from src.discovery.pnl_provider import pnl_provider, PnLResult
from src.discovery.lineage_checker import lineage_checker, LineageCheckResult
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
    sol_balance: float = 0.0
    runner_hit_count: int = 0         # Phase 2: runner token hits
    dead_hit_count: int = 0
    neutral_hit_count: int = 0
    total_early_buys: int = 0
    hit_ratio: float = 0.0
    status: str = "PENDING"           # 'QUALIFIED' | 'REJECTED'
    rejection_reason: str = ""
    history_hits: list[EarlyBuyRecord] = field(default_factory=list)
    # P&L fields (Phase 3b/3c — from Vybe or Helius manual)
    realized_pnl_90d_sol: float = 0.0
    realized_pnl_30d_sol: float = 0.0
    total_volume_sol: float = 0.0
    total_trades_90d: int = 0
    win_rate_pct: float = 0.0
    pnl_provider: str = "unknown"
    # Lineage fields (Phase 2b)
    funded_by_known_smart_money: bool = False
    lineage_parent_wallet: Optional[str] = None
    lineage_hop_distance: int = 0     # 0 = none, 1 = direct, 2 = grandparent


@dataclass
class DiscoveryConfig:
    max_runners: int = 5000
    min_runner_hits: int = 3
    early_window_seconds: int = 600       # 10 minutes
    runner_threshold_multiplier: float = 2.0
    dead_threshold_multiplier: float = 0.1
    runner_fdv_threshold_usd: float = 20_000
    max_token_age_hours: float = 168.0    # 7 days
    batch_size: int = 50
    batch_delay_seconds: float = 1.0
    wallet_history_tx_limit: int = 100
    dexscreener_batch_size: int = 30
    # Phase 2 size gate (Bug #3 fix)
    min_entry_sol: float = 1.0            # minimum SOL per buy to count as conviction entry
    # Phase 3b: Vybe trade count gate
    min_trades_90d: int = 20              # min trades in 90d for new wallets
    lineage_min_trades_90d: int = 5       # relaxed threshold for SM-lineage wallets
    # Phase 3c: Vybe P&L gate
    min_pnl_90d_sol: float = 0.0          # realized PnL 90d must be > this
    min_pnl_30d_sol: float = 0.0          # realized PnL 30d must be > this



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

    async def _fetch_raydium_pools(
        self,
        client: httpx.AsyncClient,
        config: DiscoveryConfig,
        seen_addresses: set[str],
        max_pools: int = 500,
    ) -> list[RunnerToken]:
        """
        Fetch high-volume active meme pools on Raydium DEX.
        Filters for pools paired with WSOL having volume24h >= $50,000.
        """
        runners: list[RunnerToken] = []
        wsol_mint = "So11111111111111111111111111111111111111112"

        for page in range(1, 11):  # up to 10 pages * 100 = 1000 pools checked
            url = (
                f"https://api-v3.raydium.io/pools/info/list"
                f"?poolType=all&poolSortField=volume24h&sortType=desc&pageSize=100&page={page}"
            )
            try:
                resp = await client.get(url, timeout=15.0)
                if resp.status_code != 200:
                    break
                data = resp.json().get("data", {})
                pools = data.get("data", [])
                if not pools:
                    break

                for p in pools:
                    addr_a = p.get("mintA", {}).get("address", "")
                    addr_b = p.get("mintB", {}).get("address", "")
                    sym_a = p.get("mintA", {}).get("symbol", "")
                    sym_b = p.get("mintB", {}).get("symbol", "")
                    vol_24h = float(p.get("day", {}).get("volume") or 0.0)
                    tvl = float(p.get("tvl") or 0.0)

                    # Only look at pools with >= $25k 24h volume
                    if vol_24h < 25_000:
                        continue

                    # Identify target meme token paired against WSOL
                    target_mint = None
                    target_symbol = None
                    if addr_a == wsol_mint and addr_b not in self.EXCLUDED_MINTS:
                        target_mint = addr_b
                        target_symbol = sym_b
                    elif addr_b == wsol_mint and addr_a not in self.EXCLUDED_MINTS:
                        target_mint = addr_a
                        target_symbol = sym_a

                    if not target_mint or target_mint in seen_addresses or target_mint in self.EXCLUDED_MINTS:
                        continue

                    open_time = p.get("openTime")
                    created_at = None
                    if open_time and open_time != "0":
                        try:
                            created_at = datetime.fromtimestamp(int(open_time), tz=timezone.utc)
                        except Exception:
                            pass

                    runners.append(RunnerToken(
                        token_address=target_mint,
                        symbol=target_symbol or "UNKNOWN",
                        chain_id="solana",
                        created_at=created_at,
                        peak_multiplier=2.5,
                        current_fdv_usd=tvl * 2.0 if tvl > 0 else 100_000.0,
                        source="raydium_pools",
                    ))
                    seen_addresses.add(target_mint)

                    if len(runners) >= max_pools:
                        return runners

                await asyncio.sleep(0.2)
            except Exception as e:
                logger.debug(f"Raydium pool fetch page {page} error: {e}")
                break

        return runners

    async def collect(
        self,
        config: DiscoveryConfig,
        already_traced: set[str],
        progress: Progress,
    ) -> list[RunnerToken]:
        """
        Collect up to config.max_runners runner tokens from FRESH sources:
          1. Direct pump.fun API: fresh runners (<=168h old, graduated or mcap >= $20k)
          2. Raydium DEX pools: high-volume active meme pairs on Raydium (>= $25k vol24h)
          3. DexScreener live boosts & profiles: fresh Solana runners
          4. Local Supabase backtest_tokens (runners already verified in DB)
        """
        task = progress.add_task(
            "[cyan]Phase 1: Collecting FRESH runners (<=168h active tokens)...",
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

            # Source 2: Raydium DEX high-volume active pools (expands runner pool to 1000+)
            if len(collected) < config.max_runners:
                needed = config.max_runners - len(collected)
                raydium_runners = await self._fetch_raydium_pools(client, config, seen_addresses, max_pools=needed)
                for r in raydium_runners:
                    if r.token_address not in collected:
                        collected[r.token_address] = r
                        progress.advance(task)
                        if len(collected) >= config.max_runners:
                            break

            # Source 3: DexScreener fresh boosts (active trending runners)
            if len(collected) < config.max_runners:
                dex_runners = await self._fetch_dex_fresh_boosts(client, config, seen_addresses)
                for r in dex_runners:
                    if r.token_address not in collected:
                        collected[r.token_address] = r
                        progress.advance(task)
                        if len(collected) >= config.max_runners:
                            break

        # Source 4: Fallback / seed from local backtest runners if still needed
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

                # FIX Bug #3: sum all native SOL transfers FROM fee_payer in this tx
                # (tokenAmount × 0.0 was incorrect — always produced 0)
                buy_sol = 0.0
                native_transfers = tx.get("nativeTransfers") or []
                for nt in native_transfers:
                    if nt.get("fromUserAccount") == fee_payer:
                        buy_sol += nt.get("amount", 0) / 1e9

                # Phase 2 SIZE GATE: skip low-conviction entries (< 1 SOL)
                if buy_sol < config.min_entry_sol:
                    continue

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
        Run the redesigned 4-phase qualification funnel:
          Phase 2b — Funding lineage check (reuses FundingGraphTracer)
          Phase 3a — Runner hit count ≥ min_runner_hits
          Phase 3b — Vybe trade count gate (lineage-aware threshold)
          Phase 3c — Vybe P&L gate (90d AND 30d must be > 0)
        Returns list of all WalletEvaluation (QUALIFIED and REJECTED).
        """
        all_candidates = [
            addr for addr, hits in wallet_hits.items()
            if addr not in already_evaluated
        ]

        # ---- Phase 2b: Funding Lineage Check ----
        console.print(f"\n  [bold]Phase 2b:[/bold] Funding lineage check ({len(all_candidates):,} candidates)...")
        task_lin = progress.add_task(
            "[cyan]  Phase 2b: Checking funding lineage...",
            total=len(all_candidates),
        )
        # Load known smart money wallets ONCE (deterministic — new qualifiers in THIS run
        # do not appear here; they become eligible parents in the NEXT run)
        known_sm_wallets = await db_manager.get_active_smart_money_addresses()
        lineage_map: dict[str, LineageCheckResult] = {}
        lineage_found = 0

        for addr in all_candidates:
            lin = await lineage_checker.check(addr, known_sm_wallets)
            lineage_map[addr] = lin
            if lin.funded_by_known_smart_money:
                lineage_found += 1
                logger.info(
                    f"🔗 [Lineage] {addr[:8]} ← {lin.lineage_parent_wallet[:8]} "
                    f"(hop={lin.lineage_hop_distance})"
                )
            progress.advance(task_lin)
            await asyncio.sleep(0.05)

        console.print(
            f"    ↳ [cyan]{lineage_found:,}[/cyan] wallets with smart money lineage "
            f"(relaxed threshold: ≥{config.lineage_min_trades_90d} trades vs ≥{config.min_trades_90d})"
        )

        # ---- Phase 3a: Runner hit count filter ----
        candidates_by_hits = {
            addr: hits
            for addr, hits in wallet_hits.items()
            if len(set(r.token_address for r in hits)) >= config.min_runner_hits
            and addr not in already_evaluated
        }

        console.print(f"\n  [bold]Phase 3a:[/bold] Runner hit count ≥ {config.min_runner_hits}")
        console.print(
            f"    [green]✓ {len(candidates_by_hits):,} / {len(wallet_hits):,}[/green] passed"
        )

        # ---- Phase 3b + 3c: Vybe P&L + trade count gate ----
        task_pnl = progress.add_task(
            "[cyan]  Phase 3b/3c: Vybe P&L qualification...",
            total=len(candidates_by_hits),
        )

        evaluations: list[WalletEvaluation] = []

        for addr, runner_hits in candidates_by_hits.items():
            runner_hit_count = len(set(r.token_address for r in runner_hits))
            lin = lineage_map.get(addr, LineageCheckResult(wallet_address=addr))

            eval_result = WalletEvaluation(
                wallet_address=addr,
                runner_hit_count=runner_hit_count,
                total_early_buys=runner_hit_count,
                funded_by_known_smart_money=lin.funded_by_known_smart_money,
                lineage_parent_wallet=lin.lineage_parent_wallet,
                lineage_hop_distance=lin.lineage_hop_distance,
            )

            # Fetch P&L (Vybe primary, Helius fallback)
            try:
                pnl = await pnl_provider.get_wallet_pnl(addr)
            except Exception as e:
                logger.debug(f"PnL fetch error for {addr[:8]}: {e}")
                pnl = PnLResult(wallet_address=addr, is_successful=False)

            # Populate P&L fields regardless of outcome
            eval_result.realized_pnl_90d_sol = pnl.realized_pnl_90d_sol
            eval_result.realized_pnl_30d_sol = pnl.realized_pnl_30d_sol
            eval_result.total_volume_sol = pnl.total_volume_sol
            eval_result.total_trades_90d = pnl.total_trades_90d
            eval_result.win_rate_pct = pnl.win_rate_pct
            eval_result.pnl_provider = pnl.provider

            # Phase 3b: Trade count gate (lineage-aware)
            effective_min_trades = (
                config.lineage_min_trades_90d
                if eval_result.funded_by_known_smart_money
                else config.min_trades_90d
            )
            if pnl.is_successful and pnl.total_trades_90d < effective_min_trades:
                eval_result.status = "REJECTED"
                eval_result.rejection_reason = (
                    f"insufficient_trades ({pnl.total_trades_90d} < {effective_min_trades})"
                )
                evaluations.append(eval_result)
                await _save_wallet_to_db(eval_result)
                progress.advance(task_pnl)
                await asyncio.sleep(0.05)
                continue

            # Phase 3c: P&L gate (both 90d and 30d must be positive)
            if pnl.is_successful:
                if pnl.realized_pnl_90d_sol <= config.min_pnl_90d_sol:
                    eval_result.status = "REJECTED"
                    eval_result.rejection_reason = (
                        f"negative_pnl_90d ({pnl.realized_pnl_90d_sol:.2f} SOL via {pnl.provider})"
                    )
                    evaluations.append(eval_result)
                    await _save_wallet_to_db(eval_result)
                    progress.advance(task_pnl)
                    await asyncio.sleep(0.05)
                    continue
                if pnl.realized_pnl_30d_sol <= config.min_pnl_30d_sol:
                    eval_result.status = "REJECTED"
                    eval_result.rejection_reason = (
                        f"negative_pnl_30d ({pnl.realized_pnl_30d_sol:.2f} SOL via {pnl.provider})"
                    )
                    evaluations.append(eval_result)
                    await _save_wallet_to_db(eval_result)
                    progress.advance(task_pnl)
                    await asyncio.sleep(0.05)
                    continue
            else:
                # P&L provider completely unavailable — still qualify if lineage is strong
                # (lineage wallets are given benefit of the doubt when all providers fail)
                if not eval_result.funded_by_known_smart_money:
                    eval_result.status = "REJECTED"
                    eval_result.rejection_reason = "pnl_provider_unavailable"
                    evaluations.append(eval_result)
                    await _save_wallet_to_db(eval_result)
                    progress.advance(task_pnl)
                    await asyncio.sleep(0.05)
                    continue
                logger.debug(
                    f"[Qualify] {addr[:8]}: PnL unavailable but has SM lineage — allowing through"
                )

            # All gates passed
            eval_result.status = "QUALIFIED"
            eval_result.hit_ratio = 1.0   # meaningful only for legacy reporting
            evaluations.append(eval_result)
            await _save_wallet_to_db(eval_result)

            progress.advance(task_pnl)
            await asyncio.sleep(0.05)

        qualified = [e for e in evaluations if e.status == "QUALIFIED"]
        rejected_trades = [e for e in evaluations if "insufficient_trades" in e.rejection_reason]
        rejected_pnl = [e for e in evaluations if "negative_pnl" in e.rejection_reason]
        rejected_other = [
            e for e in evaluations
            if e.status == "REJECTED"
            and "insufficient_trades" not in e.rejection_reason
            and "negative_pnl" not in e.rejection_reason
        ]

        console.print(f"\n  [bold]Phase 3b/3c:[/bold] Vybe P&L qualification")
        console.print(
            f"    [yellow]⚠ {len(rejected_trades):,}[/yellow] rejected: insufficient trades"
        )
        console.print(
            f"    [yellow]⚠ {len(rejected_pnl):,}[/yellow] rejected: negative P&L (90d or 30d)"
        )
        console.print(
            f"    [yellow]⚠ {len(rejected_other):,}[/yellow] rejected: other (provider unavailable)"
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
    Insert QUALIFIED wallets into smart_money_profiles as SEED tier.
    Populates P&L fields (net_realized_profit_sol, total_volume_sol, win_rate_pct)
    and lineage_parent_wallet from Phase 2b.
    """
    count = 0
    for eval_result in qualified:
        lineage_note = (
            f" | lineage={eval_result.lineage_parent_wallet[:8]}(hop{eval_result.lineage_hop_distance})"
            if eval_result.lineage_parent_wallet else ""
        )
        profile = SmartMoneyProfileModel(
            wallet_address=eval_result.wallet_address,
            tier="SEED",
            is_active=True,
            total_trades_recorded=eval_result.total_trades_90d,
            net_realized_profit_sol=eval_result.realized_pnl_90d_sol,
            total_volume_sol=eval_result.total_volume_sol,
            win_rate_pct=eval_result.win_rate_pct,
            source="AUTO_DISCOVERY",
            notes=(
                f"Discovered by discovery engine. "
                f"runner_hits={eval_result.runner_hit_count}, "
                f"pnl_90d={eval_result.realized_pnl_90d_sol:.2f} SOL, "
                f"pnl_30d={eval_result.realized_pnl_30d_sol:.2f} SOL, "
                f"provider={eval_result.pnl_provider}"
                f"{lineage_note}"
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
            "[bold cyan]\U0001f50d Smart Money Discovery Engine v2.0[/bold cyan]\n"
            f"Max runners: [bold]{self.config.max_runners:,}[/bold] | "
            f"Min hits: [bold]{self.config.min_runner_hits}[/bold] | "
            f"Min entry: [bold]{self.config.min_entry_sol} SOL[/bold] | "
            f"Min trades: [bold]{self.config.min_trades_90d}[/bold] | "
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
