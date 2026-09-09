"""
Capability Artifact Schema — the core data model for recorded automations.

This is the central contract of the system. A Capability artifact is:
- Created by the LLM-driven discovery agent after a successful run
- Stored as versioned JSON
- Replayed deterministically without the LLM in the loop
- Invoked by an AI agent as a typed function with inputs and outputs

Design principles:
1. Every step is a traceable node: pre_condition → action → post_condition
2. Element targeting uses a single best locator + semantic descriptor (hybrid)
3. Parameters flow visibly through steps via template variables ({{param_name}})
4. Errors are classified into business_outcome / recoverable / hard_failure
5. The artifact is both human-reviewable and machine-invocable
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SchemaVersion(str, Enum):
    """Version of the artifact schema format itself."""
    V1 = "1.0"


class SurfaceType(str, Enum):
    """The type of application surface this artifact targets."""
    WEB_BROWSER = "web_browser"
    LEGACY_WEB = "legacy_web"
    DESKTOP = "desktop"


class ActionType(str, Enum):
    """Primitive actions the automation can perform on a surface."""
    NAVIGATE = "navigate"       # Go to a URL or screen
    CLICK = "click"             # Click/tap an element
    TYPE = "type"               # Enter text into a field
    SELECT = "select"           # Choose from a dropdown/list
    CLEAR = "clear"             # Clear a field's content
    PRESS_KEY = "press_key"     # Press a keyboard key (Enter, Tab, etc.)
    HOVER = "hover"             # Hover over an element
    SCROLL = "scroll"           # Scroll the page or element
    WAIT = "wait"               # Explicit wait for a condition
    EXTRACT = "extract"         # Read data from the surface
    ASSERT = "assert"           # Verify a condition holds
    SCREENSHOT = "screenshot"   # Capture visual evidence


class LocatorStrategy(str, Enum):
    """How an element/control is identified on the surface.

    Ordered by robustness for legacy enterprise apps:
    - a11y_role: Accessibility role + name — most stable, works on desktop too
    - a11y_label: Accessibility label/description
    - text_content: Visible text the user sees
    - css_selector: CSS selector (fragile on legacy apps)
    - xpath: XPath expression (fragile but sometimes the only option)
    - coordinates: Screen coordinates (last resort, most fragile)
    """
    A11Y_ROLE = "a11y_role"
    A11Y_LABEL = "a11y_label"
    TEXT_CONTENT = "text_content"
    CSS_SELECTOR = "css_selector"
    XPATH = "xpath"
    COORDINATES = "coordinates"


class ParameterType(str, Enum):
    """Types for input/output parameters."""
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    DATE = "date"
    CURRENCY = "currency"


class ErrorCategory(str, Enum):
    """Three-tier error classification.

    - business_outcome: A legitimate result the caller needs to know about
      (e.g. "no such member"). Not a failure — the system worked correctly.
    - recoverable: A transient or known condition the system can handle
      automatically (e.g. dismiss a dialog, wait for a spinner, retry).
    - hard_failure: Something unexpected that should stop execution and
      surface a debuggable error.
    """
    BUSINESS_OUTCOME = "business_outcome"
    RECOVERABLE = "recoverable"
    HARD_FAILURE = "hard_failure"


class RecoveryAction(str, Enum):
    """What to do when a recoverable error is detected."""
    RETRY = "retry"               # Retry the current step
    DISMISS = "dismiss"           # Dismiss dialog/popup, then continue
    WAIT_AND_RETRY = "wait_retry" # Wait a beat, then retry
    SKIP = "skip"                 # Skip this step (rare, use carefully)
    ESCALATE = "escalate"         # Give up and escalate to human


class RiskLevel(str, Enum):
    """How risky/reversible an action is."""
    SAFE = "safe"           # Read-only or easily reversible
    MODERATE = "moderate"   # Has side effects but recoverable
    RISKY = "risky"         # Irreversible or financial impact


class CheckpointType(str, Enum):
    """What kind of assertion a checkpoint makes."""
    ELEMENT_VISIBLE = "element_visible"       # An element exists and is visible
    ELEMENT_NOT_VISIBLE = "element_not_visible"
    TEXT_PRESENT = "text_present"             # Specific text appears on screen
    TEXT_NOT_PRESENT = "text_not_present"
    URL_MATCHES = "url_matches"              # Current URL matches a pattern
    ELEMENT_HAS_VALUE = "element_has_value"  # An input/field has a specific value
    PAGE_TITLE_MATCHES = "page_title_matches"
    ELEMENT_COUNT = "element_count"          # Number of matching elements


# ---------------------------------------------------------------------------
# Locator — how we find elements
# ---------------------------------------------------------------------------


class Locator(BaseModel):
    """Identifies a target element/control on the surface.

    Uses a hybrid approach: one primary locator (the best/most robust one
    the agent found) plus a human-readable semantic description. The primary
    locator drives deterministic replay. The semantic description aids
    debugging, human review, and potential LLM-assisted recovery.
    """
    strategy: LocatorStrategy = Field(
        description="The locator strategy used (a11y_role, css_selector, etc.)"
    )
    value: str = Field(
        description="The locator value (e.g. CSS selector string, role name)"
    )
    semantic: str = Field(
        description=(
            "Human-readable description of what this element is, e.g. "
            "'the member ID search input field' or 'the Submit Transfer button'. "
            "Used for debugging, review, and potential LLM-assisted fallback."
        )
    )
    # Optional: accessibility metadata captured during recording
    a11y_role: str | None = Field(
        default=None,
        description="Accessibility role of the element at recording time"
    )
    a11y_name: str | None = Field(
        default=None,
        description="Accessibility name of the element at recording time"
    )


# ---------------------------------------------------------------------------
# Checkpoint — state assertions for traceability
# ---------------------------------------------------------------------------


class Checkpoint(BaseModel):
    """An assertion about the expected state of the surface.

    Used as pre-conditions (what must be true before a step),
    post-conditions (what must be true after), and success conditions.
    """
    type: CheckpointType
    locator: Locator | None = Field(
        default=None,
        description="Element to check (for element-based assertions)"
    )
    expected_value: str | None = Field(
        default=None,
        description="Expected text, URL pattern, or value. Supports {{param}} templates."
    )
    description: str = Field(
        description="Human-readable description: 'Member details page is displayed'"
    )
    timeout_ms: int = Field(
        default=5000,
        description="How long to wait for this checkpoint to become true"
    )


# ---------------------------------------------------------------------------
# Error matchers — what can go wrong and how to handle it
# ---------------------------------------------------------------------------


class ErrorMatcher(BaseModel):
    """A pattern that detects a specific error/exceptional state on the surface.

    Each step can have multiple error matchers. During replay, after each action,
    the engine scans for all registered patterns before checking the post-condition.
    """
    pattern: str = Field(
        description=(
            "Text pattern or regex to detect on the page. "
            "Examples: 'Member not found', 'Session expired', 'Error:.*'"
        )
    )
    is_regex: bool = Field(
        default=False,
        description="If true, pattern is interpreted as a regex"
    )
    category: ErrorCategory = Field(
        description="How to classify this error"
    )
    outcome_code: str | None = Field(
        default=None,
        description=(
            "Machine-readable outcome code for business_outcome errors. "
            "e.g. 'MEMBER_NOT_FOUND', 'INSUFFICIENT_FUNDS'"
        )
    )
    recovery_action: RecoveryAction | None = Field(
        default=None,
        description="What to do if this is a recoverable error"
    )
    recovery_locator: Locator | None = Field(
        default=None,
        description="Element to interact with for recovery (e.g. 'OK' button on a dialog)"
    )
    message_template: str | None = Field(
        default=None,
        description="Human-readable message to include in the result, e.g. 'Member {{member_id}} was not found'"
    )


class GlobalErrorHandler(BaseModel):
    """An error handler that applies across all steps.

    Global handlers catch conditions that can appear at any point:
    session timeouts, permission dialogs, unexpected modals, etc.
    They are checked BEFORE step-level error matchers.
    """
    name: str = Field(description="Handler name, e.g. 'session_timeout_handler'")
    matcher: ErrorMatcher
    description: str = Field(
        description="What this handler catches, e.g. 'Catches session timeout dialogs'"
    )
    priority: int = Field(
        default=0,
        description="Higher priority handlers are checked first"
    )


# ---------------------------------------------------------------------------
# Parameters — typed inputs and outputs
# ---------------------------------------------------------------------------


class InputParameter(BaseModel):
    """A typed input that the calling agent supplies per invocation."""
    name: str
    type: ParameterType
    required: bool = True
    description: str = Field(
        description="What this parameter is, e.g. 'The member ID to look up'"
    )
    sensitive: bool = Field(
        default=False,
        description="If true, this value is redacted in logs and evidence"
    )
    default: str | None = None
    validation_pattern: str | None = Field(
        default=None,
        description="Regex pattern the value must match, e.g. '^M\\d{4,}$'"
    )


class OutputField(BaseModel):
    """A typed output extracted from the surface after the flow completes."""
    name: str
    type: ParameterType
    description: str = Field(
        description="What this output represents, e.g. 'The member savings account balance'"
    )
    extractor: Locator = Field(
        description="How to find and extract this value from the surface"
    )
    sensitive: bool = Field(
        default=False,
        description="If true, this value is redacted in logs"
    )


# ---------------------------------------------------------------------------
# Step — one action in the flow
# ---------------------------------------------------------------------------


class Step(BaseModel):
    """A single action in the recorded flow.

    Each step is a traceable execution node with:
    - pre_condition: what must be true BEFORE this step runs
    - action + target: what to do and where
    - post_condition: what must be true AFTER this step succeeds
    - error_matchers: what to look for if something goes wrong
    - timing and retry configuration

    Template variables ({{param_name}}) in `value` and `expected_value`
    fields are resolved from input parameters at replay time.
    """
    id: str = Field(
        description="Unique step identifier within this artifact, e.g. 'step_01'"
    )
    description: str = Field(
        description="Human-readable description: 'Type member ID into search field'"
    )
    action: ActionType
    target: Locator | None = Field(
        default=None,
        description="The element to act on. None for navigate/wait/screenshot actions."
    )
    value: str | None = Field(
        default=None,
        description=(
            "Action payload: URL for navigate, text for type, key for press_key. "
            "Supports {{param_name}} template variables."
        )
    )
    risk_level: RiskLevel = Field(
        default=RiskLevel.SAFE,
        description="How risky this action is. Risky actions may require confirmation."
    )

    # Traceability: state assertions before and after
    pre_condition: Checkpoint | None = Field(
        default=None,
        description="Expected state before this step. Fails fast if not met."
    )
    post_condition: Checkpoint | None = Field(
        default=None,
        description="Expected state after this step succeeds."
    )

    # Error handling
    error_matchers: list[ErrorMatcher] = Field(
        default_factory=list,
        description="Patterns to check for after this action, before checking post_condition"
    )

    # Timing
    timeout_ms: int = Field(
        default=10000,
        description="Max time to wait for this step to complete"
    )
    retry: StepRetry | None = Field(
        default=None,
        description="Retry configuration for transient failures"
    )

    # Metadata for tracing
    recorded_at: datetime | None = Field(
        default=None,
        description="When this step was recorded during discovery"
    )
    recorded_duration_ms: int | None = Field(
        default=None,
        description="How long this step took during the original discovery run"
    )


class StepRetry(BaseModel):
    """Retry configuration for a step."""
    max_attempts: int = Field(default=3, ge=1, le=10)
    delay_ms: int = Field(default=1000, ge=0)
    backoff_multiplier: float = Field(
        default=1.5,
        description="Multiply delay by this factor on each retry"
    )


# ---------------------------------------------------------------------------
# Capability — the top-level artifact
# ---------------------------------------------------------------------------


class CapabilityMetadata(BaseModel):
    """Metadata about the capability artifact."""
    name: str = Field(
        description="Machine-readable name, e.g. 'lookup_member_balance'"
    )
    display_name: str = Field(
        description="Human-readable name, e.g. 'Look Up Member Savings Balance'"
    )
    description: str = Field(
        description=(
            "What this capability does, in enough detail for both a human reviewer "
            "and a calling AI agent to understand."
        )
    )
    version: str = Field(
        default="1.0.0",
        description="Semantic version of this artifact (not the schema version)"
    )
    schema_version: SchemaVersion = Field(
        default=SchemaVersion.V1,
        description="Version of the artifact schema format"
    )
    surface_type: SurfaceType = Field(
        default=SurfaceType.LEGACY_WEB,
        description="What kind of surface this was recorded against"
    )
    target_url_pattern: str | None = Field(
        default=None,
        description="URL pattern this capability applies to, e.g. 'http://localhost:5000/*'"
    )
    source_goal: str = Field(
        description="The original natural-language goal that produced this artifact"
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
    )
    created_by: str = Field(
        default="discovery_agent",
        description="Who/what created this artifact"
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Tags for categorization, e.g. ['member', 'balance', 'read-only']"
    )
    tenant_id: str | None = Field(
        default=None,
        description=(
            "Tenant this was recorded for. None = generic/base artifact. "
            "Used for multi-tenant reuse: a base artifact can be overridden per-tenant."
        )
    )
    parent_artifact_id: str | None = Field(
        default=None,
        description=(
            "If this is a tenant-specific override, points to the base artifact. "
            "Enables inheritance: tenant overrides only the steps that differ."
        )
    )


class Capability(BaseModel):
    """A complete, reusable automation capability.

    This is the top-level artifact: the thing that gets saved to disk,
    versioned, reviewed, and invoked by an AI agent.

    Contract:
    - inputs: what the caller must provide (typed, validated)
    - outputs: what the caller gets back (typed, extracted from the surface)
    - steps: the ordered actions to perform
    - success_condition: how to know the flow completed successfully
    - error_handlers: global patterns checked at every step
    """
    metadata: CapabilityMetadata

    # Contract: what goes in, what comes out
    inputs: list[InputParameter] = Field(
        default_factory=list,
        description="Typed input parameters the caller supplies"
    )
    outputs: list[OutputField] = Field(
        default_factory=list,
        description="Typed output fields extracted from the surface on success"
    )

    # The flow
    steps: list[Step] = Field(
        description="Ordered list of actions to perform"
    )

    # Success
    success_condition: Checkpoint = Field(
        description="Assertion that confirms the goal was achieved"
    )

    # Global error handling
    error_handlers: list[GlobalErrorHandler] = Field(
        default_factory=list,
        description="Error handlers checked at every step (session timeout, etc.)"
    )

    def to_json(self, **kwargs: Any) -> str:
        """Serialize to pretty-printed JSON."""
        return self.model_dump_json(indent=2, **kwargs)

    @classmethod
    def from_json(cls, json_str: str) -> Capability:
        """Deserialize from JSON string."""
        return cls.model_validate_json(json_str)

    @classmethod
    def from_file(cls, path: str) -> Capability:
        """Load from a JSON file."""
        from pathlib import Path
        return cls.model_validate_json(Path(path).read_text())

    def save(self, path: str) -> None:
        """Save to a JSON file."""
        from pathlib import Path
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json())

    def get_input(self, name: str) -> InputParameter | None:
        """Look up an input parameter by name."""
        return next((p for p in self.inputs if p.name == name), None)

    def get_step(self, step_id: str) -> Step | None:
        """Look up a step by its ID."""
        return next((s for s in self.steps if s.id == step_id), None)

    def validate_inputs(self, params: dict[str, str]) -> list[str]:
        """Validate a set of input parameters. Returns list of error messages."""
        import re
        errors = []
        for inp in self.inputs:
            if inp.required and inp.name not in params:
                errors.append(f"Missing required parameter: {inp.name}")
            if inp.name in params and inp.validation_pattern:
                if not re.match(inp.validation_pattern, params[inp.name]):
                    errors.append(
                        f"Parameter '{inp.name}' value '{params[inp.name]}' "
                        f"does not match pattern '{inp.validation_pattern}'"
                    )
        return errors
