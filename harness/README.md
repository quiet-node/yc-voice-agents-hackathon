# Bayview Self-Improvement Harness

This harness turns Cekura-style test output into a local improvement dashboard.
It is intentionally read-only: it diagnoses and proposes fixes, but it does not
modify the agent by itself.

## Run

```bash
python3 harness/generate_dashboard.py \
  --input harness/examples/bayview_cekura_report_sample.json \
  --out harness/runs/demo
```

Open:

```text
harness/runs/demo/index.html
```

The generator writes three artifacts:

- `index.html` - static dashboard.
- `report.json` - normalized machine-readable facts.
- `fix_plan.md` - prioritized remediation plan.

## Run Against Real Cekura Data

If `CEKURA_API_KEY` is exported or present in `server/.env`, the harness can pull
a Cekura result directly:

```bash
python3 harness/generate_dashboard.py \
  --cekura-result-id latest \
  --cekura-agent-id 18021 \
  --out harness/runs/latest-cekura
```

Or target a specific result:

```bash
python3 harness/generate_dashboard.py \
  --cekura-result-id 591106 \
  --out harness/runs/cekura-591106
```

## Serve With Manual Refresh

To keep the dashboard open and refresh Cekura data in place, run the local
dashboard server:

```bash
python3 harness/serve_dashboard.py --cekura-agent-id 18021
```

Open the printed URL, then click **Refresh** after a new Cekura run completes.
The browser calls the local server, the server fetches the latest Cekura result
with `CEKURA_API_KEY`, and the page rerenders from the fresh `report.json`
without a reload.

If you expose the dashboard through ngrok, set `NGROK_DOMAIN` to the public
domain. The server prints a matching ngrok URL with a `refresh_token` query
parameter:

```bash
export NGROK_DOMAIN=prefashioned-jaspa-dillon.ngrok-free.dev
python3 harness/serve_dashboard.py --cekura-agent-id 18021
```

`POST /api/refresh` requires that refresh token because an ngrok URL is public.
You can set a stable token yourself with `DASHBOARD_REFRESH_TOKEN`; otherwise the
server generates one each time it starts. Keep `CEKURA_API_KEY` server-side only.
`NGROK_AUTH_TOKEN` and `NGROK_ID` are still useful for your ngrok process, but
the dashboard server only reads `NGROK_DOMAIN` to print the public URL.

## Input Shape

The best input is a JSON export with a top-level `runs`, `workflow_runs`,
`test_runs`, `call_logs`, `calls`, `evaluations`, or `results` array.

Each run can include:

```json
{
  "id": "run-123",
  "scenario_name": "Verified caller refills medication",
  "evaluation_status": "failure",
  "expected_outcome": ["Assistant verifies identity first."],
  "metrics": [
    {"name": "PHI before verification", "status": "failure", "reason": "..."}
  ],
  "transcript": [
    {"role": "user", "content": "I need a refill."},
    {"role": "tool", "name": "verify_identity", "result": {"verified": true}}
  ]
}
```

Plain text transcripts also work when lines are prefixed with `User:`,
`Assistant:`, `Agent:`, or `Tool:`.

## What It Extracts

For each call, the harness extracts:

- caller intent: refill, pickup status, medication lookup, or unknown.
- identity state: identity provided, verification attempts, verification result.
- tool sequence: `verify_identity`, `get_prescriptions`, `refill_prescription`,
  and `end_call`.
- Bayview safety signals: prescription data before verification, refill before
  verification, early call ending, caller confusion, tool errors, and infra
  signals.

## Failure Taxonomy

The dashboard groups findings into:

- `Privacy Risk`
- `Identity Flow Gap`
- `Early End Call`
- `Conversation Responsiveness`
- `Tool/Backend Issue`
- `Infra Issue`
- `Metric or Prompt Ambiguity`
- `Unclassified Failure`

Every cluster includes evidence, root cause, recommended change, target area,
confidence, and regression risk.

## Auto-Fix Loop

`auto_fix.py` reads the fix queue from `report.json`, applies patches to the
bot files, commits each fix on an isolated branch, and opens a GitHub PR.
**Nothing is merged to main automatically** — every fix requires PR review.

```bash
# Preview what would change (no files written, no branches created)
python3 harness/auto_fix.py --dry-run

# Apply all fixes from the latest dashboard run
python3 harness/auto_fix.py

# Apply only the top fix
python3 harness/auto_fix.py --max-fixes 1

# Apply and re-run Cekura after each fix to flag regressions on the PR
# (costs voice call credits — each run ~5 credits/min)
python3 harness/auto_fix.py --verify

# Point at a specific report
python3 harness/auto_fix.py --report harness/runs/cekura-591106/report.json
```

The script will refuse to run if there are uncommitted changes in the working
tree — commit or stash first so each fix lands on a clean branch.

**What auto_fix can patch automatically:**

| `change_type` | Action |
|---|---|
| `code guardrail` | Injects `call_state["verified"]` guard into `get_prescriptions` and `refill_prescription` |
| `prompt + latency handling` | Appends a first-response / keepalive rule to `system_instruction` |

**What requires human review (auto_fix prints a checklist and skips):**

| `change_type` | Why manual |
|---|---|
| `deployment/config` | Pipecat Cloud / Twilio settings — no code to patch |
| `prompt` | Requires reviewing the exact prompt change in context |
| `prompt + orchestration` | Structural change to the turn flow |
| `mock data or evaluator` | Either the scenario or the mock data needs updating |

## Recommended Loop

1. Run `/cekura-report` (or `generate_dashboard.py --cekura-result-id latest`).
2. Run `python3 harness/auto_fix.py --dry-run` to preview.
3. Run `python3 harness/auto_fix.py` to apply and open PRs.
4. Review each PR on GitHub; run Cekura against the branch before merging.
5. Merge only if pass rate improves or is unchanged.
6. Regenerate the dashboard to track the new baseline.

Only call the agent improved when the failed subset passes and the full suite
does not regress.
