"""
LLM Synthesis Engine — Fase 6
Generates factual, structured 3-bullet point on-chain reasoning for Stage 2 Telegram alerts.
"""

import json
from typing import Optional, Any

from src.filters.schemas import SafetyCheckResult
from src.ingestion.schemas import RawTokenEvent
from src.llm.deepseek_client import deepseek_client
from src.utils.logger import logger

SYSTEM_PROMPT = """Anda adalah analis quant on-chain meme coin Solana berpengalaman. Tugas Anda adalah mensintesis data on-chain faktual menjadi ringkasan 3 poin padat, santai, tajam khas trader crypto tanpa basa-basi. Dilarang berspekulasi atau memberi nasihat keuangan fiktif di luar data yang diberikan.

Format output WAJIB persis 3 poin:
💡 Thesis Beli: [1-2 kalimat ringkas tentang momentum volume buy pressure, smart money, atau holder growth]
⚠️ Faktor Risiko: [1-2 kalimat ringkas tentang risiko bundle sniper, top10 holder concentration, dev buy %, atau anomali metadata]
💧 Likuiditas & Gas: [1 kalimat tentang likuiditas pool, status LP lock/burn, dan priority fee urgency]"""


class SynthesisEngine:
    """
    Synthesizes multi-factor metrics and safety data into crisp trader reasoning.
    """

    async def generate_synthesis(
        self,
        event: RawTokenEvent,
        safety_result: SafetyCheckResult,
        score: float,
        score_breakdown: dict[str, Any],
        entry_price_usd: float,
        entry_liquidity_usd: float
    ) -> Optional[str]:
        """
        Builds the quantitative payload and queries DeepSeek for structured reasoning.
        """
        payload = {
            "token": {
                "symbol": event.symbol,
                "name": event.name,
                "token_address": event.token_address,
                "launch_venue": event.launch_venue,
                "entry_price_usd": entry_price_usd,
                "entry_liquidity_usd": entry_liquidity_usd,
                "initial_sol_liquidity": event.initial_sol_liquidity
            },
            "opportunity_score": {
                "total_score": score,
                "vol_velocity": score_breakdown.get("vol_velocity", {}),
                "smart_money": score_breakdown.get("smart_money", {}),
                "global_fee": score_breakdown.get("global_fee", {}),
                "holder_curve": score_breakdown.get("holder_curve", {}),
                "social_meta": score_breakdown.get("social_meta", {})
            },
            "safety_checks": {
                "dev_holding_pct": safety_result.dev_holding_pct,
                "top10_holder_pct": safety_result.top10_holder_pct,
                "sniper_bundle_pct": safety_result.sniper_bundle_pct,
                "lp_locked_or_burned": safety_result.lp_locked_or_burned,
                "mint_authority_renounced": safety_result.mint_authority_renounced,
                "instant_scalp_flags_count": safety_result.instant_scalp_flags_count
            }
        }

        user_prompt = f"Data On-Chain Token:\n```json\n{json.dumps(payload, indent=2)}\n```\n\nBerikan 3 poin reasoning sesuai format."

        try:
            logger.info(f"🧠 Generating Stage 2 LLM Synthesis for ${event.symbol} via DeepSeek...")
            reasoning = await deepseek_client.generate_chat_completion(
                system_prompt=SYSTEM_PROMPT,
                user_prompt=user_prompt,
                temperature=0.3,
                max_tokens=1000
            )


            if reasoning:
                logger.info(f"✅ LLM Synthesis generated successfully for ${event.symbol}.")
                return reasoning.strip()
            else:
                logger.warning(f"⚠️ LLM Synthesis returned empty for ${event.symbol}.")
                return None
        except Exception as e:
            logger.error(f"❌ Error during LLM synthesis generation: {e}")
            return None


synthesis_engine = SynthesisEngine()
