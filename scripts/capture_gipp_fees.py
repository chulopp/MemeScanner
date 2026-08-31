import asyncio
import json
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import httpx
from src.database.client import db_manager
from src.config import settings

GIPP_FIXTURE_PATH = 'tests/fixtures/gipp_fees.json'
HELIUS_TX_URL = 'https://api.helius.xyz/v0/addresses/{address}/transactions'


async def main():
    await db_manager.initialize()

    if not settings.helius_api_key:
        print('ERROR: HELIUS_API_KEY not set. Required for Enhanced TX API.')
        return

    rows = await db_manager.query('backtest_tokens', filters={'symbol': 'eq.GIPP'}, limit=1)
    if not rows:
        rows = await db_manager.query('backtest_tokens', filters={'label': 'eq.runner'}, limit=100)
        rows = [r for r in rows if 'GIPP' in (r.get('symbol') or '').upper()]

    if not rows:
        print('ERROR: GIPP not found in backtest_tokens.')
        return

    gipp = rows[0]
    mint = gipp['token_address']
    print('Found GIPP:', gipp.get('symbol'), mint[:12] + '...')

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(
            HELIUS_TX_URL.format(address=mint),
            params={'api-key': settings.helius_api_key, 'limit': 100}
        )
        if resp.status_code != 200:
            print('ERROR: Helius Enhanced TX API returned HTTP', resp.status_code)
            return
        txs = resp.json()
        if not isinstance(txs, list):
            print('ERROR: Unexpected response format:', type(txs))
            return

    tx_fees = []
    for tx in txs:
        tx_fees.append({
            'fee_lamports': tx.get('fee', 0),
            'timestamp': tx.get('timestamp', 0),
            'type': tx.get('type', 'UNKNOWN'),
            'signature': tx.get('signature', '')[:20]
        })

    print('Fetched', len(tx_fees), 'historical transactions')
    if not tx_fees:
        print('WARNING: No transactions found.')
        return

    BASE_FEE_LAMPORTS = 5000
    priority_fees = []
    for tf in tx_fees:
        pl = max(0, tf['fee_lamports'] - BASE_FEE_LAMPORTS)
        priority_fees.append(pl * 1000)

    zero_count = sum(1 for f in priority_fees if f == 0)
    zero_ratio = zero_count / len(priority_fees) if priority_fees else 0
    print(f'Zero-fee ratio: {zero_ratio:.1%} ({zero_count}/{len(priority_fees)})')
    if zero_ratio > 0.85:
        print('WARNING: >85% zero fees — this fixture would trigger wash trade detection!')

    fixture = {
        'token_address': mint,
        'symbol': gipp.get('symbol'),
        'label': gipp.get('label'),
        'label_return_pct': gipp.get('label_return_pct'),
        'priority_fees_micro_lamports': priority_fees,
        'raw_tx_fees': tx_fees,
        'total_samples': len(priority_fees),
        'capture_method': 'helius_enhanced_tx_api',
        'note': 'Historical fee data from Helius Enhanced TX API (R16).'
    }

    os.makedirs('tests/fixtures', exist_ok=True)
    with open(GIPP_FIXTURE_PATH, 'w') as f:
        json.dump(fixture, f, indent=2)
    print('Fixture saved to', GIPP_FIXTURE_PATH)


if __name__ == '__main__':
    asyncio.run(main())
