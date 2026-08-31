#!/usr/bin/env python3
"""
run_paper_trading.py -- Saklar Utama MemeScanner (Fase D)
=========================================================
Entry point tunggal untuk menjalankan bot dalam mode paper trading live.

Cara pakai:
  python scripts/run_paper_trading.py            # jalankan live selamanya
  python scripts/run_paper_trading.py --dry-run  # smoke test 60 detik
  python scripts/run_paper_trading.py --duration 120  # jalankan 120 detik

Arsitektur (Fase D):
  [PumpPortal WS] --.
                    +--> [Safety Filter] --> [Antrian T+2 (Redis/in-memory)]
  [Raydium WS]   --                                    | (tunggu 2 menit)
                                                        v
                                           [Opportunity Scoring]
                                                        | (skor >= threshold)
                                                        v
                                           [Signal Recorder] --> [Supabase paper_signals]
                                           [Telegram Notifier] --> [Chat Telegram]
                                           [Outcome Worker] --> [Supabase signal_outcomes]
"""

import argparse
import asyncio
import sys
import os

# Ensure UTF-8 output on Windows console
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Tambahkan root project ke sys.path agar import src.* bekerja
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _check_env():
    """
    Periksa variabel lingkungan wajib sebelum bot jalan.
    Kalau ada yang hilang, print pesan jelas dan exit.
    """
    required = {
        "HELIUS_RPC_URL": "Endpoint RPC Solana (dari Helius.xyz)",
        "SUPABASE_URL": "URL database Supabase",
        "SUPABASE_KEY": "API key Supabase",
    }
    optional = {
        "TELEGRAM_BOT_TOKEN": "Token bot Telegram (notifikasi dinonaktifkan kalau kosong)",
        "TELEGRAM_CHAT_ID": "Chat ID Telegram tujuan notifikasi",
        "REDIS_URL": "URL Redis untuk antrian T+2 (default: in-memory kalau kosong)",
        "OPPORTUNITY_THRESHOLD": "Ambang batas skor sinyal (default: 31.0)",
    }

    missing = []
    for var, desc in required.items():
        if not os.environ.get(var):
            missing.append(f"  MISSING {var}: {desc}")

    if missing:
        print("\nKonfigurasi wajib tidak ditemukan:")
        for m in missing:
            print(m)
        print("\nBuat file .env di root project atau set environment variable tersebut.")
        sys.exit(1)

    print("\nKonfigurasi wajib ditemukan.")
    for var, desc in optional.items():
        val = os.environ.get(var)
        status = f"OK: {val[:20]}..." if val else "KOSONG (fitur dinonaktifkan)"
        print(f"   {var}: {status}")
    print()


def _print_banner(dry_run: bool, duration, threshold: float):
    """Print banner saat bot start."""
    mode = "DRY RUN (smoke test)" if dry_run else "LIVE"
    dur_str = f"{duration}s" if duration else "tak terbatas (hingga Ctrl+C)"
    print("=" * 60)
    print("  MemeScanner -- Paper Trading Mode (Fase D)")
    print("=" * 60)
    print(f"  Mode      : {mode}")
    print(f"  Durasi    : {dur_str}")
    print(f"  Threshold : Skor >= {threshold:.1f}")
    print(f"  Stage 1   : Safety Filter (instan)")
    print(f"  Stage 2   : Opportunity Scoring (T+2 menit)")
    print(f"  Output    : Supabase paper_signals + Telegram")
    print("=" * 60)
    print()


async def _run(dry_run: bool, duration):
    """
    Menjalankan seluruh pipeline paper trading.

    dry_run=True  --> jalankan 60 detik (berguna untuk cek koneksi)
    duration      --> kalau di-set, berhenti setelah N detik
    """
    from src.config import settings
    from src.main import MemeScannerApp

    threshold = getattr(settings, "opportunity_threshold", 31.0)
    _print_banner(dry_run, duration, threshold)

    if dry_run:
        effective_duration = duration or 60
        print(f"Dry run mode: bot akan berjalan {effective_duration}s lalu berhenti otomatis.\n")
        app = MemeScannerApp(smoke_test=True, duration=effective_duration)
    elif duration:
        print(f"Timed mode: bot akan berjalan {duration}s.\n")
        app = MemeScannerApp(smoke_test=True, duration=duration)
    else:
        print("Bot berjalan. Tekan Ctrl+C untuk berhenti.\n")
        app = MemeScannerApp(smoke_test=False)

    await app.run()


def main():
    parser = argparse.ArgumentParser(
        prog="python scripts/run_paper_trading.py",
        description="MemeScanner Paper Trading -- Saklar Utama (Fase D)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Smoke test mode: jalankan 60 detik dan berhenti otomatis"
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=None,
        metavar="SECONDS",
        help="Jalankan selama N detik lalu berhenti (default: jalan selamanya)"
    )
    parser.add_argument(
        "--skip-env-check",
        action="store_true",
        help="Lewati pemeriksaan variabel lingkungan (berguna untuk CI/test)"
    )

    args = parser.parse_args()

    if not args.skip_env_check:
        _check_env()

    try:
        asyncio.run(_run(dry_run=args.dry_run, duration=args.duration))
    except KeyboardInterrupt:
        print("\nBot dihentikan oleh user (Ctrl+C).")
    except Exception as e:
        print(f"\nError fatal: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()