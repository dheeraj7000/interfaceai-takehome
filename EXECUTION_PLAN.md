# Execution Plan — Computer-Use Automation System

**Project:** interface.ai Take-Home — Backend integration layer that gives AI agents "hands" to operate legacy bank software.

**Core idea:** <cite index="1-16">Use an LLM to figure out how to accomplish a task the first time, then turn what it learned into deterministic, replayable automation that no longer needs the model in the loop.</cite>

---

## Finalized Design Decisions

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| 1 | Target app | Self-hosted mock legacy bank app (Flask + Docker) | Full control over hostile markup (tables, iframes, no test IDs), injectable error states, zero TOS risk, reviewer runs `docker compose up` |
| 2 | Computer-use tech | Playwright (execution) + accessibility tree (observation) | Playwright handles clicks/typing fast; a11y tree provides stable element identity even on garbage DOM; credible desktop extension story |
| 3 | LLM provider | Provider-agnostic adapter; implement Anthropic Claude + OpenAI GPT-4o | Claude has native computer-use; GPT-4o has strong structured output; dual adapters prove the abstraction is real |
| 4 | Artifact format | JSON with Pydantic models | Human-reviewable, diffable, strongly typed, versioned |
| 5 | Locator strategy | Single best locator + semantic descriptor (hybrid) | Primary locator for speed; semantic fallback for debuggability and potential LLM-assisted recovery |
| 6 | Parameterization | Template variables (`{{member_id}}`) with formal typed input/output contract | Maximum traceability — every input flows visibly through steps; calling agents get a typed contract |
| 7 | Wait strategy | Smart waits (poll for expected state per step) | No fixed sleeps; each step defines what "ready" looks like |
| 8 | Failure detection | Active error-pattern scanning per step | Check for known error text, unexpected dialogs, validation messages after every action |
| 9 | Recovery | Tiered: auto-retry transient → dismiss known interstitials → escalate unknown | Matches the spec's three-tier error taxonomy |
| 10 | Result contract | Three-tier: `success` / `business_outcome` / `failure` | "Member not found" is a business outcome, not a crash |
| 11 | Escalation | noVNC inside Docker + pause/resume API | Real handoff mechanism; mock operator UI; browser session stays alive during human control |
| 12 | Architecture | CLI + FastAPI server | `discover` and `replay` CLI commands; HTTP API for agent-invocable replay + escalation webhooks |
| 13 | Language | Python (Pydantic, Playwright, FastAPI) | Best ecosystem fit — LLM SDKs, typed schemas, browser automation, API server all in one language |

---

## Target App — Mock Legacy Bank

A Flask app that simulates a hostile legacy banking surface:

- **Member lookup** — search by ID, returns account details in a table-based layout
- **Account detail** — nested tables, no semantic markup, shows balances
- **Transfer form** — multi-field form with confirmation step (irreversible action)
- **Error states** — injectable: member-not-found, session timeout dialog, validation errors, permission denied
- **Hostile markup** — `<table>` layouts, `<iframe>` for nav, no `data-testid`, auto-generated class names, `<frameset>` where possible

Served via Docker. Reviewer experience: `docker compose up` → app at `localhost:5000`.

---

## Artifact Schema (centerpiece)

```
Capability (artifact root)
├── metadata: name, version, description, created_at, surface_type, source_goal
├── inputs: { param_name → { type, required, description, sensitive } }
├── outputs: { field_name → { type, description, extractor } }
├── success_condition: Checkpoint
├── error_handlers: [ GlobalErrorHandler ]  (session timeout, permission denied, etc.)
│
└── steps: [
      Step
      ├── id, description (human-readable)
      ├── action: click | type | navigate | select | extract | wait | assert
      ├── target: {
      │     primary_locator: { strategy, value }    # a11y role, css, xpath, text
      │     semantic: "the member ID search field"   # human/LLM readable
      │   }
      ├── value: "{{member_id}}" | literal           # template var or constant
      ├── pre_condition: Checkpoint                   # expected state BEFORE this step
      ├── post_condition: Checkpoint                  # expected state AFTER this step
      ├── error_matchers: [                           # step-level error patterns
      │     { pattern, type: business_outcome|recoverable|hard_failure, action }
      │   ]
      ├── timeout_ms: 5000
      └── retry: { max_attempts, delay_ms }
    ]
```

Every step is a traceable node: pre-condition → action → post-condition, with error matchers at each point. This gives maximum execution path tracing.

---

## Error Taxonomy

| Category | Example | System Response | Reported As |
|----------|---------|-----------------|-------------|
| **Business outcome** | "Member not found", "Insufficient funds" | Stop, return outcome to caller with extracted data | `{ status: "business_outcome", outcome_code: "MEMBER_NOT_FOUND", message: "..." }` |
| **Recoverable** | Spinner/loading state, unexpected dialog ("session expiring"), transient network error | Auto-retry / dismiss / wait, then continue | Logged but not surfaced unless retries exhausted |
| **Hard failure** | Element not found after retries, page crash, unknown state, permission denied | Stop, capture evidence (screenshot + DOM), report | `{ status: "failure", step_id, expected, observed, evidence_path }` |
| **Escalation trigger** | Stuck (no progress for N steps), risky action needing confirmation, unknown error state | Pause automation, raise intervention request | `{ status: "escalated", reason, session_url, context }` |

---

## Escalation & Handoff Model

```
Normal replay
    │
    ├─ stuck detected (no progress / unknown state / risky action)
    │
    ▼
PAUSE automation
    │
    ├─ Capture state: screenshot, current step, error context
    ├─ Raise intervention request (webhook / API)
    ├─ Expose live browser session via noVNC URL
    ├─ Set session_controller = "human"
    │
    ▼
HUMAN operates the live session
    │  (same browser, same cookies, same state)
    │  Actions logged via periodic screenshot capture
    │
    ├─ Human signals "resume" via API / operator UI button
    │
    ▼
RESUME automation
    ├─ Set session_controller = "automation"
    ├─ Re-observe current state
    ├─ Continue from next step (or re-evaluate position)
    └─ Log human intervention window in run evidence
```

The control-transfer model: a single `session_controller` flag (`automation` | `human`) governs who is acting. Automation will not act when the flag is `human`. The flag is changed only by explicit API calls.

---

## Safety Guardrails

1. **Allowlist** — configurable YAML/JSON: permitted domains, permitted URL path patterns, permitted action types per domain
2. **Action classification** — every action tagged `safe` (read, navigate, click non-destructive) or `risky` (submit form, delete, transfer). Risky actions: require confirmation flag in the replay invocation, or auto-escalate to human
3. **PII redaction** — sensitive input params marked in schema; values redacted in all logs, artifacts, and evidence. Screenshots can optionally have region-based redaction
4. **Scope enforcement** — agent loop checks every URL navigation and every action against the allowlist before executing. Blocked actions logged and reported as policy violations

---

## Build Order & Phases

### Phase 1 — Foundation (schema + target + surface)
| # | Task | Output |
|---|------|--------|
| 1 | Artifact schema — Pydantic models | `src/cua/schema/` — all types: Capability, Step, Locator, Checkpoint, ErrorHandler, Result |
| 2 | Target app — Flask legacy bank | `target_app/` — member lookup → detail → transfer flow with error injection |
| 3 | Docker setup | `docker-compose.yml` — target app + browser w/ noVNC |
| 4 | Surface abstraction | `src/cua/surface/` — abstract base + Playwright browser implementation + a11y tree observer |

### Phase 2 — Core loop (agent + replay)
| # | Task | Output |
|---|------|--------|
| 5 | Safety guardrails | `src/cua/safety/` — allowlist, action classifier, redactor |
| 6 | LLM adapters | `src/cua/agent/providers/` — base interface + Claude + GPT-4o |
| 7 | Agent discovery loop | `src/cua/agent/` — observe→decide→act loop, step recorder, artifact emitter |
| 8 | Replay engine | `src/cua/replay/` — deterministic executor, smart waits, error scanner, result builder |

### Phase 3 — Escalation + observability + API
| # | Task | Output |
|---|------|--------|
| 9 | Escalation system | `src/cua/escalation/` — stuck detector, handoff controller, mock operator API |
| 10 | Observability | `src/cua/observability/` — structured JSON logger, evidence capture (screenshots, DOM snapshots) |
| 11 | API server | `src/cua/api/` — FastAPI: POST /replay, POST /escalation/resume, GET /capabilities |
| 12 | CLI | `src/cua/cli.py` — `discover`, `replay`, `serve` commands |

### Phase 4 — Evidence + docs
| # | Task | Output |
|---|------|--------|
| 13 | Discovery run | `evidence/discovery_run/` — real LLM-driven run, saved artifact, screenshots, logs |
| 14 | Replay run (happy path) | `evidence/replay_run/` — deterministic replay of saved artifact |
| 15 | Replay run (error case) | `evidence/replay_error/` — replay with injected error (member not found, session timeout) |
| 16 | README.md | Setup instructions, demo path commands |
| 17 | REPORT.md | Design write-up: 7 required headings |

---

## Key Files to Get Right

These carry the most evaluation weight:

1. **`src/cua/schema/artifact.py`** — the artifact schema. This is the focal point. Every reviewer will read this file first.
2. **`src/cua/replay/engine.py`** — deterministic replay with error handling. Proves the system works without the LLM.
3. **`src/cua/escalation/handoff.py`** — the control-transfer model. Must be real, not a TODO.
4. **`src/cua/safety/allowlist.py`** — guardrail enforcement. Simple but must actually block things.
5. **`REPORT.md`** — the design write-up. Where you defend every decision.

---

## Demo Path (what the reviewer runs)

```bash
# 1. Start the target app + browser environment
docker compose up -d

# 2. Run LLM-driven discovery
python -m cua discover \
  --goal "Look up member M1001 and read their savings balance" \
  --target "http://localhost:5000" \
  --output ./artifacts/lookup_balance.json

# 3. Replay the saved artifact (happy path)
python -m cua replay \
  --artifact ./artifacts/lookup_balance.json \
  --params '{"member_id": "M1001"}'

# 4. Replay with error case (member not found)
python -m cua replay \
  --artifact ./artifacts/lookup_balance.json \
  --params '{"member_id": "M9999"}'

# 5. Start API server for agent-invocable replay
python -m cua serve --port 8000

# 6. Invoke via API
curl -X POST http://localhost:8000/replay \
  -H "Content-Type: application/json" \
  -d '{"artifact": "lookup_balance", "params": {"member_id": "M1001"}}'
```

---

## What's Mocked vs. Real

| Component | Status | Notes |
|-----------|--------|-------|
| Target app | **Real** (self-hosted) | Intentionally hostile markup, injectable errors |
| LLM-driven discovery | **Real** | At least one genuine run with evidence |
| Artifact schema | **Real** | Typed Pydantic models, versioned JSON |
| Deterministic replay | **Real** | No LLM in the loop |
| Safety allowlist | **Real** | Enforced, configurable |
| Error handling | **Real** | Three-tier taxonomy, active scanning |
| noVNC handoff | **Real mechanism** | Browser session exposed via noVNC; automation pauses/resumes |
| Operator UI | **Mocked** | Minimal HTML page with "Resume" button; the handoff protocol is real |
| Desktop surface adapter | **Design seam only** | Stub interface in `surface/desktop.py`; covered in REPORT.md |
| Multi-tenant reuse | **Design only** | Covered in REPORT.md |

---

## Risk / Open Questions

- **noVNC in Docker**: May need a custom Dockerfile with Xvfb + x11vnc + noVNC. Fallback: headed Playwright with CDP remote debugging if noVNC is too heavy.
- **LLM cost**: Discovery runs consume API tokens. One successful run is sufficient per the spec. Keep the max-steps cap tight (20 steps).
- **Accessibility tree quality**: On hostile markup, the a11y tree may be sparse. Fallback: combine with visible text content extraction.
- **Screenshot evidence size**: Cap at 10 screenshots per run, compress to JPEG.
