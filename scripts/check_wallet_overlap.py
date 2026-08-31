import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.database.client import db_manager
from src.opportunity.smart_money_seed import CURATED_SMART_MONEY_SEEDS

async def main():
    await db_manager.initialize()
    curated_wallets = {w['wallet_address'] for w in CURATED_SMART_MONEY_SEEDS}
    
    runners = await db_manager.query(
        'backtest_tokens',
        filters={'label': 'eq.runner'},
        limit=50
    )
    print(f'Total runners found: {len(runners)}')
    
    overlaps = []
    for r in runners:
        deployer = r.get('deployer_wallet_address')
        if deployer in curated_wallets:
            overlaps.append((r.get('symbol'), 'deployer', deployer))
            
    print(f'Curated wallet overlap with runner deployers: {len(overlaps)}')
    if overlaps:
        for o in overlaps:
            print(f'  Overlap: {o}')
    else:
        print('  No circular overlap detected with curated wallets.')

if __name__ == '__main__':
    asyncio.run(main())
