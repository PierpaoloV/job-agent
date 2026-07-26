"""Public deep-grading boundary.

Implementation is split across contract, persistence, service, and workflow
adapter modules so callers depend on one stable import surface.
"""

from deep_grading_contract import (
    DeepGradeResult,
    GradingContractError,
    SanitizedProfessionalProfile,
)
from deep_grading_service import DeepGradingService, TopTierPolicy
from deep_grading_store import DeepGradeStore
from portfolio_grading_adapter import PortfolioDeepGrader


__all__ = [
    "DeepGradeResult",
    "DeepGradeStore",
    "DeepGradingService",
    "GradingContractError",
    "PortfolioDeepGrader",
    "SanitizedProfessionalProfile",
    "TopTierPolicy",
]
