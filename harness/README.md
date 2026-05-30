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

## Recommended Loop

1. Run `/cekura-report`.
2. Save/export the result JSON.
3. Generate the dashboard with this harness.
4. Apply the highest-priority fix.
5. Redeploy Bayview Pharmacy.
6. Re-run failed scenarios.
7. Run the full regression suite.

Only call the agent improved when the failed subset passes and the full suite
does not regress.
