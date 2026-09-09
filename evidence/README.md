# Evidence

This directory contains evidence from automation runs.

## Structure

```
evidence/
├── discovery_run/           # LLM-driven discovery run
│   ├── discovery_log.json   # Structured log of what the agent did and why
│   └── screenshots/         # Per-step screenshots (generated when run executes)
│
├── replay_run/              # Deterministic replay (happy path)
│   ├── replay_result.json   # Full structured result with step traces
│   └── screenshots/         # Screenshots captured during replay
│
└── replay_error/            # Replay with error case (member not found)
    ├── replay_result.json   # Shows business_outcome result (not a failure)
    └── screenshots/         # Includes failure-state screenshot
```

## Running to generate fresh evidence

```bash
# Start the target app
docker compose -f docker-compose.dev.yml up -d

# Discovery (requires LLM API key in .env)
python -m cua discover \
  --goal "Look up member M1001 and read their current savings balance" \
  --target http://localhost:5000 \
  --evidence-dir evidence/discovery_run \
  --headed

# Replay happy path
python -m cua replay \
  --artifact artifacts/lookup_member_balance.json \
  --params '{"member_id": "M1001"}' \
  --evidence-dir evidence/replay_run

# Replay error case (member not found — business outcome)
python -m cua replay \
  --artifact artifacts/lookup_member_balance.json \
  --params '{"member_id": "M9999"}' \
  --evidence-dir evidence/replay_error
```

## Note on screenshots

Screenshots are generated during live runs. The `.gitkeep` files hold the directory structure. Run the commands above to populate them with actual PNGs.

The JSON logs in this directory are pre-populated examples showing the expected structure and content of real runs. Replace them by running the commands above.
