"""
Social Meta Engine — Fase 3 & C Opportunity Scoring
Evaluates token social presence (Twitter/X, Telegram, Website),
DexScreener Paid Profile status, and community boosts with artificial boost detection.

Fase C additions:
- _is_suspicious_twitter_username: detects auto-generated Twitter handles (>14 chars + ≥3 digits)
- _is_telegram_link_dead: async HEAD request to detect dead Telegram links (404/timeout)
- All-3 bonus: +10pts if Twitter + Telegram + Website all present and clean
- Scoring: suspicious Twitter → 5pts (was 25pts), dead Telegram → 0pts (was 20pts)
"""

import asyncio
import re
from typing import Optional, Any
import httpx

from src.config import settings
from src.ingestion.schemas import RawTokenEvent
from src.utils.logger import logger


class SocialMetaResult:
    def __init__(
        self,
        score: float,
        has_twitter: bool = False,
        has_telegram: bool = False,
        has_website: bool = False,
        dexscreener_paid: bool = False,
        dexscreener_boosted: bool = False,
        boost_count: int = 0,
        suspicious_artificial_boost: bool = False,
        provider_used: str = "combined",
        is_successful: bool = True,
        raw_data: Optional[dict[str, Any]] = None,
        # Fase C
        twitter_suspicious: bool = False,  # True if username looks auto-generated
        telegram_dead: bool = False,        # True if Telegram link returned 404/timeout
        all3_bonus: bool = False,           # True if Twitter + Telegram + Website all valid
    ):
        self.score = score
        self.has_twitter = has_twitter
        self.has_telegram = has_telegram
        self.has_website = has_website
        self.dexscreener_paid = dexscreener_paid
        self.dexscreener_boosted = dexscreener_boosted
        self.boost_count = boost_count
        self.suspicious_artificial_boost = suspicious_artificial_boost
        self.provider_used = provider_used
        self.is_successful = is_successful
        self.raw_data = raw_data or {}
        # Fase C
        self.twitter_suspicious = twitter_suspicious
        self.telegram_dead = telegram_dead
        self.all3_bonus = all3_bonus


class SocialMetaEngine:
    """
    Evaluates:
    1. Social presence: Twitter/X (+25 or +5 if suspicious), Telegram (+20 or 0 if dead), Website (+15) [HYPOTHESIS_INIT]
    2. DexScreener Enhanced Token Profile Paid status (+30 pts) [HYPOTHESIS_INIT]
    3. DexScreener Boosts (+10 pts bonus, penalty if artificial) [HYPOTHESIS_INIT]
    4. Fase C: All-3 bonus (+10 pts if Twitter + Telegram + Website all present and valid)
    """

    # Fase C: Regex to detect suspicious auto-generated Twitter usernames
    # Heuristic: >14 chars AND contains ≥3 digits (likely bot-created handle)
    _SUSPICIOUS_TWITTER_RE = re.compile(r'\d')

    def __init__(self):
        self._http_client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=4.0)
        return self._http_client

    def _is_suspicious_twitter_username(self, url: str) -> bool:
        """
        Fase C (Q15): Detect auto-generated Twitter/X usernames.
        Heuristic: username part is >14 characters AND contains ≥3 digits.
        Bot-created accounts (e.g. 'CryptoMeme12345678') follow this pattern.

        Examples:
          'x.com/realDonaldTrump'   → False (short, no digits)
          'twitter.com/CryptoX123'  → False (<14 chars)
          'x.com/Mg7kP2xZ9qR4wLm8'  → True  (>14 chars, 4 digits)
          'x.com/user/status/123'   → False (username is 'user', tweet ID ignored)
        """
        if not url:
            return False
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url if "://" in url else f"https://{url}")
            parts = [p for p in parsed.path.strip("/").split("/") if p]
            if not parts:
                return False
            username = parts[0].split("?")[0].strip()
            # Ignore special Twitter paths like /i/communities, /search, etc.
            if username.lower() in ["i", "intent", "search", "hashtag", "home", "explore"]:
                return False
            if len(username) <= 14:
                return False
            digit_count = len(self._SUSPICIOUS_TWITTER_RE.findall(username))
            return digit_count >= 3
        except Exception:
            return False


    async def _is_telegram_link_dead(self, url: str) -> bool:
        """
        Fase C (Q12): Check if a Telegram link is dead via HEAD request.
        Returns True if the link is 404, cannot connect, or times out.
        Budget: 4s timeout, silent fail (returns False) on unexpected errors.
        """
        if not url or "t.me" not in url:
            return False
        try:
            client = await self._get_client()
            resp = await client.head(url, follow_redirects=True, timeout=4.0)
            # Telegram returns 200 for valid groups, 404 for deleted/invalid
            return resp.status_code == 404
        except httpx.TimeoutException:
            logger.debug(f"Telegram HEAD check timed out for {url[:50]} — treating as dead")
            return True
        except Exception:
            return False  # Silent fail: unknown errors don't penalise the token

    async def evaluate_social_meta(
        self,
        event: RawTokenEvent,
        total_volume_sol: float = 0.0
    ) -> SocialMetaResult:
        """
        Evaluates social links and DexScreener meta presence for a token.
        """
        mint_address = event.token_address
        raw_payload = event.raw_payload or {}

        has_twitter = False
        has_telegram = False
        has_website = False
        dexscreener_paid = False
        dexscreener_boosted = False
        boost_count = 0
        suspicious_artificial_boost = False
        provider = "payload"
        twitter_url_raw = ""
        telegram_url_raw = ""

        # 1. Check T=0 Ingestion Event Payload (PumpPortal provides these directly)
        if raw_payload.get("twitter") or "twitter.com" in str(raw_payload) or "x.com" in str(raw_payload):
            has_twitter = True
            twitter_url_raw = raw_payload.get("twitter", "") or ""
        if raw_payload.get("telegram") or "t.me" in str(raw_payload):
            has_telegram = True
            telegram_url_raw = raw_payload.get("telegram", "") or ""
        if raw_payload.get("website") or raw_payload.get("uri"):
            has_website = True

        # 2. Query DexScreener API for Enhanced Profile & Boosts
        try:
            client = await self._get_client()
            resp = await client.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint_address}")
            if resp.status_code == 200:
                data = resp.json()
                pairs = data.get("pairs") or []
                if pairs:
                    provider = "combined"
                    best_pair = pairs[0]
                    info = best_pair.get("info") or {}
                    socials = info.get("socials") or []
                    websites = info.get("websites") or []

                    for s in socials:
                        stype = str(s.get("type", "")).lower()
                        surl = str(s.get("url", "")).lower()
                        if stype == "twitter" or "twitter.com" in surl or "x.com" in surl:
                            has_twitter = True
                            if not twitter_url_raw:
                                twitter_url_raw = s.get("url", "")
                        elif stype == "telegram" or "t.me" in surl:
                            has_telegram = True
                            if not telegram_url_raw:
                                telegram_url_raw = s.get("url", "")

                    if websites:
                        has_website = True

                    # If header or icon or info profile exists, dev paid/updated DexScreener
                    if info.get("header") or info.get("icon") or socials or websites:
                        dexscreener_paid = True

                    # Check boosts
                    boosts_data = best_pair.get("boosts") or {}
                    boost_count = int(boosts_data.get("active", 0) or 0)
                    if boost_count > 0:
                        dexscreener_boosted = True

                    # Detect artificial bot boost (high boost count with near zero on-chain volume)
                    if boost_count > 50 and total_volume_sol < 5.0 and event.initial_sol_liquidity < 5.0:
                        suspicious_artificial_boost = True
        except Exception as e:
            logger.debug(f"DexScreener social meta check skipped for {mint_address[:8]}: {e}")

        # --- Fase C: Social Quality Checks ---
        # Twitter: detect auto-generated username (Q15 heuristic)
        twitter_suspicious = (
            has_twitter and self._is_suspicious_twitter_username(twitter_url_raw)
        )

        # Telegram: check if link is dead (Q12 — async HEAD request)
        telegram_dead = False
        if has_telegram and telegram_url_raw:
            telegram_dead = await self._is_telegram_link_dead(telegram_url_raw)

        # 3. Calculate Score [HYPOTHESIS_INIT — Fase C adjusted]
        score = 0.0

        if has_twitter:
            if twitter_suspicious:
                score += 5.0   # Fase C: suspicious username → reduced from 25 to 5
                logger.debug(f"Twitter suspicious username for {mint_address[:8]}: {twitter_url_raw[:40]}")
            else:
                score += 25.0  # HYPOTHESIS_INIT

        if has_telegram:
            if telegram_dead:
                score += 0.0   # Fase C: dead link → no bonus (was 20)
                logger.debug(f"Telegram link dead for {mint_address[:8]}: {telegram_url_raw[:40]}")
            else:
                score += 20.0  # HYPOTHESIS_INIT

        if has_website:
            score += 15.0  # HYPOTHESIS_INIT

        if dexscreener_paid:
            score += 30.0  # HYPOTHESIS_INIT

        if dexscreener_boosted:
            score += 10.0  # HYPOTHESIS_INIT

        # Penalty for artificial boost manipulation
        if suspicious_artificial_boost:
            score = max(0.0, score - 20.0)  # HYPOTHESIS_INIT: -20 penalty

        # Fase C (Q12): All-3 bonus — having Twitter + Telegram + Website shows real effort
        # Only awarded when all three are present AND quality (not suspicious, not dead)
        twitter_valid = has_twitter and not twitter_suspicious
        telegram_valid = has_telegram and not telegram_dead
        all3_bonus = twitter_valid and telegram_valid and has_website
        if all3_bonus:
            score += 10.0
            logger.debug(f"All-3 bonus awarded for {mint_address[:8]} (+10pts)")

        # Cap score between 0.0 and 100.0
        final_score = round(max(0.0, min(100.0, score)), 2)

        return SocialMetaResult(
            score=final_score,
            has_twitter=has_twitter,
            has_telegram=has_telegram,
            has_website=has_website,
            dexscreener_paid=dexscreener_paid,
            dexscreener_boosted=dexscreener_boosted,
            boost_count=boost_count,
            suspicious_artificial_boost=suspicious_artificial_boost,
            provider_used=provider,
            is_successful=True,
            raw_data={
                "has_twitter": has_twitter,
                "has_telegram": has_telegram,
                "has_website": has_website,
                "dexscreener_paid": dexscreener_paid,
                "dexscreener_boosted": dexscreener_boosted,
                "boost_count": boost_count,
                "suspicious_artificial_boost": suspicious_artificial_boost,
                # Fase C
                "twitter_suspicious": twitter_suspicious,
                "twitter_url": twitter_url_raw,
                "telegram_dead": telegram_dead,
                "telegram_url": telegram_url_raw,
                "all3_bonus": all3_bonus,
            },
            twitter_suspicious=twitter_suspicious,
            telegram_dead=telegram_dead,
            all3_bonus=all3_bonus,
        )

    async def close(self):
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()


social_meta_engine = SocialMetaEngine()
