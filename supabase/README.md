# Supabase Database Migrations & Seeds

Direktori ini berisi seluruh DDL skema database, migrasi terurut, dan data seed untuk proyek **Solana Meme Coin Safety & Signal Bot**.

---

## 📁 Struktur Berkas

```
supabase/
├── migrations/
│   ├── 20260822000001_create_core_schema.sql      # DDL 11 tabel inti, indexes, FKs, RLS
│   └── 20260823000001_create_backtest_schema.sql  # DDL modul backtest (backtest_tokens, backtest_runs)
├── seed.sql                                       # Seed data awal Smart Money wallets terverifikasi
└── README.md                                      # Dokumentasi panduan migrasi
```

---

## 🚀 Cara Menjalankan Migrasi

### Opsi A: Menggunakan Supabase CLI (Rekomendasi)

1. **Login & Link Project**:
   ```bash
   npx supabase login
   npx supabase link --project-ref maaatgetltvyrqkxvkey
   ```

2. **Push Migrasi ke Remote Database**:
   ```bash
   npx supabase db push
   ```

3. **Insert Seed Data**:
   ```bash
   npx supabase db reset   # Untuk local development (otomatis menjalankan migrations + seed.sql)
   ```

---

### Opsi B: Menggunakan Supabase Dashboard (SQL Editor)

Jika tidak menggunakan Supabase CLI, Anda dapat langsung menyalin isi berkas `.sql` ke **SQL Editor** di dashboard Supabase:

1. Buka dashboard: `https://supabase.com/dashboard/project/maaatgetltvyrqkxvkey/sql`
2. Jalankan `supabase/migrations/20260822000001_create_core_schema.sql`
3. Jalankan `supabase/migrations/20260823000001_create_backtest_schema.sql`
4. Jalankan `supabase/seed.sql` (opsional jika database baru)

---

## 📊 Daftar Tabel & Deskripsi

| Tabel | Kategori | Deskripsi |
|---|---|---|
| `wallets` | Core | Master wallet deployer dan target tracking |
| `tokens` | Core | Metadata token baru (Pump.fun & Raydium) |
| `filter_results` | Core | Hasil audit keamanan keras & heuristik instant scalp |
| `wallet_relationships` | Core | Graf pendanaan 2-hop (deteksi koordinasi bundle) |
| `metric_snapshots` | Core | Snapshot volume, urgency fees, holder count, & opportunity score |
| `smart_money_profiles` | Core | Profiling performa trader profitabilitas tinggi |
| `smart_money_trades` | Core | Riwayat eksekusi trade wallet smart money |
| `signals` | Core | Log sinyal opportunity yang dievaluasi bot |
| `outcomes` | Core | Evaluasi performa harga multi-timeframe (5m s/d 24h) |
| `users` | Core | Subscriber & preferensi notifikasi Telegram |
| `notifications` | Core | Riwayat pengiriman pesan Stage 1 & Stage 2 ke Telegram |
| `backtest_tokens` | Backtest | Dataset token historis & live collection |
| `backtest_runs` | Backtest | Catatan parameter tuning, precision, recall, dan EV |
