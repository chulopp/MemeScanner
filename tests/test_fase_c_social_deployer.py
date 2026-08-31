"""
Tests — Fase C: Social Validation + Deployer Profiling

Validates:
  1. _is_suspicious_twitter_username: heuristic (>14 chars + ≥3 digits)
  2. _is_telegram_link_dead: HEAD request mock (404 = dead, 200 = live, timeout = dead)
  3. All-3 bonus: awarded only when Twitter + Telegram + Website all clean
  4. Score deltas: suspicious Twitter reduced 25→5, dead Telegram reduced 20→0
  5. pump_safety.py: deployer profile check with win-rate gate
  6. twitter_validator.py: username extraction, quality score, API error handling
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.opportunity.social_meta import SocialMetaEngine, SocialMetaResult
from src.opportunity.twitter_validator import TwitterValidator
from src.filters.pump_safety import PumpSafetyFilter, DEPLOYER_MIN_LAUNCHES, DEPLOYER_MAX_WIN_RATE
from src.ingestion.schemas import RawTokenEvent
from src.database.client import db_manager as _db_manager


# ============================================================
# Fixtures
# ============================================================

VALID_TOKEN_ADDRESS = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"


@pytest.fixture
def engine():
    return SocialMetaEngine()


@pytest.fixture
def token_event():
    return RawTokenEvent(
        token_address=VALID_TOKEN_ADDRESS,
        symbol="TEST",
        name="Test Token",
        launch_venue="pump_fun",
        raw_payload={}
    )


# ============================================================
# 1. Twitter Username Heuristic
# ============================================================

class TestTwitterUsernameHeuristic:
    """_is_suspicious_twitter_username detects auto-generated handles correctly."""

    def test_short_username_not_suspicious(self, engine):
        """Username ≤14 chars: always clean regardless of digits."""
        assert engine._is_suspicious_twitter_username("https://x.com/Pump2024") is False

    def test_clean_long_username_no_digits(self, engine):
        """Long username with no digits: not suspicious."""
        assert engine._is_suspicious_twitter_username("https://x.com/CryptoCommunityOfficial") is False

    def test_clean_long_username_few_digits(self, engine):
        """Long username with only 2 digits at end: not suspicious (threshold is ≥3)."""
        # "MemeTokenLaunch24" = 17 chars, only 2 digits
        assert engine._is_suspicious_twitter_username("https://twitter.com/MemeTokenLaunch24") is False

    def test_suspicious_long_username_many_digits(self, engine):
        """Long username >14 chars with ≥3 digits: suspicious."""
        assert engine._is_suspicious_twitter_username("https://x.com/Mg7kP2xZ9qR4wLm8") is True

    def test_bot_pattern_username(self, engine):
        """Typical bot-generated handle pattern."""
        assert engine._is_suspicious_twitter_username("https://x.com/CryptoMeme12345678") is True

    def test_twitter_url_with_status_id_not_flagged_by_tweet_id(self, engine):
        """A valid tweet URL should inspect username, not the 19-digit tweet ID."""
        assert engine._is_suspicious_twitter_username("https://x.com/gippp69/status/2091728824157311244") is False


    def test_empty_url_not_suspicious(self, engine):
        """Empty URL: returns False (not suspicious)."""
        assert engine._is_suspicious_twitter_username("") is False

    def test_twitter_url_no_username(self, engine):
        """URL without username part."""
        assert engine._is_suspicious_twitter_username("https://twitter.com") is False

    def test_exact_boundary_14_chars(self, engine):
        """Username exactly 14 chars with 3 digits: NOT suspicious (>14 required)."""
        assert engine._is_suspicious_twitter_username("https://x.com/CryptoBot12345") is False

    def test_exactly_15_chars_3_digits_suspicious(self, engine):
        """Username exactly 15 chars with 3 digits: suspicious."""
        assert engine._is_suspicious_twitter_username("https://x.com/CryptoBot123456") is True


# ============================================================
# 2. Telegram Dead Link Check
# ============================================================

class TestTelegramDeadLinkCheck:
    """_is_telegram_link_dead correctly identifies dead Telegram links."""

    @pytest.mark.asyncio
    async def test_404_response_is_dead(self, engine):
        """HTTP 404 means Telegram link is dead."""
        mock_resp = MagicMock()
        mock_resp.status_code = 404

        engine._http_client = MagicMock()
        engine._http_client.is_closed = False
        engine._http_client.head = AsyncMock(return_value=mock_resp)
        result = await engine._is_telegram_link_dead("https://t.me/deadgroup123")
        assert result is True

    @pytest.mark.asyncio
    async def test_200_response_is_live(self, engine):
        """HTTP 200 means Telegram link is alive."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        engine._http_client = MagicMock()
        engine._http_client.is_closed = False
        engine._http_client.head = AsyncMock(return_value=mock_resp)
        result = await engine._is_telegram_link_dead("https://t.me/livegroup")
        assert result is False

    @pytest.mark.asyncio
    async def test_timeout_treated_as_dead(self, engine):
        """Timeout on HEAD request means link is treated as dead."""
        import httpx
        engine._http_client = MagicMock()
        engine._http_client.is_closed = False
        engine._http_client.head = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        result = await engine._is_telegram_link_dead("https://t.me/slowgroup")
        assert result is True

    @pytest.mark.asyncio
    async def test_non_telegram_url_skipped(self, engine):
        """Non-t.me URL silently returns False without making a request."""
        result = await engine._is_telegram_link_dead("https://discord.gg/someserver")
        assert result is False

    @pytest.mark.asyncio
    async def test_empty_url_skipped(self, engine):
        """Empty URL returns False immediately."""
        result = await engine._is_telegram_link_dead("")
        assert result is False


# ============================================================
# 3. Scoring Logic: Twitter suspicious, Telegram dead, All-3 bonus
# ============================================================

class TestSocialMetaScoringFaseC:
    """Scoring updates: suspicious Twitter = 5pts, dead Telegram = 0pts, All-3 = +10pts."""

    @pytest.mark.asyncio
    async def test_suspicious_twitter_scores_5_not_25(self, engine, token_event):
        """Suspicious Twitter username gives 5pts instead of 25pts."""
        token_event.raw_payload = {"twitter": "https://x.com/Mg7kP2xZ9qR4wLm8X9"}

        # No DexScreener, no Telegram — only suspicious Twitter
        with patch.object(engine, "_get_client") as mock_client:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"pairs": []}
            mock_client.return_value.get = AsyncMock(return_value=mock_resp)
            result = await engine.evaluate_social_meta(token_event)

        assert result.twitter_suspicious is True
        # Score should be 5 (suspicious), not 25 (clean)
        assert result.score == pytest.approx(5.0, abs=1.0)

    @pytest.mark.asyncio
    async def test_clean_twitter_scores_25(self, engine, token_event):
        """Clean Twitter username (short, no excessive digits) gives 25pts."""
        token_event.raw_payload = {"twitter": "https://x.com/MemeProject"}

        with patch.object(engine, "_get_client") as mock_client:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"pairs": []}
            mock_client.return_value.get = AsyncMock(return_value=mock_resp)
            result = await engine.evaluate_social_meta(token_event)

        assert result.twitter_suspicious is False
        assert result.score == pytest.approx(25.0, abs=1.0)

    @pytest.mark.asyncio
    async def test_dead_telegram_scores_0(self, engine, token_event):
        """Dead Telegram link gives 0pts instead of 20pts."""
        token_event.raw_payload = {"telegram": "https://t.me/deletedgroup999"}

        with patch.object(engine, "_get_client") as mock_client:
            # DexScreener: empty pairs
            mock_dx_resp = MagicMock()
            mock_dx_resp.status_code = 200
            mock_dx_resp.json.return_value = {"pairs": []}
            # Telegram HEAD: 404
            mock_tg_resp = MagicMock()
            mock_tg_resp.status_code = 404
            http_client = MagicMock()
            http_client.is_closed = False
            http_client.get = AsyncMock(return_value=mock_dx_resp)
            http_client.head = AsyncMock(return_value=mock_tg_resp)
            mock_client.return_value = http_client
            engine._http_client = http_client
            result = await engine.evaluate_social_meta(token_event)

        assert result.telegram_dead is True
        assert result.score == pytest.approx(0.0, abs=1.0)

    @pytest.mark.asyncio
    async def test_all3_bonus_awarded_when_all_clean(self, engine, token_event):
        """All-3 bonus (+10pts) awarded when Twitter + Telegram + Website all clean."""
        token_event.raw_payload = {
            "twitter": "https://x.com/CleanProject",  # short, clean
            "telegram": "https://t.me/livegroup",
            "website": "https://cleanproject.xyz"
        }

        with patch.object(engine, "_get_client") as mock_client:
            mock_dx_resp = MagicMock()
            mock_dx_resp.status_code = 200
            mock_dx_resp.json.return_value = {"pairs": []}
            mock_tg_resp = MagicMock()
            mock_tg_resp.status_code = 200  # Live
            http_client = MagicMock()
            http_client.is_closed = False
            http_client.get = AsyncMock(return_value=mock_dx_resp)
            http_client.head = AsyncMock(return_value=mock_tg_resp)
            mock_client.return_value = http_client
            engine._http_client = http_client
            result = await engine.evaluate_social_meta(token_event)

        assert result.all3_bonus is True
        # 25 (clean Twitter) + 20 (live Telegram) + 15 (website) + 10 (All-3 bonus) = 70
        assert result.score == pytest.approx(70.0, abs=1.0)

    @pytest.mark.asyncio
    async def test_all3_bonus_not_awarded_if_telegram_dead(self, engine, token_event):
        """All-3 bonus NOT awarded when Telegram is dead, even with Twitter and Website."""
        token_event.raw_payload = {
            "twitter": "https://x.com/CleanProject",
            "telegram": "https://t.me/deadgroup",
            "website": "https://cleanproject.xyz"
        }

        with patch.object(engine, "_get_client") as mock_client:
            mock_dx_resp = MagicMock()
            mock_dx_resp.status_code = 200
            mock_dx_resp.json.return_value = {"pairs": []}
            mock_tg_resp = MagicMock()
            mock_tg_resp.status_code = 404  # Dead
            http_client = MagicMock()
            http_client.is_closed = False
            http_client.get = AsyncMock(return_value=mock_dx_resp)
            http_client.head = AsyncMock(return_value=mock_tg_resp)
            mock_client.return_value = http_client
            engine._http_client = http_client
            result = await engine.evaluate_social_meta(token_event)

        assert result.all3_bonus is False
        assert result.telegram_dead is True


# ============================================================
# 4. Deployer Profiling in pump_safety.py
# ============================================================

class TestDeployerProfilingPumpSafety:
    """pump_safety.py rejects serial ruggers based on win_rate from deployer_profiles."""

    @pytest.fixture
    def safety_filter(self):
        return PumpSafetyFilter()

    @pytest.fixture
    def clean_event(self):
        return RawTokenEvent(
            token_address=VALID_TOKEN_ADDRESS,
            symbol="CLEAN",
            name="Clean Token",
            launch_venue="pump_fun",
            deployer_wallet_address="4Nd1mBQtrMKBCzyLz65aoFi19797KNnVNVNmnNqWCcd",
            initial_buy_amount=30_000_000.0,
            total_supply=1_000_000_000.0
        )

    @pytest.mark.asyncio
    async def test_serial_rugger_rejected(self, safety_filter, clean_event):
        """Deployer with ≥3 launches and <10% win rate is rejected."""
        mock_profile = [{
            "wallet_address": "4Nd1mBQtrMKBCzyLz65aoFi19797KNnVNVNmnNqWCcd",
            "total_tokens_launched": 5,
            "win_rate_pct": 0.0,  # 0% win rate → serial rugger
            "dead_count": 5,
        }]

        with patch("src.filters.instant_scalp.instant_scalp_filter.evaluate", new_callable=AsyncMock) as mock_scalp:
            mock_scalp.return_value = {"flags_count": 0, "details": {}}
            with patch.object(_db_manager, "query", AsyncMock(return_value=mock_profile)):
                result = await safety_filter.evaluate(clean_event)

        assert result.filter_pass is False
        assert "Serial rugger" in result.rejection_reason

    @pytest.mark.asyncio
    async def test_new_deployer_not_rejected(self, safety_filter, clean_event):
        """New deployer with only 2 launches is NOT rejected (below MIN_LAUNCHES=3)."""
        mock_profile = [{
            "wallet_address": "4Nd1mBQtrMKBCzyLz65aoFi19797KNnVNVNmnNqWCcd",
            "total_tokens_launched": 2,
            "win_rate_pct": 0.0,  # Even 0% win rate with <3 launches: no rejection
            "dead_count": 2,
        }]

        with patch("src.filters.instant_scalp.instant_scalp_filter.evaluate", new_callable=AsyncMock) as mock_scalp:
            mock_scalp.return_value = {"flags_count": 0, "details": {}}
            with patch.object(_db_manager, "query", AsyncMock(return_value=mock_profile)):
                result = await safety_filter.evaluate(clean_event)

        assert result.filter_pass is True

    @pytest.mark.asyncio
    async def test_deployer_with_borderline_win_rate_passes(self, safety_filter, clean_event):
        """Deployer with exactly 10% win rate is NOT rejected (threshold is <10%)."""
        mock_profile = [{
            "wallet_address": "4Nd1mBQtrMKBCzyLz65aoFi19797KNnVNVNmnNqWCcd",
            "total_tokens_launched": 10,
            "win_rate_pct": 10.0,  # Exactly at threshold: passes
            "dead_count": 9,
        }]

        with patch("src.filters.instant_scalp.instant_scalp_filter.evaluate", new_callable=AsyncMock) as mock_scalp:
            mock_scalp.return_value = {"flags_count": 0, "details": {}}
            with patch.object(_db_manager, "query", AsyncMock(return_value=mock_profile)):
                result = await safety_filter.evaluate(clean_event)

        assert result.filter_pass is True


    @pytest.mark.asyncio
    async def test_unknown_deployer_passes(self, safety_filter, clean_event):
        """Deployer not in profiles table: no profile returned → passes (no false positive)."""
        with patch("src.filters.instant_scalp.instant_scalp_filter.evaluate", new_callable=AsyncMock) as mock_scalp:
            mock_scalp.return_value = {"flags_count": 0, "details": {}}
            with patch.object(_db_manager, "query", AsyncMock(return_value=[])):
                result = await safety_filter.evaluate(clean_event)

        assert result.filter_pass is True

    @pytest.mark.asyncio
    async def test_db_error_doesnt_block_filter(self, safety_filter, clean_event):
        """If deployer_profiles query fails (DB down), token still passes — no block."""
        with patch("src.filters.instant_scalp.instant_scalp_filter.evaluate", new_callable=AsyncMock) as mock_scalp:
            mock_scalp.return_value = {"flags_count": 0, "details": {}}
            with patch.object(_db_manager, "query", AsyncMock(side_effect=Exception("Connection timeout"))):
                result = await safety_filter.evaluate(clean_event)

        assert result.filter_pass is True


# ============================================================
# 5. Twitter Validator
# ============================================================

class TestTwitterValidator:
    """TwitterValidator extracts usernames and calculates quality scores."""

    @pytest.fixture
    def validator(self):
        return TwitterValidator()

    def test_extract_username_from_xcom_url(self, validator):
        """Extracts username from x.com URL."""
        assert validator._extract_username("https://x.com/SolanaProject") == "SolanaProject"

    def test_extract_username_from_twitter_url(self, validator):
        """Extracts username from twitter.com URL."""
        assert validator._extract_username("https://twitter.com/MemeToken") == "MemeToken"

    def test_extract_username_from_handle(self, validator):
        """Extracts username from @handle."""
        assert validator._extract_username("@CryptoBot") == "CryptoBot"

    def test_extract_username_with_query_params(self, validator):
        """Strips query params from URL."""
        assert validator._extract_username("https://x.com/ProjectX?ref=123") == "ProjectX"

    def test_extract_username_empty_returns_none(self, validator):
        """Empty URL returns None."""
        assert validator._extract_username("") is None

    @pytest.mark.asyncio
    async def test_validate_no_api_key_returns_neutral(self, validator):
        """Without API key, returns neutral score (50.0) without failing."""
        validator._api_key = ""  # Force no key
        result = await validator.validate("https://x.com/SomeProject")
        assert result is not None
        assert result.skip_reason == "no_api_key"
        assert result.quality_score == pytest.approx(50.0)

    @pytest.mark.asyncio
    async def test_validate_quota_exhausted_returns_neutral(self, validator):
        """On 429 quota exhaustion, returns neutral score without error."""
        validator._api_key = "fake_key"
        mock_resp = MagicMock()
        mock_resp.status_code = 429

        validator._http_client = MagicMock()
        validator._http_client.is_closed = False
        validator._http_client.get = AsyncMock(return_value=mock_resp)

        result = await validator.validate("https://x.com/SomeProject")
        assert result.skip_reason == "quota_exhausted"
        assert result.quality_score == pytest.approx(50.0)

    @pytest.mark.asyncio
    async def test_validate_new_account_penalised(self, validator):
        """New account (<30 days) gets lower quality score."""
        validator._api_key = "fake_key"
        from datetime import datetime, timezone, timedelta

        # Account created 5 days ago
        created_at = (datetime.now(tz=timezone.utc) - timedelta(days=5)).strftime("%a %b %d %H:%M:%S +0000 %Y")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "result": {"data": {"user": {"result": {"legacy": {
                "followers_count": 50,
                "statuses_count": 5,
                "verified": False,
                "is_blue_verified": False,
                "created_at": created_at,
                "description": "",
                "profile_image_url_https": "default_profile_image_normal.png"
            }}}}}
        }

        validator._http_client = MagicMock()
        validator._http_client.is_closed = False
        validator._http_client.get = AsyncMock(return_value=mock_resp)

        result = await validator.validate("https://x.com/NewBot123456789")
        assert result.is_new_account is True
        assert result.quality_score < 50.0  # Penalised relative to neutral
