"""
CLI — the user-facing entry point for the CUA system.

Commands:
  cua discover  — Run the LLM-driven discovery agent on a goal
  cua replay    — Replay a saved capability artifact deterministically
  cua serve     — Start the FastAPI server for agent-invocable replay
  cua list      — List available capability artifacts

Usage:
  python -m cua discover --goal "Look up member M1001 and read their savings balance" --target http://localhost:5000
  python -m cua replay --artifact artifacts/lookup_balance.json --params '{"member_id": "M1001"}'
  python -m cua serve --port 8000
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from cua.config import ARTIFACTS_DIR, EVIDENCE_DIR, get_settings

console = Console()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


@click.group()
@click.version_option(version="0.1.0", prog_name="cua")
def main():
    """CUA — Computer-Use Automation System.

    Record-once, replay-many automation for legacy banking applications.
    """
    pass


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------

@main.command()
@click.option("--goal", "-g", required=True, help="Natural-language goal to accomplish")
@click.option("--target", "-t", required=True, help="Target application URL")
@click.option("--output", "-o", default="", help="Output path for the artifact JSON")
@click.option("--max-steps", default=25, help="Maximum steps for the discovery loop")
@click.option("--headless/--headed", default=True, help="Run browser headless or visible")
@click.option("--evidence-dir", default="", help="Directory to save evidence (screenshots, logs)")
def discover(goal: str, target: str, output: str, max_steps: int, headless: bool, evidence_dir: str):
    """Run LLM-driven discovery to accomplish a goal and record the result as an artifact."""
    asyncio.run(_discover(goal, target, output, max_steps, headless, evidence_dir))


async def _discover(
    goal: str, target: str, output: str, max_steps: int, headless: bool, evidence_dir: str
):
    settings = get_settings()

    console.print(Panel(
        f"[bold]Goal:[/bold] {goal}\n"
        f"[bold]Target:[/bold] {target}\n"
        f"[bold]Provider:[/bold] {settings.llm_provider} ({settings.active_model})\n"
        f"[bold]Max steps:[/bold] {max_steps}",
        title="🔍 CUA Discovery",
        border_style="blue",
    ))

    # Validate API key
    api_key = (
        settings.anthropic_api_key if settings.llm_provider == "anthropic"
        else settings.openai_api_key
    )
    if not api_key:
        console.print(f"[red]Error: No API key configured for {settings.llm_provider}.[/red]")
        console.print(f"Set {settings.llm_provider.upper()}_API_KEY in .env or environment.")
        sys.exit(1)

    # Set up evidence directory
    if not evidence_dir:
        import uuid
        run_id = str(uuid.uuid4())[:8]
        evidence_dir = str(EVIDENCE_DIR / f"discovery_{run_id}")

    # Create components
    from cua.agent.providers import create_provider
    from cua.agent.loop import AgentLoop
    from cua.safety.allowlist import Allowlist
    from cua.surface.browser import BrowserSurface

    provider = create_provider(settings.llm_provider, api_key, settings.active_model)
    surface = BrowserSurface(headless=headless)
    allowlist = Allowlist()

    agent = AgentLoop(
        provider=provider,
        surface=surface,
        allowlist=allowlist,
        max_steps=max_steps,
        evidence_dir=evidence_dir,
    )

    # Run discovery
    console.print("\n[yellow]Starting discovery loop...[/yellow]\n")
    result = agent_result = await agent.run(goal, target)

    # Generate artifact
    if result.success:
        console.print(f"\n[green]✓ Goal complete![/green] Steps: {result.steps_taken}")

        # Build artifact from recorded steps
        recorder = agent._recorder

        # Ask LLM for metadata (if the run was successful)
        metadata = {}
        try:
            metadata = await provider.generate_artifact_metadata(
                goal=goal,
                actions_taken=result.actions_log,
                extracted_data=result.extracted_data,
            )
        except Exception as e:
            console.print(f"[yellow]Warning: Could not generate artifact metadata: {e}[/yellow]")

        artifact = recorder.build_artifact(
            goal=goal,
            target_url=target,
            metadata_overrides=metadata,
        )

        # Save artifact
        if not output:
            ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
            output = str(ARTIFACTS_DIR / f"{artifact.metadata.name}.json")

        artifact.save(output)
        console.print(f"[green]Artifact saved:[/green] {output}")

        # Show summary
        table = Table(title="Capability Artifact")
        table.add_column("Field", style="bold")
        table.add_column("Value")
        table.add_row("Name", artifact.metadata.name)
        table.add_row("Display Name", artifact.metadata.display_name)
        table.add_row("Steps", str(len(artifact.steps)))
        table.add_row("Inputs", str(len(artifact.inputs)))
        table.add_row("Outputs", str(len(artifact.outputs)))
        table.add_row("File", output)
        console.print(table)

    else:
        console.print(f"\n[red]✗ Discovery failed:[/red] {result.error}")
        console.print(f"  Stop reason: {result.stop_reason}")
        console.print(f"  Steps taken: {result.steps_taken}")

    if result.evidence_dir:
        console.print(f"\n[dim]Evidence saved: {result.evidence_dir}[/dim]")

    # Save discovery log
    log_path = Path(evidence_dir) / "discovery_log.json"
    log_data = {
        "goal": goal,
        "target": target,
        "success": result.success,
        "steps_taken": result.steps_taken,
        "stop_reason": result.stop_reason,
        "error": result.error,
        "actions": result.actions_log,
        "extracted_data": [
            {"name": d.name, "value": d.value, "source": d.source}
            for d in result.extracted_data
        ],
    }
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps(log_data, indent=2, default=str))

    # Clean up
    await surface.stop()


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------

@main.command()
@click.option("--artifact", "-a", required=True, help="Path to the capability artifact JSON")
@click.option("--params", "-p", default="{}", help="JSON string of input parameters")
@click.option("--target", "-t", default="", help="Override target URL")
@click.option("--headless/--headed", default=True, help="Run browser headless or visible")
@click.option("--confirm-risky", is_flag=True, help="Allow risky actions without escalation")
@click.option("--evidence-dir", default="", help="Directory to save evidence")
def replay(artifact: str, params: str, target: str, headless: bool, confirm_risky: bool, evidence_dir: str):
    """Replay a saved capability artifact deterministically (no LLM)."""
    try:
        params_dict = json.loads(params)
    except json.JSONDecodeError as e:
        console.print(f"[red]Error: Invalid JSON in --params: {e}[/red]")
        sys.exit(1)

    asyncio.run(_replay(artifact, params_dict, target, headless, confirm_risky, evidence_dir))


async def _replay(
    artifact_path: str,
    params: dict,
    target: str,
    headless: bool,
    confirm_risky: bool,
    evidence_dir: str,
):
    from cua.schema.artifact import Capability
    from cua.replay.engine import ReplayEngine
    from cua.safety.allowlist import Allowlist
    from cua.safety.redactor import Redactor
    from cua.surface.browser import BrowserSurface

    settings = get_settings()

    # Load capability
    try:
        capability = Capability.from_file(artifact_path)
    except FileNotFoundError:
        # Try in artifacts dir
        try:
            capability = Capability.from_file(str(ARTIFACTS_DIR / artifact_path))
        except Exception:
            console.print(f"[red]Error: Artifact not found: {artifact_path}[/red]")
            sys.exit(1)

    console.print(Panel(
        f"[bold]Capability:[/bold] {capability.metadata.display_name}\n"
        f"[bold]Version:[/bold] {capability.metadata.version}\n"
        f"[bold]Steps:[/bold] {len(capability.steps)}\n"
        f"[bold]Params:[/bold] {json.dumps(params)}",
        title="▶ CUA Replay",
        border_style="green",
    ))

    # Validate inputs
    errors = capability.validate_inputs(params)
    if errors:
        console.print("[red]Input validation errors:[/red]")
        for err in errors:
            console.print(f"  • {err}")
        sys.exit(1)

    # Set up evidence
    if not evidence_dir:
        import uuid
        run_id = str(uuid.uuid4())[:8]
        evidence_dir = str(EVIDENCE_DIR / f"replay_{run_id}")

    # Create components
    sensitive_params = {inp.name for inp in capability.inputs if inp.sensitive}
    redactor = Redactor(sensitive_param_names=sensitive_params)

    surface = BrowserSurface(headless=headless)
    allowlist = Allowlist()

    engine = ReplayEngine(
        surface=surface,
        allowlist=allowlist,
        redactor=redactor,
        evidence_dir=evidence_dir,
        confirm_risky=confirm_risky,
    )

    target_url = target or settings.target_app_url

    # Run replay
    console.print("\n[yellow]Executing replay...[/yellow]\n")
    result = await engine.replay(
        capability=capability,
        params=params,
        target_url=target_url,
    )

    # Display result
    if result.succeeded:
        console.print(f"[green]✓ {result.message}[/green]")
        if result.outputs:
            table = Table(title="Extracted Outputs")
            table.add_column("Name", style="bold")
            table.add_column("Value")
            table.add_column("Source")
            for out in result.outputs:
                table.add_row(out.name, str(out.value), out.extracted_from)
            console.print(table)

    elif result.is_business_outcome:
        console.print(f"[yellow]⚡ Business Outcome: {result.outcome_code}[/yellow]")
        console.print(f"   {result.outcome_message or result.message}")

    elif result.status.value == "escalated":
        console.print(f"[blue]🖐 Escalated: {result.message}[/blue]")
        if result.escalation:
            console.print(f"   Reason: {result.escalation.reason}")
            if result.escalation.session_url:
                console.print(f"   Session: {result.escalation.session_url}")

    else:
        console.print(f"[red]✗ Failed: {result.message}[/red]")
        if result.failed_step_id:
            console.print(f"   Step: {result.failed_step_id}")
        if result.error_details:
            console.print(f"   Error: {result.error_details}")
        if result.expected_state:
            console.print(f"   Expected: {result.expected_state}")
        if result.observed_state:
            console.print(f"   Observed: {result.observed_state[:200]}")

    # Step trace summary
    if result.step_traces:
        trace_table = Table(title="Step Traces")
        trace_table.add_column("Step", style="bold")
        trace_table.add_column("Status")
        trace_table.add_column("Duration")
        trace_table.add_column("Details")

        for trace in result.step_traces:
            status_style = {
                "passed": "green",
                "failed": "red",
                "recovered": "yellow",
                "escalated": "blue",
                "skipped": "dim",
                "not_reached": "dim",
            }.get(trace.status.value, "white")

            duration = f"{trace.duration_ms}ms" if trace.duration_ms else "-"
            details = trace.error_detected or trace.action_performed or ""

            trace_table.add_row(
                trace.step_id,
                f"[{status_style}]{trace.status.value}[/{status_style}]",
                duration,
                details[:60],
            )

        console.print(trace_table)

    # Save result
    result_path = Path(evidence_dir) / "replay_result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(result.to_json())
    console.print(f"\n[dim]Result saved: {result_path}[/dim]")

    if result.evidence_dir:
        console.print(f"[dim]Evidence: {result.evidence_dir}[/dim]")

    # Clean up
    await surface.stop()

    # Exit code based on result
    if not result.succeeded and not result.is_business_outcome:
        sys.exit(1)


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------

@main.command()
@click.option("--port", "-p", default=8000, help="Port to run the API server on")
@click.option("--host", default="0.0.0.0", help="Host to bind to")
def serve(port: int, host: str):
    """Start the FastAPI server for agent-invocable replay."""
    import uvicorn

    console.print(Panel(
        f"[bold]Host:[/bold] {host}:{port}\n"
        f"[bold]Docs:[/bold] http://{host}:{port}/docs\n"
        f"[bold]Operator:[/bold] http://{host}:{port}/operator",
        title="🌐 CUA API Server",
        border_style="cyan",
    ))

    uvicorn.run(
        "cua.api.server:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

@main.command("list")
def list_artifacts():
    """List available capability artifacts."""
    if not ARTIFACTS_DIR.exists():
        console.print("[dim]No artifacts directory found.[/dim]")
        return

    artifacts = list(ARTIFACTS_DIR.glob("*.json"))
    if not artifacts:
        console.print("[dim]No artifacts found in artifacts/[/dim]")
        return

    table = Table(title="Available Capabilities")
    table.add_column("Name", style="bold")
    table.add_column("Display Name")
    table.add_column("Version")
    table.add_column("Steps")
    table.add_column("Inputs")
    table.add_column("File")

    from cua.schema.artifact import Capability

    for path in sorted(artifacts):
        try:
            cap = Capability.from_file(str(path))
            table.add_row(
                cap.metadata.name,
                cap.metadata.display_name,
                cap.metadata.version,
                str(len(cap.steps)),
                str(len(cap.inputs)),
                path.name,
            )
        except Exception as e:
            table.add_row("?", str(path.name), "?", "?", "?", f"Error: {e}")

    console.print(table)


# ---------------------------------------------------------------------------
# Module entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
