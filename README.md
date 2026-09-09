# CUA — Computer-Use Automation System

A record-once, replay-many automation system for legacy banking applications. An LLM discovers how to accomplish a task by driving a real UI, records the flow as a typed, versioned artifact, and then a deterministic replay engine executes it without the LLM — reliably and cheaply.

Built for the [interface.ai](https://interface.ai) take-home project.

## Quick Start

### Prerequisites

- Python 3.11+
- Docker & Docker Compose
- An LLM API key (Anthropic or OpenAI) — only needed for discovery runs

### 1. Clone and install

```bash
git clone <repo-url>
cd interfaceai-takehome

python -m venv .venv
source .venv/bin/activate   # Linux/macOS
# .venv\Scripts\activate    # Windows

pip install -e ".[dev,target-app]"
playwright install chromium
```

### 2. Start the target app

```bash
# Full setup (target app + browser with noVNC for escalation demos)
docker compose up -d

# Or lightweight (target app only — faster for development)
docker compose -f docker-compose.dev.yml up -d
```

The mock legacy bank app is at http://localhost:5000.

### 3. Configure API keys (for discovery only)

```bash
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY or OPENAI_API_KEY
```

### 4. Run the demo

#### Discovery — LLM-driven (requires API key)

```bash
python -m cua discover \
  --goal "Look up member M1001 and read their current savings balance" \
  --target http://localhost:5000 \
  --headed
```

This runs the LLM against the live app, records the flow, and saves an artifact to `artifacts/`.

#### Replay — deterministic (no API key needed)

A pre-recorded artifact is included. Replay it:

```bash
# Happy path — member exists
python -m cua replay \
  --artifact artifacts/lookup_member_balance.json \
  --params '{"member_id": "M1001"}' \
  --headed

# Error case — member not found (business outcome, not a crash)
python -m cua replay \
  --artifact artifacts/lookup_member_balance.json \
  --params '{"member_id": "M9999"}'

# Error case — inject session timeout
python -m cua replay \
  --artifact artifacts/lookup_member_balance.json \
  --params '{"member_id": "M1001"}' \
  --target "http://localhost:5000?inject_error=timeout"
```

#### API Server — agent-invocable replay

```bash
# Start the server
python -m cua serve --port 8000

# List capabilities
curl http://localhost:8000/capabilities

# Invoke a replay via API
curl -X POST http://localhost:8000/replay \
  -H "Content-Type: application/json" \
  -d '{"artifact": "lookup_member_balance", "params": {"member_id": "M1001"}}'

# Operator console (human-in-the-loop)
# Open http://localhost:8000/operator in a browser
```

#### List available artifacts

```bash
python -m cua list
```

## Project Structure

```
├── README.md                       # This file
├── REPORT.md                       # Design write-up (7 headings)
├── EXECUTION_PLAN.md               # Build plan and decisions
├── pyproject.toml                  # Python project config
├── docker-compose.yml              # Full: target app + browser/noVNC
├── docker-compose.dev.yml          # Dev: target app only
├── .env.example                    # Environment variables template
│
├── target_app/                     # Mock legacy bank application
│   ├── Dockerfile
│   ├── app.py                      # Flask app with hostile markup
│   └── templates/                  # Frameset, tables, no test IDs
│
├── src/cua/                        # Main package
│   ├── cli.py                      # CLI: discover, replay, serve, list
│   ├── config.py                   # Settings from env vars
│   ├── schema/                     # Artifact schema (Pydantic)
│   │   ├── artifact.py             # Capability, Step, Locator, Checkpoint
│   │   └── results.py             # ReplayResult, StepTrace, 3-tier results
│   ├── agent/                      # LLM-driven discovery
│   │   ├── loop.py                 # Observe → decide → act loop
│   │   ├── recorder.py             # Records steps into artifact
│   │   └── providers/              # LLM adapters
│   │       ├── base.py             # Abstract provider interface
│   │       ├── anthropic.py        # Claude adapter
│   │       └── openai.py           # GPT-4o adapter
│   ├── replay/                     # Deterministic replay engine
│   │   ├── engine.py               # Step executor, template resolution
│   │   ├── waiter.py               # Smart waits (adaptive polling)
│   │   └── error_scanner.py        # Active error pattern detection
│   ├── surface/                    # Surface abstraction
│   │   ├── base.py                 # Abstract: observe + act + pause/resume
│   │   ├── browser.py              # Playwright + a11y tree
│   │   └── desktop.py              # Design stub for desktop apps
│   ├── escalation/                 # Human-in-the-loop
│   │   ├── detector.py             # Stuck detection
│   │   ├── handoff.py              # Control transfer (pause/resume)
│   │   └── operator_api.py         # Mock operator console HTML
│   ├── safety/                     # Guardrails
│   │   ├── allowlist.py            # Domain/action policy (YAML config)
│   │   ├── classifier.py           # Safe vs risky action classification
│   │   └── redactor.py             # PII/secret redaction
│   ├── observability/              # Logging & evidence
│   │   ├── logger.py               # Structured JSON run logs
│   │   └── evidence.py             # Screenshots, state snapshots
│   └── api/                        # FastAPI server
│       └── server.py               # Replay, capabilities, operator endpoints
│
├── config/
│   └── allowlist.yml               # Safety allowlist configuration
├── artifacts/                      # Saved capability artifacts
│   └── lookup_member_balance.json  # Pre-recorded example
└── evidence/                       # Run evidence (screenshots, logs)
```

## Target App — Heritage Federal Credit Union BackOffice

The mock target app simulates a hostile legacy banking surface:

- **Frameset-based layout** (not SPA)
- **Table-based rendering** (no CSS grid/flexbox)
- **No `data-testid` attributes**
- **Non-semantic markup** (nested `<table>`, `<td>` for layout)
- **Minimal accessibility** (some `aria-label`, inconsistent)

Flows: Member Search → Member Detail (balances) → Fund Transfer (with confirmation)

Error injection via query params: `?inject_error=not_found|timeout|permission|validation|server_error|slow`

## Running Without Live Services

- **Replay without Docker**: You can run replays against any running instance of the target app. Just set `--target` to the app URL.
- **Replay without LLM**: The pre-recorded artifact in `artifacts/` can be replayed without any API key. Discovery is the only phase that requires the LLM.
- **Without browser container**: Use `docker-compose.dev.yml` and local Playwright (no noVNC needed unless demonstrating escalation).
