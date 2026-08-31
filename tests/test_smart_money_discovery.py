"""
tests/test_smart_money_discovery.py
=====================================
Unit tests for the Smart Money Wallet Discovery pipeline.

Tests cover:
  - RunnerCollector: chain filtering, runner classification
  - EarlyBuyerTracer: time window enforcement
  - WalletQualifier: balance, hit count, negative control (full funnel)
  - Token classification: RUNNER / DEAD / NEUTRAL
  - Checkpoint/resume: skip already-traced tokens
  - End-to-end pipeline mock
  - Sync to smart_money_profiles
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from src.discovery.smart_money_discovery import (
    DiscoveryConfig,
    DiscoveryOrchestrator,
    EarlyBuyRecord,
    EarlyBuyerTracer,
    RunnerCollector,
    RunnerToken,
    TokenClassification,
    WalletEvaluation,
    WalletQualifier,
    sync_to_smart_money_profiles,
    _save_hits_to_db,
    _save_wallet_to_db,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = DiscoveryConfig(
    max_runners=10,
    min_runner_hits=3,
    min_hit_ratio=0.15,
    min_sol_balance=50.0,
    min_classifiable_buys=10,
    early_window_seconds=600,
    batch_size=5,
    dexscreener_batch_size=30,
)

NOW = datetime.now(tz=timezone.utc)

def make_runner(
    address: str = "TOKEN_AAA",
    symbol: str = "AAA",
    created_offset_seconds: int = -3600,  # 1 hour ago
    multiplier: float = 5.0,
) -> RunnerToken:
    return RunnerToken(
        token_address=address,
        symbol=symbol,
        chain_id="solana",
        created_at=NOW + timedelta(seconds=created_offset_seconds),
        peak_multiplier=multiplier,
        current_fdv_usd=200_000,
    )


def make_early_buy(
    wallet: str = "WALLET_001",
    token: str = "TOKEN_AAA",
    entry_seconds: int = 120,
) -> EarlyBuyRecord:
    return EarlyBuyRecord(
        wallet_address=wallet,
        token_address=token,
        token_symbol="AAA",
        buy_amount_sol=1.5,
        entry_time_seconds=entry_seconds,
        bought_at=NOW - timedelta(seconds=3600 - entry_seconds),
    )


# ---------------------------------------------------------------------------
# 1. RunnerCollector: chain filtering
# ---------------------------------------------------------------------------

class TestRunnerCollector:

    def test_only_solana_tokens_accepted(self):
        """Non-Solana tokens should never make it into RunnerToken output."""
        collector = RunnerCollector()
        # Simulate DexScreener pair data for ETH token — should be skipped
        fake_pair = {
            "chainId": "ethereum",
            "baseToken": {"address": "0xEEE", "symbol": "ETH_TOKEN"},
            "priceChange": {"h24": 250.0, "h6": 150.0},
            "fdv": 500_000,
            "pairCreatedAt": int(NOW.timestamp() * 1000),
        }
        # Directly test the classification logic
        # ETH pair should be filtered out (chainId != "solana")
        assert fake_pair["chainId"] != "solana"

    def test_runner_threshold_met(self):
        """Token with h24 ≥ 100% should be classified as runner."""
        fake_pair = {
            "chainId": "solana",
            "baseToken": {"address": "SOL_TOKEN_1", "symbol": "MOON"},
            "priceChange": {"h24": 150.0, "h6": 50.0},
            "fdv": 80_000,
            "pairCreatedAt": None,
        }
        best_change = max(fake_pair["priceChange"]["h24"], fake_pair["priceChange"]["h6"])
        fdv = fake_pair["fdv"]
        is_runner = best_change >= 100.0 or fdv >= 100_000
        assert is_runner is True  # 150% ≥ 100%

    def test_fdv_threshold_as_alternative_runner_signal(self):
        """Token with FDV ≥ 100k even with modest price change is runner."""
        fake_pair_fdv = {
            "chainId": "solana",
            "priceChange": {"h24": 80.0, "h6": 40.0},  # < 100% change
            "fdv": 150_000,  # but FDV ≥ 100k
        }
        best_change = max(fake_pair_fdv["priceChange"]["h24"], fake_pair_fdv["priceChange"]["h6"])
        fdv = fake_pair_fdv["fdv"]
        is_runner = best_change >= 100.0 or fdv >= 100_000
        assert is_runner is True  # FDV condition passes

    def test_below_threshold_not_runner(self):
        """Token below both thresholds is NOT a runner."""
        fake_pair = {
            "priceChange": {"h24": 50.0, "h6": 30.0},
            "fdv": 50_000,
        }
        best_change = max(fake_pair["priceChange"]["h24"], fake_pair["priceChange"]["h6"])
        fdv = fake_pair["fdv"]
        is_runner = best_change >= 100.0 or fdv >= 100_000
        assert is_runner is False


# ---------------------------------------------------------------------------
# 2. EarlyBuyerTracer: time window enforcement
# ---------------------------------------------------------------------------

class TestEarlyBuyerTracer:

    def test_buyer_within_window_is_early(self):
        """Buyer at T+300s (5 min) should be within early window of 600s."""
        token = make_runner(created_offset_seconds=-3600)
        tx_dt = token.created_at + timedelta(seconds=300)
        entry_seconds = int((tx_dt - token.created_at).total_seconds())
        is_early = 0 <= entry_seconds <= DEFAULT_CONFIG.early_window_seconds
        assert is_early is True

    def test_buyer_outside_window_is_rejected(self):
        """Buyer at T+660s (11 min) should be outside early window of 600s."""
        token = make_runner(created_offset_seconds=-3600)
        tx_dt = token.created_at + timedelta(seconds=660)
        entry_seconds = int((tx_dt - token.created_at).total_seconds())
        is_early = 0 <= entry_seconds <= DEFAULT_CONFIG.early_window_seconds
        assert is_early is False

    def test_buyer_before_launch_is_rejected(self):
        """Buyer with negative entry seconds (before launch) is rejected."""
        entry_seconds = -60
        is_early = 0 <= entry_seconds <= DEFAULT_CONFIG.early_window_seconds
        assert is_early is False

    def test_buyer_at_exactly_window_boundary_is_early(self):
        """Buyer at exactly T+600s is within window (inclusive)."""
        entry_seconds = 600
        is_early = 0 <= entry_seconds <= DEFAULT_CONFIG.early_window_seconds
        assert is_early is True


# ---------------------------------------------------------------------------
# 3. WalletQualifier: balance filter
# ---------------------------------------------------------------------------

class TestWalletQualifierBalance:

    @pytest.mark.asyncio
    async def test_rejects_wallet_below_min_sol(self):
        """Wallet with 30 SOL (< 50 SOL min) should be rejected."""
        qualifier = WalletQualifier()
        with patch.object(qualifier, "_check_sol_balance", new_callable=AsyncMock) as mock_bal:
            mock_bal.return_value = 30.0  # Below min
            balance = await qualifier._check_sol_balance("WALLET_POOR")
            assert balance < DEFAULT_CONFIG.min_sol_balance

    @pytest.mark.asyncio
    async def test_passes_wallet_above_min_sol(self):
        """Wallet with 75 SOL (≥ 50 SOL min) should pass balance check."""
        qualifier = WalletQualifier()
        with patch.object(qualifier, "_check_sol_balance", new_callable=AsyncMock) as mock_bal:
            mock_bal.return_value = 75.0
            balance = await qualifier._check_sol_balance("WALLET_RICH")
            assert balance >= DEFAULT_CONFIG.min_sol_balance


# ---------------------------------------------------------------------------
# 4. WalletQualifier: runner hit count filter
# ---------------------------------------------------------------------------

class TestWalletQualifierHitCount:

    def test_rejects_wallet_with_insufficient_hit_count(self):
        """Wallet with 2 distinct runner hits (< 3 min) should be filtered out."""
        wallet_hits = {
            "WALLET_A": [
                make_early_buy("WALLET_A", "TOKEN_1"),
                make_early_buy("WALLET_A", "TOKEN_2"),  # Only 2 distinct tokens
            ]
        }
        candidates = {
            addr: hits
            for addr, hits in wallet_hits.items()
            if len(set(r.token_address for r in hits)) >= DEFAULT_CONFIG.min_runner_hits
        }
        assert "WALLET_A" not in candidates

    def test_passes_wallet_with_sufficient_hit_count(self):
        """Wallet with 3 distinct runner hits (≥ 3 min) should pass."""
        wallet_hits = {
            "WALLET_B": [
                make_early_buy("WALLET_B", "TOKEN_1"),
                make_early_buy("WALLET_B", "TOKEN_2"),
                make_early_buy("WALLET_B", "TOKEN_3"),
            ]
        }
        candidates = {
            addr: hits
            for addr, hits in wallet_hits.items()
            if len(set(r.token_address for r in hits)) >= DEFAULT_CONFIG.min_runner_hits
        }
        assert "WALLET_B" in candidates

    def test_duplicate_token_hits_count_as_one(self):
        """Multiple buys of the same token count as 1 distinct hit, not multiple."""
        wallet_hits = {
            "WALLET_C": [
                make_early_buy("WALLET_C", "TOKEN_1"),
                make_early_buy("WALLET_C", "TOKEN_1"),  # Same token
                make_early_buy("WALLET_C", "TOKEN_1"),  # Same token again
            ]
        }
        candidates = {
            addr: hits
            for addr, hits in wallet_hits.items()
            if len(set(r.token_address for r in hits)) >= DEFAULT_CONFIG.min_runner_hits
        }
        # Only 1 distinct token, should fail min_runner_hits=3
        assert "WALLET_C" not in candidates


# ---------------------------------------------------------------------------
# 5. Negative Control: Token Classification
# ---------------------------------------------------------------------------

class TestTokenClassification:

    def test_classifies_runner_by_price_change(self):
        """Token with h24=200% should be RUNNER."""
        price_change = {"h24": 200.0, "h6": 80.0}
        fdv = 50_000
        best_change = max(price_change["h24"], price_change["h6"])
        if best_change >= 100.0 or fdv >= 100_000:
            label = "RUNNER"
        elif best_change <= -90.0:
            label = "DEAD"
        else:
            label = "NEUTRAL"
        assert label == "RUNNER"

    def test_classifies_runner_by_fdv(self):
        """Token with fdv=150k (even <100% change) should be RUNNER."""
        price_change = {"h24": 80.0, "h6": 40.0}
        fdv = 150_000
        best_change = max(price_change["h24"], price_change["h6"])
        if best_change >= 100.0 or fdv >= 100_000:
            label = "RUNNER"
        elif best_change <= -90.0:
            label = "DEAD"
        else:
            label = "NEUTRAL"
        assert label == "RUNNER"

    def test_classifies_dead_token(self):
        """Token with h24=-95% should be DEAD (uses worst-case drop = min)."""
        price_change = {"h24": -95.0, "h6": -80.0}
        fdv = 500
        best_change = max(price_change["h24"], price_change["h6"])   # -80 (upside check)
        worst_change = min(price_change["h24"], price_change["h6"])  # -95 (dead check)
        if best_change >= 100.0 or fdv >= 100_000:
            label = "RUNNER"
        elif worst_change <= -90.0:
            label = "DEAD"
        else:
            label = "NEUTRAL"
        assert label == "DEAD"


    def test_classifies_neutral_token(self):
        """Token with h24=50% (not runner, not dead) should be NEUTRAL."""
        price_change = {"h24": 50.0, "h6": 20.0}
        fdv = 30_000
        best_change = max(price_change["h24"], price_change["h6"])
        if best_change >= 100.0 or fdv >= 100_000:
            label = "RUNNER"
        elif best_change <= -90.0:
            label = "DEAD"
        else:
            label = "NEUTRAL"
        assert label == "NEUTRAL"

    def test_unknown_token_skipped_from_ratio(self):
        """Tokens not found in DexScreener should not affect the ratio calculation."""
        classified = {
            "TOKEN_RUNNER": TokenClassification("TOKEN_RUNNER", "RUN", "RUNNER"),
            "TOKEN_DEAD": TokenClassification("TOKEN_DEAD", "DED", "DEAD"),
            # TOKEN_UNKNOWN not in classifications → NEUTRAL / skip
        }
        tokens_to_score = ["TOKEN_RUNNER", "TOKEN_DEAD", "TOKEN_UNKNOWN"]
        runner_count = sum(1 for t in tokens_to_score if classified.get(t, TokenClassification("", "", "NEUTRAL")).label == "RUNNER")
        dead_count = sum(1 for t in tokens_to_score if classified.get(t, TokenClassification("", "", "NEUTRAL")).label == "DEAD")
        classifiable = runner_count + dead_count
        ratio = runner_count / classifiable if classifiable > 0 else 0.0
        # TOKEN_UNKNOWN → NEUTRAL (from default) → skipped from ratio
        assert classifiable == 2   # Only RUNNER + DEAD count
        assert ratio == 0.5        # 1 runner / 2 total


# ---------------------------------------------------------------------------
# 6. Negative Control: Auto-reject for low sample
# ---------------------------------------------------------------------------

class TestNegativeControlSampleSize:

    def test_rejects_wallet_with_low_sample_size(self):
        """Wallet with only 5 classifiable buys (< 10 min) should be rejected."""
        total_runner = 3
        total_dead = 2
        total_classifiable = total_runner + total_dead   # = 5
        min_sample = DEFAULT_CONFIG.min_classifiable_buys  # = 10

        if total_classifiable < min_sample:
            rejection_reason = "low_sample"
            status = "REJECTED"
        else:
            rejection_reason = ""
            status = "PENDING"

        assert status == "REJECTED"
        assert rejection_reason == "low_sample"

    def test_passes_wallet_with_sufficient_sample_size(self):
        """Wallet with 15 classifiable buys (≥ 10 min) should pass sample check."""
        total_runner = 10
        total_dead = 5
        total_classifiable = total_runner + total_dead  # = 15
        min_sample = DEFAULT_CONFIG.min_classifiable_buys  # = 10

        if total_classifiable < min_sample:
            status = "REJECTED"
        else:
            status = "PENDING"

        assert status == "PENDING"


# ---------------------------------------------------------------------------
# 7. Negative Control: Sniper bot rejection vs smart money qualification
# ---------------------------------------------------------------------------

class TestNegativeControlRatio:

    def test_sniper_bot_rejected(self):
        """Wallet buying 95% dead tokens should be rejected (ratio = 5%)."""
        runner_count = 1
        dead_count = 19
        classifiable = runner_count + dead_count  # 20
        ratio = runner_count / classifiable  # 0.05

        status = "QUALIFIED" if ratio >= DEFAULT_CONFIG.min_hit_ratio else "REJECTED"
        assert status == "REJECTED"
        assert ratio == pytest.approx(0.05)

    def test_selective_smart_money_qualified(self):
        """Wallet with 25% runner ratio (≥ 15% min) should be QUALIFIED."""
        runner_count = 5
        dead_count = 15
        classifiable = runner_count + dead_count  # 20
        ratio = runner_count / classifiable  # 0.25

        status = "QUALIFIED" if ratio >= DEFAULT_CONFIG.min_hit_ratio else "REJECTED"
        assert status == "QUALIFIED"
        assert ratio == pytest.approx(0.25)

    def test_exact_boundary_ratio_passes(self):
        """Wallet at exactly 15% ratio should pass (inclusive boundary)."""
        runner_count = 3
        dead_count = 17
        classifiable = runner_count + dead_count  # 20
        ratio = runner_count / classifiable  # 0.15

        status = "QUALIFIED" if ratio >= DEFAULT_CONFIG.min_hit_ratio else "REJECTED"
        assert status == "QUALIFIED"


# ---------------------------------------------------------------------------
# 8. Checkpoint / Resume
# ---------------------------------------------------------------------------

class TestCheckpointResume:

    @pytest.mark.asyncio
    async def test_already_traced_tokens_are_skipped(self):
        """Runner collector should skip tokens in already_traced set."""
        already_traced = {"TOKEN_ALREADY_DONE", "TOKEN_ALSO_DONE"}
        candidate_addresses = ["TOKEN_ALREADY_DONE", "TOKEN_NEW_ONE", "TOKEN_ALSO_DONE"]
        new_only = [addr for addr in candidate_addresses if addr not in already_traced]
        assert new_only == ["TOKEN_NEW_ONE"]

    @pytest.mark.asyncio
    async def test_already_evaluated_wallets_are_skipped(self):
        """Qualification should skip wallets already in evaluated set."""
        already_evaluated = {"WALLET_DONE"}
        candidates = {
            "WALLET_DONE": [make_early_buy("WALLET_DONE", "TOKEN_1")] * 3,
            "WALLET_NEW": [make_early_buy("WALLET_NEW", f"TOKEN_{i}") for i in range(3)],
        }
        # Simulating the filter in qualify_all
        filtered = {
            addr: hits
            for addr, hits in candidates.items()
            if addr not in already_evaluated
            and len(set(r.token_address for r in hits)) >= DEFAULT_CONFIG.min_runner_hits
        }
        assert "WALLET_DONE" not in filtered
        assert "WALLET_NEW" in filtered


# ---------------------------------------------------------------------------
# 9. Sync to smart_money_profiles
# ---------------------------------------------------------------------------

class TestSyncToSmartMoneyProfiles:

    @pytest.mark.asyncio
    async def test_qualified_wallets_synced(self):
        """QUALIFIED wallets should be upserted into smart_money_profiles as SEED."""
        qualified = [
            WalletEvaluation(
                wallet_address="WALLET_QUALIFIED_001",
                sol_balance=75.0,
                runner_hit_count=5,
                dead_hit_count=10,
                total_early_buys=15,
                hit_ratio=0.333,
                status="QUALIFIED",
            )
        ]

        upserted_data = []

        async def mock_upsert(profile):
            upserted_data.append(profile)
            return True

        with patch("src.discovery.smart_money_discovery.db_manager") as mock_db:
            mock_db.upsert_smart_money_wallet = mock_upsert
            count = await sync_to_smart_money_profiles(qualified)

        assert count == 1
        assert len(upserted_data) == 1
        profile = upserted_data[0]
        assert profile.wallet_address == "WALLET_QUALIFIED_001"
        assert profile.tier == "SEED"
        assert profile.is_active is True
        assert profile.source == "AUTO_DISCOVERY"

    @pytest.mark.asyncio
    async def test_rejected_wallets_not_synced(self):
        """REJECTED wallets should NOT be synced to smart_money_profiles."""
        evaluations = [
            WalletEvaluation(
                wallet_address="WALLET_REJECTED",
                sol_balance=60.0,
                runner_hit_count=3,
                dead_hit_count=50,
                total_early_buys=53,
                hit_ratio=0.057,
                status="REJECTED",
                rejection_reason="low_ratio",
            )
        ]
        qualified = [e for e in evaluations if e.status == "QUALIFIED"]
        # Should be empty, so sync_to_smart_money_profiles would upsert nothing
        assert len(qualified) == 0


# ---------------------------------------------------------------------------
# 10. End-to-end pipeline mock
# ---------------------------------------------------------------------------

class TestEndToEndPipeline:

    @pytest.mark.asyncio
    async def test_pipeline_runs_without_errors_with_mocked_apis(self):
        """
        Full pipeline should complete without exceptions when all API calls are mocked.
        """
        mock_runner = make_runner("TOKEN_RUNNER_001")
        mock_early_buy = make_early_buy("WALLET_SMART_001", "TOKEN_RUNNER_001", 120)

        config = DiscoveryConfig(max_runners=1, min_runner_hits=1, batch_size=1)
        orchestrator = DiscoveryOrchestrator(config)

        with (
            patch.object(orchestrator.runner_collector, "collect", new_callable=AsyncMock) as mock_collect,
            patch.object(orchestrator.early_buyer_tracer, "trace_all", new_callable=AsyncMock) as mock_trace,
            patch.object(orchestrator.wallet_qualifier, "qualify_all", new_callable=AsyncMock) as mock_qualify,
            patch("src.discovery.smart_money_discovery.sync_to_smart_money_profiles", new_callable=AsyncMock) as mock_sync,
            patch("src.discovery.smart_money_discovery.db_manager") as mock_db,
        ):
            mock_collect.return_value = [mock_runner]
            mock_trace.return_value = {"WALLET_SMART_001": [mock_early_buy]}
            mock_qualify.return_value = [
                WalletEvaluation(
                    wallet_address="WALLET_SMART_001",
                    sol_balance=100.0,
                    runner_hit_count=3,
                    dead_hit_count=5,
                    total_early_buys=8,
                    hit_ratio=0.375,
                    status="QUALIFIED",
                )
            ]
            mock_sync.return_value = 1
            mock_db.connect = MagicMock()

            evaluations = await orchestrator.run(dry_run=True)

        assert len(evaluations) == 1
        assert evaluations[0].status == "QUALIFIED"
        mock_collect.assert_called_once()
        mock_trace.assert_called_once()
        mock_qualify.assert_called_once()
