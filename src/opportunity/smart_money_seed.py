"""
Smart Money Seed and Import Manager.
Allows importing verified Solana Smart Money wallet profiles from user lists or JSON/CSV files.
Empty by default until user provides verified addresses from GMGN/Cielo/Arkham.
"""

import asyncio
import json
import re
from datetime import datetime
from typing import Optional
from src.database.models import SmartMoneyProfileModel, WalletModel
from src.database.client import db_manager
from src.utils.logger import logger

SOLANA_PUBKEY_REGEX = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

# Seed list template — waiting for user's curated verified wallets
CURATED_SMART_MONEY_SEEDS: list[dict] = []


async def seed_smart_money_wallets(wallets_list: Optional[list[dict]] = None) -> int:
    """
    Inserts or updates verified Smart Money profiles into the database.
    Validates Base58 addresses and ensures parent wallet records exist in the 'wallets' table first.
    Returns the count of seeded wallets.
    """
    seeds = wallets_list if wallets_list is not None else CURATED_SMART_MONEY_SEEDS
    if not seeds:
        logger.info("ℹ️ No Smart Money seeds to insert (registry is clean/waiting for user input).")
        return 0

    valid_seeds = []
    for item in seeds:
        addr = item.get("wallet_address", "").strip()
        if not addr or not SOLANA_PUBKEY_REGEX.match(addr):
            logger.warning(f"Skipping invalid Base58 smart money address: '{addr}'")
            continue
        valid_seeds.append(item)

    if not valid_seeds:
        return 0

    # 1. Upsert parent wallets
    for item in valid_seeds:
        await db_manager.upsert_wallet(WalletModel(
            wallet_address=item["wallet_address"],
            first_seen=datetime.utcnow(),
            reputation_score=85.0,
            rug_count_history=0,
            total_tokens_launched=0,
            tags=["SMART_MONEY", item.get("source", "MANUAL")]
        ))

    # 2. Upsert smart money profiles
    profiles = [
        SmartMoneyProfileModel(
            wallet_address=item["wallet_address"],
            net_realized_profit_sol=float(item.get("net_realized_profit_sol", 0.0)),
            total_volume_sol=float(item.get("total_volume_sol", 0.0)),
            total_trades_recorded=int(item.get("total_trades_recorded", 0)),
            win_rate_pct=float(item.get("win_rate_pct", 0.0)),
            profit_factor=float(item.get("profit_factor", 0.0)),
            tier=item.get("tier", "SEED"),
            source=item.get("source", "MANUAL"),
            notes=item.get("notes", ""),
            is_active=True,
            first_added=datetime.utcnow(),
            last_active_at=datetime.utcnow()
        )
        for item in valid_seeds
    ]

    await db_manager.batch_upsert_smart_money_wallets(profiles)
    logger.info(f"🌱 Seeded {len(profiles)} verified Smart Money Profiles into database successfully.")
    return len(profiles)


async def import_smart_money_from_json(file_path: str) -> int:
    """Imports smart money profiles from a JSON file."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return await seed_smart_money_wallets(data)
        elif isinstance(data, dict) and "wallets" in data:
            return await seed_smart_money_wallets(data["wallets"])
        else:
            logger.error(f"Invalid JSON structure in {file_path}")
            return 0
    except Exception as e:
        logger.error(f"Failed to import smart money from JSON: {e}")
        return 0


if __name__ == "__main__":
    db_manager.connect()
    asyncio.run(seed_smart_money_wallets())
