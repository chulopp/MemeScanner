"""
Checkpoint Reporter — Paper Trading Live
Generates structured checkpoint reports with mandatory per-source breakdown.

Called from:
  - Telegram /checkpoint_now command
  - Scheduled checkpoint evaluations (Day 7, 20, 30, 40, 60)

Report includes:
  - DATA INSUFFICIENT guard (< 10 trades → no performance conclusions)
  - PnL breakdown per signal source (PINTU_A vs PINTU_B)
  - MFE breakdown (entry quality)
  - Captured ratio (exit quality)
  - Recall rate from internal outcome data
  - Skipped signals summary
  - Mandatory polling disclaimer

Decisions frozen per Implementation Plan (do not modify until Checkpoint Day 40).
"""

from datetime import datetime, timezone
from typing import Optional

from src.database.client import db_manager
from src.paper_trading.position_tracker import FROZEN_PARAMS, POLL_DISCLAIMER
from src.utils.logger import logger

# Minimum trades required before drawing performance conclusions
MIN_INTERPRETABLE_TRADES = 10
MIN_CONCLUSIVE_TRADES = 30


async def generate_checkpoint_report(trigger: str = "MANUAL") -> str:
    """
    Generate a full checkpoint report.

    Args:
        trigger: Who triggered this (e.g., 'MANUAL', 'DAY_7', 'DAY_20')

    Returns:
        Formatted string report ready to send via Telegram or print to console.
    """
    now_utc = datetime.now(tz=timezone.utc)
    lines: list[str] = []

    lines.append(f"📊 <b>Checkpoint Report</b> [{trigger}]")
    lines.append(f"🕐 Generated: {now_utc.strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("─" * 40)

    # ── Fetch all positions from DB ──
    try:
        all_positions = await db_manager.query("paper_trade_positions", limit=5000)
    except Exception as e:
        logger.error(f"[CheckpointReporter] DB query failed: {e}")
        return "❌ Checkpoint report failed — DB query error."

    if not all_positions:
        return (
            "📊 <b>Checkpoint Report</b>\n\n"
            "ℹ️ Belum ada data posisi sama sekali.\n"
            "Pastikan bot sudah berjalan dan sinyal sudah masuk."
        )

    # ── Split: closed trades vs open positions vs skipped ──
    closed_trades = [
        p for p in all_positions
        if p.get("exit_reason") and p.get("exit_reason") not in ("OPEN", None)
        and not p.get("skipped_reason")
    ]
    open_positions = [p for p in all_positions if p.get("exit_reason") == "OPEN"]
    skipped = [p for p in all_positions if p.get("skipped_reason")]

    total_closed = len(closed_trades)
    total_open = len(open_positions)
    total_skipped = len(skipped)

    lines.append(f"🗂️ Posisi Tertutup: <b>{total_closed}</b>")
    lines.append(f"🔄 Posisi Aktif: <b>{total_open}</b>")
    lines.append(f"⛔ Dilewati (kapasitas/duplikat): <b>{total_skipped}</b>")
    lines.append("")

    # ── DATA INSUFFICIENT guard ──
    if total_closed < MIN_INTERPRETABLE_TRADES:
        lines.append(
            f"⚠️ <b>DATA INSUFFICIENT</b>\n"
            f"Total trade tertutup: {total_closed} (minimum untuk evaluasi: {MIN_INTERPRETABLE_TRADES})\n\n"
            f"Laporan ini hanya memverifikasi infrastruktur berjalan.\n"
            f"Jangan buat kesimpulan performa sebelum ada ≥{MIN_INTERPRETABLE_TRADES} trade."
        )
        lines.append("")
        lines.append(_infrastructure_status_block(open_positions, skipped))
        lines.append("")
        lines.append(POLL_DISCLAIMER)
        return "\n".join(lines)

    # ── PnL breakdown: WAJIB per source ──
    lines.append("=" * 40)
    lines.append("<b>📈 PnL Breakdown per Sumber Sinyal</b>")
    lines.append("=" * 40)

    for source in ("PINTU_A", "PINTU_B"):
        source_trades = [t for t in closed_trades if t.get("signal_source") == source]
        lines.append(f"\n<b>{source}</b> ({len(source_trades)} trades)")

        if len(source_trades) < 3:
            lines.append("  ⚠️ Terlalu sedikit data untuk kesimpulan per sumber ini")
            continue

        returns = [t.get("realized_return_pct", 0.0) or 0.0 for t in source_trades]
        wins = [r for r in returns if r > 0]
        losses = [r for r in returns if r <= 0]

        win_rate = len(wins) / len(returns) * 100 if returns else 0.0
        avg_ev = sum(returns) / len(returns) if returns else 0.0
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0

        by_exit = {}
        for t in source_trades:
            reason = t.get("exit_reason", "UNKNOWN")
            by_exit[reason] = by_exit.get(reason, 0) + 1

        lines.append(f"  Win Rate: {win_rate:.1f}%")
        lines.append(f"  EV/trade: {avg_ev:+.2f}%")
        lines.append(f"  Avg Win: {avg_win:+.1f}% | Avg Loss: {avg_loss:+.1f}%")
        lines.append(f"  Exit reasons: " + " | ".join(f"{k}:{v}" for k, v in sorted(by_exit.items())))

        if len(source_trades) < MIN_CONCLUSIVE_TRADES:
            lines.append(f"  ⚠️ Belum conclusive ({len(source_trades)}/{MIN_CONCLUSIVE_TRADES} min)")

    # ── Entry quality (MFE) ──
    lines.append("\n" + "=" * 40)
    lines.append("<b>🎯 Entry Quality (MFE — Maximum Favorable Excursion)</b>")
    lines.append("=" * 40)

    mfe_values = [t.get("mfe_pct", 0.0) or 0.0 for t in closed_trades if t.get("mfe_pct") is not None]
    if mfe_values:
        avg_mfe = sum(mfe_values) / len(mfe_values)
        poor_entries = [m for m in mfe_values if m < 30.0]
        lines.append(f"  Avg MFE: {avg_mfe:+.1f}%")
        lines.append(f"  Poor entries (MFE < +30%): {len(poor_entries)}/{len(mfe_values)} ({len(poor_entries)/len(mfe_values)*100:.0f}%)")
        lines.append("  Interpretasi: MFE < +30% = token tidak pernah naik berarti sejak entry")
    else:
        lines.append("  ⚠️ Tidak ada data MFE")

    # ── Exit quality (captured ratio) ──
    lines.append("\n" + "=" * 40)
    lines.append("<b>🚪 Exit Quality (Captured Ratio)</b>")
    lines.append("=" * 40)

    captured_values = [
        t.get("captured_ratio", None)
        for t in closed_trades
        if t.get("captured_ratio") is not None
    ]
    if captured_values:
        avg_captured = sum(captured_values) / len(captured_values)
        lines.append(f"  Avg Captured Ratio: {avg_captured*100:.1f}%")
        lines.append(
            "  Interpretasi: 100% = bot berhasil capture semua potensi profit. "
            "< 30% = exit engine meninggalkan banyak profit di meja."
        )
    else:
        lines.append("  ⚠️ Tidak ada data captured ratio")

    # ── Recall rate ──
    lines.append("\n" + "=" * 40)
    lines.append("<b>🔭 Recall Rate (dari internal outcomes)</b>")
    lines.append("=" * 40)

    recall_info = await _compute_recall(now_utc)
    lines.append(recall_info)

    # ── Skipped signals summary ──
    if total_skipped > 0:
        skipped_capacity = sum(1 for s in skipped if s.get("skipped_reason") == "SKIPPED_CAPACITY")
        skipped_dup = sum(1 for s in skipped if s.get("skipped_reason") == "DUPLICATE")
        lines.append("\n" + "=" * 40)
        lines.append("<b>⛔ Sinyal yang Dilewati</b>")
        lines.append("=" * 40)
        lines.append(f"  Kapasitas penuh: {skipped_capacity}")
        lines.append(f"  Duplikat token: {skipped_dup}")
        if skipped_capacity > 0:
            lines.append("  ⚠️ Ada sinyal valid yang missed — pertimbangkan naikkan max_positions di Day 40+ jika EV positif")

    # ── Disclaimer ──
    lines.append("")
    lines.append("─" * 40)
    lines.append(POLL_DISCLAIMER)

    return "\n".join(lines)


def _infrastructure_status_block(open_positions: list, skipped: list) -> str:
    """Simple infra verification block for Day-7 checkpoint."""
    lines = [
        "<b>✅ Status Infrastruktur</b>",
        f"  Posisi aktif terpantau: {len(open_positions)}",
        f"  Sinyal tercatat (termasuk skip): {len(skipped)}",
        "  DB terhubung: ✅" if True else "  DB terhubung: ❌",
    ]
    return "\n".join(lines)


async def _compute_recall(now_utc: datetime) -> str:
    """
    Compute recall rate using internal paper_signals + signal_outcomes.
    Definition: recall = tokens that hit ≥+100% AND passed threshold / all tokens that hit ≥+100%
    """
    try:
        # Get all paper_signals that were evaluated (threshold passed = is_baseline=False + resolved_24h=True)
        above_thresh = await db_manager.query(
            "paper_signals",
            filters={"is_baseline": "eq.false", "resolved_24h": "eq.true"},
            limit=5000
        )
        below_thresh = await db_manager.query(
            "paper_signals",
            filters={"is_baseline": "eq.true", "resolved_24h": "eq.true"},
            limit=5000
        )

        if not above_thresh and not below_thresh:
            return "  ⚠️ Tidak ada data signal_outcomes yang resolved — recall belum bisa dihitung"

        # Get outcomes for all signals
        all_outcomes = await db_manager.query(
            "signal_outcomes",
            filters={"time_window": "eq.24h"},
            limit=10000
        )
        outcomes_by_signal: dict[str, dict] = {
            o["signal_id"]: o for o in all_outcomes if o.get("signal_id")
        }

        # Classify: runner = peak_24h (ath_return_pct) >= 100%
        def is_runner(signal_id: str) -> bool:
            outcome = outcomes_by_signal.get(signal_id)
            if not outcome:
                return False
            return (outcome.get("ath_return_pct") or 0.0) >= 100.0

        runners_above = [s for s in above_thresh if is_runner(s.get("id"))]
        runners_below = [s for s in below_thresh if is_runner(s.get("id"))]

        total_runners = len(runners_above) + len(runners_below)
        caught_runners = len(runners_above)

        if total_runners == 0:
            return "  ℹ️ Belum ada runner (token +100% dalam 24h) yang terdeteksi dalam universe ini"

        recall_pct = caught_runners / total_runners * 100.0
        lines = [
            f"  Runner universe (total +100% token): {total_runners}",
            f"  Ditangkap oleh scoring ≥{FROZEN_PARAMS['opportunity_threshold']:.0f}: {caught_runners}",
            f"  <b>Recall: {recall_pct:.1f}%</b>",
        ]

        if recall_pct < 20.0:
            lines.append("  ⚠️ Recall rendah — bot melewatkan banyak runner")
        elif recall_pct > 60.0:
            lines.append("  ✅ Recall baik — bot menangkap mayoritas runner")

        return "\n".join(lines)

    except Exception as e:
        logger.warning(f"[CheckpointReporter] Recall compute error: {e}")
        return f"  ⚠️ Recall tidak dapat dihitung: {e}"


# Singleton-style entry point
async def run_checkpoint(trigger: str = "MANUAL") -> str:
    """Convenience wrapper used by Telegram command handler."""
    return await generate_checkpoint_report(trigger=trigger)
