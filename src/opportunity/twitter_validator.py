"""
Twitter Validator — Fase C

Deep Twitter account validation via RapidAPI (Twitter/X API free tier).

Budget: 100 requests/day on free tier.
Usage: ~5-15 tokens/day that pass safety + score threshold → budget safe.

Pre-filter: Only called AFTER _is_suspicious_twitter_username passes.
If username is already suspicious, skip API call (heuristic is enough).

Signals checked:
  - Account age: new account (<30 days) → penalise
  - Follower count: <100 followers → penalise
  - Tweet count: <10 tweets → penalise (no history)
  - Account verified: blue check → small bonus
  - Profile completeness: missing bio/photo → penalise

The validator is OPTIONAL and SOFT — scores/signals are advisory only.
A failed API call (quota exhausted, timeout) silently returns None
so the scoring pipeline continues unaffected.
"""

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

from src.utils.logger import logger


@dataclass
class TwitterValidationResult:
    """Result of a deep Twitter account validation."""
    username: str
    account_age_days: Optional[int] = None
    follower_count: Optional[int] = None
    tweet_count: Optional[int] = None
    is_verified: bool = False
    has_profile_image: bool = False
    has_bio: bool = False
    # Derived quality score: 0-100
    quality_score: float = 50.0
    # Outcome flags
    is_new_account: bool = False       # Created <30 days ago
    is_low_follower: bool = False      # <100 followers
    is_low_activity: bool = False      # <10 tweets
    api_error: bool = False            # True if API call failed
    skip_reason: Optional[str] = None  # Why validation was skipped


class TwitterValidator:
    """
    Validates Twitter/X accounts via RapidAPI Twitter241 endpoint.

    API: https://rapidapi.com/omarmhaimdat/api/twitter241
    Free tier: 100 requests/day

    Config: Set RAPIDAPI_KEY env var or RAPIDAPI_TWITTER_KEY in .env
    """

    RAPIDAPI_HOST = "twitter241.p.rapidapi.com"
    RAPIDAPI_URL = "https://twitter241.p.rapidapi.com/user-by-username"
    TIMEOUT_SECONDS = 5.0
    NEW_ACCOUNT_THRESHOLD_DAYS = 30
    LOW_FOLLOWER_THRESHOLD = 100
    LOW_ACTIVITY_THRESHOLD = 10

    def __init__(self):
        self._http_client: Optional[httpx.AsyncClient] = None
        self._api_key: Optional[str] = None
        self._request_count = 0

    def _get_api_key(self) -> Optional[str]:
        """Lazy-load RapidAPI key from environment."""
        if self._api_key is None:
            self._api_key = (
                os.environ.get("RAPIDAPI_KEY") or
                os.environ.get("RAPIDAPI_TWITTER_KEY") or
                ""
            )
        return self._api_key or None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=self.TIMEOUT_SECONDS)
        return self._http_client

    def _extract_username(self, url: str) -> Optional[str]:
        """Extract Twitter username from URL (handles both x.com and twitter.com)."""
        if not url:
            return None
        try:
            # Handle: https://x.com/username, https://twitter.com/username, @username
            url = url.strip().rstrip("/")
            if url.startswith("@"):
                return url[1:]
            parts = url.split("/")
            username = parts[-1].split("?")[0].strip()
            return username if username else None
        except Exception:
            return None

    async def validate(self, twitter_url: str) -> Optional[TwitterValidationResult]:
        """
        Validate a Twitter account. Returns None if API unavailable or quota exhausted.

        Args:
            twitter_url: Twitter/X URL or @handle

        Returns:
            TwitterValidationResult with quality signals, or None if validation skipped.
        """
        api_key = self._get_api_key()
        if not api_key:
            logger.debug("TwitterValidator: No RAPIDAPI_KEY configured. Skipping deep validation.")
            return TwitterValidationResult(
                username="unknown",
                skip_reason="no_api_key",
                api_error=False,
                quality_score=50.0  # Neutral — don't penalise without API key
            )

        username = self._extract_username(twitter_url)
        if not username:
            return None

        try:
            client = await self._get_client()
            resp = await client.get(
                self.RAPIDAPI_URL,
                headers={
                    "X-RapidAPI-Host": self.RAPIDAPI_HOST,
                    "X-RapidAPI-Key": api_key
                },
                params={"username": username},
                timeout=self.TIMEOUT_SECONDS
            )
            self._request_count += 1

            if resp.status_code == 429:
                logger.warning(f"TwitterValidator: Daily quota exhausted (request #{self._request_count}). Skipping.")
                return TwitterValidationResult(
                    username=username,
                    skip_reason="quota_exhausted",
                    api_error=False,
                    quality_score=50.0  # Neutral on quota exhaustion
                )

            if resp.status_code != 200:
                logger.debug(f"TwitterValidator: HTTP {resp.status_code} for @{username}")
                return TwitterValidationResult(
                    username=username,
                    api_error=True,
                    skip_reason=f"http_{resp.status_code}",
                    quality_score=50.0
                )

            data = resp.json()
            user_data = (
                data.get("result", {})
                    .get("data", {})
                    .get("user", {})
                    .get("result", {})
                    .get("legacy", {})
            )

            if not user_data:
                return TwitterValidationResult(
                    username=username,
                    api_error=True,
                    skip_reason="no_user_data",
                    quality_score=30.0  # Account not found → lower score
                )

            # Parse account metrics
            follower_count = int(user_data.get("followers_count", 0) or 0)
            tweet_count = int(user_data.get("statuses_count", 0) or 0)
            is_verified = bool(user_data.get("verified") or user_data.get("is_blue_verified"))
            has_profile_image = "default_profile_image" not in str(user_data.get("profile_image_url_https", ""))
            has_bio = bool(user_data.get("description", "").strip())

            # Parse account creation date
            account_age_days = None
            created_at_str = user_data.get("created_at", "")
            if created_at_str:
                try:
                    # Twitter format: "Sat Jan 01 00:00:00 +0000 2023"
                    created_at = datetime.strptime(created_at_str, "%a %b %d %H:%M:%S %z %Y")
                    account_age_days = (datetime.now(tz=timezone.utc) - created_at).days
                except ValueError:
                    pass

            # Derived flags
            is_new_account = account_age_days is not None and account_age_days < self.NEW_ACCOUNT_THRESHOLD_DAYS
            is_low_follower = follower_count < self.LOW_FOLLOWER_THRESHOLD
            is_low_activity = tweet_count < self.LOW_ACTIVITY_THRESHOLD

            # Calculate quality score (0-100)
            quality = 50.0  # Base

            # Account age signal
            if is_new_account:
                quality -= 20.0
            elif account_age_days and account_age_days > 365:
                quality += 10.0  # Established account

            # Follower count signal
            if is_low_follower:
                quality -= 15.0
            elif follower_count > 1000:
                quality += 10.0

            # Activity signal
            if is_low_activity:
                quality -= 10.0

            # Profile completeness
            if has_bio:
                quality += 5.0
            if has_profile_image:
                quality += 5.0

            # Verification
            if is_verified:
                quality += 10.0

            quality = round(max(0.0, min(100.0, quality)), 1)

            logger.debug(
                f"TwitterValidator @{username}: age={account_age_days}d, "
                f"followers={follower_count}, tweets={tweet_count}, "
                f"verified={is_verified}, quality={quality:.0f}/100"
            )

            return TwitterValidationResult(
                username=username,
                account_age_days=account_age_days,
                follower_count=follower_count,
                tweet_count=tweet_count,
                is_verified=is_verified,
                has_profile_image=has_profile_image,
                has_bio=has_bio,
                quality_score=quality,
                is_new_account=is_new_account,
                is_low_follower=is_low_follower,
                is_low_activity=is_low_activity,
                api_error=False
            )

        except httpx.TimeoutException:
            logger.debug(f"TwitterValidator: Timeout for @{username}. Skipping.")
            return TwitterValidationResult(
                username=username,
                api_error=True,
                skip_reason="timeout",
                quality_score=50.0
            )
        except Exception as e:
            logger.debug(f"TwitterValidator: Unexpected error for @{username}: {e}")
            return TwitterValidationResult(
                username=username,
                api_error=True,
                skip_reason=f"error:{type(e).__name__}",
                quality_score=50.0
            )

    async def close(self):
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()

    @property
    def request_count(self) -> int:
        return self._request_count


# Singleton instance
twitter_validator = TwitterValidator()
