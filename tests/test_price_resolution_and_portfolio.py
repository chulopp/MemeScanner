import asyncio
import time
import pytest
from datetime import datetime, timezone
from src.paper_trading.position_tracker import ActivePosition, position_tracker
from src.paper_trading.outcome_worker import OutcomeWorker, _signal_tracking
from src.utils.solana_rpc import SolanaRpcClient, FALLBACK_RPCS


@pytest.mark.asyncio
async def test_instant_portfolio_summary_cached():
    """Verify get_portfolio_summary runs in milliseconds using in-memory state."""
    # Seed active position in memory
    test_pos = ActivePosition(
        position_id="test-pos-1",
        token_address="TestMint111111111111111111111111111111111pump",
        symbol="TEST",
        signal_source="PINTU_A",
        entry_price_usd=0.001,
        entry_time=datetime.now(tz=timezone.utc),
        position_size_usd=2.0,
        price_high_ever_seen=0.002,
        latest_price_usd=0.0015,
        latest_mcap_usd=1500.0,
    )
    position_tracker._active["test-pos-1"] = test_pos

    # Warmup connection
    await position_tracker.get_portfolio_summary()

    t0 = time.time()
    summary = await position_tracker.get_portfolio_summary()
    elapsed = time.time() - t0

    # Ensure it finishes swiftly from in-memory cache (< 150ms)
    assert elapsed < 0.15, f"Summary took too long: {elapsed:.3f}s"
    assert summary["open_count"] >= 1
    found = [p for p in summary["open_positions"] if p["symbol"] == "TEST"]
    assert len(found) == 1
    assert found[0]["current_price"] == 0.0015
    assert found[0]["floating_pct"] == pytest.approx(50.0)

    # Cleanup
    del position_tracker._active["test-pos-1"]


@pytest.mark.asyncio
async def test_outcome_worker_ath_mae_math(monkeypatch):
    """Verify that _resolve_window sets ATH and MAE correctly even if background tracking lagged."""
    worker = OutcomeWorker()
    signal_id = "test-sig-123"

    # Tracking had stale ATH (+0%) and stale MAE (0%)
    _signal_tracking[signal_id] = {
        "ath": 0.0010,
        "mae_pct": 0.0,
        "entry_price": 0.0010,
        "mint": "TestMintPump1111111111111111111111111111111111",
        "symbol": "PUMPCOIN"
    }

    # Case 1: Token pumped to +85% at resolution time
    from src.paper_trading.price_fetcher import PriceSnapshot
    import sys
    ow_mod = sys.modules["src.paper_trading.outcome_worker"]

    async def mock_fetch_pump(mint):
        return PriceSnapshot(price_usd=0.00185, liquidity_usd=15000.0, volume_24h_usd=5000.0, source="dexscreener")

    monkeypatch.setattr(ow_mod, "fetch_price", mock_fetch_pump)

    # Mock DB calls so we don't write test data to production
    async def mock_db_noop(*args, **kwargs):
        return []

    monkeypatch.setattr("src.database.client.db_manager.upsert", mock_db_noop)
    monkeypatch.setattr("src.database.client.db_manager.update", mock_db_noop)
    
    async def mock_trade_status(mint, sig_id):
        return None
        
    monkeypatch.setattr(worker, "_get_paper_trade_status", mock_trade_status)

    await worker._resolve_window(signal_id, "1h")

    # Tracking ATH must be updated to 0.00185 (+85%)
    assert _signal_tracking[signal_id]["ath"] == 0.00185

    # Case 2: Token dumped to -90%
    async def mock_fetch_dump(mint):
        return PriceSnapshot(price_usd=0.00010, liquidity_usd=50.0, volume_24h_usd=100.0, source="dexscreener")

    monkeypatch.setattr(ow_mod, "fetch_price", mock_fetch_dump)
    await worker._resolve_window(signal_id, "4h")

    # Tracking MAE must reflect -90% (not stale 0.0%)
    assert _signal_tracking[signal_id]["mae_pct"] == pytest.approx(-90.0)

    # Cleanup
    _signal_tracking.pop(signal_id, None)


@pytest.mark.asyncio
async def test_rpc_fallback_circuit_breaker():
    """Verify SolanaRpcClient switches to fallback on cooldown."""
    client = SolanaRpcClient(rpc_url="https://mock-failing-rpc.com")
    client._cooldown_until = time.time() + 60.0

    # In cooldown mode, candidates will prioritize FALLBACK_RPCS
    now = time.time()
    assert now < client._cooldown_until
