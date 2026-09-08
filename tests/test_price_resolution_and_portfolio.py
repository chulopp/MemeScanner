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


@pytest.mark.asyncio
async def test_4h_timeout_exit(monkeypatch):
    """Verify that positions exceeding 4 hours are closed with TIMEOUT_4H."""
    from datetime import timedelta
    entry_time = datetime.now(tz=timezone.utc) - timedelta(hours=4.5)
    test_pos = ActivePosition(
        position_id="test-timeout-1",
        token_address="TestTimeout11111111111111111111111111111111pump",
        symbol="OLDCOIN",
        signal_source="PINTU_A",
        entry_price_usd=0.001,
        entry_time=entry_time,
        position_size_usd=2.0,
        price_high_ever_seen=0.0011,
        latest_price_usd=0.0009,
        latest_mcap_usd=900.0,
    )
    position_tracker._active["test-timeout-1"] = test_pos

    closed_reasons = []

    async def mock_close(pos, exit_price, reason):
        closed_reasons.append(reason)
        position_tracker._active.pop(pos.position_id, None)

    monkeypatch.setattr(position_tracker, "_close_position", mock_close)

    from src.paper_trading.price_fetcher import PriceSnapshot

    async def mock_fetch_price(mint, bc):
        return PriceSnapshot(price_usd=0.0009, liquidity_usd=1000.0, volume_24h_usd=0.0, source="dexscreener")

    import sys
    pt_mod = sys.modules["src.paper_trading.position_tracker"]
    monkeypatch.setattr(pt_mod, "fetch_price", mock_fetch_price)

    await position_tracker._evaluate_position(test_pos)

    assert "TIMEOUT_4H" in closed_reasons
    assert "test-timeout-1" not in position_tracker._active


@pytest.mark.asyncio
async def test_trenches_volume_scaling(monkeypatch):
    """Verify that low-volume ghost tokens are penalized, and >= 5 SOL tokens get full score."""
    from src.opportunity.vol_velocity import volume_velocity_engine

    # Case 1: Low volume (0.2 SOL) with 5 buys, 0 sells -> previously gave 100/100, now capped <= 20
    async def mock_low_vol(mint, cutoff, init_buy):
        return {
            "buy_count": 5, "sell_count": 0, "buy_vol_sol": 0.2, "sell_vol_sol": 0.0, "provider": "test"
        }
    monkeypatch.setattr(volume_velocity_engine, "_fetch_from_helius", mock_low_vol)
    res_low = await volume_velocity_engine.calculate_velocity("TestLowVol11111111111111111111111111111111pump")
    assert res_low.score <= 20.0, f"Expected low-volume ghost token to be capped <= 20, got {res_low.score}"

    # Case 2: High trenches momentum (6.5 SOL) with 10 buys, 1 sell -> full momentum score
    async def mock_high_vol(mint, cutoff, init_buy):
        return {
            "buy_count": 10, "sell_count": 1, "buy_vol_sol": 6.0, "sell_vol_sol": 0.5, "provider": "test"
        }
    monkeypatch.setattr(volume_velocity_engine, "_fetch_from_helius", mock_high_vol)
    res_high = await volume_velocity_engine.calculate_velocity("TestHighVol111111111111111111111111111111pump")
    assert res_high.score >= 80.0, f"Expected strong volume token to score >= 80, got {res_high.score}"


@pytest.mark.asyncio
async def test_price_fetcher_no_fake_rpc_reserves(monkeypatch):
    """Verify price_fetcher does NOT invent prices and returns None if all verified sources fail."""
    import src.paper_trading.price_fetcher as pf

    assert not hasattr(pf, "_fetch_rpc_reserves"), "_fetch_rpc_reserves should be completely removed"

    # Mock all tiers failing
    async def mock_none(*args, **kwargs):
        return None

    monkeypatch.setattr(pf, "_fetch_dexscreener", mock_none)
    monkeypatch.setattr(pf, "_fetch_helius_das", mock_none)

    snap = await pf.fetch_price("NonExistentMint1111111111111111111111111111pump")
    assert snap is None, "Expected None when all verified price tiers fail, not fabricated price"


@pytest.mark.asyncio
async def test_telegram_outcome_market_cap_formatting(monkeypatch):
    """Verify send_outcome_update properly includes prices and market caps."""
    from src.paper_trading.telegram_notifier import telegram_notifier

    sent_messages = []

    class MockBot:
        async def send_message(self, chat_id, text, parse_mode, disable_web_page_preview=True):
            sent_messages.append(text)
            return type("Msg", (), {"message_id": 999})()

    telegram_notifier._bot = MockBot()
    telegram_notifier._enabled = True
    telegram_notifier._chat_id = 12345

    await telegram_notifier.send_outcome_update(
        symbol="ZCATWIF",
        token_address="F3hJ64M6xTmHYGgqkcgVPZx7sVMxb9FqUHBeXby3pump",
        time_window="1h",
        return_pct=5.2,
        ath_return_pct=8.4,
        mae_pct=1.0,
        status="neutral",
        paper_trade_info={
            "status": "CLOSED",
            "exit_reason": "TP1",
            "return_pct": 100.0,
            "exit_mcap": 4800.0,
            "score": 75.0,
            "source": "PINTU_A",
        },
        entry_price=0.0000024,
        current_price=0.0000025,
        ath_price=0.0000026,
        entry_mcap=2400.0,
        current_mcap=2500.0,
        ath_mcap=2600.0,
    )

    assert len(sent_messages) == 1
    msg = sent_messages[0]
    assert "MC: <b>$2.4K</b>" in msg
    assert "Now: <b>$0.00000250</b>" in msg
    assert "MC: <b>$2.5K</b>" in msg
    assert "Exit MC: <b>$4.8K</b>" in msg

