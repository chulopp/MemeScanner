# Fase 4 — Laporan Evaluasi Kuantitatif Backtest & Walk-Forward Cross Validation

**Waktu Dibuat**: 2026-08-25 03:44 UTC
**Total Run Backtest Terekam**: 2

---

## 🏆 Parameter Optimal (Walk-Forward Best Run)

### 1. Parameter Terpilih
| Parameter | Nilai Terkalibrasi | Hipotesis Awal |
|---|---|---|
| `weight_vol_velocity` | **0.3991** | 0.35 |
| `weight_smart_money` | **0.1959** | 0.30 |
| `weight_global_fee` | **0.2059** | 0.15 |
| `opportunity_threshold` | **29.5** | 60.0 |

### 2. Metrik Kinerja Out-of-Sample (OOS / Data Uji Tanpa Leakage)
| Metrik | Hasil Out-of-Sample (OOS) | Target PRD | Status |
|---|---|---|---|
| **EV per Trade (Bersih)** | **+0.00%** | Positif (>0%) | ❌ EVALUASI |
| **Filter Precision** | **16.7%** | ≥60.0% | ⚠️ PERLU OPTIMASI |
| **Opportunity Recall** | **0.0%** | High Conviction | — |
| **Total Sampel Token** | **203** | ≥200 token | — |

### 3. Rincian Kinerja 5-Fold Walk-Forward Cross Validation

| Fold | Train Size | Test Size | Train (In-Sample) EV | Test (Out-of-Sample) EV | OOS Precision | OOS Recall |
|---|---|---|---|---|---|---|
| **Fold 1** | 50 | 51 | +0.00% | **+0.00%** | 0.0% | 0.0% |
| **Fold 2** | 101 | 51 | +0.00% | **+0.00%** | 0.0% | 0.0% |
| **Fold 3** | 152 | 51 | +0.00% | **+0.00%** | 50.0% | 0.0% |

---

## 🔬 Metodologi Anti-Lookahead Bias & Cost Model

1. **T=0 Ingestion**: Token dicatat murni pada detik pertama launch via WebSocket stream.
2. **Disiplin Waktu Resolusi**: Return 24 jam baru dihitung setelah genap 24 jam (`resolution_due_at`) dari waktu listing.
3. **Realistic Cost Model**: Menggunakan slippage bergradasi (5.0% untuk likuiditas <$50K, 2.0% untuk $50K–$200K, 0.5% untuk >$200K) + P80 Priority fee on-chain.
4. **Out-of-Sample Validation**: Seluruh metrik akhir dilaporkan dari test folds yang **tidak pernah dilihat** oleh Bayesian optimizer.

---
*Laporan ini di-generate secara otomatis oleh MemeScanner Phase 4 Engine.*