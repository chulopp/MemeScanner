"""
Phase 3: Opportunity Layer, Global Fees, and Smart Money Profiling Module.
"""

from src.opportunity.vol_velocity import volume_velocity_engine, VolumeVelocityResult
from src.opportunity.smart_money import smart_money_engine, SmartMoneyMatchResult
from src.opportunity.global_fee import global_fee_engine, GlobalFeeResult
from src.opportunity.holder_curve import holder_curve_engine, HolderCurveResult
from src.opportunity.social_meta import social_meta_engine, SocialMetaResult
from src.opportunity.scorer import opportunity_scorer, OpportunityScoreResult

__all__ = [
    "volume_velocity_engine",
    "VolumeVelocityResult",
    "smart_money_engine",
    "SmartMoneyMatchResult",
    "global_fee_engine",
    "GlobalFeeResult",
    "holder_curve_engine",
    "HolderCurveResult",
    "social_meta_engine",
    "SocialMetaResult",
    "opportunity_scorer",
    "OpportunityScoreResult"
]

