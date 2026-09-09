"""
Smart Waiter — polls for expected state instead of using fixed sleeps.

Each step defines what "ready" looks like via its checkpoint conditions.
The waiter polls the surface until the condition is met or the timeout
is reached. This eliminates flaky fixed-delay waits while still handling
the variable latency of legacy enterprise apps.

Polling strategy:
- Start with short intervals (100ms)
- Back off to longer intervals if the condition isn't met quickly
- Cap at the step's timeout
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

from cua.schema.artifact import Checkpoint, CheckpointType
from cua.surface.base import Surface

logger = logging.getLogger(__name__)

# Polling intervals: start fast, back off
POLL_INTERVALS_MS = [100, 200, 300, 500, 500, 1000, 1000, 1000, 2000, 2000]


class SmartWaiter:
    """Waits for checkpoint conditions using adaptive polling."""

    def __init__(self, surface: Surface):
        self._surface = surface

    async def wait_for_checkpoint(
        self,
        checkpoint: Checkpoint,
        params: dict[str, str] | None = None,
        timeout_ms: int = 10000,
    ) -> bool:
        """Wait for a checkpoint condition to become true.

        Args:
            checkpoint: The condition to wait for
            params: Template parameter values for resolving expected values
            timeout_ms: Maximum wait time in milliseconds

        Returns:
            True if the condition was met within the timeout
        """
        params = params or {}
        expected = self._resolve_template(checkpoint.expected_value or "", params)
        deadline = time.time() + (timeout_ms / 1000)
        poll_idx = 0

        while time.time() < deadline:
            met = await self._check(checkpoint.type, expected, checkpoint)
            if met:
                logger.debug(f"Checkpoint met: {checkpoint.description}")
                return True

            # Adaptive polling delay
            delay = POLL_INTERVALS_MS[min(poll_idx, len(POLL_INTERVALS_MS) - 1)]
            await asyncio.sleep(delay / 1000)
            poll_idx += 1

        logger.warning(
            f"Checkpoint timed out after {timeout_ms}ms: {checkpoint.description}"
        )
        return False

    async def _check(
        self,
        check_type: CheckpointType,
        expected: str,
        checkpoint: Checkpoint,
    ) -> bool:
        """Check a single condition."""
        try:
            if check_type == CheckpointType.ELEMENT_VISIBLE:
                if checkpoint.locator:
                    return await self._surface.element_exists(
                        checkpoint.locator.strategy.value, checkpoint.locator.value
                    )

            elif check_type == CheckpointType.ELEMENT_NOT_VISIBLE:
                if checkpoint.locator:
                    exists = await self._surface.element_exists(
                        checkpoint.locator.strategy.value, checkpoint.locator.value
                    )
                    return not exists

            elif check_type == CheckpointType.TEXT_PRESENT:
                if expected:
                    text = await self._surface.get_visible_text()
                    return expected.lower() in text.lower()

            elif check_type == CheckpointType.TEXT_NOT_PRESENT:
                if expected:
                    text = await self._surface.get_visible_text()
                    return expected.lower() not in text.lower()

            elif check_type == CheckpointType.URL_MATCHES:
                state = await self._surface.observe()
                if expected:
                    pattern = re.escape(expected).replace(r"\*", ".*").replace(r"\{\{", "{{").replace(r"\}\}", "}}")
                    return bool(re.search(pattern, state.url))

            elif check_type == CheckpointType.ELEMENT_HAS_VALUE:
                if checkpoint.locator and expected:
                    text = await self._surface.get_element_text(
                        checkpoint.locator.strategy.value, checkpoint.locator.value
                    )
                    return text == expected if text else False

            elif check_type == CheckpointType.PAGE_TITLE_MATCHES:
                state = await self._surface.observe()
                return expected.lower() in state.page_title.lower() if expected else True

            elif check_type == CheckpointType.ELEMENT_COUNT:
                if checkpoint.locator and expected:
                    elements = await self._surface.find_elements(
                        checkpoint.locator.strategy.value, checkpoint.locator.value
                    )
                    return len(elements) == int(expected)

        except Exception as e:
            logger.debug(f"Checkpoint check error: {e}")

        return False

    @staticmethod
    def _resolve_template(template: str, params: dict[str, str]) -> str:
        """Resolve {{param_name}} template variables."""
        result = template
        for key, value in params.items():
            result = result.replace(f"{{{{{key}}}}}", value)
        return result
