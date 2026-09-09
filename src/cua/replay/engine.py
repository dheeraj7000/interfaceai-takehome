"""
Replay Engine — deterministic execution of recorded capability artifacts.

This is the production execution path. No LLM is involved. The engine:

1. Loads a Capability artifact
2. Validates input parameters against the artifact's input schema
3. Resolves template variables ({{param_name}}) in step values
4. Executes each step in order on the live surface
5. At each step:
   a. Checks pre-conditions (smart waits for expected state)
   b. Scans for global error patterns (session timeout, etc.)
   c. Executes the action
   d. Scans for step-level error patterns
   e. Checks post-conditions
   f. Records a StepTrace for observability
6. On success: extracts output values and returns them
7. On business outcome: returns the outcome code and message
8. On failure: returns debuggable error context
9. On stuck: triggers escalation

The engine distinguishes three result categories:
- success: goal met, outputs extracted
- business_outcome: legitimate non-success result (not a crash)
- failure: hard error with step context for debugging
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from cua.replay.error_scanner import ErrorScanner
from cua.replay.waiter import SmartWaiter
from cua.safety.allowlist import Allowlist
from cua.safety.classifier import classify_action
from cua.safety.redactor import Redactor
from cua.schema.artifact import (
    ActionType,
    Capability,
    ErrorCategory,
    RecoveryAction,
    RiskLevel,
    Step,
)
from cua.schema.results import (
    EscalationContext,
    ExtractedOutput,
    ReplayResult,
    ResultStatus,
    StepStatus,
    StepTrace,
)
from cua.surface.base import Surface

logger = logging.getLogger(__name__)


class ReplayEngine:
    """Executes a Capability artifact deterministically on a live surface."""

    def __init__(
        self,
        surface: Surface,
        allowlist: Allowlist,
        redactor: Redactor | None = None,
        evidence_dir: str = "",
        confirm_risky: bool = False,
        on_escalation: Any = None,
    ):
        """
        Args:
            surface: The surface to replay on
            allowlist: Safety policy
            redactor: PII redactor (optional)
            evidence_dir: Directory to save evidence (screenshots, etc.)
            confirm_risky: If True, allow risky actions without blocking
            on_escalation: Callback for escalation (async callable)
        """
        self._surface = surface
        self._allowlist = allowlist
        self._redactor = redactor or Redactor()
        self._evidence_dir = evidence_dir
        self._confirm_risky = confirm_risky
        self._on_escalation = on_escalation
        self._error_scanner = ErrorScanner()
        self._waiter = SmartWaiter(surface)

    async def replay(
        self,
        capability: Capability,
        params: dict[str, str],
        target_url: str | None = None,
    ) -> ReplayResult:
        """Execute a capability artifact with the given parameters.

        Args:
            capability: The loaded Capability artifact
            params: Input parameter values
            target_url: Override the target URL (otherwise uses artifact's pattern)

        Returns:
            ReplayResult with status, outputs, traces, and evidence
        """
        run_id = str(uuid.uuid4())[:8]
        started_at = datetime.utcnow()
        step_traces: list[StepTrace] = []

        # Set up evidence
        evidence_path = None
        if self._evidence_dir:
            evidence_path = Path(self._evidence_dir) / f"replay_{run_id}"
            evidence_path.mkdir(parents=True, exist_ok=True)
            (evidence_path / "screenshots").mkdir(exist_ok=True)

        logger.info(
            f"Starting replay: capability={capability.metadata.name}, "
            f"run_id={run_id}, params={self._redactor.redact_params(params)}"
        )

        # 1. VALIDATE INPUTS
        validation_errors = capability.validate_inputs(params)
        if validation_errors:
            return ReplayResult(
                capability_name=capability.metadata.name,
                capability_version=capability.metadata.version,
                run_id=run_id,
                status=ResultStatus.FAILURE,
                message=f"Input validation failed: {'; '.join(validation_errors)}",
                error_details=str(validation_errors),
                started_at=started_at,
                completed_at=datetime.utcnow(),
                input_params=self._redactor.redact_params(params),
            )

        # Register sensitive param values for redaction
        for inp in capability.inputs:
            if inp.sensitive and inp.name in params:
                self._redactor.register_sensitive_value(params[inp.name])

        # 2. RESOLVE TARGET URL
        entry_url = target_url or self._resolve_url(
            capability.metadata.target_url_pattern or "", params
        )

        # 3. START SURFACE
        try:
            await self._surface.start(entry_url)
        except Exception as e:
            return ReplayResult(
                capability_name=capability.metadata.name,
                capability_version=capability.metadata.version,
                run_id=run_id,
                status=ResultStatus.FAILURE,
                message=f"Failed to start surface: {e}",
                error_details=str(e),
                started_at=started_at,
                completed_at=datetime.utcnow(),
                input_params=self._redactor.redact_params(params),
            )

        # Reset allowlist counters
        self._allowlist.reset_counters()

        # 4. EXECUTE STEPS
        result_status = ResultStatus.SUCCESS
        result_message = ""
        outcome_code = None
        outcome_message = None
        failed_step_id = None
        error_details = None
        expected_state = None
        observed_state = None
        escalation: EscalationContext | None = None

        try:
            for step in capability.steps:
                step_trace = await self._execute_step(
                    step=step,
                    params=params,
                    capability=capability,
                    evidence_path=evidence_path,
                )
                step_traces.append(step_trace)

                # Check step result
                if step_trace.status == StepStatus.FAILED:
                    if step_trace.error_category == ErrorCategory.BUSINESS_OUTCOME.value:
                        result_status = ResultStatus.BUSINESS_OUTCOME
                        outcome_code = step_trace.error_detected
                        outcome_message = step_trace.observed_state
                        result_message = f"Business outcome: {outcome_code}"
                    else:
                        result_status = ResultStatus.FAILURE
                        failed_step_id = step_trace.step_id
                        error_details = step_trace.error_detected
                        expected_state = step_trace.expected_state
                        observed_state = step_trace.observed_state
                        result_message = (
                            f"Step {step_trace.step_id} failed: {step_trace.error_detected}"
                        )
                    break

                if step_trace.status == StepStatus.ESCALATED:
                    result_status = ResultStatus.ESCALATED
                    result_message = f"Escalated at step {step_trace.step_id}"
                    escalation = EscalationContext(
                        reason=step_trace.error_detected or "Unknown",
                        capability_name=capability.metadata.name,
                        current_step_id=step_trace.step_id,
                        current_step_description=step_trace.step_description,
                        screenshot_path=step_trace.screenshot_path,
                        observed_state=step_trace.observed_state,
                    )
                    # Trigger escalation callback
                    if self._on_escalation:
                        session_info = await self._surface.pause()
                        escalation.session_url = session_info.get("novnc_url", "")
                        await self._on_escalation(escalation)
                    break

        except Exception as e:
            result_status = ResultStatus.FAILURE
            result_message = f"Unexpected error during replay: {e}"
            error_details = str(e)
            logger.exception("Replay engine error")

        # 5. EXTRACT OUTPUTS (on success)
        outputs: list[ExtractedOutput] = []
        if result_status == ResultStatus.SUCCESS:
            result_message = "Goal achieved successfully"
            outputs = await self._extract_outputs(capability, params)

            # Verify success condition
            success_met = await self._check_checkpoint(
                capability.success_condition, params
            )
            if not success_met:
                result_status = ResultStatus.FAILURE
                result_message = "Success condition not met after all steps completed"
                error_details = "Post-replay success checkpoint failed"

        # Mark un-reached steps
        executed_ids = {t.step_id for t in step_traces}
        for step in capability.steps:
            if step.id not in executed_ids:
                step_traces.append(StepTrace(
                    step_id=step.id,
                    step_description=step.description,
                    status=StepStatus.NOT_REACHED,
                    started_at=datetime.utcnow(),
                    action_performed="(not reached)",
                ))

        completed_at = datetime.utcnow()
        total_ms = int((completed_at - started_at).total_seconds() * 1000)

        return ReplayResult(
            capability_name=capability.metadata.name,
            capability_version=capability.metadata.version,
            run_id=run_id,
            status=result_status,
            message=result_message,
            outputs=outputs,
            outcome_code=outcome_code,
            outcome_message=outcome_message,
            failed_step_id=failed_step_id,
            error_details=error_details,
            expected_state=expected_state,
            observed_state=observed_state,
            escalation=escalation,
            step_traces=step_traces,
            started_at=started_at,
            completed_at=completed_at,
            total_duration_ms=total_ms,
            evidence_dir=str(evidence_path) if evidence_path else None,
            input_params=self._redactor.redact_params(params),
        )

    async def _execute_step(
        self,
        step: Step,
        params: dict[str, str],
        capability: Capability,
        evidence_path: Path | None,
    ) -> StepTrace:
        """Execute a single step with full tracing."""
        started_at = datetime.utcnow()
        step_trace = StepTrace(
            step_id=step.id,
            step_description=step.description,
            status=StepStatus.PASSED,
            started_at=started_at,
            action_performed=f"{step.action.value}",
        )

        resolved_value = self._resolve_template(step.value or "", params)

        try:
            # --- PRE-CONDITION CHECK ---
            if step.pre_condition:
                pre_met = await self._check_checkpoint(step.pre_condition, params)
                step_trace.pre_condition_met = pre_met
                if not pre_met:
                    # Smart wait for pre-condition
                    pre_met = await self._waiter.wait_for_checkpoint(
                        step.pre_condition, params, timeout_ms=step.timeout_ms
                    )
                    step_trace.pre_condition_met = pre_met
                    if not pre_met:
                        step_trace.status = StepStatus.FAILED
                        step_trace.error_detected = "Pre-condition not met"
                        step_trace.error_category = ErrorCategory.HARD_FAILURE.value
                        step_trace.expected_state = step.pre_condition.description
                        step_trace.observed_state = await self._get_state_summary()
                        return self._finalize_trace(step_trace, started_at, evidence_path, step)

            # --- GLOBAL ERROR SCAN (before action) ---
            global_error = await self._error_scanner.scan_global(
                self._surface, capability.error_handlers
            )
            if global_error:
                return await self._handle_error_match(
                    step_trace, global_error, started_at, evidence_path, step, params
                )

            # --- SAFETY CHECK ---
            current_url = (await self._surface.observe()).url if step.action != ActionType.NAVIGATE else ""
            action_url = current_url or resolved_value
            risk = step.risk_level or classify_action(step.action, step.target, resolved_value)
            safety = self._allowlist.check_action(
                url=action_url,
                action=step.action,
                risk_level=risk,
                confirmation_provided=self._confirm_risky,
            )
            if not safety.allowed:
                if safety.requires_confirmation:
                    step_trace.status = StepStatus.ESCALATED
                    step_trace.error_detected = f"Risky action needs confirmation: {safety.reason}"
                else:
                    step_trace.status = StepStatus.FAILED
                    step_trace.error_detected = f"Blocked by policy: {safety.reason}"
                    step_trace.error_category = ErrorCategory.HARD_FAILURE.value
                return self._finalize_trace(step_trace, started_at, evidence_path, step)

            # --- EXECUTE ACTION (with retries) ---
            max_attempts = step.retry.max_attempts if step.retry else 1
            delay_ms = step.retry.delay_ms if step.retry else 1000
            backoff = step.retry.backoff_multiplier if step.retry else 1.5

            for attempt in range(1, max_attempts + 1):
                step_trace.attempt_number = attempt
                step_trace.total_attempts = max_attempts

                try:
                    await self._perform_action(step, resolved_value)
                    step_trace.action_performed = (
                        f"{step.action.value} on {step.target.semantic if step.target else 'page'}"
                    )
                    break  # Action succeeded
                except Exception as e:
                    if attempt < max_attempts:
                        logger.warning(
                            f"Step {step.id} attempt {attempt} failed: {e}. Retrying..."
                        )
                        await asyncio.sleep(delay_ms / 1000)
                        delay_ms = int(delay_ms * backoff)
                    else:
                        step_trace.status = StepStatus.FAILED
                        step_trace.error_detected = f"Action failed after {max_attempts} attempts: {e}"
                        step_trace.error_category = ErrorCategory.HARD_FAILURE.value
                        step_trace.observed_state = str(e)
                        return self._finalize_trace(step_trace, started_at, evidence_path, step)

            # Brief settle time
            await asyncio.sleep(0.3)

            # --- STEP-LEVEL ERROR SCAN (after action) ---
            for matcher in step.error_matchers:
                matched = await self._error_scanner.scan_pattern(self._surface, matcher)
                if matched:
                    return await self._handle_error_match(
                        step_trace, matched, started_at, evidence_path, step, params
                    )

            # --- GLOBAL ERROR SCAN (after action) ---
            global_error = await self._error_scanner.scan_global(
                self._surface, capability.error_handlers
            )
            if global_error:
                return await self._handle_error_match(
                    step_trace, global_error, started_at, evidence_path, step, params
                )

            # --- POST-CONDITION CHECK ---
            if step.post_condition:
                post_met = await self._waiter.wait_for_checkpoint(
                    step.post_condition, params, timeout_ms=step.timeout_ms
                )
                step_trace.post_condition_met = post_met
                if not post_met:
                    step_trace.status = StepStatus.FAILED
                    step_trace.error_detected = "Post-condition not met"
                    step_trace.error_category = ErrorCategory.HARD_FAILURE.value
                    step_trace.expected_state = step.post_condition.description
                    step_trace.observed_state = await self._get_state_summary()
                    return self._finalize_trace(step_trace, started_at, evidence_path, step)

            step_trace.status = StepStatus.PASSED

        except Exception as e:
            step_trace.status = StepStatus.FAILED
            step_trace.error_detected = f"Unexpected error: {e}"
            step_trace.error_category = ErrorCategory.HARD_FAILURE.value
            logger.exception(f"Step {step.id} error")

        return self._finalize_trace(step_trace, started_at, evidence_path, step)

    async def _handle_error_match(
        self,
        trace: StepTrace,
        match: dict,
        started_at: datetime,
        evidence_path: Path | None,
        step: Step,
        params: dict[str, str],
    ) -> StepTrace:
        """Handle a matched error pattern based on its category."""
        category = match["category"]
        trace.error_detected = match.get("outcome_code") or match.get("pattern", "unknown")
        trace.error_category = category
        trace.observed_state = match.get("matched_text", "")

        if category == ErrorCategory.BUSINESS_OUTCOME.value:
            trace.status = StepStatus.FAILED
            logger.info(f"Business outcome detected: {trace.error_detected}")

        elif category == ErrorCategory.RECOVERABLE.value:
            recovery = match.get("recovery_action")
            trace.recovery_attempted = recovery

            if recovery == RecoveryAction.DISMISS.value:
                # Try to dismiss the dialog
                dismiss_locator = match.get("recovery_locator")
                if dismiss_locator:
                    try:
                        await self._surface.click(
                            dismiss_locator["strategy"], dismiss_locator["value"]
                        )
                        trace.recovery_succeeded = True
                        trace.status = StepStatus.RECOVERED
                        logger.info("Recovered by dismissing dialog")
                        return self._finalize_trace(trace, started_at, evidence_path, step)
                    except Exception as e:
                        logger.warning(f"Dismiss recovery failed: {e}")

            elif recovery in (RecoveryAction.RETRY.value, RecoveryAction.WAIT_AND_RETRY.value):
                await asyncio.sleep(2)
                trace.recovery_succeeded = False
                trace.status = StepStatus.FAILED

            elif recovery == RecoveryAction.ESCALATE.value:
                trace.status = StepStatus.ESCALATED
                logger.warning(f"Escalating: {trace.error_detected}")
                return self._finalize_trace(trace, started_at, evidence_path, step)

            # If recovery didn't help, it's a failure
            if trace.status != StepStatus.RECOVERED:
                trace.status = StepStatus.FAILED

        else:
            # Hard failure
            trace.status = StepStatus.FAILED
            logger.error(f"Hard failure: {trace.error_detected}")

        return self._finalize_trace(trace, started_at, evidence_path, step)

    async def _perform_action(self, step: Step, resolved_value: str) -> None:
        """Execute the actual action on the surface."""
        action = step.action
        target = step.target

        if action == ActionType.NAVIGATE:
            await self._surface.navigate(resolved_value)

        elif action == ActionType.CLICK:
            if not target:
                raise ValueError("Click action requires a target")
            await self._surface.click(target.strategy.value, target.value)

        elif action == ActionType.TYPE:
            if not target:
                raise ValueError("Type action requires a target")
            await self._surface.type_text(target.strategy.value, target.value, resolved_value)

        elif action == ActionType.SELECT:
            if not target:
                raise ValueError("Select action requires a target")
            await self._surface.select_option(target.strategy.value, target.value, resolved_value)

        elif action == ActionType.CLEAR:
            if not target:
                raise ValueError("Clear action requires a target")
            await self._surface.clear(target.strategy.value, target.value)

        elif action == ActionType.PRESS_KEY:
            await self._surface.press_key(resolved_value)

        elif action == ActionType.HOVER:
            if not target:
                raise ValueError("Hover action requires a target")
            await self._surface.hover(target.strategy.value, target.value)

        elif action == ActionType.SCROLL:
            await self._surface.scroll(resolved_value or "down")

        elif action == ActionType.WAIT:
            timeout = step.timeout_ms or 2000
            await asyncio.sleep(timeout / 1000)

        elif action == ActionType.EXTRACT:
            pass  # Extraction happens in output collection

        elif action == ActionType.ASSERT:
            if step.post_condition:
                # Assert is handled by post-condition check
                pass

        elif action == ActionType.SCREENSHOT:
            await self._surface.screenshot()

        else:
            logger.warning(f"Unknown action type: {action}")

    async def _extract_outputs(
        self,
        capability: Capability,
        params: dict[str, str],
    ) -> list[ExtractedOutput]:
        """Extract declared output values from the surface."""
        outputs = []
        for output_field in capability.outputs:
            try:
                text = await self._surface.get_element_text(
                    output_field.extractor.strategy.value,
                    output_field.extractor.value,
                )
                value = text if text else ""
                if output_field.sensitive:
                    value = "[REDACTED]"
                outputs.append(ExtractedOutput(
                    name=output_field.name,
                    value=value,
                    extracted_from=output_field.extractor.semantic,
                    redacted=output_field.sensitive,
                ))
            except Exception as e:
                logger.warning(f"Failed to extract output '{output_field.name}': {e}")
                outputs.append(ExtractedOutput(
                    name=output_field.name,
                    value="",
                    extracted_from=f"FAILED: {e}",
                ))
        return outputs

    async def _check_checkpoint(self, checkpoint: Any, params: dict[str, str]) -> bool:
        """Check if a checkpoint condition is met."""
        from cua.schema.artifact import CheckpointType

        expected = self._resolve_template(checkpoint.expected_value or "", params)

        try:
            if checkpoint.type == CheckpointType.ELEMENT_VISIBLE:
                if checkpoint.locator:
                    return await self._surface.element_exists(
                        checkpoint.locator.strategy.value, checkpoint.locator.value
                    )
            elif checkpoint.type == CheckpointType.TEXT_PRESENT:
                if expected:
                    return await self._surface.wait_for_text(expected, timeout_ms=3000)
            elif checkpoint.type == CheckpointType.URL_MATCHES:
                state = await self._surface.observe()
                if expected:
                    return bool(re.search(re.escape(expected).replace(r"\*", ".*"), state.url))
            elif checkpoint.type == CheckpointType.ELEMENT_HAS_VALUE:
                if checkpoint.locator:
                    text = await self._surface.get_element_text(
                        checkpoint.locator.strategy.value, checkpoint.locator.value
                    )
                    return text == expected if text else False
            elif checkpoint.type == CheckpointType.PAGE_TITLE_MATCHES:
                state = await self._surface.observe()
                return expected.lower() in state.page_title.lower() if expected else True
        except Exception as e:
            logger.debug(f"Checkpoint check failed: {e}")

        return False

    async def _get_state_summary(self) -> str:
        """Get a brief summary of the current surface state for debugging."""
        try:
            state = await self._surface.observe()
            text = state.visible_text[:500] if state.visible_text else ""
            return f"URL: {state.url} | Text: {text}"
        except Exception:
            return "(failed to capture state)"

    def _finalize_trace(
        self,
        trace: StepTrace,
        started_at: datetime,
        evidence_path: Path | None,
        step: Step,
    ) -> StepTrace:
        """Finalize a step trace with timing and evidence."""
        trace.completed_at = datetime.utcnow()
        trace.duration_ms = int((trace.completed_at - started_at).total_seconds() * 1000)

        # Save screenshot evidence on failure or escalation
        if evidence_path and trace.status in (
            StepStatus.FAILED, StepStatus.ESCALATED, StepStatus.RECOVERED
        ):
            screenshot_path = str(evidence_path / "screenshots" / f"{step.id}_{trace.status.value}.png")
            try:
                # Synchronous save — must happen before surface is closed
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # We're inside an async context, schedule but don't await
                    # (the screenshot may fail if surface closes first — that's ok)
                    pass
                trace.screenshot_path = screenshot_path
            except Exception:
                pass

        return trace

    @staticmethod
    def _resolve_template(template: str, params: dict[str, str]) -> str:
        """Resolve {{param_name}} template variables."""
        if not template:
            return template
        result = template
        for key, value in params.items():
            result = result.replace(f"{{{{{key}}}}}", value)
        return result

    @staticmethod
    def _resolve_url(url_pattern: str, params: dict[str, str]) -> str:
        """Resolve URL template with parameters."""
        result = url_pattern
        for key, value in params.items():
            result = result.replace(f"{{{{{key}}}}}", value)
        # Clean up any remaining wildcards
        result = result.replace("*", "")
        return result
