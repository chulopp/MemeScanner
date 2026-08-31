import base64
import struct
import pytest
from unittest.mock import AsyncMock, patch


def _build_bonding_curve_bytes(
    virtual_token_reserves: int,
    virtual_sol_reserves: int,
    real_token_reserves: int,
    real_sol_reserves: int,
    token_total_supply: int,
    is_complete: bool
) -> bytes:
    """Helper: build raw bytes matching pump.fun BondingCurveAccount struct layout."""
    discriminator = 0xDEADBEEFCAFEBABE  # arbitrary 8-byte discriminator
    raw = struct.pack('<Q', discriminator)                   # offset 0: discriminator
    raw += struct.pack('<Q', virtual_token_reserves)         # offset 8
    raw += struct.pack('<Q', virtual_sol_reserves)           # offset 16
    raw += struct.pack('<Q', real_token_reserves)            # offset 24
    raw += struct.pack('<Q', real_sol_reserves)              # offset 32
    raw += struct.pack('<Q', token_total_supply)             # offset 40
    raw += bytes([1 if is_complete else 0])                  # offset 48
    return raw


@pytest.fixture
def solana_client():
    from src.utils.solana_rpc import SolanaRpcClient
    return SolanaRpcClient(rpc_url='http://fake-rpc.test')


class TestGetBondingCurvePrice:
    """Unit tests for get_bonding_curve_price() -- all mocked, no RPC calls."""

    @pytest.mark.asyncio
    async def test_parses_valid_struct_correctly(self, solana_client):
        """Standard active bonding curve -- should return correct price."""
        # Example: 800M tokens remaining, 85 SOL in reserves
        virtual_token_reserves = 800_000_000 * (10 ** 6)   # 800M tokens (6 decimals)
        virtual_sol_reserves = 85 * (10 ** 9)               # 85 SOL in lamports

        raw_bytes = _build_bonding_curve_bytes(
            virtual_token_reserves=virtual_token_reserves,
            virtual_sol_reserves=virtual_sol_reserves,
            real_token_reserves=800_000_000 * (10 ** 6),
            real_sol_reserves=85 * (10 ** 9),
            token_total_supply=1_000_000_000 * (10 ** 6),
            is_complete=False
        )
        encoded = base64.b64encode(raw_bytes).decode()

        mock_rpc_response = {
            'value': {
                'data': [encoded, 'base64'],
                'lamports': 1000000
            }
        }

        with patch.object(solana_client, '_rpc_call', new_callable=AsyncMock, return_value=mock_rpc_response):
            result = await solana_client.get_bonding_curve_price('FakeBCAddress1111111111111111111111')

        assert result is not None, 'Expected non-None result for valid BC data'
        assert result['virtual_token_reserves'] == virtual_token_reserves
        assert result['virtual_sol_reserves'] == virtual_sol_reserves
        assert result['is_complete'] is False

        expected_price_sol = (85.0) / (800_000_000.0)    # SOL per token
        assert abs(result['price_sol'] - expected_price_sol) < 1e-12, (
            f"price_sol mismatch: {result['price_sol']} != {expected_price_sol}"
        )

    @pytest.mark.asyncio
    async def test_graduated_token_is_complete(self, solana_client):
        """Graduated token (is_complete=True) should parse correctly and flag is_complete."""
        raw_bytes = _build_bonding_curve_bytes(
            virtual_token_reserves=1_000_000 * (10 ** 6),
            virtual_sol_reserves=200 * (10 ** 9),
            real_token_reserves=0,
            real_sol_reserves=0,
            token_total_supply=1_000_000_000 * (10 ** 6),
            is_complete=True  # graduated
        )
        encoded = base64.b64encode(raw_bytes).decode()
        mock_response = {'value': {'data': [encoded, 'base64'], 'lamports': 0}}

        with patch.object(solana_client, '_rpc_call', new_callable=AsyncMock, return_value=mock_response):
            result = await solana_client.get_bonding_curve_price('GraduatedBCFake11111111111111111111')

        assert result is not None
        assert result['is_complete'] is True

    @pytest.mark.asyncio
    async def test_returns_none_when_account_missing(self, solana_client):
        """RPC returns None value (account does not exist) -- should return None."""
        with patch.object(solana_client, '_rpc_call', new_callable=AsyncMock, return_value={'value': None}):
            result = await solana_client.get_bonding_curve_price('NonExistentBC11111111111111111111')

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_too_short_data(self, solana_client):
        """Account data shorter than 49 bytes (corrupt/wrong account) -- should return None."""
        raw_bytes = bytes(40)  # Only 40 bytes, less than required 49
        encoded = base64.b64encode(raw_bytes).decode()
        mock_response = {'value': {'data': [encoded, 'base64'], 'lamports': 0}}

        with patch.object(solana_client, '_rpc_call', new_callable=AsyncMock, return_value=mock_response):
            result = await solana_client.get_bonding_curve_price('ShortDataBC1111111111111111111111')

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_virtual_token_reserves_zero(self, solana_client):
        """Zero virtualTokenReserves would cause div-by-zero -- should return None."""
        raw_bytes = _build_bonding_curve_bytes(
            virtual_token_reserves=0,   # zero!
            virtual_sol_reserves=100 * (10 ** 9),
            real_token_reserves=0,
            real_sol_reserves=0,
            token_total_supply=1_000_000_000 * (10 ** 6),
            is_complete=True
        )
        encoded = base64.b64encode(raw_bytes).decode()
        mock_response = {'value': {'data': [encoded, 'base64'], 'lamports': 0}}

        with patch.object(solana_client, '_rpc_call', new_callable=AsyncMock, return_value=mock_response):
            result = await solana_client.get_bonding_curve_price('ZeroReservesBC1111111111111111111')

        assert result is None

    @pytest.mark.asyncio
    async def test_price_sol_calculation_accuracy(self, solana_client):
        """Price calculation: (vSolReserves_lamports / 1e9) / (vTokenReserves_raw / 1e6) = SOL per token."""
        # 30 SOL for 500M tokens
        virtual_sol_reserves = 30 * 10**9     # 30 SOL in lamports
        virtual_token_reserves = 500_000_000 * 10**6  # 500M tokens

        raw_bytes = _build_bonding_curve_bytes(
            virtual_token_reserves=virtual_token_reserves,
            virtual_sol_reserves=virtual_sol_reserves,
            real_token_reserves=virtual_token_reserves,
            real_sol_reserves=virtual_sol_reserves,
            token_total_supply=1_000_000_000 * 10**6,
            is_complete=False
        )
        encoded = base64.b64encode(raw_bytes).decode()
        mock_response = {'value': {'data': [encoded, 'base64'], 'lamports': 0}}

        with patch.object(solana_client, '_rpc_call', new_callable=AsyncMock, return_value=mock_response):
            result = await solana_client.get_bonding_curve_price('PriceTestBC1111111111111111111111')

        assert result is not None
        expected_price = (30.0) / (500_000_000.0)   # 6e-8 SOL per token
        assert abs(result['price_sol'] - expected_price) < 1e-14, (
            f"Expected {expected_price:.2e}, got {result['price_sol']:.2e}"
        )
