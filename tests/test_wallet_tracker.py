"""
Unit tests for Pintu B: WalletTrackerListener

Tests cover:
  1. SWAP BUY detection from Helius WebSocket messages
  2. Conviction gate (rejects < 0.5 SOL, accepts >= 0.5 SOL)
  3. Token mint filtering (skips WSOL and other non-memecoin mints)
  4. RawTokenEvent source attribution (source='WALLET_TRACKER')
  5. Wallet sync and dynamic tracking set management
"""

from __future__ import annotations

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from src.ingestion.schemas import RawTokenEvent
from src.ingestion.wallet_tracker_ws import (
    WalletTrackerListener,
    MIN_CONVICTION_SOL,
    _SKIP_MINTS,
)
from src.discovery.wallet_replay_audit import (
    SwapEvent,
    RoundTripTrade,
    _parse_swaps,
    _match_round_trips,
    WalletAuditReport,
)


# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------

MOCK_WALLET = "9s4ji7MRh6xx9PVx7D4by8YjpHMfPVtHfYuMUy8VaTTp"
MOCK_TOKEN_MINT = "2zcGoHYwuz6zxPy3zvCQuarRaqX8JzsCX9vAHNXSpump"
MOCK_WSOL = "So11111111111111111111111111111111111111112"


def _make_helius_tx_message(
    wallet: str,
    token_mint: str,
    sol_pre: int,
    sol_post: int,
    token_post_amount: float,
    direction: str = "BUY",
    tx_sig: str = "abc123",
) -> dict:
    """Build a minimal Helius transactionSubscribe WebSocket message for testing."""
    if direction == "BUY":
        pre_token_balances = []
        post_token_balances = [
            {
                "owner": wallet,
                "mint": token_mint,
                "uiTokenAmount": {"uiAmount": token_post_amount},
            }
        ]
    else:
        pre_token_balances = [
            {
                "owner": wallet,
                "mint": token_mint,
                "uiTokenAmount": {"uiAmount": token_post_amount},
            }
        ]
        post_token_balances = []

    return {
        "params": {
            "result": {
                "value": {
                    "signature": tx_sig,
                    "transaction": {
                        "message": {
                            "accountKeys": [{"pubkey": wallet}, {"pubkey": "SomeOtherProgram1111111111111111111111"}]
                        }
                    },
                    "meta": {
                        "preBalances": [sol_pre, 0],
                        "postBalances": [sol_post, 0],
                        "innerInstructions": [],
                        "preTokenBalances": pre_token_balances,
                        "postTokenBalances": post_token_balances,
                    },
                }
            }
        }
    }


# ---------------------------------------------------------------------------
# WalletTrackerListener Tests
# ---------------------------------------------------------------------------

class TestConvictionGate:
    """Test that the conviction SOL threshold is correctly applied."""

    @pytest.mark.asyncio
    async def test_rejects_buy_below_min_conviction(self):
        """Transactions spending < MIN_CONVICTION_SOL must NOT trigger callback."""
        emitted: list[RawTokenEvent] = []

        async def callback(event: RawTokenEvent):
            emitted.append(event)

        listener = WalletTrackerListener(callback)
        listener._tracked_wallets = {MOCK_WALLET}

        # Spend 0.2 SOL (below 0.5 SOL threshold)
        # 0.2 SOL = 200_000_000 lamports
        sol_pre = 1_000_000_000   # 1.0 SOL
        sol_post = 800_000_000    # 0.8 SOL → net spend 0.2 SOL

        msg = _make_helius_tx_message(MOCK_WALLET, MOCK_TOKEN_MINT, sol_pre, sol_post, 1_000_000.0)
        await listener._handle_message(msg)

        assert len(emitted) == 0, "Should NOT emit event for sub-conviction buy"

    @pytest.mark.asyncio
    async def test_accepts_buy_at_min_conviction(self):
        """Transactions spending exactly MIN_CONVICTION_SOL must pass conviction gate."""
        emitted: list[RawTokenEvent] = []
        mock_metadata = {
            "symbol": "TEST",
            "name": "Test Token",
            "deployer": None,
            "launch_venue": "pump_fun",
            "total_supply": 1_000_000_000.0,
            "initial_sol_liquidity": 30.0,
            "bonding_curve_address": None,
            "pool_address": None,
        }

        async def callback(event: RawTokenEvent):
            emitted.append(event)

        listener = WalletTrackerListener(callback)
        listener._tracked_wallets = {MOCK_WALLET}
        listener._fetch_token_metadata = AsyncMock(return_value=mock_metadata)

        # Spend exactly 0.5 SOL = 500_000_000 lamports
        sol_pre = 1_500_000_000
        sol_post = 1_000_000_000

        msg = _make_helius_tx_message(MOCK_WALLET, MOCK_TOKEN_MINT, sol_pre, sol_post, 5_000_000.0)
        await listener._handle_message(msg)

        assert len(emitted) == 1, "Should emit event for buy at MIN_CONVICTION_SOL"

    @pytest.mark.asyncio
    async def test_accepts_buy_above_min_conviction(self):
        """Transactions spending > MIN_CONVICTION_SOL must trigger callback."""
        emitted: list[RawTokenEvent] = []
        mock_metadata = {
            "symbol": "RUNNER",
            "name": "Runner Token",
            "deployer": None,
            "launch_venue": "pump_fun",
            "total_supply": 1_000_000_000.0,
            "initial_sol_liquidity": 30.0,
            "bonding_curve_address": None,
            "pool_address": None,
        }

        async def callback(event: RawTokenEvent):
            emitted.append(event)

        listener = WalletTrackerListener(callback)
        listener._tracked_wallets = {MOCK_WALLET}
        listener._fetch_token_metadata = AsyncMock(return_value=mock_metadata)

        # Spend 2.0 SOL = 2_000_000_000 lamports
        sol_pre = 5_000_000_000
        sol_post = 3_000_000_000

        msg = _make_helius_tx_message(MOCK_WALLET, MOCK_TOKEN_MINT, sol_pre, sol_post, 20_000_000.0)
        await listener._handle_message(msg)

        assert len(emitted) == 1
        assert emitted[0].source == "WALLET_TRACKER"
        assert emitted[0].triggered_by_wallet == MOCK_WALLET


class TestMintFiltering:
    """Test that known non-token mints are skipped."""

    @pytest.mark.asyncio
    async def test_skips_wsol_as_output_token(self):
        """If wallet received WSOL as output (WSOL swap), it should not trigger as a memecoin buy."""
        emitted: list[RawTokenEvent] = []

        async def callback(event: RawTokenEvent):
            emitted.append(event)

        listener = WalletTrackerListener(callback)
        listener._tracked_wallets = {MOCK_WALLET}

        # Attempt to trigger with WSOL as the received token mint
        sol_pre = 3_000_000_000
        sol_post = 2_000_000_000
        msg = _make_helius_tx_message(MOCK_WALLET, MOCK_WSOL, sol_pre, sol_post, 1_000_000.0)
        await listener._handle_message(msg)

        assert len(emitted) == 0, "WSOL output should be skipped (not a memecoin)"


class TestEventAttribution:
    """Test RawTokenEvent is correctly attributed for Pintu B."""

    @pytest.mark.asyncio
    async def test_event_has_wallet_tracker_source(self):
        """Emitted event must have source='WALLET_TRACKER' and wallet attribution fields."""
        emitted: list[RawTokenEvent] = []
        mock_metadata = {
            "symbol": "ALPHA",
            "name": "Alpha Token",
            "deployer": None,
            "launch_venue": "pump_fun",
            "total_supply": 1_000_000_000.0,
            "initial_sol_liquidity": 30.0,
            "bonding_curve_address": None,
            "pool_address": None,
        }

        async def callback(event: RawTokenEvent):
            emitted.append(event)

        listener = WalletTrackerListener(callback)
        listener._tracked_wallets = {MOCK_WALLET}
        listener._fetch_token_metadata = AsyncMock(return_value=mock_metadata)

        sol_pre = 3_000_000_000
        sol_post = 1_000_000_000  # 2 SOL spent

        msg = _make_helius_tx_message(
            MOCK_WALLET, MOCK_TOKEN_MINT, sol_pre, sol_post, 10_000_000.0, tx_sig="SIG_XYZ"
        )
        await listener._handle_message(msg)

        assert len(emitted) == 1
        event = emitted[0]
        assert event.source == "WALLET_TRACKER"
        assert event.triggered_by_wallet == MOCK_WALLET
        assert event.triggered_by_wallet_sol_spent == pytest.approx(2.0, abs=0.001)
        assert event.triggered_by_tx_signature == "SIG_XYZ"
        assert event.token_address == MOCK_TOKEN_MINT

    @pytest.mark.asyncio
    async def test_new_pair_event_has_default_source(self):
        """A standard RawTokenEvent (Pintu A) must default to source='NEW_PAIR'."""
        event = RawTokenEvent(
            token_address=MOCK_TOKEN_MINT,
            symbol="TEST",
            name="Test Token",
            launch_venue="pump_fun",
        )
        assert event.source == "NEW_PAIR"
        assert event.triggered_by_wallet is None


class TestWalletSyncManagement:
    """Test dynamic wallet list sync logic."""

    @pytest.mark.asyncio
    async def test_wallet_sync_loads_active_wallets(self):
        """_sync_wallets should populate _tracked_wallets from DB."""
        mock_wallets = [
            {"wallet_address": "Wallet111111111111111111111111111111111111"},
            {"wallet_address": "Wallet222222222222222222222222222222222222"},
        ]

        async def callback(event: RawTokenEvent):
            pass

        listener = WalletTrackerListener(callback)

        with patch("src.ingestion.wallet_tracker_ws.db_manager") as mock_db:
            mock_db.get_smart_money_wallets = AsyncMock(return_value=mock_wallets)
            await listener._sync_wallets()

        assert "Wallet111111111111111111111111111111111111" in listener._tracked_wallets
        assert "Wallet222222222222222222222222222222222222" in listener._tracked_wallets
        assert len(listener._tracked_wallets) == 2

    @pytest.mark.asyncio
    async def test_wallet_sync_handles_db_error_gracefully(self):
        """_sync_wallets should not crash on DB errors (maintains existing wallet list)."""
        async def callback(event: RawTokenEvent):
            pass

        listener = WalletTrackerListener(callback)
        listener._tracked_wallets = {"ExistingWallet1111111111111111111111111111"}

        with patch("src.ingestion.wallet_tracker_ws.db_manager") as mock_db:
            mock_db.get_smart_money_wallets = AsyncMock(side_effect=Exception("DB unreachable"))
            await listener._sync_wallets()  # Should not raise

        # Existing wallets should remain
        assert "ExistingWallet1111111111111111111111111111" in listener._tracked_wallets


# ---------------------------------------------------------------------------
# WalletReplayAudit Tests
# ---------------------------------------------------------------------------

class TestSwapEventParsing:
    """Test _parse_swaps correctly extracts BUY and SELL events."""

    def _make_helius_rest_tx(self, direction: str, sol_amount: float, token_mint: str, token_amount: float) -> dict:
        """Create a minimal Helius REST API transaction dict."""
        ts = 1_725_000_000
        if direction == "BUY":
            return {
                "timestamp": ts,
                "signature": f"SIG_{direction}",
                "source": "JUPITER",
                "events": {
                    "swap": {
                        "nativeInput": {"amount": int(sol_amount * 1e9)},
                        "nativeOutput": None,
                        "tokenInputs": [],
                        "tokenOutputs": [{
                            "mint": token_mint,
                            "rawTokenAmount": {"tokenAmount": str(int(token_amount * 1e6)), "decimals": 6}
                        }],
                    }
                }
            }
        else:
            return {
                "timestamp": ts + 120,
                "signature": f"SIG_{direction}",
                "source": "JUPITER",
                "events": {
                    "swap": {
                        "nativeInput": None,
                        "nativeOutput": {"amount": int(sol_amount * 1e9)},
                        "tokenInputs": [{
                            "mint": token_mint,
                            "rawTokenAmount": {"tokenAmount": str(int(token_amount * 1e6)), "decimals": 6}
                        }],
                        "tokenOutputs": [],
                    }
                }
            }

    def test_parses_buy_event_correctly(self):
        txs = [self._make_helius_rest_tx("BUY", 1.0, MOCK_TOKEN_MINT, 1_000_000.0)]
        events = _parse_swaps(txs, MOCK_WALLET)
        assert len(events) == 1
        e = events[0]
        assert e.direction == "BUY"
        assert e.token_mint == MOCK_TOKEN_MINT
        assert e.sol_amount == pytest.approx(1.0, abs=0.001)
        assert e.token_amount == pytest.approx(1_000_000.0, abs=1.0)

    def test_parses_sell_event_correctly(self):
        txs = [self._make_helius_rest_tx("SELL", 1.5, MOCK_TOKEN_MINT, 1_000_000.0)]
        events = _parse_swaps(txs, MOCK_WALLET)
        assert len(events) == 1
        e = events[0]
        assert e.direction == "SELL"
        assert e.sol_amount == pytest.approx(1.5, abs=0.001)

    def test_skips_wsol_mint(self):
        txs = [self._make_helius_rest_tx("BUY", 1.0, MOCK_WSOL, 100.0)]
        events = _parse_swaps(txs, MOCK_WALLET)
        assert len(events) == 0, "WSOL must be filtered out"

    def test_skips_dust_trades_below_threshold(self):
        txs = [self._make_helius_rest_tx("BUY", 0.0005, MOCK_TOKEN_MINT, 10.0)]
        events = _parse_swaps(txs, MOCK_WALLET)
        assert len(events) == 0, "Sub-0.001 SOL dust trades should be skipped"


class TestRoundTripMatching:
    """Test _match_round_trips pairs BUY and SELL events correctly."""

    def _make_event(self, direction: str, mint: str, sol: float, tokens: float, ts_offset: int = 0) -> SwapEvent:
        return SwapEvent(
            tx_signature=f"SIG_{direction}_{ts_offset}",
            timestamp=datetime.fromtimestamp(1_725_000_000 + ts_offset, tz=timezone.utc),
            direction=direction,
            token_mint=mint,
            sol_amount=sol,
            token_amount=tokens,
            price_sol_per_token=sol / tokens,
            source_platform="JUPITER",
        )

    def test_matches_buy_sell_pair(self):
        events = [
            self._make_event("BUY", MOCK_TOKEN_MINT, 1.0, 1_000_000.0, 0),
            self._make_event("SELL", MOCK_TOKEN_MINT, 2.0, 1_000_000.0, 120),
        ]
        trips = _match_round_trips(events)
        assert len(trips) == 1
        rt = trips[0]
        assert rt.is_closed
        assert rt.realized_pnl_sol == pytest.approx(1.0, abs=0.001)
        assert rt.return_pct == pytest.approx(100.0, abs=0.1)

    def test_open_position_when_no_sell(self):
        events = [self._make_event("BUY", MOCK_TOKEN_MINT, 1.0, 1_000_000.0)]
        trips = _match_round_trips(events)
        assert len(trips) == 1
        assert not trips[0].is_closed

    def test_loss_trade_correctly_calculated(self):
        events = [
            self._make_event("BUY", MOCK_TOKEN_MINT, 1.0, 1_000_000.0, 0),
            self._make_event("SELL", MOCK_TOKEN_MINT, 0.4, 1_000_000.0, 600),
        ]
        trips = _match_round_trips(events)
        assert trips[0].realized_pnl_sol == pytest.approx(-0.6, abs=0.001)
        assert trips[0].return_pct == pytest.approx(-60.0, abs=0.1)

    def test_different_mints_not_cross_matched(self):
        mint_a = MOCK_TOKEN_MINT
        mint_b = "AnotherTokenMint11111111111111111111111111p"
        events = [
            self._make_event("BUY", mint_a, 1.0, 1_000_000.0, 0),
            self._make_event("SELL", mint_b, 2.0, 1_000_000.0, 120),
        ]
        trips = _match_round_trips(events)
        # mint_a has an open BUY, mint_b has an unmatched SELL (no prior BUY)
        assert len(trips) == 1
        assert trips[0].token_mint == mint_a
        assert not trips[0].is_closed
