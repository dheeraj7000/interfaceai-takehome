# Design Report — Computer-Use Automation System

## 1. Architecture

### Overview

The system has four main layers, each with a clean boundary:

```
  ┌──────────────────────────────────────────────────┐
  │  CLI / API Server                                │  Entry points
  ├──────────────────────────────────────────────────┤
  │  Agent (discovery)  │  Replay Engine (production)│  Execution
  ├──────────────────────────────────────────────────┤
  │  Surface Abstraction (Browser / Desktop stub)    │  Perception + Action
  ├──────────────────────────────────────────────────┤
  │  Schema │ Safety │ Escalation │ Observability    │  Cross-cutting
  └──────────────────────────────────────────────────┘
```

**Discovery path**: CLI → Agent Loop → LLM Provider → Surface → Recorder → Artifact (JSON)

**Production path**: API/CLI → Replay Engine → Surface → Result (no LLM)

### Key decisions

**Python as the sole language.** Pydantic gives typed schemas with JSON serialization for free. Playwright's Python bindings are mature. FastAPI for the API surface. One language, one runtime, fewer integration seams.

**Playwright + accessibility tree (hybrid observation).** Playwright handles clicks and typing fast and reliably. The accessibility tree provides stable element identification even on hostile DOMs — and it's the same concept available on desktop platforms via OS accessibility APIs (UIA, AXUIElement, AT-SPI). This is the credible bridge from web to desktop without rearchitecting.

**Single process, not microservices.** The discovery agent, replay engine, and API server share a Python process and import the same schema types. There's no serialization boundary between them. Simpler to debug, deploy, and demo. The surface abstraction and artifact schema are the real boundaries, not service boundaries.

**Local mock target app instead of a public site.** Full control over the surface, error states can be injected on demand, no TOS issues, and the intentionally hostile markup (framesets, nested tables, no test IDs) directly demonstrates the legacy problem described in the brief.

### Trade-offs accepted

- **No persistent database.** Artifacts are JSON files on disk. For hundreds of tenants this would need a proper store, but for the core system it's the right simplicity level.
- **Synchronous replay.** Each replay blocks until complete. At scale you'd want a job queue (Celery, etc.), but premature infrastructure is explicitly called out as unwanted.
- **Single-browser sessions.** Each replay gets its own browser instance. At scale you'd pool them.

---

## 2. Artifact Schema

The artifact schema (`src/cua/schema/artifact.py`) is the centerpiece. It represents a **typed, versioned, agent-invocable capability** — not just a step list.

### Structure

```
Capability
├── metadata     (name, version, surface_type, source_goal, tenant_id)
├── inputs       (typed parameters the caller supplies)
├── outputs      (typed fields extracted from the surface)
├── steps[]      (ordered actions with full traceability)
│   └── Step
│       ├── action + target (what to do, where)
│       ├── pre_condition   (what must be true before)
│       ├── post_condition  (what must be true after)
│       ├── error_matchers  (what to check for after the action)
│       ├── retry config    (how to handle transient failures)
│       └── risk_level      (safe / moderate / risky)
├── success_condition  (how to verify the goal was met)
└── error_handlers[]   (global patterns checked at every step)
```

### Why this shape

**Every step is a traceable node.** Pre-condition → action → post-condition, with error matchers in between. This means on failure, you know exactly: which step, what was expected, what was observed, and what error pattern (if any) matched. No guessing.

**Locator = single best + semantic descriptor.** Each target element has one primary locator (the most robust one — typically an accessibility role/label) plus a human-readable semantic description ("the member ID search input field"). The primary drives replay. The semantic aids debugging, human review, and potential LLM-assisted fallback recovery.

**Template variables for parameterization.** Steps reference inputs as `{{member_id}}`. This makes the data flow visible in the artifact JSON — a reviewer can trace exactly where each input value goes. The formal input contract (typed, with validation regex) means a calling agent knows exactly what to pass.

**Three-tier error classification baked into the schema.** Error matchers declare their category upfront: `business_outcome`, `recoverable`, or `hard_failure`. The replay engine doesn't have to figure out the classification at runtime — the artifact author (the discovery agent + human reviewer) made that call when the capability was recorded.

**Global error handlers for cross-cutting concerns.** Session timeouts, permission denials, and server errors can happen at any step. Rather than duplicating matchers on every step, global handlers are checked at every step, sorted by priority.

**Multi-tenant extension points.** `tenant_id` and `parent_artifact_id` fields enable an inheritance model: a base artifact works for the common case, tenant-specific overrides only change the steps that differ. Not implemented, but the schema supports it without breaking changes.

---

## 3. Determinism & Error Handling

### How replay is deterministic

1. **No LLM in the loop.** Every decision is encoded in the artifact: which element, what action, what value.
2. **Template resolution.** `{{member_id}}` is resolved from the input params before execution. Same inputs → same steps → same actions.
3. **Smart waits replace fixed sleeps.** Each step's checkpoint defines what "ready" looks like. The `SmartWaiter` polls with adaptive intervals (100ms → 2s backoff) until the condition is met or the timeout expires. No flaky fixed delays.
4. **Ordered, explicit steps.** The artifact is a linear sequence. There are no conditional branches in v1 — this is a deliberate simplification. Branching would be the first extension.

### Error handling taxonomy

| Category | Example | System response | Reported as |
|---|---|---|---|
| **Business outcome** | "Member not found" | Stop, return outcome code | `{ status: "business_outcome", outcome_code: "MEMBER_NOT_FOUND" }` |
| **Recoverable** | Session timeout dialog, loading spinner | Auto-dismiss / wait-retry / escalate | Logged, not surfaced unless retries exhausted |
| **Hard failure** | Element not found, server 500, permission denied | Stop, capture evidence | `{ status: "failure", step_id, expected, observed, evidence }` |

This separation is the most important design decision in the replay engine. "No such member" is a legitimate answer the caller needs — not a crash. The calling AI agent handles business outcomes differently from failures.

### Error detection flow (per step)

1. Check pre-condition (smart wait)
2. Scan global error handlers (before action)
3. Execute action (with retries on failure)
4. Scan step-level error matchers (after action)
5. Scan global error handlers again (after action)
6. Check post-condition (smart wait)

If any scan matches, the engine classifies and responds based on the matcher's declared category and recovery action.

### UI drift (secondary concern)

The brief correctly identifies that layout drift is not the primary concern — the UIs are stable. The primary locator handles the common case. If the locator fails after retries, it's a hard failure with a debuggable error message (which step, which locator, what was expected). The semantic descriptor helps a human reviewer fix the locator without re-running discovery. In a future version, the semantic descriptor could feed an LLM-assisted single-step recovery.

---

## 4. Heterogeneity & Multi-Tenant

### Surface abstraction

The `Surface` abstract class (`src/cua/surface/base.py`) defines the interface between the automation system and any application surface:

- **Observation:** `observe()` returns a `SurfaceState` (URL, visible text, accessibility tree, screenshot)
- **Action:** `click()`, `type_text()`, `navigate()`, etc.
- **Session management:** `pause()`, `resume()`, `is_alive()`

The browser implementation (`BrowserSurface`) uses Playwright for actions and the accessibility tree for observation. A desktop implementation would use platform accessibility APIs (Windows UIA, macOS AXUIElement, Linux AT-SPI) for observation and OS-level input simulation for actions.

**Why accessibility tree as the primary observation modality:** It's the only approach that works consistently across web, legacy web, and desktop. Browsers expose it. Operating systems expose it. Screen readers depend on it. The recorded artifact uses accessibility roles and names as locators — these translate directly to desktop a11y queries.

**The seam:** The artifact schema stores locators and actions in surface-agnostic terms (`a11y_role: textbox`, `a11y_name: Member ID`, `action: click`). The Surface implementation translates these into platform-specific operations. A new surface type requires a new Surface implementation, not changes to the schema or replay engine.

### Multi-tenant reuse

The artifact schema supports multi-tenant reuse through two mechanisms:

1. **`tenant_id` + `parent_artifact_id`:** A base artifact (tenant_id=null) captures the common flow. Tenant-specific variants set `parent_artifact_id` and override only the steps that differ (e.g., a slightly different button label, an extra confirmation dialog). The replay engine would merge the base steps with the overrides.

2. **URL pattern parameterization:** The recorder normalizes concrete routes into patterns (`/member/M1001` → `/member/{{member_id}}`). Tenant-specific URL prefixes become configuration, not artifact changes.

**Drift detection (design, not built):** Compare the accessibility tree snapshot from recording with the current tree during replay. If key structural elements are missing or renamed, flag the artifact as potentially stale for that tenant. This could run as a scheduled health check across all tenant-artifact combinations.

---

## 5. Escalation & Handoff

### Detecting "stuck"

The `StuckDetector` tracks three signals:

1. **No progress:** URL and visible text content haven't changed after N actions (default: 3). Detected by hashing the page content after each action.
2. **Repeated failure:** The same step has failed more than N times (default: 3).
3. **Unknown state:** The surface is in a state that doesn't match any error matcher or expected condition.

Additionally, risky/irreversible actions (classified by the `ActionClassifier`) can trigger escalation if `confirm_risky` is not set.

### Control transfer model

```
AUTOMATION running
       │
  stuck/error detected
       │
       ▼
AUTOMATION pauses (stops issuing actions)
       │
  ┌────┴────────────────────────────────┐
  │ session_controller = "human"        │
  │ InterventionRequest created with:   │
  │   - reason, step, screenshot        │
  │   - noVNC URL for live session      │
  │   - current state context           │
  └─────────────────────────────────────┘
       │
  Human operates the SAME browser session
  (same cookies, same state, same window)
       │
  Human clicks "Resume" in operator UI
       │
       ▼
AUTOMATION resumes
  ┌────┴────────────────────────────────┐
  │ session_controller = "automation"   │
  │ Re-observe current state            │
  │ Continue from next step             │
  │ Log human intervention window       │
  └─────────────────────────────────────┘
```

**Key invariant:** Automation NEVER acts while `session_controller == "human"`. This flag is the single source of truth for who is in control, and it only changes via explicit API calls.

**What's real:** The pause/resume mechanism, the intervention request with full context, the session_controller flag, and the operator API endpoint that completes the handoff.

**What's mocked:** The operator UI is a minimal HTML page. In production it would be a full application with live session viewing (via noVNC integration), team routing, priority queues, and SLA tracking.

### Evidence across handoff

Before handing over: screenshot + state snapshot + intervention request details.
After resume: screenshot + what the human reported doing. The entire handoff window is logged with timestamps.

---

## 6. Safety

### Allowlist enforcement

Every URL navigation and every action is checked against a configurable YAML allowlist (`config/allowlist.yml`). The allowlist specifies:

- **Permitted domains** (regex patterns) — only localhost by default
- **Allowed action types** per domain
- **Blocked paths** (regex) — e.g., `/admin`
- **Per-run action caps** — safety limit on total actions

An action outside the allowlist is blocked and logged. No exceptions.

### Action risk classification

Every action is classified as `safe`, `moderate`, or `risky`:

- **Safe:** Read-only operations (navigate, extract, assert, screenshot)
- **Moderate:** Writes that are typically reversible (type, clear, select)
- **Risky:** Irreversible side effects (click on submit/confirm/delete/transfer)

Classification uses both the action type and contextual signals (button text, form action, URL path). Banking-specific patterns like "confirm," "execute," "transfer" trigger the risky classification.

Risky actions during discovery are blocked. During replay, they require either the `confirm_risky` flag or escalation to a human.

### PII redaction

- Input parameters marked `sensitive: true` in the artifact schema have their values replaced with `[REDACTED]` in all logs and evidence.
- Known PII patterns (SSN, account numbers, phone numbers, email) are detected and redacted from free-text fields.
- Sensitive values seen during a run are tracked and redacted from all subsequent text captures in that run.
- Screenshots are NOT redacted in this version — region-based screenshot redaction is noted as a future enhancement.

### Limits

- The allowlist is domain-based, not element-based. A compromised target app could potentially trick the agent into interacting with a malicious element within an allowed domain.
- Screenshot redaction is not implemented. Sensitive data visible on screen is captured in evidence screenshots. A production deployment would need region-based blurring.
- The PII regex patterns are US-centric. International formats would need additional patterns.

---

## 7. Cuts

### What's implemented (thin but real)

| Capability | Status |
|---|---|
| Goal-driven agent loop (LLM) | Real — observe → decide → act with safety checks |
| Structured artifact with typed contract | Real — full Pydantic schema, versioned JSON |
| Deterministic replay | Real — no LLM, smart waits, error scanning |
| Three-tier error handling | Real — business_outcome / recoverable / hard_failure |
| Human-in-the-loop escalation | Real mechanism — pause/resume/control transfer, mock operator UI |
| Safety guardrails | Real — allowlist, action classification, PII redaction |
| Observability | Real — structured JSON logs, evidence capture |
| API server (agent-invocable) | Real — FastAPI with capability catalog |
| Target app (legacy surface) | Real — hostile markup, injectable errors |

### What's mocked or stubbed

- **Operator console UI**: Minimal HTML page. Real mechanism underneath, mock presentation.
- **Desktop surface adapter**: Design stub with documented extension notes. Interface is complete.
- **noVNC integration**: Docker config included but the browser container may need manual setup depending on the environment. CDP-based debugging works as a fallback.

### What I'd build next (priority order)

1. **Conditional branching in artifacts.** The current schema is linear. Real flows need if/else (e.g., "if the member is frozen, skip the transfer step"). I'd add a `condition` field on steps and a simple expression evaluator.

2. **LLM-assisted single-step recovery.** On replay failure at a specific step, invoke the LLM for that one step only (bounded, policy-checked), then record what it did as evidence. This bridges the gap between deterministic replay and full LLM reasoning.

3. **Screenshot region redaction.** Use the element bounding boxes to blur sensitive data regions in evidence screenshots before saving.

4. **Cross-tenant artifact inheritance.** Implement the base/override merge logic the schema already supports. Add a drift detection job that compares a11y trees across tenants.

5. **Approval workflow.** Artifacts start as `draft`, move to `approved` after human review, and only `approved` artifacts can run in unattended mode. Track replay success rate as a confidence score.

6. **Parallel replay with stability reporting.** Run an artifact N times, report flakiness signal, and use it to gate approval.
