"""
Structured Run Logger — JSON-based logging for discovery and replay runs.

Every run (discovery or replay) produces a structured log file that captures:
- Run metadata (id, goal, start/end times, status)
- Per-step entries (what happened, timing, errors)
- Evidence pointers (screenshot paths, DOM snapshots)

The log is designed for both human debugging and programmatic analysis.
Sensitive data is redacted before writing.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from cua.safety.redactor import Redactor

logger = logging.getLogger(__name__)


class RunLogger:
    """Structured logger for a single automation run."""

    def __init__(
        self,
        run_id: str,
        run_type: str,  # "discovery" or "replay"
        output_dir: str | None = None,
        redactor: Redactor | None = None,
    ):
        self._run_id = run_id
        self._run_type = run_type
        self._output_dir = output_dir
        self._redactor = redactor or Redactor()
        self._entries: list[dict[str, Any]] = []
        self._metadata: dict[str, Any] = {
            "run_id": run_id,
            "run_type": run_type,
            "started_at": datetime.utcnow().isoformat(),
        }

    def set_metadata(self, **kwargs: Any) -> None:
        """Set run-level metadata."""
        self._metadata.update(kwargs)

    def log_step(
        self,
        step_id: str,
        action: str,
        status: str,
        details: str = "",
        duration_ms: int | None = None,
        error: str | None = None,
        screenshot_path: str | None = None,
        **extra: Any,
    ) -> None:
        """Log a single step execution."""
        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "step_id": step_id,
            "action": action,
            "status": status,
            "details": details,
        }
        if duration_ms is not None:
            entry["duration_ms"] = duration_ms
        if error:
            entry["error"] = error
        if screenshot_path:
            entry["screenshot"] = screenshot_path
        entry.update(extra)

        # Redact sensitive data
        entry = self._redactor.redact_log_entry(entry)
        self._entries.append(entry)

        # Also log to standard Python logging
        level = logging.ERROR if status == "failed" else logging.INFO
        logger.log(level, f"[{self._run_id}] {step_id}: {action} -> {status}")

    def log_event(self, event: str, details: str = "", **extra: Any) -> None:
        """Log a non-step event (escalation, policy check, etc.)."""
        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "event": event,
            "details": details,
        }
        entry.update(extra)
        entry = self._redactor.redact_log_entry(entry)
        self._entries.append(entry)
        logger.info(f"[{self._run_id}] {event}: {details}")

    def finalize(self, status: str, message: str = "") -> dict[str, Any]:
        """Finalize the run log and optionally write to disk.

        Returns the complete log as a dict.
        """
        self._metadata["completed_at"] = datetime.utcnow().isoformat()
        self._metadata["status"] = status
        self._metadata["message"] = message
        self._metadata["total_steps"] = len([
            e for e in self._entries if "step_id" in e
        ])

        log_data = {
            "metadata": self._metadata,
            "entries": self._entries,
        }

        # Write to disk
        if self._output_dir:
            self._write(log_data)

        return log_data

    def _write(self, log_data: dict) -> None:
        """Write the log to a JSON file."""
        output_path = Path(self._output_dir)  # type: ignore
        output_path.mkdir(parents=True, exist_ok=True)
        filepath = output_path / f"{self._run_type}_{self._run_id}.json"
        filepath.write_text(json.dumps(log_data, indent=2, default=str))
        logger.info(f"Run log saved: {filepath}")
