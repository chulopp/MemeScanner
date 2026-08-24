"""
Phase 6: LLM Synthesis Module (DeepSeek API).
"""

from src.llm.deepseek_client import deepseek_client, DeepSeekClient
from src.llm.synthesis_engine import synthesis_engine, SynthesisEngine

__all__ = [
    "deepseek_client",
    "DeepSeekClient",
    "synthesis_engine",
    "SynthesisEngine"
]
