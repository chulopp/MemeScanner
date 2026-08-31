"""
Tests for Fase B: Delayed Evaluator & Pipeline Integration

Validates:
  1. delayed_evaluator.enqueue stores payload in memory / redis
  2. delayed_evaluator._process_token executes OpportunityScorer and records signal if score >= threshold
  3. pipeline.py process_token enqueues to delayed_evaluator when safety passes
  4. pipeline.py retry mechanism when paper trading signal recording fails
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

from src.ingestion.schemas import RawTokenEvent
from src.filters.pipeline import filter_pipeline
from src.paper_trading.delayed_evaluator import DelayedEvaluator


@pytest.fixture
def mock_token_event():
    return RawTokenEvent(
        token_address="7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr",
        symbol="DELAY",
        name="Delayed Token",
        launch_venue="pump_fun",
        initial_buy_amount=30_000_000.0,
        total_supply=1_000_000_000.0
    )


@pytest.mark.asyncio
async def test_delayed_evaluator_enqueue_in_memory(mock_token_event):
    """When Redis is None, delayed_evaluator enqueues in memory."""
    evaluator = DelayedEvaluator()
    evaluator._redis = None  # force in-memory mode

    mock_result = MagicMock()
    mock_result.filter_pass = True

    await evaluator.enqueue(mock_token_event, mock_result, stage1_scores={"fee": 50})

    assert len(evaluator._in_memory_queue) == 1
    item = evaluator._in_memory_queue[0]
    assert item["token_address"] == mock_token_event.token_address
    assert item["symbol"] == "DELAY"
    assert item["stage1_scores"] == {"fee": 50}


@pytest.mark.asyncio
async def test_delayed_evaluator_process_token_scores_and_records(mock_token_event):
    """Stage 2 _process_token scores token and records signal if above threshold."""
    evaluator = DelayedEvaluator()

    payload = {
        "token_address": mock_token_event.token_address,
        "symbol": mock_token_event.symbol,
        "event_json": mock_token_event.model_dump_json(),
        "stage1_scores": {}
    }

    mock_score_res = MagicMock()
    mock_score_res.opportunity_score = 75.0
    mock_score_res.breakdown = {"vol_velocity": 80}

    with patch("src.opportunity.scorer.OpportunityScorer.score_token", AsyncMock(return_value=mock_score_res)):
        with patch("src.paper_trading.signal_recorder.record_signal", AsyncMock(return_value="sig-123")) as mock_record:
            with patch("src.paper_trading.outcome_worker.outcome_worker.schedule_signal", AsyncMock()) as mock_sched:
                with patch("src.paper_trading.price_fetcher.fetch_price", AsyncMock(return_value=None)):
                    await evaluator._process_token(mock_token_event.token_address, payload)

                    mock_record.assert_called_once()
                    mock_sched.assert_called_once()


@pytest.mark.asyncio
async def test_pipeline_enqueues_to_delayed_evaluator(mock_token_event):
    """Pipeline enqueues token into delayed_evaluator when safety passes."""
    with patch("src.filters.instant_scalp.instant_scalp_filter.evaluate", new_callable=AsyncMock) as mock_scalp:
        mock_scalp.return_value = {"flags_count": 0, "details": {}}
        with patch("src.paper_trading.delayed_evaluator.delayed_evaluator.enqueue", new_callable=AsyncMock) as mock_enqueue:
            with patch("src.paper_trading.signal_recorder.record_signal", new_callable=AsyncMock):
                result = await filter_pipeline.process_token(mock_token_event)

                assert result.filter_pass is True
                mock_enqueue.assert_called_once()
                args, kwargs = mock_enqueue.call_args
                assert args[0].token_address == mock_token_event.token_address


@pytest.mark.asyncio
async def test_stage2_signal_recording_retries_on_failure(mock_token_event):
    """
    Stage 2 (_process_token) retries record_signal 1x if the first attempt fails.
    Sebelumnya test ini menguji retry di pipeline.py (Stage 1),
    tapi setelah Fase D, recording dipindahkan ke Stage 2 di delayed_evaluator.
    """
    evaluator = DelayedEvaluator()
    call_count = 0

    async def flaky_record(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ConnectionError("Temporary DB timeout")
        return "signal-retry-success"

    payload = {
        "token_address": mock_token_event.token_address,
        "symbol": mock_token_event.symbol,
        "event_json": mock_token_event.model_dump_json(),
        "stage1_scores": {}
    }

    mock_score_res = MagicMock()
    mock_score_res.opportunity_score = 75.0
    mock_score_res.breakdown = {"vol_velocity": 80}

    with patch("src.opportunity.scorer.OpportunityScorer.score_token", AsyncMock(return_value=mock_score_res)):
        with patch("src.paper_trading.signal_recorder.record_signal", side_effect=flaky_record):
            with patch("src.paper_trading.outcome_worker.outcome_worker.schedule_signal", AsyncMock()):
                with patch("src.paper_trading.price_fetcher.fetch_price", AsyncMock(return_value=None)):
                    await evaluator._process_token(mock_token_event.token_address, payload)

    # Harus dipanggil 2x: attempt 1 gagal, retry berhasil
    assert call_count == 2, (
        f"record_signal seharusnya dipanggil 2x (1 gagal + 1 retry), tapi dipanggil {call_count}x. "
        "Pastikan _process_token punya retry logic untuk signal recording."
    )

