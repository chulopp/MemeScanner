import pytest
from src.ingestion.schemas import RawTokenEvent
from src.utils.logger import mask_url


def test_raw_token_event_instantiation():
    """Verify RawTokenEvent schema validations and default values."""
    event = RawTokenEvent(
        token_address="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        symbol="USDC",
        name="USD Coin",
        deployer_wallet_address="2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo",
        launch_venue="pump_fun",
        initial_buy_amount=50_000_000.0,
        total_supply=1_000_000_000.0,
        raw_payload={"test": 123}
    )

    assert event.token_address == "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    assert event.symbol == "USDC"
    assert event.launch_venue == "pump_fun"
    assert event.initial_buy_amount == 50_000_000.0
    assert event.total_supply == 1_000_000_000.0
    assert event.raw_payload["test"] == 123


def test_invalid_token_address_rejected():
    """Verify that malformed or non-base58 addresses are rejected with ValueError."""
    with pytest.raises(ValueError):
        RawTokenEvent(
            token_address="invalid_token_too_short",
            symbol="TEST",
            name="Test Token",
            launch_venue="pump_fun"
        )

    # Address with forbidden base58 characters (0, O, I, l)
    with pytest.raises(ValueError):
        RawTokenEvent(
            token_address="00000000000000000000000000000000000000000000",
            symbol="TEST",
            name="Test Token",
            launch_venue="pump_fun"
        )


def test_mask_url_redaction():
    """Verify that mask_url safely redacts api keys in URLs."""
    raw_url = "https://mainnet.helius-rpc.com/?api-key=a8dc17e0-5a5e-4c2f-aa25-e9a12660626a"
    masked = mask_url(raw_url)
    assert "a8dc17e0-5a5e-4c2f-aa25-e9a12660626a" not in masked
    assert "api-key=[REDACTED]" in masked

    raw_ws = "wss://mainnet.helius-rpc.com/?api-key=secret_key_12345&other=param"
    masked_ws = mask_url(raw_ws)
    assert "secret_key_12345" not in masked_ws
    assert "api-key=[REDACTED]" in masked_ws

