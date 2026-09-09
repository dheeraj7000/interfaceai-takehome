"""
Agent Discovery Loop — the LLM-driven observe → decide → act cycle.

This is the "model discovers" phase. The loop:
1. Observes the current surface state (a11y tree + screenshot)
2. Asks the LLM provider to decide the next action
3. Checks the action against safety guardrails
4. Executes the action on the surface
5. Records the step for artifact generation
6. Repeats until the goal is met or a stopping condition is hit

The loop itself is provider-agnostic and surface-agnostic — it orchestrates
the interaction between the LLM, the surface, and the safety layer.

Stopping conditions:
- Goal complete (LLM signals success)
- Max steps reached
- Stuck (LLM signals it can't proceed, after retries)
- Safety violation (action blocked by allowlist)
- Surface died (browser crashed, session expired)
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from cua.agent.providers.base import (
    AgentDecision,
    DecisionType,
    ExtractedData,
    LLMProvider,
)
from cua.agent.recorder import StepRecorder
from cua.safety.allowlist import Allowlist
from cua.safety.classifier import classify_action
from cua.schema.artifact import ActionType, RiskLevel
from cua.surface.base import Surface, SurfaceState

logger = logging.getLogger(__name__)


class DiscoveryResult:
    """Result of a discovery run."""

    def __init__(self):
        self.success: bool = False
        self.goal: str = ""
        self.steps_taken: int = 0
        self.actions_log: list[str] = []
        self.extracted_data: list[ExtractedData] = []
        self.artifact_path: str | None = None
        self.evidence_dir: str | None = None
        self.error: str | None = None
        self.stop_reason: str = ""
        self.started_at: datetime = datetime.utcnow()
        self.completed_at: datetime | None = None
        self.screenshots: list[str] = []


class AgentLoop:
    """The core discovery loop — drives an LLM to accomplish a goal on a live surface."""

    def __init__(
        self,
        provider: LLMProvider,
        surface: Surface,
        allowlist: Allowlist,
        max_steps: int = 25,
        evidence_dir: str = "",
        stuck_threshold: int = 3,
    ):
        self._provider = provider
        self._surface = surface
        self._allowlist = allowlist
        self._max_steps = max_steps
        self._evidence_dir = evidence_dir
        self._stuck_threshold = stuck_threshold
        self._recorder = StepRecorder()

    async def run(self, goal: str, target_url: str) -> DiscoveryResult:
        """Execute the discovery loop.

        Args:
            goal: Natural-language goal to accomplish
            target_url: Entry point URL for the target application

        Returns:
            DiscoveryResult with success status, recorded steps, and evidence
        """
        result = DiscoveryResult()
        result.goal = goal

        # Set up evidence directory
        if self._evidence_dir:
            evidence_path = Path(self._evidence_dir)
            evidence_path.mkdir(parents=True, exist_ok=True)
            screenshots_path = evidence_path / "screenshots"
            screenshots_path.mkdir(exist_ok=True)
            result.evidence_dir = str(evidence_path)

        logger.info(f"Starting discovery: goal='{goal}', target='{target_url}'")
        logger.info(f"Using model: {self._provider.model_name}")

        # Check URL against allowlist
        url_check = self._allowlist.check_url(target_url)
        if not url_check.allowed:
            result.error = f"Target URL blocked by policy: {url_check.reason}"
            result.stop_reason = "policy_violation"
            logger.error(result.error)
            return result

        # Start the surface
        try:
            await self._surface.start(target_url)
        except Exception as e:
            result.error = f"Failed to start surface: {e}"
            result.stop_reason = "surface_error"
            logger.error(result.error)
            return result

        actions_log: list[str] = []
        stuck_count = 0

        try:
            for step_num in range(1, self._max_steps + 1):
                logger.info(f"--- Step {step_num}/{self._max_steps} ---")

                # 1. OBSERVE
                state = await self._observe_with_evidence(step_num, result)

                if not await self._surface.is_alive():
                    result.error = "Surface session died"
                    result.stop_reason = "surface_died"
                    break

                # 2. DECIDE
                decision = await self._provider.decide(
                    goal=goal,
                    surface_state_text=state.accessibility_tree_text,
                    screenshot_base64=state.screenshot_base64,
                    step_number=step_num,
                    max_steps=self._max_steps,
                    history=[],
                    previous_actions=actions_log,
                )

                logger.info(
                    f"Decision: {decision.decision_type.value} | "
                    f"Reasoning: {decision.reasoning[:120]}..."
                )

                # 3. CHECK STOPPING CONDITIONS
                if decision.decision_type == DecisionType.GOAL_COMPLETE:
                    logger.info("Goal complete!")
                    result.success = True
                    result.stop_reason = "goal_complete"
                    result.extracted_data.extend(decision.extracted_data)
                    # Record a final extract step if there's data
                    if decision.extracted_data:
                        self._recorder.record_extract(
                            step_num, decision.extracted_data, state
                        )
                    break

                if decision.decision_type == DecisionType.GOAL_FAILED:
                    result.error = f"Agent determined goal cannot be achieved: {decision.reasoning}"
                    result.stop_reason = "goal_failed"
                    break

                if decision.decision_type == DecisionType.STUCK:
                    stuck_count += 1
                    logger.warning(
                        f"Agent stuck ({stuck_count}/{self._stuck_threshold}): {decision.reasoning}"
                    )
                    if stuck_count >= self._stuck_threshold:
                        result.error = f"Agent stuck {stuck_count} times, giving up"
                        result.stop_reason = "stuck"
                        break
                    # Wait a bit and retry
                    await asyncio.sleep(1)
                    continue

                if decision.decision_type != DecisionType.ACTION or not decision.action:
                    logger.warning(f"Unexpected decision type: {decision.decision_type}")
                    continue

                # Reset stuck counter on successful action decision
                stuck_count = 0
                action = decision.action

                # 4. SAFETY CHECK
                action_type = self._parse_action_type(action.action_type)
                risk_level = classify_action(
                    action_type,
                    value=action.value,
                    current_url=state.url,
                )

                safety_check = self._allowlist.check_action(
                    url=state.url,
                    action=action_type,
                    risk_level=risk_level,
                    # During discovery, auto-confirm moderate actions,
                    # block risky ones
                    confirmation_provided=(risk_level == RiskLevel.MODERATE),
                )

                if not safety_check.allowed:
                    if safety_check.requires_confirmation:
                        logger.warning(
                            f"Risky action blocked during discovery: {action.action_type} "
                            f"({safety_check.reason}). Skipping."
                        )
                        actions_log.append(
                            f"[BLOCKED] {action.action_type} on {action.target_semantic} "
                            f"— {safety_check.reason}"
                        )
                    else:
                        logger.error(f"Action blocked by policy: {safety_check.reason}")
                        actions_log.append(f"[POLICY VIOLATION] {safety_check.reason}")
                    continue

                # 5. ACT
                action_desc = await self._execute_action(action, state)
                actions_log.append(action_desc)
                result.steps_taken = step_num

                # 6. RECORD
                self._recorder.record_step(
                    step_num=step_num,
                    action=action,
                    state=state,
                    risk_level=risk_level,
                )

                # Collect any extracted data from this step
                if decision.extracted_data:
                    result.extracted_data.extend(decision.extracted_data)

                # Brief pause to let the surface settle
                await asyncio.sleep(0.5)

            else:
                # Loop exhausted without goal completion
                result.error = f"Max steps ({self._max_steps}) reached without completing goal"
                result.stop_reason = "max_steps"

        except Exception as e:
            result.error = f"Unexpected error during discovery: {e}"
            result.stop_reason = "error"
            logger.exception("Discovery loop error")

        finally:
            # Take final screenshot
            await self._observe_with_evidence(999, result, label="final")

        result.actions_log = actions_log
        result.completed_at = datetime.utcnow()

        logger.info(
            f"Discovery finished: success={result.success}, "
            f"steps={result.steps_taken}, stop_reason={result.stop_reason}"
        )

        return result

    async def _observe_with_evidence(
        self,
        step_num: int,
        result: DiscoveryResult,
        label: str = "",
    ) -> SurfaceState:
        """Observe the surface and save evidence screenshots."""
        state = await self._surface.observe()

        if self._evidence_dir:
            fname = f"step_{step_num:03d}" if not label else f"{label}"
            screenshot_path = str(Path(self._evidence_dir) / "screenshots" / f"{fname}.png")
            try:
                await self._surface.screenshot(screenshot_path)
                result.screenshots.append(screenshot_path)
            except Exception as e:
                logger.debug(f"Failed to save screenshot: {e}")

        return state

    async def _execute_action(self, action: Any, state: SurfaceState) -> str:
        """Execute an action on the surface and return a log description."""
        a = action
        desc = f"{a.action_type}"

        try:
            if a.action_type == "navigate":
                await self._surface.navigate(a.value)
                desc = f"navigate to {a.value}"

            elif a.action_type == "click":
                await self._surface.click(a.target_strategy, a.target_value)
                desc = f"click on {a.target_semantic} [{a.target_strategy}={a.target_value}]"

            elif a.action_type == "type":
                await self._surface.type_text(a.target_strategy, a.target_value, a.value)
                desc = f"type '{a.value}' into {a.target_semantic}"

            elif a.action_type == "select":
                await self._surface.select_option(a.target_strategy, a.target_value, a.value)
                desc = f"select '{a.value}' in {a.target_semantic}"

            elif a.action_type == "clear":
                await self._surface.clear(a.target_strategy, a.target_value)
                desc = f"clear {a.target_semantic}"

            elif a.action_type == "press_key":
                await self._surface.press_key(a.value)
                desc = f"press key '{a.value}'"

            elif a.action_type == "hover":
                await self._surface.hover(a.target_strategy, a.target_value)
                desc = f"hover over {a.target_semantic}"

            elif a.action_type == "scroll":
                await self._surface.scroll(a.value or "down")
                desc = f"scroll {a.value or 'down'}"

            elif a.action_type == "wait":
                await asyncio.sleep(2)
                desc = "wait 2 seconds"

            elif a.action_type == "extract":
                desc = f"extract data from {a.target_semantic}"

            elif a.action_type == "screenshot":
                desc = "capture screenshot"

            else:
                logger.warning(f"Unknown action type: {a.action_type}")
                desc = f"unknown action: {a.action_type}"

            logger.info(f"Executed: {desc}")

        except Exception as e:
            desc = f"FAILED: {a.action_type} on {a.target_semantic} — {e}"
            logger.error(desc)

        return desc

    @staticmethod
    def _parse_action_type(action_str: str) -> ActionType:
        """Parse an action type string into the ActionType enum."""
        try:
            return ActionType(action_str.lower())
        except ValueError:
            return ActionType.WAIT  # Safe default
