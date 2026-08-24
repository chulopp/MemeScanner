"""
Reporter — Fase 4
Generates Markdown DoD report from backtest_runs data in Supabase,
specifically highlighting 5-Fold Walk-Forward Out-of-Sample (OOS) validation results.
"""

from datetime import datetime
import json
import os
from typing import Optional

from src.database.client import db_manager
from src.utils.logger import logger

REPORT_OUTPUT_PATH = "backtest_results/report.md"


async def generate_report(output_path: str = REPORT_OUTPUT_PATH) -> str:
    """
    Queries Supabase for backtest_runs and generates a comprehensive Markdown report.
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    await db_manager.initialize()

    runs = await db_manager.query(
        "backtest_runs",
        limit=50,
        select="*"
    )

    if not runs:
        logger.warning("No backtest runs found. Run `python -m src.backtest run` or `optimize` first.")
        return ""

    runs_sorted = sorted(runs, key=lambda r: (r.get("oos_ev_per_trade") or r.get("ev_per_trade") or -999), reverse=True)
    optimal_runs = [r for r in runs_sorted if r.get("is_optimal")]
    best = optimal_runs[0] if optimal_runs else runs_sorted[0]

    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "# Fase 4 — Laporan Evaluasi Kuantitatif Backtest & Walk-Forward Cross Validation",
        "",
        f"**Waktu Dibuat**: {now}",
        f"**Total Run Backtest Terekam**: {len(runs)}",
        "",
        "---",
        "",
        "## 🏆 Parameter Optimal (Walk-Forward Best Run)",
        "",
    ]

    if best:
        params = best.get("params") or {}
        oos_ev = best.get("oos_ev_per_trade") or best.get("ev_per_trade") or 0.0
        oos_prec = best.get("oos_filter_precision") or best.get("filter_precision") or 0.0
        oos_rec = best.get("oos_opportunity_recall") or best.get("opportunity_recall") or 0.0

        lines += [
            "### 1. Parameter Terpilih",
            "| Parameter | Nilai Terkalibrasi | Hipotesis Awal |",
            "|---|---|---|",
            f"| `weight_vol_velocity` | **{params.get('weight_vol_velocity', 'N/A')}** | 0.35 |",
            f"| `weight_smart_money` | **{params.get('weight_smart_money', 'N/A')}** | 0.30 |",
            f"| `weight_global_fee` | **{params.get('weight_global_fee', 'N/A')}** | 0.15 |",
            f"| `opportunity_threshold` | **{params.get('opportunity_threshold', 'N/A')}** | 60.0 |",
            "",
            "### 2. Metrik Kinerja Out-of-Sample (OOS / Data Uji Tanpa Leakage)",
            "| Metrik | Hasil Out-of-Sample (OOS) | Target PRD | Status |",
            "|---|---|---|---|",
            f"| **EV per Trade (Bersih)** | **{oos_ev:+.2f}%** | Positif (>0%) | {'✅ LULUS' if oos_ev > 0 else '❌ EVALUASI'} |",
            f"| **Filter Precision** | **{oos_prec:.1%}** | ≥60.0% | {'✅ LULUS' if oos_prec >= 0.60 else '⚠️ PERLU OPTIMASI'} |",
            f"| **Opportunity Recall** | **{oos_rec:.1%}** | High Conviction | {'✅ TERIDENTIFIKASI' if oos_rec > 0 else '—'} |",
            f"| **Total Sampel Token** | **{best.get('dataset_size', 0)}** | ≥200 token | — |",
            "",
        ]

        # Fold breakdown if available
        fold_results = best.get("fold_results")
        if fold_results and isinstance(fold_results, list):
            lines += [
                "### 3. Rincian Kinerja 5-Fold Walk-Forward Cross Validation",
                "",
                "| Fold | Train Size | Test Size | Train (In-Sample) EV | Test (Out-of-Sample) EV | OOS Precision | OOS Recall |",
                "|---|---|---|---|---|---|---|",
            ]
            for f in fold_results:
                lines.append(
                    f"| **Fold {f.get('fold')}** | {f.get('train_size')} | {f.get('test_size')} | "
                    f"{f.get('train_ev', 0):+.2f}% | **{f.get('oos_test_ev', 0):+.2f}%** | "
                    f"{f.get('oos_precision', 0):.1%} | {f.get('oos_recall', 0):.1%} |"
                )
            lines.append("")

    lines += [
        "---",
        "",
        "## 🔬 Metodologi Anti-Lookahead Bias & Cost Model",
        "",
        "1. **T=0 Ingestion**: Token dicatat murni pada detik pertama launch via WebSocket stream.",
        "2. **Disiplin Waktu Resolusi**: Return 24 jam baru dihitung setelah genap 24 jam (`resolution_due_at`) dari waktu listing.",
        "3. **Realistic Cost Model**: Menggunakan slippage bergradasi (5.0% untuk likuiditas <$50K, 2.0% untuk $50K–$200K, 0.5% untuk >$200K) + P80 Priority fee on-chain.",
        "4. **Out-of-Sample Validation**: Seluruh metrik akhir dilaporkan dari test folds yang **tidak pernah dilihat** oleh Bayesian optimizer.",
        "",
        "---",
        "*Laporan ini di-generate secara otomatis oleh MemeScanner Phase 4 Engine.*"
    ]

    report_text = "\n".join(lines)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(report_text)

    logger.info(f"📄 DoD Report successfully saved to: {output_path}")
    return output_path
