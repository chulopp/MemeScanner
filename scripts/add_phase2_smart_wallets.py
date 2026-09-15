#!/usr/bin/env python3
"""
add_phase2_smart_wallets.py — Insert 20 user-curated Smart Money wallets into Supabase.

Inserts into:
  1. `wallets` (parent table, tags: ['SMART_MONEY', 'USER_CURATED'])
  2. `smart_money_profiles` (active scoring registry, is_active=True, tier='ACTIVE')
  3. `smart_money_wallets` (discovery qualification table, status='QUALIFIED')

Usage:
  python scripts/add_phase2_smart_wallets.py
"""

import asyncio
import os
import sys
from datetime import datetime, timezone

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from src.database.client import db_manager
from src.database.models import WalletModel, SmartMoneyProfileModel
from src.utils.logger import logger

PHASE2_WALLETS = [
    "2M2vLX34LXMg24dMEnjWHvRXS1tshpEDRWzmXgV8ENNZ",
    "6HcrxubcevZQs1fcPTVnywzw7N2XWqsyAPxqnmg78UMg",
    "5FGoPPj1nL8LCnfVnpTmreqQtqLuMXXAwuS1uahMrp8V",
    "7YoMjEFGiEcMhYPoksbPP3fdtvpUenYYixdX9TgXz1AY",
    "DgHAu1pJKydHqRM7SteoS2K4U8pNMkxW4X2SsSTHYwwY",
    "BnYk6Dph4CXM5FDhskAqbcGurq4QYQx2fQwLxPzPYKT6",
    "CzwWvTVn39dSd4LiVc6W9gZxgu36737M2fcX4EWhquh4",
    "EoxtfjMw48FxC158esdyvbejj2t6tQw1VTxgHwcHkb72",
    "Fzz2amRoCCpEvxtwdurs8qLVLSdrd3dcraJVpNjE4rp4",
    "7dsEvQJ8wJxRJnWGqjWhL52UnnktTpwQJRrbVHik7XiT",
    "CNudZYFgpbT26fidsiNrWfHeGTBMMeVWqruZXsEkcUPc",
    "CPZfoAcQKnsWYApcFR7jh2GvtbUGjcjSARCZsypmBGhP",
    "B3LuC4vz1JuHrQAoK4Todgcu75vmD5Z9pWTFBxw5XigZ",
    "u2vbjD14qYsm36WJXkg2wJckQJrcA4v9ZwP1Y8ypGzk",
    "GUGHa8viMpJuyEAFU9BnVFy5LjwgYMLuG8qN4oqd5ufJ",
    "AcoNeFQsTPYs7ZrH8RMWaxxGJTTQJJ4H5aTXmptaz5UK",
    "2aPnFbwMj2oGCDzS9MjCWvJiQdSNaF9o3zs3CVR6ZJ49",
    "Cr1n5ZTc1W42zxHQ2LEAHUyvKPrm3ABAHKmFABMh9bKT",
    "J9jUL2vgRb8fDurmEUiPx5hP91trbpdWTQt6cq7jjMVR",
    "4UrFSCrGxgoCtCUBAEZq7ZmPK3Pczkxx7PwYnkBMi1KR",
]

async def main():
    db_manager.connect()
    print(f"🚀 Preparing to add {len(PHASE2_WALLETS)} Smart Money wallets to Supabase...")
    now = datetime.now(timezone.utc)

    # 1. Parent wallets
    print("\n[Step 1/3] Upserting parent records into 'wallets' table...")
    for addr in PHASE2_WALLETS:
        wallet = WalletModel(
            wallet_address=addr,
            first_seen=now,
            reputation_score=85.0,
            rug_count_history=0,
            total_tokens_launched=0,
            tags=["SMART_MONEY", "USER_CURATED"]
        )
        await db_manager.upsert_wallet(wallet)
    print(f"  ✅ Upserted {len(PHASE2_WALLETS)} records in 'wallets'.")

    # 2. Smart money profiles (operational table for Pintu A & B)
    print("\n[Step 2/3] Upserting into 'smart_money_profiles' table...")
    profiles = [
        SmartMoneyProfileModel(
            wallet_address=addr,
            tier="ACTIVE",
            is_active=True,
            first_added=now,
            last_active_at=now,
            total_trades_recorded=0,
            total_volume_sol=0.0,
            net_realized_profit_sol=0.0,
            win_rate_pct=0.0,
            profit_factor=1.0,
            source="USER_CURATED",
            notes="User-curated wallet added for Phase 2 paper trading"
        )
        for addr in PHASE2_WALLETS
    ]
    await db_manager.batch_upsert_smart_money_wallets(profiles)
    print(f"  ✅ Upserted {len(profiles)} records in 'smart_money_profiles'.")

    # 3. Smart money wallets (discovery table)
    print("\n[Step 3/3] Upserting into 'smart_money_wallets' table (discovery registry)...")
    if db_manager._connected and db_manager._client:
        loop = asyncio.get_running_loop()
        discovery_rows = [
            {
                "wallet_address": addr,
                "sol_balance": 0.0,
                "runner_hit_count": 1,
                "dead_hit_count": 0,
                "neutral_hit_count": 0,
                "total_early_buys": 1,
                "hit_ratio": 1.0,
                "status": "QUALIFIED",
                "rejection_reason": "",
                "notes": "User-curated Phase 2 wallet"
            }
            for addr in PHASE2_WALLETS
        ]
        try:
            await loop.run_in_executor(
                None,
                lambda: db_manager._client.table("smart_money_wallets").upsert(discovery_rows).execute()
            )
            print(f"  ✅ Upserted {len(discovery_rows)} records in 'smart_money_wallets'.")
        except Exception as e:
            print(f"  ⚠️ Warning upserting to smart_money_wallets: {e}")

    # Verify total active wallets in DB
    active_wallets = await db_manager.get_smart_money_wallets(active_only=True)
    print(f"\n🎉 Done! Total active Smart Money wallets in DB: {len(active_wallets)}")
    for i, w in enumerate(active_wallets, start=1):
        print(f"  {i:2d}. {w.get('wallet_address')} ({w.get('tier', 'N/A')} - {w.get('source', 'N/A')})")

if __name__ == "__main__":
    asyncio.run(main())
