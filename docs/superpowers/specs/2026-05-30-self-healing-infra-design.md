# Self-Healing Infrastructure Design
*Date: 2026-05-30 | Project: yc-voice-agents-hackathon / Bayview Pharmacy*

## Overview

An event-driven self-healing loop that automatically detects failing Cekura evaluation
scenarios, proposes targeted prompt patches via GPT-5.5 (token router), deploys the fix
to Pipecat Cloud, verifies the score improves, and auto-merges the PR — all without human
intervention.

---

## Architecture

```
Cekura evaluation completes (any result with ≥1 failure)
    │
    ▼  POST /webhook/cekura
    │  Header: X-CEKURA-SECRET: <secret>
    │  Body: { event_type: "result.completed", data: { runs: {...} } }
    │
┌───┴──────────────────────────────────────────┐
│  harness/webhook_server.py  (aiohttp, :8888) │
│                                              │
│  1. Verify X-CEKURA-SECRET header            │
│  2. Parse event_type == "result.completed"   │
│  3. Extract failing scenario IDs:            │
│     [run["scenario"]["id"]                   │
│      for run in data["runs"].values()        │
│      if not run["success"]]                  │
│  4. Skip IDs already in _in_progress set     │
│  5. asyncio.create_task(heal(id)) per ID     │
│  6. Return HTTP 200 immediately              │
└───┬──────────────────────────────────────────┘
    │  thread executor (non-blocking)
    ▼
┌───────────────────────────────────────────────┐
│  harness/self_heal.py → run_heal(scenario_id) │
│                                               │
│  · Baseline run via Cekura API                │
│  · If 0 agent turns → infra issue, skip       │
│  · GPT-5.5 (token router) proposes patch      │
│  · Apply patch to server/bot-nemotron.py      │
│  · pc cloud deploy + poll until active        │
│  · Re-run scenario                            │
│  · Repeat up to max_iterations (default: 3)   │
│  · If score == 100 → open_pr() → auto_merge() │
│  · If score < 100 → revert all patches        │
└───┬───────────────────────────────────────────┘
    │
    ▼
harness/runs/webhook-<timestamp>/scenario-<id>.json
```

---

## Files

### New: `harness/webhook_server.py`

- **Framework:** `aiohttp` (already present as Pipecat transitive dependency)
- **Port:** 8888 (configurable via `--port`)
- **Route:** `POST /webhook/cekura`
- **Auth:** Compares `X-CEKURA-SECRET` header to `CEKURA_WEBHOOK_SECRET` env var; returns 401 on mismatch
- **Event handling:**
  - `result.completed` with failures → queue scenario heals
  - `result.completed` with `success_rate == 100` → log and return 200
  - Error runs (`error_message` set, zero agent turns) → log infra issue, skip self-heal
- **Deduplication:** in-memory `set[int]` of scenario IDs currently being healed; duplicate webhooks for an in-progress scenario are skipped
- **Concurrency:** one `asyncio.Task` per scenario; tasks run in `loop.run_in_executor` to avoid blocking the event loop
- **Logging:** results written to `harness/runs/webhook-<ISO-timestamp>/scenario-<id>.json`

### Modified: `harness/self_heal.py`

Two additions only — no restructuring:

1. **`run_heal(scenario_id, max_iterations=3)`** — thin wrapper that builds an `argparse.Namespace` and calls the existing `self_heal()` function. Enables calling from `webhook_server.py` without subprocess.

2. **`auto_merge` flag on `open_pr()`** — after `gh pr create` succeeds, calls:
   ```
   gh pr merge <pr_url> --squash --delete-branch --yes
   ```
   Only fires when score reached 100%.

### New: `harness/start.sh`

```bash
#!/usr/bin/env bash
set -e
PORT=8888
DOMAIN=$(grep "^NGROK_DOMAIN=" server/.env | cut -d= -f2)

# Start ngrok with static domain
ngrok http --domain="$DOMAIN" $PORT &
sleep 2

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "  Webhook live → https://$DOMAIN/webhook/cekura"
echo "  Configure this URL in: Cekura → Agent Settings → Webhook URL"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# Start webhook server (foreground, logs to stdout)
python3 harness/webhook_server.py --port $PORT
```

---

## Environment Variables

All read from `server/.env`:

| Variable | Purpose |
|---|---|
| `CEKURA_WEBHOOK_SECRET` | Validates `X-CEKURA-SECRET` header on every incoming webhook |
| `CEKURA_API_KEY` | Cekura REST API (run scenarios, poll results) |
| `TOKEN_ROUTER_BASE_URL` | OpenAI-compatible base URL for GPT-5.5 |
| `TOKEN_ROUTER_API_KEY` | Auth for token router |
| `NGROK_DOMAIN` | Static ngrok domain (`prefashioned-jaspa-dillon.ngrok-free.dev`) |

---

## Cekura Webhook Configuration

- **URL:** `https://prefashioned-jaspa-dillon.ngrok-free.dev/webhook/cekura`
- **Secret:** configured in `CEKURA_WEBHOOK_SECRET` in `server/.env`
- **Notifications enabled:**
  - Result: Success ✓, Failed ✓, Error ✓
  - Cronjob: Failed ✓ (to catch scheduled run failures)

---

## Webhook Payload (confirmed from Cekura docs)

```json
{
  "event_type": "result.completed",
  "data": {
    "id": 4,
    "agent": 1,
    "status": "completed",
    "success_rate": 0.0,
    "runs": {
      "4": {
        "id": 4,
        "scenario": { "id": 13, "name": "Secure Cell Activation Workflow" },
        "success": false,
        "evaluation": {
          "metrics": [{ "score": 0, "explanation": ["..."] }]
        },
        "transcript_object": [...]
      }
    }
  }
}
```

Failing scenario IDs extracted as:
```python
[run["scenario"]["id"] for run in data["runs"].values() if not run["success"]]
```

---

## Error Handling

| Condition | Behaviour |
|---|---|
| Invalid `X-CEKURA-SECRET` | Return 401, log warning |
| Unknown `event_type` | Return 200, log and ignore |
| `success_rate == 100` | Return 200, log "all passed" |
| All runs have `error_message` (infra failure) | Return 200, log infra issue, skip self-heal |
| Scenario ID already in `_in_progress` | Return 200, log "already healing", skip |
| GPT-5.5 returns non-JSON | Skip iteration, log warning |
| `find` text not in bot file | Skip iteration, log warning |
| Deploy timeout | Revert patch, stop loop |
| Score never reaches 100 after N iterations | Revert all patches, log final score |

---

## Regression Guard

The existing `self_heal.py` logic already handles this: if after applying a patch the score does not improve to 100%, all patches are reverted before the run completes. No partial fixes are left in the codebase.

Future enhancement (out of scope for now): after auto-merge, trigger a full Cekura suite run to check no other scenarios regressed.

---

## Usage

```bash
# One-time setup
ngrok config add-authtoken $NGROK_AUTH_TOKEN   # already done

# Start everything
./harness/start.sh

# Test the webhook manually
curl -X POST http://localhost:8888/webhook/cekura \
  -H "X-CEKURA-SECRET: $CEKURA_WEBHOOK_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"event_type":"result.completed","data":{"success_rate":0,"runs":{"1":{"id":1,"scenario":{"id":272668,"name":"test"},"success":false,"evaluation":{"metrics":[]},"transcript_object":[]}}}}'
```

---

## Out of Scope

- Auto-merging when score improves but doesn't reach 100% (partial improvement)
- Regression testing other scenarios after a successful fix
- Notification (Slack/email) on heal completion
- Persistent state across restarts (dedup set is in-memory only)
