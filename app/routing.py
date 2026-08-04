"""Fail-closed product routing for model recommendations.

RR-K output is advisory in the MVP. This module deliberately has no path that
returns an approval decision: every recognized, malformed, or failed outcome is
sent to the human review queue.
"""

import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

RR_K_AUTO_ACCEPT_ENV = "RR_K_AUTO_ACCEPT_ENABLED"
REVIEW_REQUIRED_REASON = "Model recommendation requires explicit human confirmation"


@dataclass(frozen=True)
class ReviewRoute:
    """A model outcome normalized to the MVP's mandatory review queue."""

    decision: str
    confidence: float
    review_status: str = "needs_review"
    reason: str = REVIEW_REQUIRED_REASON


def rr_k_auto_accept_enabled() -> bool:
    """Return the effective auto-accept setting.

    The environment variable is retained as an explicit operational guard, but
    this MVP cannot enable auto-accept. A truthy value is logged and ignored so
    a deployment configuration mistake fails closed rather than changing routing.
    """
    configured = os.getenv(RR_K_AUTO_ACCEPT_ENV, "false").strip().lower()
    if configured in {"1", "true", "yes", "on"}:
        logger.error("%s=true was ignored: mandatory human review is enforced", RR_K_AUTO_ACCEPT_ENV)
    return False


def route_rr_k_outcome(
    decision: Optional[str],
    confidence: Optional[float],
    *,
    provider_error: bool = False,
    valid_output: bool = True,
) -> ReviewRoute:
    """Fail closed for every RR-K outcome, including malformed/provider failures."""
    normalized = (decision or "").upper()
    if provider_error:
        normalized = "PROVIDER_ERROR"
    elif not valid_output or normalized not in {"SELECT", "REVIEW", "NOT_FOUND"}:
        normalized = "INVALID_OUTPUT"

    try:
        normalized_confidence = float(confidence) if confidence is not None else 0.0
    except (TypeError, ValueError):
        normalized = "INVALID_OUTPUT"
        normalized_confidence = 0.0
    if not 0.0 <= normalized_confidence <= 1.0:
        normalized = "INVALID_OUTPUT"
        normalized_confidence = 0.0

    # This call makes an attempted enable observable without allowing it to
    # change the review-only route.
    rr_k_auto_accept_enabled()
    return ReviewRoute(decision=normalized, confidence=normalized_confidence)
