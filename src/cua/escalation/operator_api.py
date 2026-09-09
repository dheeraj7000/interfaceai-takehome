"""
Mock Operator Surface — minimal UI for human-in-the-loop intervention.

This is intentionally minimal: a simple HTML page that shows the intervention
request context and provides a "Resume" button. The real operator console
would be a full application with live session viewing, action logging, and
team routing — but that's out of scope per the assignment.

What IS real here:
- The handoff mechanism (pause/resume/control transfer)
- The intervention request with full context
- The ability to resume and hand control back

What's mocked:
- The operator UI (just a basic HTML page)
- Live session viewing (we show the noVNC URL as a link)
- Action logging during human control (we capture before/after screenshots)

The operator endpoints are mounted on the main FastAPI server.
"""

from __future__ import annotations

HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<title>CUA Operator Console</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }}
.header {{ background: #1a1a2e; color: white; padding: 16px 24px; margin: -20px -20px 20px; }}
.header h1 {{ margin: 0; font-size: 18px; }}
.card {{ background: white; border-radius: 8px; padding: 20px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
.card h2 {{ margin-top: 0; color: #1a1a2e; font-size: 16px; }}
.label {{ font-weight: 600; color: #555; display: inline-block; width: 140px; }}
.value {{ color: #111; }}
.alert {{ background: #fff3cd; border: 1px solid #ffc107; border-radius: 6px; padding: 12px 16px; margin-bottom: 16px; }}
.alert.error {{ background: #f8d7da; border-color: #f5c6cb; }}
.btn {{ display: inline-block; padding: 10px 24px; border-radius: 6px; font-size: 14px; font-weight: 600; cursor: pointer; border: none; text-decoration: none; }}
.btn-primary {{ background: #28a745; color: white; }}
.btn-primary:hover {{ background: #218838; }}
.btn-vnc {{ background: #007bff; color: white; }}
.btn-vnc:hover {{ background: #0069d9; }}
.text-snippet {{ background: #f8f9fa; border: 1px solid #dee2e6; border-radius: 4px; padding: 12px; font-family: monospace; font-size: 12px; white-space: pre-wrap; max-height: 200px; overflow-y: auto; }}
.status {{ display: inline-block; padding: 4px 10px; border-radius: 12px; font-size: 12px; font-weight: 600; }}
.status.human {{ background: #ffc107; color: #111; }}
.status.automation {{ background: #28a745; color: white; }}
#resolution {{ width: 100%; padding: 8px; border: 1px solid #ccc; border-radius: 4px; margin-top: 8px; }}
.actions {{ margin-top: 16px; }}
.no-requests {{ color: #888; text-align: center; padding: 40px; }}
</style>
</head>
<body>
<div class="header">
    <h1>🖥️ CUA Operator Console</h1>
</div>

{content}

</body>
</html>
"""

NO_REQUESTS_CONTENT = """
<div class="card">
    <div class="no-requests">
        <h2>No Active Intervention Requests</h2>
        <p>The automation system is running normally. You'll see requests here when the system needs human help.</p>
        <p><span class="status automation">● Automation in control</span></p>
    </div>
</div>
"""


def render_intervention_page(
    request_id: str,
    reason: str,
    capability_name: str,
    step_id: str | None,
    step_description: str | None,
    session_url: str,
    current_url: str,
    visible_text: str,
    trigger_type: str,
) -> str:
    """Render the operator intervention page HTML."""
    content = f"""
<div class="alert">
    ⚠️ <strong>Intervention Required</strong> — The automation needs your help.
    <span class="status human">● Human in control</span>
</div>

<div class="card">
    <h2>Intervention Request: {request_id}</h2>
    <p><span class="label">Reason:</span> <span class="value">{reason}</span></p>
    <p><span class="label">Trigger Type:</span> <span class="value">{trigger_type}</span></p>
    <p><span class="label">Capability:</span> <span class="value">{capability_name}</span></p>
    <p><span class="label">Current Step:</span> <span class="value">{step_id or 'N/A'} — {step_description or 'N/A'}</span></p>
    <p><span class="label">Current URL:</span> <span class="value">{current_url}</span></p>
</div>

<div class="card">
    <h2>Current Screen Content</h2>
    <div class="text-snippet">{visible_text}</div>
</div>

<div class="card">
    <h2>Take Control</h2>
    <p>Use the link below to access the live browser session. Perform the needed manual steps, then come back here and click Resume.</p>
    <p>
        <a href="{session_url}" target="_blank" class="btn btn-vnc">
            🖥️ Open Live Session (noVNC)
        </a>
    </p>
</div>

<div class="card">
    <h2>Resume Automation</h2>
    <p>After you've completed the manual steps, describe what you did and click Resume.</p>
    <form method="POST" action="/operator/resume">
        <input type="hidden" name="request_id" value="{request_id}">
        <label for="resolution"><strong>What did you do?</strong></label>
        <textarea id="resolution" name="resolution" rows="3" placeholder="e.g. Dismissed the session timeout dialog and re-entered the search"></textarea>
        <div class="actions">
            <button type="submit" class="btn btn-primary">✓ Resume Automation</button>
        </div>
    </form>
</div>
"""
    return HTML_TEMPLATE.format(content=content)


def render_no_requests_page() -> str:
    """Render the page when there are no active requests."""
    return HTML_TEMPLATE.format(content=NO_REQUESTS_CONTENT)


def render_resumed_page(request_id: str, resolution: str) -> str:
    """Render the confirmation page after resuming."""
    content = f"""
<div class="card" style="border-left: 4px solid #28a745;">
    <h2>✓ Automation Resumed</h2>
    <p><span class="label">Request:</span> <span class="value">{request_id}</span></p>
    <p><span class="label">Resolution:</span> <span class="value">{resolution or '(none provided)'}</span></p>
    <p><span class="status automation">● Automation in control</span></p>
    <br>
    <a href="/operator" class="btn btn-primary">Back to Console</a>
</div>
"""
    return HTML_TEMPLATE.format(content=content)
