"""
Step Recorder — captures agent actions and builds the artifact.

During discovery, each action the agent takes is recorded as a Step
in the artifact schema. The recorder:

1. Translates agent decisions into typed Step objects
2. Captures locator information for each target element
3. Generates pre/post conditions based on observed state
4. Builds the complete Capability artifact at the end

The artifact is the bridge between discovery and replay: what the LLM
figured out gets crystallized into a deterministic, replayable form.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from cua.agent.providers.base import AgentAction, ExtractedData
from cua.schema.artifact import (
    ActionType,
    Capability,
    CapabilityMetadata,
    Checkpoint,
    CheckpointType,
    ErrorCategory,
    ErrorMatcher,
    GlobalErrorHandler,
    InputParameter,
    Locator,
    LocatorStrategy,
    OutputField,
    ParameterType,
    RecoveryAction,
    RiskLevel,
    SchemaVersion,
    Step,
    StepRetry,
    SurfaceType,
)
from cua.surface.base import SurfaceState

logger = logging.getLogger(__name__)


class RecordedStep:
    """Internal representation of a recorded step during discovery."""

    def __init__(
        self,
        step_num: int,
        action: AgentAction,
        state: SurfaceState,
        risk_level: RiskLevel = RiskLevel.SAFE,
    ):
        self.step_num = step_num
        self.action = action
        self.state = state
        self.risk_level = risk_level
        self.timestamp = datetime.utcnow()
        self.extracted_data: list[ExtractedData] = []


class StepRecorder:
    """Records agent actions during discovery and emits a Capability artifact."""

    def __init__(self):
        self._recorded_steps: list[RecordedStep] = []
        self._extracted_data: list[ExtractedData] = []
        self._start_time: datetime | None = None

    def record_step(
        self,
        step_num: int,
        action: AgentAction,
        state: SurfaceState,
        risk_level: RiskLevel = RiskLevel.SAFE,
    ) -> None:
        """Record a single agent action."""
        if not self._start_time:
            self._start_time = datetime.utcnow()

        self._recorded_steps.append(RecordedStep(
            step_num=step_num,
            action=action,
            state=state,
            risk_level=risk_level,
        ))
        logger.debug(f"Recorded step {step_num}: {action.action_type} on {action.target_semantic}")

    def record_extract(
        self,
        step_num: int,
        data: list[ExtractedData],
        state: SurfaceState,
    ) -> None:
        """Record data extraction."""
        self._extracted_data.extend(data)

    def build_artifact(
        self,
        goal: str,
        target_url: str,
        metadata_overrides: dict[str, Any] | None = None,
    ) -> Capability:
        """Build a Capability artifact from all recorded steps.

        Args:
            goal: The original natural-language goal
            target_url: The target application URL
            metadata_overrides: Optional LLM-generated metadata to merge in
        """
        overrides = metadata_overrides or {}

        # Build metadata
        metadata = CapabilityMetadata(
            name=overrides.get("name", self._generate_name(goal)),
            display_name=overrides.get("display_name", goal[:80]),
            description=overrides.get("description", f"Automated capability: {goal}"),
            version="1.0.0",
            schema_version=SchemaVersion.V1,
            surface_type=SurfaceType.LEGACY_WEB,
            target_url_pattern=self._url_to_pattern(target_url),
            source_goal=goal,
            created_at=self._start_time or datetime.utcnow(),
            created_by=f"discovery_agent",
            tags=overrides.get("tags", []),
        )

        # Build input parameters from overrides or detected patterns
        inputs = self._build_inputs(overrides.get("input_parameters", []))

        # Build output fields from extracted data
        outputs = self._build_outputs(overrides.get("output_fields", []))

        # Build steps
        steps = self._build_steps()

        # Build success condition from the last observed state
        success_condition = self._build_success_condition()

        # Build default global error handlers
        error_handlers = self._build_global_error_handlers()

        return Capability(
            metadata=metadata,
            inputs=inputs,
            outputs=outputs,
            steps=steps,
            success_condition=success_condition,
            error_handlers=error_handlers,
        )

    def _build_steps(self) -> list[Step]:
        """Convert recorded steps into artifact Step objects."""
        steps = []
        for i, rec in enumerate(self._recorded_steps):
            action = rec.action
            step_id = f"step_{i+1:02d}"

            # Build the target locator
            target = None
            if action.target_value:
                target = Locator(
                    strategy=self._parse_locator_strategy(action.target_strategy),
                    value=action.target_value,
                    semantic=action.target_semantic or f"Element for {action.action_type}",
                )

            # Build post-condition from observed URL change or expected text
            post_condition = None
            if i < len(self._recorded_steps) - 1:
                next_state = self._recorded_steps[i + 1].state
                if next_state.url != rec.state.url:
                    post_condition = Checkpoint(
                        type=CheckpointType.URL_MATCHES,
                        expected_value=self._url_to_pattern(next_state.url),
                        description=f"Page navigated after {action.action_type}",
                    )

            step = Step(
                id=step_id,
                description=action.target_semantic or f"{action.action_type} action",
                action=self._parse_action_type(action.action_type),
                target=target,
                value=action.value if action.value else None,
                risk_level=rec.risk_level,
                post_condition=post_condition,
                timeout_ms=10000,
                retry=StepRetry(max_attempts=2, delay_ms=1000),
                recorded_at=rec.timestamp,
            )
            steps.append(step)

        return steps

    def _build_success_condition(self) -> Checkpoint:
        """Build the success condition from the final state."""
        if self._recorded_steps:
            last_state = self._recorded_steps[-1].state
            if last_state.url:
                return Checkpoint(
                    type=CheckpointType.URL_MATCHES,
                    expected_value=last_state.url,
                    description="Final page reached after all steps",
                )
        return Checkpoint(
            type=CheckpointType.TEXT_PRESENT,
            expected_value="",
            description="Goal completion checkpoint (needs manual review)",
        )

    def _build_global_error_handlers(self) -> list[GlobalErrorHandler]:
        """Build default error handlers for common banking app errors."""
        return [
            GlobalErrorHandler(
                name="session_timeout",
                description="Catches session timeout / expiry dialogs",
                priority=10,
                matcher=ErrorMatcher(
                    pattern="Session.*expired|Session.*Timeout|session has expired",
                    is_regex=True,
                    category=ErrorCategory.RECOVERABLE,
                    recovery_action=RecoveryAction.ESCALATE,
                    message_template="Session timed out — escalating to human operator",
                ),
            ),
            GlobalErrorHandler(
                name="permission_denied",
                description="Catches permission/authorization denied errors",
                priority=9,
                matcher=ErrorMatcher(
                    pattern="PERMISSION DENIED|Access Denied|not have access",
                    is_regex=True,
                    category=ErrorCategory.HARD_FAILURE,
                    message_template="Permission denied — operator lacks required access",
                ),
            ),
            GlobalErrorHandler(
                name="server_error",
                description="Catches internal server errors (500)",
                priority=8,
                matcher=ErrorMatcher(
                    pattern="Internal Server Error|INTERNAL_SYS_ERR|Error \\(500\\)",
                    is_regex=True,
                    category=ErrorCategory.HARD_FAILURE,
                    message_template="Server error encountered",
                ),
            ),
            GlobalErrorHandler(
                name="member_not_found",
                description="Catches member-not-found results (business outcome, not error)",
                priority=5,
                matcher=ErrorMatcher(
                    pattern="NO RESULTS:.*not found|Member.*not found",
                    is_regex=True,
                    category=ErrorCategory.BUSINESS_OUTCOME,
                    outcome_code="MEMBER_NOT_FOUND",
                    message_template="Member not found in the system",
                ),
            ),
            GlobalErrorHandler(
                name="validation_error",
                description="Catches form validation errors",
                priority=4,
                matcher=ErrorMatcher(
                    pattern="VALIDATION ERROR:",
                    is_regex=False,
                    category=ErrorCategory.BUSINESS_OUTCOME,
                    outcome_code="VALIDATION_ERROR",
                    message_template="Form validation error",
                ),
            ),
        ]

    def _build_inputs(self, overrides: list[dict]) -> list[InputParameter]:
        """Build input parameters from LLM overrides."""
        inputs = []
        for param in overrides:
            inputs.append(InputParameter(
                name=param.get("name", "unnamed"),
                type=ParameterType(param.get("type", "string")),
                required=param.get("required", True),
                description=param.get("description", ""),
                sensitive=param.get("sensitive", False),
            ))
        return inputs

    def _build_outputs(self, overrides: list[dict]) -> list[OutputField]:
        """Build output fields from extracted data and LLM overrides."""
        outputs = []
        for field in overrides:
            outputs.append(OutputField(
                name=field.get("name", "unnamed"),
                type=ParameterType(field.get("type", "string")),
                description=field.get("description", ""),
                extractor=Locator(
                    strategy=LocatorStrategy.TEXT_CONTENT,
                    value=field.get("name", ""),
                    semantic=field.get("description", "output field"),
                ),
            ))
        # Also add outputs from extracted data if not already covered
        existing_names = {o.name for o in outputs}
        for data in self._extracted_data:
            if data.name and data.name not in existing_names:
                outputs.append(OutputField(
                    name=data.name,
                    type=ParameterType.STRING,
                    description=f"Extracted: {data.source}",
                    extractor=Locator(
                        strategy=LocatorStrategy.TEXT_CONTENT,
                        value=data.source or data.name,
                        semantic=f"Source of {data.name}",
                    ),
                ))
                existing_names.add(data.name)
        return outputs

    @staticmethod
    def _generate_name(goal: str) -> str:
        """Generate a snake_case name from the goal text."""
        import re
        name = goal.lower().strip()
        name = re.sub(r"[^a-z0-9\s]", "", name)
        name = re.sub(r"\s+", "_", name)
        return name[:60]

    @staticmethod
    def _url_to_pattern(url: str) -> str:
        """Convert a concrete URL to a pattern (for multi-tenant reuse)."""
        import re
        # Replace specific IDs with wildcards
        pattern = re.sub(r"/M\d+", "/{{member_id}}", url)
        pattern = re.sub(r"/\d+", "/*", pattern)
        return pattern

    @staticmethod
    def _parse_locator_strategy(strategy_str: str) -> LocatorStrategy:
        try:
            return LocatorStrategy(strategy_str.lower())
        except ValueError:
            return LocatorStrategy.CSS_SELECTOR

    @staticmethod
    def _parse_action_type(action_str: str) -> ActionType:
        try:
            return ActionType(action_str.lower())
        except ValueError:
            return ActionType.WAIT
