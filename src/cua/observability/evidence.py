"""
Evidence Capture — screenshots, DOM snapshots, and a11y tree dumps.

Evidence is captured at key moments:
- Every step during discovery (screenshots)
- On failure/error during replay (screenshot + state summary)
- Before and after human-in-the-loop interventions
- Final state after run completion

Evidence is saved to the run's evidence directory and referenced
from the structured run log via file paths.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from cua.surface.base import Surface, SurfaceState

logger = logging.getLogger(__name__)


class EvidenceCapture:
    """Captures and saves evidence from the surface."""

    def __init__(self, evidence_dir: str, max_screenshots: int = 50):
        self._dir = Path(evidence_dir)
        self._screenshots_dir = self._dir / "screenshots"
        self._snapshots_dir = self._dir / "snapshots"
        self._max_screenshots = max_screenshots
        self._screenshot_count = 0

        # Create directories
        self._dir.mkdir(parents=True, exist_ok=True)
        self._screenshots_dir.mkdir(exist_ok=True)
        self._snapshots_dir.mkdir(exist_ok=True)

    async def capture_screenshot(
        self,
        surface: Surface,
        label: str,
    ) -> str | None:
        """Capture and save a screenshot. Returns the file path."""
        if self._screenshot_count >= self._max_screenshots:
            logger.debug("Max screenshots reached, skipping")
            return None

        filename = f"{label}.png"
        filepath = str(self._screenshots_dir / filename)

        try:
            await surface.screenshot(filepath)
            self._screenshot_count += 1
            return filepath
        except Exception as e:
            logger.debug(f"Screenshot capture failed: {e}")
            return None

    async def capture_state_snapshot(
        self,
        surface: Surface,
        label: str,
    ) -> str | None:
        """Capture a full state snapshot (a11y tree + visible text + URL)."""
        try:
            state = await surface.observe()
            snapshot = {
                "timestamp": datetime.utcnow().isoformat(),
                "label": label,
                "url": state.url,
                "page_title": state.page_title,
                "visible_text": state.visible_text[:2000],
                "accessibility_tree": state.accessibility_tree_text[:5000],
            }

            filename = f"{label}.json"
            filepath = self._snapshots_dir / filename
            filepath.write_text(json.dumps(snapshot, indent=2))
            return str(filepath)
        except Exception as e:
            logger.debug(f"State snapshot failed: {e}")
            return None

    async def capture_failure_evidence(
        self,
        surface: Surface,
        step_id: str,
        error: str,
    ) -> dict[str, str | None]:
        """Capture comprehensive evidence on failure."""
        label = f"failure_{step_id}"
        screenshot = await self.capture_screenshot(surface, label)
        snapshot = await self.capture_state_snapshot(surface, label)
        return {
            "screenshot": screenshot,
            "snapshot": snapshot,
        }

    @property
    def evidence_dir(self) -> str:
        return str(self._dir)
