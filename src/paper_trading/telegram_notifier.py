"""
Telegram Notifier — Fase 5 (Stage 1 Fast-Path)
Sends instant Telegram notification when a signal is generated.
Target latency: ≤ 5 seconds from signal generation.

Stage 2 (LLM synthesis / message edit) is deferred to Fase 6.
"""

import asyncio
from typing import Optional

from src.config import settings
from src.utils.logger import logger

# Guard import — python-telegram-bot is optional during testing
try:
    from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    Bot = None


class TelegramNotifier:
    """Sends Stage 1 fast-path notifications to a Telegram chat."""

    def __init__(self):
        self._bot: Optional[object] = None
        self._chat_id: str = settings.telegram_chat_id
        self._enabled: bool = bool(settings.telegram_bot_token and settings.telegram_chat_id)

    def _ensure_bot(self):
        if not TELEGRAM_AVAILABLE:
            logger.warning("python-telegram-bot not installed. Telegram notifications disabled.")
            self._enabled = False
            return
        if self._bot is None and self._enabled:
            self._bot = Bot(token=settings.telegram_bot_token)

    async def send_signal_notification(
        self,
        token_address: str,
        symbol: str,
        name: str,
        opportunity_score: float,
        score_breakdown: dict,
        entry_price_usd: float,
        entry_liquidity_usd: float,
        launch_venue: str,
        is_baseline: bool = False
    ) -> Optional[int]:
        """
        Sends a Stage 1 fast-path signal notification to Telegram.
        Returns the Telegram message_id for future edits (Stage 2 in Fase 6).
        """
        if not self._enabled:
            return None

        self._ensure_bot()
        import html

        signal_type = "📊 <b>BASELINE</b>" if is_baseline else "🚨 <b>SIGNAL</b>"

        venue_emoji = "🟢" if launch_venue == "pump_fun" else "🔵"

        # Score breakdown
        vol_score = score_breakdown.get("vol_velocity", {}).get("score", 0)
        sm_score = score_breakdown.get("smart_money", {}).get("score", 0)
        fee_score = score_breakdown.get("global_fee", {}).get("score", 0)
        holder_score = score_breakdown.get("holder_curve", {}).get("score", 0)
        social_score = score_breakdown.get("social_meta", {}).get("score", 0)

        # Build inline keyboard with trading links
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔫 Photon", url=f"https://photon-sol.tinyastro.io/en/lp/{token_address}"),
                InlineKeyboardButton("📊 BullX", url=f"https://bullx.io/terminal?chainId=1399811149&address={token_address}"),
            ],
            [
                InlineKeyboardButton("🔍 GMGN", url=f"https://gmgn.ai/sol/token/{token_address}"),
                InlineKeyboardButton("🌐 Solscan", url=f"https://solscan.io/token/{token_address}"),
            ]
        ])

        price_display = f"${entry_price_usd:.8f}" if entry_price_usd < 0.01 else f"${entry_price_usd:.6f}"
        liq_display = f"${entry_liquidity_usd:,.0f}" if entry_liquidity_usd else "N/A"
        safe_sym = html.escape(symbol)
        safe_name = html.escape(name)

        text = (
            f"{signal_type}: <b>${safe_sym}</b> | {safe_name}\n"
            f"\n"
            f"{venue_emoji} Venue: <b>{launch_venue.replace('_', ' ').title()}</b>\n"
            f"📍 Mint: <code>{token_address}</code>\n"
            f"\n"
            f"💯 <b>Score:</b> {opportunity_score:.1f} / 100\n"
            f"🔥 Vol: {vol_score:.0f} | SM: {sm_score:.0f} | Fee: {fee_score:.0f} | Holder: {holder_score:.0f} | Social: {social_score:.0f}\n"
            f"\n"
            f"💰 Price: <b>{price_display}</b>\n"
            f"💧 Liquidity: <b>{liq_display}</b>\n"
        )

        try:
            msg = await self._bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True
            )
            logger.info(f"📨 Telegram notification sent for {symbol} (msg_id: {msg.message_id})")
            return msg.message_id
        except Exception as e:
            logger.error(f"❌ Telegram send failed for {symbol}: {e}")
            return None

    async def edit_signal_with_synthesis(
        self,
        message_id: int,
        token_address: str,
        symbol: str,
        name: str,
        opportunity_score: float,
        score_breakdown: dict,
        entry_price_usd: float,
        entry_liquidity_usd: float,
        launch_venue: str,
        reasoning_text: str,
        is_baseline: bool = False
    ) -> bool:
        """
        Stage 2: Seamlessly edits an existing Telegram alert in-place with LLM 3-bullet reasoning.
        """
        if not self._enabled or not message_id:
            return False

        self._ensure_bot()
        if not self._bot:
            return False

        import html

        signal_type = "📊 <b>BASELINE</b>" if is_baseline else "🚨 <b>SIGNAL</b>"
        venue_emoji = "🟢" if launch_venue == "pump_fun" else "🔵"

        vol_score = score_breakdown.get("vol_velocity", {}).get("score", 0)
        sm_score = score_breakdown.get("smart_money", {}).get("score", 0)
        fee_score = score_breakdown.get("global_fee", {}).get("score", 0)
        holder_score = score_breakdown.get("holder_curve", {}).get("score", 0)
        social_score = score_breakdown.get("social_meta", {}).get("score", 0)

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔫 Photon", url=f"https://photon-sol.tinyastro.io/en/lp/{token_address}"),
                InlineKeyboardButton("📊 BullX", url=f"https://bullx.io/terminal?chainId=1399811149&address={token_address}"),
            ],
            [
                InlineKeyboardButton("🔍 GMGN", url=f"https://gmgn.ai/sol/token/{token_address}"),
                InlineKeyboardButton("🌐 Solscan", url=f"https://solscan.io/token/{token_address}"),
            ]
        ])

        price_display = f"${entry_price_usd:.8f}" if entry_price_usd < 0.01 else f"${entry_price_usd:.6f}"
        liq_display = f"${entry_liquidity_usd:,.0f}" if entry_liquidity_usd else "N/A"
        safe_sym = html.escape(symbol)
        safe_name = html.escape(name)
        safe_reasoning = html.escape(reasoning_text)

        text = (
            f"{signal_type}: <b>${safe_sym}</b> | {safe_name}\n"
            f"\n"
            f"{venue_emoji} Venue: <b>{launch_venue.replace('_', ' ').title()}</b>\n"
            f"📍 Mint: <code>{token_address}</code>\n"
            f"\n"
            f"💯 <b>Score:</b> {opportunity_score:.1f} / 100\n"
            f"🔥 Vol: {vol_score:.0f} | SM: {sm_score:.0f} | Fee: {fee_score:.0f} | Holder: {holder_score:.0f} | Social: {social_score:.0f}\n"
            f"\n"
            f"💰 Price: <b>{price_display}</b>\n"
            f"💧 Liquidity: <b>{liq_display}</b>\n"
            f"\n"
            f"🧠 <b>AI Synthesis (DeepSeek)</b>:\n"
            f"{safe_reasoning}\n"
        )

        try:
            await self._bot.edit_message_text(
                chat_id=self._chat_id,
                message_id=message_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True
            )
            logger.info(f"✨ Telegram message #{message_id} successfully updated with Stage 2 synthesis for ${symbol}")
            return True
        except Exception as e:
            logger.warning(f"Failed to edit Telegram message #{message_id} for ${symbol}: {e}")
            return False



    async def send_outcome_update(
        self,
        symbol: str,
        token_address: str,
        time_window: str,
        return_pct: float,
        ath_return_pct: float,
        mae_pct: float,
        status: str
    ) -> Optional[int]:
        """Sends a compact outcome resolution update for a specific window."""
        if not self._enabled:
            return None

        self._ensure_bot()
        if not self._bot:
            return None

        status_emoji = {"runner": "🚀", "dead": "💀", "neutral": "➖"}.get(status, "❓")

        text = (
            f"📋 Outcome [{time_window}]: ${symbol}\n"
            f"`{token_address[:12]}...`\n"
            f"\n"
            f"📈 Return: {return_pct:+.1f}%\n"
            f"🏔 ATH: {ath_return_pct:+.1f}%\n"
            f"📉 Max Drawdown: {mae_pct:.1f}%\n"
            f"{status_emoji} Status: {status.upper()}"
        )

        try:
            msg = await self._bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode="Markdown",
                disable_web_page_preview=True
            )
            return msg.message_id
        except Exception as e:
            logger.debug(f"Telegram outcome update failed: {e}")
            return None


telegram_notifier = TelegramNotifier()
