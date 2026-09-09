"""
Stuck Detector — determines when automation should escalate to a human.

Escalation triggers:
1. No progress: the surface state hasn't meaningfully changed after N actions
2. Repeated failures: the same step fails multiple times
3. Unknown state: the surface is in a state not covered by any error matcher
4. Risky action: an irreversible action that requires human confirmation
5. Confidence drop: the agent's confidence falls below a threshold (discovery only)

The detector is stateful — it tracks recent states and actions to detect
patterns like "clicking the same button 3 times with no effect."

Design: The detector doesn't decide HOW to escalate (that's the handoff
controller's job). It only decides WHETHER to escalate, and provides the
context needed for the intervention request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class EscalationTrigger:
    """Why escalation was triggered."""
    reason: str
    trigger_type: str   # no_progress, repeated_failure, unknown_state, risky_action, confidence
    step_id: str | None = None
    details: str = ""
    timestamp: datetime = field(default_factory=datetime.utcnow)


class StuckDetector:
    """Detects when the automation is stuck and should escalate."""

    def __init__(
        self,
        no_progress_threshold: int = 3,
        repeated_failure_threshold: int = 3,
        state_similarity_threshold: float = 0.9,
    ):
        self._no_progress_threshold = no_progress_threshold
        self._repeated_failure_threshold = repeated_failure_threshold
        self._similarity_threshold = state_similarity_threshold

        # State tracking
        self._recent_states: list[str] = []
        self._failure_counts: dict[str, int] = {}  # step_id -> count
        self._actions_since_change: int = 0
        self._last_url: str = ""
        self._last_text_hash: int = 0

    def check_progress(
        self,
        current_url: str,
        visible_text: str,
        step_id: str | None = None,
    ) -> EscalationTrigger | None:
        """Check if the automation is making progress.

        Call this after each action. Returns an EscalationTrigger if
        stuck, None if progress is being made.
        """
        # Compute a simple hash of the visible content
        text_hash = hash(visible_text[:500]) if visible_text else 0
        url_changed = current_url != self._last_url
        content_changed = text_hash != self._last_text_hash

        if url_changed or content_changed:
            # Progress detected — reset counter
            self._actions_since_change = 0
            self._last_url = current_url
            self._last_text_hash = text_hash
            return None

        # No change detected
        self._actions_since_change += 1

        if self._actions_since_change >= self._no_progress_threshold:
            trigger = EscalationTrigger(
                reason=(
                    f"No progress detected after {self._actions_since_change} actions. "
                    f"URL and content unchanged."
                ),
                trigger_type="no_progress",
                step_id=step_id,
                details=f"URL: {current_url}",
            )
            logger.warning(f"Stuck detected: {trigger.reason}")
            return trigger

        return None

    def record_failure(self, step_id: str) -> EscalationTrigger | None:
        """Record a step failure. Returns trigger if threshold exceeded."""
        self._failure_counts[step_id] = self._failure_counts.get(step_id, 0) + 1
        count = self._failure_counts[step_id]

        if count >= self._repeated_failure_threshold:
            trigger = EscalationTrigger(
                reason=f"Step '{step_id}' has failed {count} times",
                trigger_type="repeated_failure",
                step_id=step_id,
                details=f"Failure count: {count}",
            )
            logger.warning(f"Repeated failure: {trigger.reason}")
            return trigger

        return None

    def check_risky_action(
        self,
        step_id: str,
        action_description: str,
    ) -> EscalationTrigger:
        """Flag a risky/irreversible action for human confirmation."""
        return EscalationTrigger(
            reason=f"Risky action requires human confirmation: {action_description}",
            trigger_type="risky_action",
            step_id=step_id,
            details=action_description,
        )

    def check_unknown_state(
        self,
        step_id: str,
        observed_state: str,
    ) -> EscalationTrigger:
        """Flag an unknown/unexpected surface state."""
        return EscalationTrigger(
            reason="Surface is in an unknown state not covered by error matchers",
            trigger_type="unknown_state",
            step_id=step_id,
            details=observed_state[:300],
        )

    def reset(self) -> None:
        """Reset all tracking state (call at start of each run)."""
        self._recent_states.clear()
        self._failure_counts.clear()
        self._actions_since_change = 0
        self._last_url = ""
        self._last_text_hash = 0
