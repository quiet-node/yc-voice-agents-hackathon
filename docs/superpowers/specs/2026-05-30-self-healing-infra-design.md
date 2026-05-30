# Self-Healing Infrastructure Design
*Date: 2026-05-30 | Project: yc-voice-agents-hackathon / Bayview Pharmacy*

## Overview

An event-driven self-healing loop that automatically detects failing Cekura evaluation
scenarios, proposes targeted prompt patches via GPT-5.5 (token router), deploys the fix
to Pipecat Cloud, verifies the repaired scenario passes, then runs a full Cekura suite to
confirm no regressions before auto-merging the PR.

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
│  4. Enqueue failing IDs into asyncio.Queue    │
│     (skip IDs already queued or in progress) │
│  5. Return HTTP 200 immediately              │
│                                              │
│  Serial worker (single asyncio.Task,         │
│  started once at server startup):            │
│  · Pulls one scenario ID from queue          │
│  · Runs run_heal() via run_in_executor       │
│  · Logs result                               │
│  · Pulls next — never two heals at once      │
└───┬──────────────────────────────────────────┘
    │  ThreadPoolExecutor (blocking-safe)
    ▼
┌───────────────────────────────────────────────────┐
│  harness/self_heal.py → run_heal(scenario_id)     │
│                                                   │
│  · Baseline run via Cekura REST API               │
│    (GET /results/{run_id}/ → expected_outcome     │
│     .score field for pass/fail)                   │
│  · If 0 agent turns → infra issue, raise          │
│    RuntimeError (not sys.exit)                    │
│  · GPT-5.5 (token router) proposes patch          │
│  · Apply patch to server/bot-nemotron.py          │
│  · pc cloud deploy + poll until active            │
│  · Re-run scenario, check expected_outcome.score  │
│  · Repeat up to max_iterations (default: 3)       │
│  · If score == 100:                               │
│      open_pr() → full Cekura suite re-run         │
│      → if no regressions: gh pr merge --squash    │
│      → if regressions: leave PR open for review   │
│  · If score < 100: revert all patches             │
└───┬───────────────────────────────────────────────┘
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
- **Auth:** Compares `X-CEKURA-SECRET` header to `CEKURA_WEBHOOK_SECRET` env var (read via existing `_read_env_var()` helper pattern from `self_heal.py`); returns 401 on mismatch
- **Event handling:**
  - `result.completed` with `success_rate < 100` → queue scenario heals
  - `result.completed` with `success_rate >= 100.0` → log and return 200
  - Error runs (zero agent turns, all runs have `error_message`) → log infra issue, skip self-heal, return 200
  - Unknown `event_type` → log and return 200
- **Serial execution — one scenario at a time:** the server processes exactly one heal at a time. Running multiple concurrent heals would interleave `bot-nemotron.py` patches, git branches, and Pipecat Cloud deploys, causing collisions. This is enforced by a single long-running `asyncio.Task` (the "worker") that drains an `asyncio.Queue` one item at a time.
- **Queue + dedup:** incoming webhook scenario IDs are pushed onto an `asyncio.Queue`. A `set[int]` of already-queued-or-in-progress IDs (guarded by `asyncio.Lock`) prevents the same scenario from being enqueued twice. If a webhook arrives for a scenario already in the queue or being healed, it is silently dropped.
- **Worker task:**
  ```python
  async def _worker() -> None:
      while True:
          scenario_id = await _queue.get()
          try:
              result = await loop.run_in_executor(None, run_heal, scenario_id)
              _log_result(result)
          except Exception as exc:
              logger.error("heal failed for scenario %d: %s", scenario_id, exc)
          finally:
              async with _lock:
                  _queued.discard(scenario_id)
              _queue.task_done()
  ```
- `run_in_executor` offloads the blocking `self_heal()` (subprocess calls, `time.sleep`, `urllib` I/O) to a thread pool so the aiohttp event loop stays responsive while a heal runs.
- **Env loading:** calls the existing `_cekura_key()` / `_token_router_key()` / `_token_router_base_url()` helpers from `self_heal.py`, which already handle `.env` fallback parsing
- **Logging:** results written to `harness/runs/webhook-<ISO-timestamp>/scenario-<id>.json`

### Modified: `harness/self_heal.py`

Three targeted additions — no restructuring:

**1. `run_heal(scenario_id, max_iterations=3, dry_run=False, no_deploy=False) -> HealResult`**

Thin callable wrapper for `webhook_server.py`. Builds `argparse.Namespace` and calls `self_heal()`.
Critically: replaces the two `sys.exit()` calls in `self_heal()` (dirty working tree, detached HEAD)
with `raise RuntimeError(...)` so a `SystemExit` cannot propagate through `run_in_executor`
and kill the webhook server process.

```python
def run_heal(
    scenario_id: int,
    max_iterations: int = 3,
    dry_run: bool = False,
    no_deploy: bool = False,
) -> HealResult:
    ns = argparse.Namespace(
        scenario=scenario_id,
        max_iterations=max_iterations,
        dry_run=dry_run,
        no_deploy=no_deploy,
    )
    return self_heal(ns)  # self_heal() raises RuntimeError instead of sys.exit()
```

**2. `sys.exit()` → `raise RuntimeError()` everywhere reachable from `run_heal()`**

All of the following must use `raise RuntimeError(...)` instead of `sys.exit()`, because
`sys.exit()` raises `SystemExit` which propagates out of `run_in_executor` threads and
terminates the entire aiohttp server process:

- `self_heal()`: both guard sites (detached HEAD, uncommitted changes)
- `_cekura_key()`: missing `CEKURA_API_KEY`
- `_token_router_key()`: missing `TOKEN_ROUTER_API_KEY`
- `_token_router_base_url()`: missing `TOKEN_ROUTER_BASE_URL`

The CLI `main()` entry point wraps `self_heal(args)` in a `try/except RuntimeError` and
calls `sys.exit(str(e))` so CLI behaviour is unchanged.

**3. `auto_merge` flag on `open_pr()`**

New signature:
```python
def open_pr(
    proposals: list[PatchProposal],
    scenario_name: str,
    before_score: int | None,
    after_score: int,
    base_branch: str,
    auto_merge: bool = False,   # NEW — default False preserves CLI behaviour
) -> str | None:
```

After `gh pr create` succeeds and `auto_merge=True`:
1. Trigger a full Cekura suite re-run (`cekura_post("/results/run_all/", {"agent": CEKURA_AGENT_ID})`)
2. Poll until complete
3. If `success_rate >= previous_baseline`: call `gh pr merge <pr_url> --squash --delete-branch --yes`
4. If regressions detected: print warning, leave PR open for human review

This ensures LLM-generated changes are verified against all scenarios — not just the
repaired one — before touching `main`.

### New: `harness/start.sh`

```bash
#!/usr/bin/env bash
set -e
PORT=8888

# Load .env so webhook_server.py inherits all vars
set -a
source "$(dirname "$0")/../server/.env"
set +a

DOMAIN="${NGROK_DOMAIN}"

# Start ngrok with static domain in background
ngrok http --domain="$DOMAIN" "$PORT" --log=stdout &
sleep 2

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "  Webhook live → https://$DOMAIN/webhook/cekura               "
echo "  Paste this URL in: Cekura → Agent Settings → Webhook URL    "
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# Start webhook server (foreground, logs to stdout)
python3 "$(dirname "$0")/webhook_server.py" --port "$PORT"
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

`start.sh` sources `server/.env` with `set -a / set +a` before launching the webhook server,
so all variables are available in the subprocess environment without any additional loading logic.

---

## Cekura Webhook Configuration

- **URL:** `https://prefashioned-jaspa-dillon.ngrok-free.dev/webhook/cekura`
- **Secret:** `CEKURA_WEBHOOK_SECRET` in `server/.env`
- **Notifications enabled:**
  - Result: Success ✓, Failed ✓, Error ✓
  - Cronjob: Failed ✓

---

## Webhook vs REST Payload Schema

These are **two distinct schemas** used in two distinct places:

| Schema | Used by | Pass/fail field |
|---|---|---|
| Webhook payload | `webhook_server.py` — trigger detection only | `run["success"]` (bool) |
| REST `/results/{id}/` | `self_heal.py poll_run()` — heal loop termination | `expected_outcome.score` (int, 100 = pass) |

The webhook `run["success"]` field is only used to identify *which* scenario IDs to queue
for healing. The actual heal loop uses `poll_run()` which calls the REST API and reads
`expected_outcome.score` — this is the existing, tested code path that does not change.

`success_rate` comparison uses `>= 100.0` (float-safe) rather than `== 100`.

---

## Error Handling

| Condition | Behaviour |
|---|---|
| Invalid `X-CEKURA-SECRET` | Return 401, log warning |
| Unknown `event_type` | Return 200, log and ignore |
| `success_rate >= 100.0` | Return 200, log "all passed" |
| All runs have `error_message` (infra failure) | Return 200, log infra issue, skip self-heal |
| Scenario ID already in `_in_progress` | Return 200, log "already healing", skip |
| `run_heal()` raises `RuntimeError` | Catch in task, log error, release from `_in_progress` |
| GPT-5.5 returns non-JSON | Skip iteration, log warning |
| `find` text not in bot file | Skip iteration, log warning |
| Deploy timeout | Revert patch, stop loop |
| Score never reaches 100 after N iterations | Revert all patches, log final score |
| Full suite re-run shows regression | Leave PR open, log regression warning |
| `gh pr merge` fails | Log error, leave PR open for manual merge |

---

## Auto-Merge Safety Gate

Auto-merge only fires when ALL of the following are true:
1. The repaired scenario reached `expected_outcome.score == 100`
2. A full Cekura suite re-run completed
3. `success_rate` of the full suite is `>=` the baseline recorded at heal-loop start

If the full suite re-run shows any regression the PR is left open with a comment explaining
why auto-merge was skipped. A human reviews and merges manually.

---

## Usage

```bash
# Start everything
./harness/start.sh

# Test the webhook locally (without ngrok)
curl -X POST http://localhost:8888/webhook/cekura \
  -H "X-CEKURA-SECRET: $CEKURA_WEBHOOK_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "event_type": "result.completed",
    "data": {
      "success_rate": 0.0,
      "runs": {
        "1": {
          "id": 1,
          "scenario": {"id": 272668, "name": "test"},
          "success": false,
          "evaluation": {"metrics": []},
          "transcript_object": []
        }
      }
    }
  }'

# Manual heal (existing CLI, unchanged)
python3 harness/self_heal.py --scenario 272668 --dry-run
```

---

## Out of Scope

- Notification (Slack/email) on heal completion
- Persistent dedup state across webhook server restarts (set is in-memory only)
- Healing multiple scenarios in strict dependency order
