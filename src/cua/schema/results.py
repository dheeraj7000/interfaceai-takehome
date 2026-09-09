"""
Result types — the three-tier outcome contract for replay runs.

Every replay produces a structured result that clearly separates:
1. success — goal achieved, outputs extracted
2. business_outcome — legitimate non-success result (e.g. "member not found")
3. failure — hard error with debugging context

This separation is critical: "no such member" is a legitimate answer the
caller needs, not a crash. Conflating the two is the most common design
mistake in automation systems.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ResultStatus(str, Enum):
    """Top-level status of a replay run."""
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    FAILURE = "failure"
    ESCALATED = "escalated"
    TIMEOUT = "timeout"


class StepStatus(str, Enum):
    """Status of an individual step during replay."""
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    RECOVERED = "recovered"     # Failed then recovered (e.g. dismissed dialog)
    ESCALATED = "escalated"
    NOT_REACHED = "not_reached"


# ---------------------------------------------------------------------------
# Per-step trace
# ---------------------------------------------------------------------------


class StepTrace(BaseModel):
    """Execution trace for a single step — the core of observability.

    Captures what happened at each step during replay, enabling
    precise debugging when something goes wrong.
    """
    step_id: str
    step_description: str
    status: StepStatus
    started_at: datetime
    completed_at: datetime | None = None
    duration_ms: int | None = None

    # What happened
    action_performed: str = Field(
        description="Description of the action taken, e.g. 'Clicked element: Search button'"
    )
    pre_condition_met: bool | None = Field(
        default=None,
        description="Whether the pre-condition was satisfied before this step"
    )
    post_condition_met: bool | None = Field(
        default=None,
        description="Whether the post-condition was satisfied after this step"
    )

    # Error details (if any)
    error_detected: str | None = Field(
        default=None,
        description="Error pattern that matched, if any"
    )
    error_category: str | None = Field(
        default=None,
        description="business_outcome / recoverable / hard_failure"
    )
    recovery_attempted: str | None = Field(
        default=None,
        description="What recovery action was taken, if any"
    )
    recovery_succeeded: bool | None = None

    # Retry tracking
    attempt_number: int = Field(default=1)
    total_attempts: int = Field(default=1)

    # Evidence pointers
    screenshot_path: str | None = None
    dom_snapshot_path: str | None = None
    a11y_snapshot_path: str | None = None

    # Raw details for debugging
    observed_state: str | None = Field(
        default=None,
        description="What the surface actually looked like (truncated text or summary)"
    )
    expected_state: str | None = Field(
        default=None,
        description="What we expected to see"
    )


# ---------------------------------------------------------------------------
# Extracted outputs
# ---------------------------------------------------------------------------


class ExtractedOutput(BaseModel):
    """A single output value extracted from the surface."""
    name: str
    value: Any
    extracted_from: str = Field(
        description="Description of where this was extracted, e.g. 'Balance cell in row 2'"
    )
    redacted: bool = Field(
        default=False,
        description="If true, the value has been redacted for sensitivity"
    )


# ---------------------------------------------------------------------------
# Escalation context
# ---------------------------------------------------------------------------


class EscalationContext(BaseModel):
    """Context passed to a human operator when the system escalates."""
    reason: str = Field(
        description="Why the system is escalating, e.g. 'Unknown error state after 3 retries'"
    )
    capability_name: str
    current_step_id: str | None = None
    current_step_description: str | None = None
    session_url: str | None = Field(
        default=None,
        description="URL where the human can take control (e.g. noVNC URL)"
    )
    screenshot_path: str | None = None
    observed_state: str | None = None
    escalated_at: datetime = Field(default_factory=datetime.utcnow)
    human_action_log: list[str] = Field(
        default_factory=list,
        description="Actions the human took during the intervention (captured after resume)"
    )
    resolved_at: datetime | None = None
    resolution: str | None = Field(
        default=None,
        description="What the human did to resolve it"
    )


# ---------------------------------------------------------------------------
# Top-level replay result
# ---------------------------------------------------------------------------


class ReplayResult(BaseModel):
    """The complete result of a replay run.

    This is what the calling AI agent receives back. It contains:
    - status: did it succeed, return a business outcome, or fail?
    - outputs: extracted data (on success)
    - outcome: business outcome details (on business_outcome)
    - error: debugging info (on failure)
    - step_traces: full execution trace for observability
    """
    # Identity
    capability_name: str
    capability_version: str
    run_id: str = Field(description="Unique identifier for this run")

    # Top-level result
    status: ResultStatus
    message: str = Field(
        description="Human-readable summary of the result"
    )

    # Success path: extracted outputs
    outputs: list[ExtractedOutput] = Field(
        default_factory=list,
        description="Extracted output values (populated on success)"
    )

    # Business outcome path
    outcome_code: str | None = Field(
        default=None,
        description="Machine-readable outcome code, e.g. 'MEMBER_NOT_FOUND'"
    )
    outcome_message: str | None = Field(
        default=None,
        description="Human-readable outcome message"
    )

    # Failure path
    failed_step_id: str | None = None
    error_details: str | None = Field(
        default=None,
        description="What went wrong, with debugging context"
    )
    expected_state: str | None = None
    observed_state: str | None = None

    # Escalation
    escalation: EscalationContext | None = None

    # Full execution trace
    step_traces: list[StepTrace] = Field(
        default_factory=list,
        description="Ordered trace of every step executed"
    )

    # Timing
    started_at: datetime
    completed_at: datetime | None = None
    total_duration_ms: int | None = None

    # Evidence
    evidence_dir: str | None = Field(
        default=None,
        description="Path to the directory containing evidence (screenshots, logs)"
    )

    # Input params used (redacted if sensitive)
    input_params: dict[str, str] = Field(
        default_factory=dict,
        description="The input parameters used for this run (sensitive values redacted)"
    )

    def to_json(self, **kwargs: Any) -> str:
        """Serialize to pretty-printed JSON."""
        return self.model_dump_json(indent=2, **kwargs)

    @property
    def succeeded(self) -> bool:
        return self.status == ResultStatus.SUCCESS

    @property
    def is_business_outcome(self) -> bool:
        return self.status == ResultStatus.BUSINESS_OUTCOME

    @property
    def steps_executed(self) -> int:
        return len([t for t in self.step_traces if t.status != StepStatus.NOT_REACHED])

    @property
    def steps_failed(self) -> int:
        return len([t for t in self.step_traces if t.status == StepStatus.FAILED])
