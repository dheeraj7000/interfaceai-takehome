"""
FastAPI Server — agent-invocable replay, capabilities catalog, and operator console.

Endpoints:
  POST /replay              — Execute a capability with parameters (what an AI agent calls)
  GET  /capabilities        — List available capability artifacts
  GET  /capabilities/{name} — Get a specific capability's contract
  GET  /operator            — Mock operator console (human-in-the-loop UI)
  POST /operator/resume     — Resume automation after human intervention
  GET  /health              — Health check

This is the interface between the calling AI agent and the CUA system.
The agent discovers capabilities via GET /capabilities, then invokes them
via POST /replay with typed parameters.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from cua.config import ARTIFACTS_DIR, EVIDENCE_DIR, get_settings
from cua.escalation.handoff import HandoffController
from cua.escalation.operator_api import (
    render_intervention_page,
    render_no_requests_page,
    render_resumed_page,
)
from cua.replay.engine import ReplayEngine
from cua.safety.allowlist import Allowlist
from cua.safety.redactor import Redactor
from cua.schema.artifact import Capability
from cua.surface.browser import BrowserSurface

logger = logging.getLogger(__name__)

app = FastAPI(
    title="CUA — Computer-Use Automation",
    description="Agent-invocable replay of recorded capabilities against legacy banking applications",
    version="0.1.0",
)

# Shared state (initialized on startup)
_handoff_controller: HandoffController | None = None
_active_surface: BrowserSurface | None = None


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ReplayRequest(BaseModel):
    """Request to replay a capability."""
    artifact: str = Field(description="Capability name or path to artifact JSON")
    params: dict[str, str] = Field(
        default_factory=dict,
        description="Input parameters for the capability"
    )
    target_url: str | None = Field(
        default=None,
        description="Override target URL (uses artifact default otherwise)"
    )
    confirm_risky: bool = Field(
        default=False,
        description="If true, risky/irreversible actions are allowed without escalation"
    )


class CapabilitySummary(BaseModel):
    """Summary of a capability for the catalog."""
    name: str
    display_name: str
    description: str
    version: str
    inputs: list[dict[str, Any]]
    outputs: list[dict[str, Any]]
    tags: list[str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_capability(name_or_path: str) -> Capability:
    """Load a capability artifact by name or file path."""
    # Try as a direct file path first
    path = Path(name_or_path)
    if path.exists() and path.suffix == ".json":
        return Capability.from_file(str(path))

    # Try in the artifacts directory
    artifacts_path = ARTIFACTS_DIR / f"{name_or_path}.json"
    if artifacts_path.exists():
        return Capability.from_file(str(artifacts_path))

    # Try with the name as given
    artifacts_path = ARTIFACTS_DIR / name_or_path
    if artifacts_path.exists():
        return Capability.from_file(str(artifacts_path))

    raise FileNotFoundError(f"Capability not found: {name_or_path}")


def _list_capabilities() -> list[CapabilitySummary]:
    """List all available capability artifacts."""
    capabilities = []
    artifacts_dir = ARTIFACTS_DIR
    if not artifacts_dir.exists():
        return capabilities

    for path in sorted(artifacts_dir.glob("*.json")):
        try:
            cap = Capability.from_file(str(path))
            capabilities.append(CapabilitySummary(
                name=cap.metadata.name,
                display_name=cap.metadata.display_name,
                description=cap.metadata.description,
                version=cap.metadata.version,
                inputs=[
                    {
                        "name": inp.name,
                        "type": inp.type.value,
                        "required": inp.required,
                        "description": inp.description,
                    }
                    for inp in cap.inputs
                ],
                outputs=[
                    {
                        "name": out.name,
                        "type": out.type.value,
                        "description": out.description,
                    }
                    for out in cap.outputs
                ],
                tags=cap.metadata.tags,
            ))
        except Exception as e:
            logger.warning(f"Failed to load capability from {path}: {e}")

    return capabilities


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    """Health check."""
    return {"status": "ok", "version": "0.1.0"}


@app.get("/capabilities", response_model=list[CapabilitySummary])
async def list_capabilities():
    """List all available capability artifacts.

    An AI agent uses this to discover what capabilities are available
    and what parameters they need.
    """
    return _list_capabilities()


@app.get("/capabilities/{name}")
async def get_capability(name: str):
    """Get the full contract for a specific capability.

    Returns the complete artifact including steps, error handlers,
    and success conditions — everything an agent or human reviewer
    needs to understand the capability.
    """
    try:
        cap = _load_capability(name)
        return json.loads(cap.to_json())
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Capability not found: {name}")


@app.post("/replay")
async def replay_capability(request: ReplayRequest):
    """Execute a capability artifact with the given parameters.

    This is the primary endpoint an AI agent calls to invoke a capability.
    It runs the deterministic replay engine (no LLM in the loop) and
    returns a structured result.
    """
    global _handoff_controller, _active_surface

    # Load capability
    try:
        capability = _load_capability(request.artifact)
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"Capability not found: {request.artifact}"
        )

    settings = get_settings()

    # Set up surface
    surface = BrowserSurface(
        headless=True,
        novnc_url=settings.novnc_url,
    )
    _active_surface = surface

    # Set up safety
    allowlist = Allowlist()
    sensitive_params = {inp.name for inp in capability.inputs if inp.sensitive}
    redactor = Redactor(sensitive_param_names=sensitive_params)

    # Set up escalation
    _handoff_controller = HandoffController(
        surface=surface,
        novnc_url=settings.novnc_url,
    )

    # Set up evidence directory
    evidence_dir = str(EVIDENCE_DIR / "replay_runs")

    # Build engine
    engine = ReplayEngine(
        surface=surface,
        allowlist=allowlist,
        redactor=redactor,
        evidence_dir=evidence_dir,
        confirm_risky=request.confirm_risky,
    )

    # Determine target URL
    target_url = request.target_url or settings.target_app_url

    try:
        result = await engine.replay(
            capability=capability,
            params=request.params,
            target_url=target_url,
        )
        return json.loads(result.to_json())
    except Exception as e:
        logger.exception("Replay failed")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try:
            await surface.stop()
        except Exception:
            pass
        _active_surface = None


# ---------------------------------------------------------------------------
# Operator console (human-in-the-loop)
# ---------------------------------------------------------------------------

@app.get("/operator", response_class=HTMLResponse)
async def operator_console():
    """Mock operator console — shows active intervention requests."""
    global _handoff_controller

    if _handoff_controller and _handoff_controller.active_request:
        req = _handoff_controller.active_request
        return render_intervention_page(
            request_id=req.id,
            reason=req.trigger.reason,
            capability_name=req.capability_name,
            step_id=req.current_step_id,
            step_description=req.current_step_description,
            session_url=req.session_url,
            current_url=req.current_url,
            visible_text=req.visible_text_snippet,
            trigger_type=req.trigger.trigger_type,
        )

    return render_no_requests_page()


@app.post("/operator/resume", response_class=HTMLResponse)
async def operator_resume(
    request_id: str = Form(...),
    resolution: str = Form(""),
):
    """Resume automation after human intervention."""
    global _handoff_controller

    if not _handoff_controller:
        raise HTTPException(status_code=400, detail="No active handoff controller")

    success = await _handoff_controller.complete_handoff(
        request_id=request_id,
        resolution=resolution,
    )

    if not success:
        raise HTTPException(
            status_code=404,
            detail=f"No active request with ID: {request_id}"
        )

    return render_resumed_page(request_id, resolution)


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    """Ensure directories exist on startup."""
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("CUA API server started")


@app.on_event("shutdown")
async def shutdown():
    """Clean up on shutdown."""
    global _active_surface
    if _active_surface:
        try:
            await _active_surface.stop()
        except Exception:
            pass
    logger.info("CUA API server stopped")
