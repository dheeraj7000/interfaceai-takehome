"""
Active Error Scanner — detects error states on the surface after each action.

Instead of blindly proceeding after an action, the scanner checks the current
surface state against known error patterns. This catches:

- Business outcomes: "Member not found", "Insufficient funds"
- Recoverable conditions: Session timeout dialogs, loading spinners
- Hard failures: Server errors, permission denied

The scanner runs:
1. Global error handlers (checked at every step, highest priority first)
2. Step-level error matchers (checked after that specific step's action)

This active scanning is what makes replay robust against the runtime errors
that legitimately occur in enterprise banking apps.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from cua.schema.artifact import ErrorMatcher, GlobalErrorHandler
from cua.surface.base import Surface

logger = logging.getLogger(__name__)


class ErrorScanner:
    """Scans the surface for known error patterns."""

    async def scan_global(
        self,
        surface: Surface,
        handlers: list[GlobalErrorHandler],
    ) -> dict[str, Any] | None:
        """Scan for global error patterns.

        Checks all global handlers against the current surface state,
        ordered by priority (highest first).

        Returns:
            A dict with match details if an error is found, None otherwise.
        """
        if not handlers:
            return None

        # Get current visible text
        try:
            visible_text = await surface.get_visible_text()
        except Exception as e:
            logger.debug(f"Failed to get visible text for error scan: {e}")
            return None

        if not visible_text:
            return None

        # Check handlers in priority order
        sorted_handlers = sorted(handlers, key=lambda h: h.priority, reverse=True)

        for handler in sorted_handlers:
            match = self._check_pattern(visible_text, handler.matcher)
            if match:
                logger.info(
                    f"Global error matched: {handler.name} | "
                    f"Category: {handler.matcher.category.value} | "
                    f"Text: {match[:100]}"
                )
                return {
                    "handler_name": handler.name,
                    "pattern": handler.matcher.pattern,
                    "category": handler.matcher.category.value,
                    "outcome_code": handler.matcher.outcome_code,
                    "recovery_action": handler.matcher.recovery_action.value if handler.matcher.recovery_action else None,
                    "recovery_locator": (
                        {
                            "strategy": handler.matcher.recovery_locator.strategy.value,
                            "value": handler.matcher.recovery_locator.value,
                        }
                        if handler.matcher.recovery_locator
                        else None
                    ),
                    "matched_text": match,
                    "message": handler.matcher.message_template or handler.description,
                }

        return None

    async def scan_pattern(
        self,
        surface: Surface,
        matcher: ErrorMatcher,
    ) -> dict[str, Any] | None:
        """Scan for a specific error pattern.

        Returns:
            A dict with match details if found, None otherwise.
        """
        try:
            visible_text = await surface.get_visible_text()
        except Exception:
            return None

        if not visible_text:
            return None

        match = self._check_pattern(visible_text, matcher)
        if match:
            logger.info(
                f"Error pattern matched: {matcher.pattern} | "
                f"Category: {matcher.category.value}"
            )
            return {
                "pattern": matcher.pattern,
                "category": matcher.category.value,
                "outcome_code": matcher.outcome_code,
                "recovery_action": matcher.recovery_action.value if matcher.recovery_action else None,
                "recovery_locator": (
                    {
                        "strategy": matcher.recovery_locator.strategy.value,
                        "value": matcher.recovery_locator.value,
                    }
                    if matcher.recovery_locator
                    else None
                ),
                "matched_text": match,
                "message": matcher.message_template or "",
            }

        return None

    @staticmethod
    def _check_pattern(text: str, matcher: ErrorMatcher) -> str | None:
        """Check if a pattern matches the given text.

        Returns the matched text if found, None otherwise.
        """
        if matcher.is_regex:
            match = re.search(matcher.pattern, text, re.IGNORECASE | re.MULTILINE)
            if match:
                return match.group(0)
        else:
            if matcher.pattern.lower() in text.lower():
                # Find the surrounding context
                idx = text.lower().index(matcher.pattern.lower())
                start = max(0, idx - 20)
                end = min(len(text), idx + len(matcher.pattern) + 50)
                return text[start:end].strip()

        return None
