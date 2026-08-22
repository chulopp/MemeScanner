import asyncio
from typing import Optional
from upstash_redis.asyncio import Redis
from src.config import settings
from src.utils.logger import logger


class RedisManager:
    """Async Redis Manager with in-memory fallback for local testing & resilience."""

    def __init__(self):
        self._redis: Optional[Redis] = None
        self._local_dedup_set: set[str] = set()
        self._lock = asyncio.Lock()
        self._connected = False

    async def connect(self):
        if settings.upstash_redis_rest_url and settings.upstash_redis_rest_token:
            try:
                self._redis = Redis(
                    url=settings.upstash_redis_rest_url,
                    token=settings.upstash_redis_rest_token
                )
                # Test ping
                await self._redis.ping()
                self._connected = True
                logger.info("Connected to Upstash Redis REST API.")
            except Exception as e:
                logger.warning(f"Failed to connect to Upstash Redis: {e}. Using in-memory cache.")
                self._redis = None
                self._connected = False
        else:
            logger.info("No Upstash Redis credentials provided. Using in-memory fallback.")

    async def check_and_set_token(self, token_address: str, ttl_seconds: int = 3600) -> bool:
        """
        Atomically checks if token was seen before.
        Returns True if NEW (not seen before), False if already processed (duplicate).
        """
        key = f"seen:token:{token_address}"
        if self._connected and self._redis:
            try:
                # set with nx=True returns True if key was set, None/False if existed
                result = await self._redis.set(key, "1", ex=ttl_seconds, nx=True)
                return bool(result)
            except Exception as e:
                logger.warning(f"Redis error during check_and_set_token: {e}. Falling back to memory.")

        # In-memory fallback
        async with self._lock:
            if token_address in self._local_dedup_set:
                return False
            self._local_dedup_set.add(token_address)
            return True

    async def set_dev_rug_history(self, wallet_address: str, is_rug: bool):
        key = f"dev:rug:{wallet_address}"
        val = "1" if is_rug else "0"
        if self._connected and self._redis:
            try:
                await self._redis.set(key, val, ex=86400 * 7)
            except Exception as e:
                logger.debug(f"Redis set dev rug error: {e}")

    async def is_dev_rug(self, wallet_address: str) -> Optional[bool]:
        key = f"dev:rug:{wallet_address}"
        if self._connected and self._redis:
            try:
                val = await self._redis.get(key)
                if val is not None:
                    return val == "1"
            except Exception as e:
                logger.debug(f"Redis get dev rug error: {e}")
        return None

    async def close(self):
        if self._redis:
            await self._redis.close()


redis_manager = RedisManager()
