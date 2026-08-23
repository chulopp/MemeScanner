"""
Reporter — Fase 4
Generates Markdown DoD report from backtest_runs data in Supabase.
"""

from datetime import datetime
from typing import Optional

from src.database.client import db_manager
from src.utils.logger import logger

REPORT_OUTPUT_PATH = "backtest_results/report.md"


async def generate_report(output_path: str = REPORT_OUTPUT_PATH) -> str:
    """
    Query Supabase for backtest_runs and generate a Markdown report.
    Returns the path of the written report file.
    """
    import os
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    runs = await db_manager.query(
        "backtest_runs",
        limit=200,
        select="*"
    )

    if not runs:
        logger.warning("No backtest runs found. Run `python -m src.backtest run` first.")
        return ""

    runs_sorted = sorted(runs, key=lambda r: r.get("ev_per_trade") or 0, reverse=True)
    optimal_runs = [r for r in runs_sorted if r.get("is_optimal")]
    best = optimal_runs[0] if optimal_runs else runs_sorted[0]

    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "# Fase 4 — Laporan Evaluasi Kuantitatif Backtest",
        f"",
        f"Generated: {now}",
        f"Total run backtest: **{len(runs)}**",
        f"",
        "---",
        "",
        "## Parameter Optimal (Best Run)",
        "",
    ]

    if best:
        params = best.get("params") or {}
        lines += [
            f"| Parameter | Nilai | Hipotesis Awal |",
            f"|---|---|---|",
            f"| `weight_vol_velocity` | **{params.get('weight_vol_velocity', 'N/A')}** | 0.35 |",
            f"| `weight_smart_money` | **{params.get('weight_smart_money', 'N/A')}** | 0.30 |",
            f"| `weight_global_fee` | **{params.get('weight_global_fee', 'N/A')}** | 0.15 |",
            f"| `opportunity_threshold` | **{params.get('opportunity_threshold', 'N/A')}** | 60.0 |",
            f"",
            f"### Hasil Metrik Terbaik",
            f"",
            f"| Metrik | Nilai | Target |",
            f"|---|---|---|",
            f"| Filter Precision | **{best.get('filter_precision', 0):.1%}** | ≥60% |",
            f"| Opportunity Recall | **{best.get('opportunity_recall', 0):.1%}** | — |",
            f"| EV per Trade | **{best.get('ev_per_trade', 0):+.2f}%** | Positif |",
            f"| Dataset Size | {best.get('dataset_size', 0)} | ≥200 |",
            f"| Runners | {best.get('runner_count', 0)} | — |",
            f"| Dead | {best.get('dead_count', 0)} | — |",
            f"| Neutral | {best.get('neutral_count', 0)} | — |",
            f"",
            f"**EV Positif**: {'✅ YA' if (best.get('ev_per_trade') or 0) > 0 else '❌ TIDAK — perlu review lebih lanjut'}",
        ]

    lines += [
        "",
        "---",
        "",
        "## Top 10 Run Berdasarkan EV per Trade",
        "",
        "| Rank | EV/Trade | Filter Precision | Recall | Threshold | vol_w | sm_w | fee_w |",
        "|---|---|---|---|---|---|---|---|",
    ]

    for i, run in enumerate(runs_sorted[:10], 1):
        params = run.get("params") or {}
        lines.append(
            f"| {i} | {run.get('ev_per_trade', 0):+.2f}% | "
            f"{run.get('filter_precision', 0):.1%} | "
            f"{run.get('opportunity_recall', 0):.1%} | "
            f"{params.get('opportunity_threshold', '—')} | "
            f"{params.get('weight_vol_velocity', '—')} | "
            f"{params.get('weight_smart_money', '—')} | "
            f"{params.get('weight_global_fee', '—')} |"
        )

    lines += [
        "",
        "---",
        "",
        "## Keterbatasan & Catatan",
        "",
        "> [!NOTE]",
        "> Filter RPC-dependent (deployer history, ATA resolution, 2-hop funding graph) **di-skip** dalam mode offline replay.",
        "> Safety check backtest menggunakan proxy: liquidity floor + wash trade ratio dari data DexScreener.",
        "> Hal ini menyebabkan filter precision backtest kemungkinan lebih rendah dari produksi live.",
        "",
        "> [!IMPORTANT]",
        "> Semua parameter optimal dari laporan ini perlu **diupdate ke `src/config.py` dan `.env`** sebelum dipakai di live trading.",
        "> Backtesting adalah validasi hipotesis, bukan garansi performa masa depan.",
        "",
        "---",
        f"*Laporan ini di-generate otomatis oleh MemeScanner Fase 4 Backtest Engine.*",
    ]

    report_text = "\n".join(lines)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(report_text)

    logger.info(f"📄 DoD Report saved to: {output_path}")
    return output_path
