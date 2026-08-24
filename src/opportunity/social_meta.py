"""
Social Meta Engine — Fase 3 Opportunity Scoring
Evaluates token social presence (Twitter/X, Telegram, Website),
DexScreener Paid Profile status, and community boosts with artificial boost detection.
"""

import asyncio
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
        raw_data: Optional[dict[str, Any]] = None
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


class SocialMetaEngine:
    """
    Evaluates:
    1. Social presence: Twitter/X (+25 pts), Telegram (+20 pts), Website (+15 pts) [HYPOTHESIS_INIT]
    2. DexScreener Enhanced Token Profile Paid status (+30 pts) [HYPOTHESIS_INIT]
    3. DexScreener Boosts (+10 pts bonus, penalty if artificial) [HYPOTHESIS_INIT]
    """

    def __init__(self):
        self._http_client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=4.0)
        return self._http_client

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

        # 1. Check T=0 Ingestion Event Payload (PumpPortal provides these directly)
        if raw_payload.get("twitter") or "twitter.com" in str(raw_payload) or "x.com" in str(raw_payload):
            has_twitter = True
        if raw_payload.get("telegram") or "t.me" in str(raw_payload):
            has_telegram = True
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
                        elif stype == "telegram" or "t.me" in surl:
                            has_telegram = True

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

        # 3. Calculate Score [HYPOTHESIS_INIT]
        score = 0.0

        if has_twitter:
            score += 25.0  # HYPOTHESIS_INIT
        if has_telegram:
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
                "suspicious_artificial_boost": suspicious_artificial_boost
            }
        )

    async def close(self):
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()


social_meta_engine = SocialMetaEngine()
