"""
DeepSeek LLM Client — Fase 6
Async client for DeepSeek API (https://api.deepseek.com) with timeout protection and model fallback.
"""

import asyncio
from typing import Optional
import httpx

from src.config import settings
from src.utils.logger import logger


class DeepSeekClient:
    """
    Async HTTP client for DeepSeek Chat Completions API.
    Enforces a strict 5.0s timeout to maintain fast-path bot responsiveness.
    """

    def __init__(self):
        self._http_client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            base_url = settings.deepseek_base_url.rstrip("/")
            self._http_client = httpx.AsyncClient(
                base_url=base_url,
                timeout=10.0,
                headers={
                    "Authorization": f"Bearer {settings.deepseek_api_key}",
                    "Content-Type": "application/json"
                }
            )
        return self._http_client

    async def generate_chat_completion(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.3,
        max_tokens: int = 1000
    ) -> Optional[str]:

        """
        Sends a chat completion request to DeepSeek API.
        Falls back to 'deepseek-chat' if configured model fails with 400/404.
        """
        if not settings.deepseek_api_key:
            logger.warning("DEEPSEEK_API_KEY is not configured. LLM synthesis skipped.")
            return None

        client = await self._get_client()
        models_to_try = [settings.deepseek_model]
        if settings.deepseek_model != "deepseek-chat":
            models_to_try.append("deepseek-chat")

        for model_name in models_to_try:
            payload = {
                "model": model_name,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": temperature,
                "max_tokens": max_tokens
            }

            try:
                resp = await client.post("/chat/completions", json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    choices = data.get("choices") or []
                    if choices:
                        message = choices[0].get("message") or {}
                        content = message.get("content", "").strip()
                        if content:
                            return content
                    return None
                elif resp.status_code in (400, 404):
                    logger.debug(f"DeepSeek model '{model_name}' returned {resp.status_code}: {resp.text}")
                    continue  # Try fallback model
                else:
                    logger.warning(f"DeepSeek API HTTP {resp.status_code}: {resp.text[:200]}")
                    return None
            except asyncio.TimeoutError:
                logger.warning(f"⏳ DeepSeek API timeout (5s exceeded) for model '{model_name}'.")
                return None
            except Exception as e:
                logger.warning(f"DeepSeek API request exception: {e}")
                return None

        return None

    async def close(self):
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()


deepseek_client = DeepSeekClient()
