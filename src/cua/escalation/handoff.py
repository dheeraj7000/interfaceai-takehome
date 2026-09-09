"""
Handoff Controller — manages the transfer of control between automation and human.

The control-transfer model:
1. Automation detects it's stuck or needs human help
2. Automation PAUSES — stops issuing actions to the surface
3. An intervention request is created with full context
4. The live browser session is exposed via noVNC for the human
5. session_controller flag flips from "automation" to "human"
6. Human operates the live session (same browser, same cookies, same state)
7. Human signals "resume" via the operator API
8. session_controller flag flips back to "automation"
9. Automation re-observes the current state and continues

Key invariant: automation NEVER acts while session_controller == "human".
This is the core safety property of the handoff model.

Evidence: the handoff window is logged — timestamps, who was in control,
and screenshots before/after the human's intervention.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable

from cua.escalation.detector import EscalationTrigger
from cua.schema.results import EscalationContext
from cua.surface.base import Surface

logger = logging.getLogger(__name__)


class SessionController(str, Enum):
    """Who currently has control of the live session."""
    AUTOMATION = "automation"
    HUMAN = "human"


@dataclass
class InterventionRequest:
    """A request for human intervention — what the operator sees."""
    id: str
    trigger: EscalationTrigger
    capability_name: str
    current_step_id: str | None
    current_step_description: str | None
    session_url: str             # noVNC URL for taking control
    screenshot_path: str | None
    current_url: str
    visible_text_snippet: str    # First 500 chars of visible text
    created_at: datetime = field(default_factory=datetime.utcnow)
    resolved_at: datetime | None = None
    resolution: str | None = None
    human_actions: list[str] = field(default_factory=list)


class HandoffController:
    """Manages automation ↔ human control transfers.

    Usage:
        controller = HandoffController(surface)

        # When stuck:
        request = await controller.initiate_handoff(trigger, context)

        # The controller pauses automation and waits...

        # Human operates the session via noVNC...

        # Human clicks "Resume" in operator UI:
        await controller.complete_handoff(request.id, resolution="Fixed the dialog")

        # Automation resumes with fresh state observation
    """

    def __init__(
        self,
        surface: Surface,
        novnc_url: str = "",
        on_request: Callable[[InterventionRequest], Any] | None = None,
    ):
        self._surface = surface
        self._novnc_url = novnc_url
        self._on_request = on_request  # Callback when intervention is requested
        self._controller = SessionController.AUTOMATION
        self._active_request: InterventionRequest | None = None
        self._resume_event: asyncio.Event = asyncio.Event()
        self._request_counter = 0

    @property
    def session_controller(self) -> SessionController:
        """Who currently has control."""
        return self._controller

    @property
    def active_request(self) -> InterventionRequest | None:
        """The current intervention request, if any."""
        return self._active_request

    @property
    def is_human_controlling(self) -> bool:
        return self._controller == SessionController.HUMAN

    async def initiate_handoff(
        self,
        trigger: EscalationTrigger,
        capability_name: str = "",
        step_id: str | None = None,
        step_description: str | None = None,
        evidence_dir: str | None = None,
    ) -> InterventionRequest:
        """Pause automation and request human intervention.

        1. Pauses the surface (stops automation actions)
        2. Captures current state as evidence
        3. Creates an intervention request
        4. Notifies the operator (via callback)
        5. Waits for resume signal

        Returns the completed InterventionRequest after the human finishes.
        """
        logger.info(f"Initiating handoff: {trigger.reason}")

        # 1. Pause surface
        session_info = await self._surface.pause()
        self._controller = SessionController.HUMAN
        self._resume_event.clear()

        # 2. Capture state
        screenshot_path = None
        current_url = ""
        visible_text = ""
        try:
            state = await self._surface.observe()
            current_url = state.url
            visible_text = state.visible_text[:500]
            if evidence_dir:
                screenshot_path = f"{evidence_dir}/screenshots/escalation_{trigger.trigger_type}.png"
                await self._surface.screenshot(screenshot_path)
        except Exception as e:
            logger.warning(f"Failed to capture state for escalation: {e}")

        # 3. Create intervention request
        self._request_counter += 1
        request = InterventionRequest(
            id=f"esc_{self._request_counter:04d}",
            trigger=trigger,
            capability_name=capability_name,
            current_step_id=step_id,
            current_step_description=step_description,
            session_url=self._novnc_url or session_info.get("novnc_url", ""),
            screenshot_path=screenshot_path,
            current_url=current_url,
            visible_text_snippet=visible_text,
        )
        self._active_request = request

        # 4. Notify operator
        if self._on_request:
            try:
                result = self._on_request(request)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"Escalation notification failed: {e}")

        logger.info(
            f"Handoff initiated. Request ID: {request.id}. "
            f"Session URL: {request.session_url}. "
            f"Waiting for human to resume..."
        )

        return request

    async def wait_for_resume(self, timeout_seconds: int = 3600) -> bool:
        """Wait for the human to signal resume.

        Returns True if resumed, False if timed out.
        """
        try:
            await asyncio.wait_for(
                self._resume_event.wait(),
                timeout=timeout_seconds,
            )
            return True
        except asyncio.TimeoutError:
            logger.warning(f"Handoff timed out after {timeout_seconds}s")
            return False

    async def complete_handoff(
        self,
        request_id: str,
        resolution: str = "",
        human_actions: list[str] | None = None,
    ) -> bool:
        """Signal that the human is done and automation should resume.

        Called by the operator API when the human clicks "Resume."
        """
        if not self._active_request or self._active_request.id != request_id:
            logger.warning(f"No active request with ID: {request_id}")
            return False

        # Record what the human did
        self._active_request.resolved_at = datetime.utcnow()
        self._active_request.resolution = resolution
        if human_actions:
            self._active_request.human_actions = human_actions

        # Take a screenshot of the state the human left
        try:
            await self._surface.screenshot()  # Evidence
        except Exception:
            pass

        # Resume automation
        await self._surface.resume()
        self._controller = SessionController.AUTOMATION
        self._resume_event.set()

        logger.info(
            f"Handoff complete. Request: {request_id}. "
            f"Resolution: {resolution}. "
            f"Control returned to automation."
        )

        return True

    def to_escalation_context(self) -> EscalationContext | None:
        """Convert the active request to a schema EscalationContext."""
        req = self._active_request
        if not req:
            return None

        return EscalationContext(
            reason=req.trigger.reason,
            capability_name=req.capability_name,
            current_step_id=req.current_step_id,
            current_step_description=req.current_step_description,
            session_url=req.session_url,
            screenshot_path=req.screenshot_path,
            observed_state=req.visible_text_snippet,
            escalated_at=req.created_at,
            human_action_log=req.human_actions,
            resolved_at=req.resolved_at,
            resolution=req.resolution,
        )
