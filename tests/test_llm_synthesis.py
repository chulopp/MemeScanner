"""
Tests — Fase 6: Two-Stage Telegram Delivery & LLM Synthesis
Unit tests for DeepSeek client, Synthesis Engine prompt formatting,
in-place Telegram message editing, and Stage 2 async orchestration.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from src.ingestion.schemas import RawTokenEvent
from src.filters.schemas import SafetyCheckResult
from src.llm.deepseek_client import DeepSeekClient
from src.llm.synthesis_engine import SynthesisEngine
from src.paper_trading.telegram_notifier import TelegramNotifier


VALID_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


# ============================================================
# DeepSeek Client Tests
# ============================================================

@pytest.mark.asyncio
async def test_deepseek_client_success():
    """Verify DeepSeekClient parses valid chat completion response."""
    client = DeepSeekClient()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "💡 Thesis: High buy volume\n⚠️ Risiko: Dev buy 5%\n💧 Likuiditas: 50 SOL locked"
                }
            }
        ]
    }

    with patch.object(client, "_get_client") as mock_getter:
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_resp)
        mock_getter.return_value = mock_http

        with patch("src.llm.deepseek_client.settings.deepseek_api_key", "test_key"):
            res = await client.generate_chat_completion("System prompt", "User prompt")
            assert res is not None
            assert "💡 Thesis" in res
            assert "⚠️ Risiko" in res


@pytest.mark.asyncio
async def test_deepseek_client_fallback_to_chat():
    """If primary model returns 404, should attempt fallback model."""
    client = DeepSeekClient()

    resp_404 = MagicMock(status_code=404, text="Model not found")
    resp_200 = MagicMock(
        status_code=200,
        json=lambda: {
            "choices": [{"message": {"content": "Fallback response success"}}]
        }
    )

    with patch.object(client, "_get_client") as mock_getter:
        mock_http = AsyncMock()
        # First call (v4-flash) returns 404, second call (chat) returns 200
        mock_http.post = AsyncMock(side_effect=[resp_404, resp_200])
        mock_getter.return_value = mock_http

        with patch("src.llm.deepseek_client.settings.deepseek_api_key", "test_key"):
            with patch("src.llm.deepseek_client.settings.deepseek_model", "deepseek-v4-flash"):
                res = await client.generate_chat_completion("System", "User")
                assert res == "Fallback response success"
                assert mock_http.post.call_count == 2


@pytest.mark.asyncio
async def test_deepseek_client_timeout_graceful():
    """Timeout during DeepSeek call should return None without crashing."""
    import asyncio
    client = DeepSeekClient()

    with patch.object(client, "_get_client") as mock_getter:
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=asyncio.TimeoutError("Request timed out"))
        mock_getter.return_value = mock_http

        with patch("src.llm.deepseek_client.settings.deepseek_api_key", "test_key"):
            res = await client.generate_chat_completion("System", "User")
            assert res is None


# ============================================================
# Synthesis Engine Tests
# ============================================================

@pytest.mark.asyncio
async def test_synthesis_engine_generates_3_bullets():
    """Verify SynthesisEngine prepares on-chain payload and queries DeepSeek."""
    engine = SynthesisEngine()

    event = RawTokenEvent(
        token_address=VALID_MINT,
        symbol="SYNTH",
        name="Synthetic Token",
        launch_venue="pump_fun",
        initial_sol_liquidity=30.0
    )

    safety = SafetyCheckResult(
        token_address=VALID_MINT,
        venue="pump_fun",
        filter_pass=True,
        dev_holding_pct=4.5,
        top10_holder_pct=18.0,
        sniper_bundle_pct=8.0,
        lp_locked_or_burned=True,
        mint_authority_renounced=True
    )

    score_breakdown = {
        "vol_velocity": {"score": 90, "buy_count": 15},
        "smart_money": {"score": 80, "matched_count": 2},
        "global_fee": {"score": 75},
        "holder_curve": {"score": 85},
        "social_meta": {"score": 100}
    }

    mock_reasoning = (
        "💡 Thesis Beli: Net buy pressure kuat didukung 2 smart money wallet terverifikasi.\n"
        "⚠️ Faktor Risiko: Top 10 holder memegang 18% suplai, monitor potensi aksi take-profit.\n"
        "💧 Likuiditas & Gas: Likuiditas 30 SOL awal aman, priority fee tinggi menunjukkan urgensi beli."
    )

    with patch("src.llm.synthesis_engine.deepseek_client.generate_chat_completion", AsyncMock(return_value=mock_reasoning)):
        res = await engine.generate_synthesis(
            event=event,
            safety_result=safety,
            score=86.5,
            score_breakdown=score_breakdown,
            entry_price_usd=0.000045,
            entry_liquidity_usd=5000.0
        )

        assert res is not None
        assert "💡 Thesis Beli" in res
        assert "⚠️ Faktor Risiko" in res
        assert "💧 Likuiditas & Gas" in res


# ============================================================
# Telegram In-Place Message Edit Tests
# ============================================================

@pytest.mark.asyncio
async def test_telegram_edit_message_in_place():
    """Verify edit_signal_with_synthesis calls bot.edit_message_text."""
    notifier = TelegramNotifier()

    mock_bot = AsyncMock()
    mock_bot.edit_message_text = AsyncMock(return_value=True)

    with patch.object(notifier, "_ensure_bot"):
        notifier._bot = mock_bot
        notifier._enabled = True
        notifier._chat_id = "123456789"

        success = await notifier.edit_signal_with_synthesis(
            message_id=999,
            token_address=VALID_MINT,
            symbol="SYNTH",
            name="Synthetic Token",
            opportunity_score=88.0,
            score_breakdown={"vol_velocity": {"score": 90}, "smart_money": {"score": 80}, "global_fee": {"score": 75}},
            entry_price_usd=0.00005,
            entry_liquidity_usd=6000.0,
            launch_venue="pump_fun",
            reasoning_text="💡 Thesis: Strong volume\n⚠️ Risiko: Low bundle\n💧 Fee: Urgency confirmed"
        )

        assert success is True
        mock_bot.edit_message_text.assert_called_once()
        call_kwargs = mock_bot.edit_message_text.call_args.kwargs
        assert call_kwargs["message_id"] == 999
        assert "AI Synthesis (DeepSeek)" in call_kwargs["text"]
