"""
Telegram Notifier — Fase 5 (Paper Trading Live)
Sends instant Telegram notification when a signal is generated.
Target latency: ≤ 5 seconds from signal generation.

Paper Trading Live additions:
  - send_position_opened(): notif saat virtual position dibuka
  - send_tp_hit(): notif saat TP1/TP2/TP3 triggered
  - send_sl_hit(): notif saat hard stop loss triggered
  - send_trailing_stop_hit(): notif saat moonbag trailing stop triggered
  - setup_command_listener(): read-only Telegram bot commands
    (/status, /pnl, /positions, /checkpoint_now)

Stage 2 (LLM synthesis / message edit) is deferred to Fase 6.
"""

import asyncio
from typing import Optional

from src.config import settings
from src.utils.logger import logger

# Guard import — python-telegram-bot is optional during testing
try:
    from telegram import Bot, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    Bot = None


import html
import re


def _format_markdown_to_html(raw_text: str) -> str:
    """Converts standard LLM markdown formatting to Telegram-compatible HTML."""
    if not raw_text:
        return ""
    # 1. Escape basic HTML entities
    escaped = html.escape(raw_text)
    # 2. Bold: **text** -> <b>text</b>
    escaped = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', escaped)
    # 3. Italic: *text* -> <i>text</i>
    escaped = re.sub(r'(?<!\w)\*([^\*\n]+?)\*(?!\w)', r'<i>\1</i>', escaped)
    # 4. Inline code: `code` -> <code>code</code>
    escaped = re.sub(r'`([^`\n]+?)`', r'<code>\1</code>', escaped)
    return escaped.strip()


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

    async def send_text(self, text: str, parse_mode: str = "HTML") -> bool:
        """Kirim pesan teks umum ke chat yang dikonfigurasi."""
        if not self._enabled:
            return False
        self._ensure_bot()
        if not self._bot:
            return False
        try:
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode=parse_mode,
                disable_web_page_preview=True
            )
            return True
        except Exception as e:
            logger.warning(f"Failed to send Telegram text message: {e}")
            return False

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
        safe_reasoning = _format_markdown_to_html(reasoning_text)


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

        status_emoji = {"runner": "🚀", "dead": "💀", "neutral": "⟶"}.get(status, "❓")

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

    # ──────────────────────────────────────────
    # Paper Trading Live: position lifecycle notifications
    # ──────────────────────────────────────────

    async def send_position_opened(
        self,
        symbol: str,
        token_address: str,
        signal_source: str,
        entry_price: float,
        opportunity_score: float,
        position_size: float,
    ) -> None:
        """Notif saat virtual position baru dibuka."""
        if not self._enabled:
            return
        self._ensure_bot()
        if not self._bot:
            return

        import html as _html
        source_emoji = "🔵" if signal_source == "PINTU_B" else "🟢"
        price_display = f"${entry_price:.8f}" if entry_price < 0.01 else f"${entry_price:.6f}"
        text = (
            f"📂 <b>Posisi Dibuka</b>: ${_html.escape(symbol)}\n"
            f"<code>{token_address}</code>\n"
            f"\n"
            f"{source_emoji} Sumber: <b>{signal_source}</b>\n"
            f"💰 Entry: <b>{price_display}</b>\n"
            f"💵 Ukuran Posisi: <b>${position_size:.2f}</b>\n"
            f"📊 Score: <b>{opportunity_score:.1f}/100</b>"
        )
        try:
            await self._bot.send_message(
                chat_id=self._chat_id, text=text, parse_mode="HTML",
                disable_web_page_preview=True
            )
        except Exception as e:
            logger.debug(f"send_position_opened failed: {e}")

    async def send_tp_hit(
        self,
        symbol: str,
        token_address: str,
        tier: str,
        return_pct: float,
        sell_fraction: float,
        remaining_fraction: float,
    ) -> None:
        """Notif saat TP1/TP2/TP3 triggered (partial sell)."""
        if not self._enabled:
            return
        self._ensure_bot()
        if not self._bot:
            return

        import html as _html
        tier_emoji = {"TP1": "✅", "TP2": "📚", "TP3": "💎"}.get(tier, "🎯")
        text = (
            f"{tier_emoji} <b>{tier} Hit</b>: ${_html.escape(symbol)}\n"
            f"<code>{token_address[:20]}...</code>\n"
            f"\n"
            f"📈 Return saat ini: <b>{return_pct:+.1f}%</b>\n"
            f"💰 Dijual: <b>{sell_fraction*100:.0f}% posisi</b>\n"
            f"🔒 Sisa di-hold: <b>{remaining_fraction*100:.0f}%</b>"
        )
        try:
            await self._bot.send_message(
                chat_id=self._chat_id, text=text, parse_mode="HTML",
                disable_web_page_preview=True
            )
        except Exception as e:
            logger.debug(f"send_tp_hit failed: {e}")

    async def send_sl_hit(
        self,
        symbol: str,
        token_address: str,
        return_pct: float,
        hold_minutes: float,
    ) -> None:
        """Notif saat hard stop loss triggered."""
        if not self._enabled:
            return
        self._ensure_bot()
        if not self._bot:
            return

        import html as _html
        text = (
            f"🛑 <b>Stop Loss</b>: ${_html.escape(symbol)}\n"
            f"<code>{token_address[:20]}...</code>\n"
            f"\n"
            f"📉 Return: <b>{return_pct:+.1f}%</b>\n"
            f"⏱ Di-hold: <b>{hold_minutes:.0f} menit</b>\n"
            f"⚠️ <i>Disclaimer: angka ini dari polling 30s, bisa 10-30% lebih buruk di pasar nyata.</i>"
        )
        try:
            await self._bot.send_message(
                chat_id=self._chat_id, text=text, parse_mode="HTML",
                disable_web_page_preview=True
            )
        except Exception as e:
            logger.debug(f"send_sl_hit failed: {e}")

    async def send_trailing_stop_hit(
        self,
        symbol: str,
        token_address: str,
        return_pct: float,
        mfe_pct: float,
    ) -> None:
        """Notif saat moonbag trailing stop triggered."""
        if not self._enabled:
            return
        self._ensure_bot()
        if not self._bot:
            return

        import html as _html
        captured_ratio = return_pct / mfe_pct * 100.0 if mfe_pct > 0.1 else 0.0
        text = (
            f"🌙 <b>Trailing Stop (Moonbag)</b>: ${_html.escape(symbol)}\n"
            f"<code>{token_address[:20]}...</code>\n"
            f"\n"
            f"🏔 MFE (puncak tertinggi): <b>{mfe_pct:+.1f}%</b>\n"
            f"📈 Return terealisasi: <b>{return_pct:+.1f}%</b>\n"
            f"🎯 Captured: <b>{captured_ratio:.0f}% dari potensi</b>"
        )
        try:
            await self._bot.send_message(
                chat_id=self._chat_id, text=text, parse_mode="HTML",
                disable_web_page_preview=True
            )
        except Exception as e:
            logger.debug(f"send_trailing_stop_hit failed: {e}")

    # ──────────────────────────────────────────
    # Read-only Telegram command listener
    # ──────────────────────────────────────────

    async def setup_command_listener(self) -> None:
        """
        Start polling-based Telegram command listener.

        Available read-only commands (FROZEN during parameter freeze window):
          /status        — Show active positions count and bot uptime
          /pnl           — PnL summary (calls CheckpointReporter quick summary)
          /positions     — List all currently open positions
          /checkpoint_now — Full checkpoint report

        NOTE: No parameter-changing commands are registered.
        /pause, /settp, /setsl, /threshold are intentionally ABSENT.
        """
        if not self._enabled or not TELEGRAM_AVAILABLE:
            logger.info("[Telegram] Command listener not started (bot disabled)")
            return

        self._ensure_bot()
        if not self._bot:
            return

        # Register official Telegram Bot Menu shortcuts
        try:
            from telegram import BotCommand
            commands = [
                BotCommand("menu", "🔘 Buka menu shortcut tombol"),
                BotCommand("status", "🤖 Status bot & kapasitas posisi"),
                BotCommand("positions", "📊 Daftar posisi aktif & floating MFE"),
                BotCommand("pnl", "💰 Ringkasan performa & win rate"),
                BotCommand("checkpoint_now", "📋 Laporan audit checkpoint"),
            ]
            await self._bot.set_my_commands(commands)
            logger.info("🤖 [Telegram] Menu commands registered with BotFather API.")
        except Exception as cmd_err:
            logger.debug(f"[Telegram] Failed to register menu commands: {cmd_err}")

        logger.info("🤖 [Telegram] Starting command listener (commands: /menu /status /pnl /positions /checkpoint_now)")
        asyncio.create_task(self._command_poll_loop())

    async def _command_poll_loop(self) -> None:
        """Long-poll Telegram for incoming messages and callback queries."""
        last_update_id: Optional[int] = None
        while True:
            try:
                updates = await self._bot.get_updates(
                    offset=last_update_id,
                    timeout=30,
                    allowed_updates=["message", "callback_query"]
                )
                for update in updates:
                    last_update_id = update.update_id + 1

                    # Handle inline button taps (callback queries)
                    if update.callback_query:
                        cb = update.callback_query
                        try:
                            await cb.answer()
                        except Exception:
                            pass
                        cb_chat = cb.message.chat_id if cb.message else int(self._chat_id)
                        if str(cb_chat) == self._chat_id:
                            action = cb.data or ""
                            if action == "cmd_status":
                                asyncio.create_task(self._cmd_status(cb_chat))
                            elif action == "cmd_positions":
                                asyncio.create_task(self._cmd_positions(cb_chat))
                            elif action == "cmd_pnl":
                                asyncio.create_task(self._cmd_pnl(cb_chat))
                            elif action == "cmd_checkpoint":
                                asyncio.create_task(self._cmd_checkpoint(cb_chat))
                            elif action == "cmd_menu":
                                asyncio.create_task(self._cmd_menu(cb_chat))
                        continue

                    message = update.message
                    if not message or not message.text:
                        continue
                    text = message.text.strip().lower()
                    # Only respond to commands from the authorized chat
                    chat_id = str(message.chat.id)
                    if chat_id != self._chat_id:
                        continue

                    if text.startswith(("/start", "/menu", "menu")):
                        asyncio.create_task(self._cmd_menu(message.chat.id))
                    elif text.startswith(("/status", "🤖 status")):
                        asyncio.create_task(self._cmd_status(message.chat.id))
                    elif text.startswith(("/pnl", "💰 pnl")):
                        asyncio.create_task(self._cmd_pnl(message.chat.id))
                    elif text.startswith(("/positions", "📊 posisi aktif")):
                        asyncio.create_task(self._cmd_positions(message.chat.id))
                    elif text.startswith(("/checkpoint_now", "📋 audit checkpoint")):
                        asyncio.create_task(self._cmd_checkpoint(message.chat.id))

            except Exception as e:
                logger.debug(f"[Telegram] Command poll error: {e}")
                await asyncio.sleep(5)
            else:
                await asyncio.sleep(1)

    async def _cmd_status(self, chat_id: int) -> None:
        try:
            from src.paper_trading.position_tracker import position_tracker, FROZEN_PARAMS
            open_count = await position_tracker.get_open_count()
            text = (
                f"🤖 <b>Bot Status</b>\n"
                f"Posisi aktif: <b>{open_count}/{FROZEN_PARAMS['max_active_positions']}</b>\n"
                f"Threshold (frozen): <b>{FROZEN_PARAMS['opportunity_threshold']:.0f}</b>\n"
                f"SL: <b>{FROZEN_PARAMS['stop_loss_pct']:.0f}%</b> | "
                f"TP1: <b>+{FROZEN_PARAMS['tp1_pct']:.0f}%</b> | "
                f"TP2: <b>+{FROZEN_PARAMS['tp2_pct']:.0f}%</b> | "
                f"TP3: <b>+{FROZEN_PARAMS['tp3_pct']:.0f}%</b>\n"
                f"Parameter version: <b>{FROZEN_PARAMS['parameter_version']}</b>"
            )
            await self._bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        except Exception as e:
            logger.debug(f"[Telegram] /status error: {e}")

    async def _cmd_pnl(self, chat_id: int) -> None:
        try:
            from src.paper_trading.checkpoint_reporter import generate_checkpoint_report
            report = await generate_checkpoint_report(trigger="/pnl command")
            # Telegram message limit is 4096 chars
            if len(report) > 4000:
                report = report[:3990] + "\n...\n<i>(truncated)</i>"
            await self._bot.send_message(chat_id=chat_id, text=report, parse_mode="HTML")
        except Exception as e:
            logger.debug(f"[Telegram] /pnl error: {e}")

    async def _cmd_positions(self, chat_id: int) -> None:
        try:
            from src.paper_trading.position_tracker import position_tracker
            from src.database.client import db_manager

            rows = await db_manager.query(
                "paper_trade_positions",
                filters={"exit_reason": "eq.OPEN"},
                limit=20
            )
            if not rows:
                await self._bot.send_message(
                    chat_id=chat_id, text="📊 Tidak ada posisi aktif saat ini.", parse_mode="HTML"
                )
                return

            lines = [f"📊 <b>Posisi Aktif ({len(rows)})</b>"]
            for r in rows:
                import html as _html
                sym = _html.escape(r.get("symbol", "?"))
                entry = r.get("entry_price_usd", 0.0) or 0.0
                high = r.get("price_high_ever_seen", 0.0) or 0.0
                mfe_est = ((high - entry) / entry * 100.0) if entry > 0 else 0.0
                src = r.get("signal_source", "?")
                lines.append(f"• ${sym} | {src} | Entry ${entry:.6f} | MFE est: {mfe_est:+.0f}%")

            await self._bot.send_message(
                chat_id=chat_id, text="\n".join(lines), parse_mode="HTML"
            )
        except Exception as e:
            logger.debug(f"[Telegram] /positions error: {e}")

    async def _cmd_checkpoint(self, chat_id: int) -> None:
        try:
            from src.paper_trading.checkpoint_reporter import generate_checkpoint_report
            await self._bot.send_message(
                chat_id=chat_id, text="⏳ Generating checkpoint report...", parse_mode="HTML"
            )
            report = await generate_checkpoint_report(trigger="/checkpoint_now")
            if len(report) > 4000:
                report = report[:3990] + "\n...\n<i>(truncated)</i>"
            await self._bot.send_message(chat_id=chat_id, text=report, parse_mode="HTML")
        except Exception as e:
            logger.debug(f"[Telegram] /checkpoint_now error: {e}")

    async def _cmd_menu(self, chat_id: int) -> None:
        """Kirim menu interaktif dengan tombol Inline dan Keyboard Shortcuts."""
        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton

            # 1. Inline Buttons (di dalam bubble chat)
            inline_kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🤖 Status Bot", callback_data="cmd_status"),
                    InlineKeyboardButton("📊 Posisi Aktif", callback_data="cmd_positions"),
                ],
                [
                    InlineKeyboardButton("💰 Ringkasan PnL", callback_data="cmd_pnl"),
                    InlineKeyboardButton("📋 Audit Checkpoint", callback_data="cmd_checkpoint"),
                ]
            ])

            # 2. Reply Keyboard (shortcut permanen di bawah input keyboard HP)
            reply_kb = ReplyKeyboardMarkup([
                [KeyboardButton("🤖 Status"), KeyboardButton("📊 Posisi Aktif")],
                [KeyboardButton("💰 PnL"), KeyboardButton("📋 Audit Checkpoint")]
            ], resize_keyboard=True)

            text = (
                "🎯 <b>MemeScanner Quick Menu</b>\n\n"
                "Pilih shortcut di bawah ini untuk melihat data live:"
            )
            await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=inline_kb
            )
            await self._bot.send_message(
                chat_id=chat_id,
                text="<i>⚡ Keyboard shortcut diaktifkan di bawah chat bar.</i>",
                parse_mode="HTML",
                reply_markup=reply_kb
            )
        except Exception as e:
            logger.debug(f"[Telegram] /menu error: {e}")


telegram_notifier = TelegramNotifier()
