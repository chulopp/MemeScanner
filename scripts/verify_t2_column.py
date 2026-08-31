import asyncio, os, sys
sys.path.insert(0, '.')
from src.database.client import db_manager

async def main():
    await db_manager.initialize()
    # Apply migration: add price_usd_at_t2 column
    # Supabase doesn't expose DDL via postgrest, but we can use the rpc or just verify the column
    # For now: verify by inserting a test record with the new column and checking error
    try:
        # Try a dry-run upsert with the new column to see if it exists
        test_rows = await db_manager.query('backtest_tokens', filters={'token_address': 'eq.NONEXISTENT_TEST'}, limit=1)
        print(f'Queried backtest_tokens OK (returned {len(test_rows)} rows)')
        
        # Check if any existing row has price_usd_at_t2
        all_rows = await db_manager.query('backtest_tokens', filters={'label': 'eq.runner'}, limit=3)
        for r in all_rows:
            has_col = 'price_usd_at_t2' in r
            print(f'  Token {r.get(\"symbol\")}: price_usd_at_t2 key present = {has_col}, value = {r.get(\"price_usd_at_t2\")}')
        
        print('\nVerification complete.')
    except Exception as e:
        print(f'Error: {e}')

asyncio.run(main())
