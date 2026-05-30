# Cekura eval runbook — Bayview Pharmacy

How to run `/cekura-report` so it works first try. Agent `18037`, project `5875`, org `tuilux` (Pipecat Cloud) / Nam's Cekura org.

## The command

```
/cekura-report 18037 pipecat-v2
```

Always pass **`pipecat-v2`** explicitly. Cekura's provider enum has no `pipecat` value, so the agent is registered as `self_hosted`; without the explicit mode the skill computes "0 connection candidates" and **stops**. The mode arg (and a run-mode note baked into the agent description) is what keeps it from halting.

## Before you run (10 seconds)

1. **Auth** — if any Cekura tool returns 401, run `/mcp` → `cekura` → Authenticate. The OAuth token lapses periodically.
2. **Warm agents** — the deployed bot must be warm or burst test calls cold-start and falsely fail as "agent didn't answer." Check:
   ```
   pc cloud organizations select -o tuilux
   pc cloud agent status bayview-pharmacy
   ```
   Want `min_agents` ≥ the scenario count and status showing that many "agents Ready." Currently `min_agents = 12` (set in `server/pcc-deploy.toml`).

## The 2 questions the skill will ask, and what to answer

| Prompt | Answer |
|---|---|
| **How many scenarios?** (it proposes 12–15) | Say **8** — keeps the run ~10–12 min and credits low. |
| **Mock data handling?** | Pick **"Get a list of mock data to add"**, then reply *"already seeded in mock_backend.py, proceed."* Do **NOT** pick "Skip" — that deletes the refill/verify scenarios. |
| Connection mode (only if it still asks) | **pipecat-v2**. |

Grounding is already handled: the agent description pins every passing scenario to Jane Doe / 1985-04-12, so autogen won't invent mismatched identities.

## Run in batches of 3 (REQUIRED — avoids the latency flags)

The agent is fast per call (LLM first-token ~150ms), but the **NVIDIA STT + Nemotron LLM endpoints are shared event GPUs**. Firing many calls at once queues them → some turns stall >10s and trip "Infrastructure Issues" (looks like "the agent went silent"). Cekura confirmed: **run at most 3 concurrent.**

`scenarios_run_pipecat_v2` has **no concurrency parameter**, so cap it by **how many scenario IDs you pass per run call**: pass **3 at a time**, wait for that result's `status == "completed"`, then submit the next 3. Same suite, chained in groups of 3.

```
scenarios = [all your IDs]
for batch in chunks_of_3(scenarios):
    result = scenarios_run_pipecat_v2(scenarios=batch)      # 3 calls, 3 warm slots
    poll results_retrieve(result.id) until status == "completed"
# then merge the batch result_ids for the report
```

When running via Claude: just say "run these in batches of 3" — Claude chunks the IDs and polls each batch before the next.

## What to expect

- A suite of N tests runs as ⌈N/3⌉ batches; each batch ≈2–3 min. An 8-test suite ≈ 3 batches ≈ 8–10 min.
- Each batch is its own `result_<id>` — the report aggregates across them.
- With ≤3 concurrent, the >10s "Infrastructure Issues" flags disappear (proven: a solo call shows "No infrastructure issues detected").

## After the event — revert the warm floor (it bills continuously)

Cekura credits expire **2026-05-31 18:00 UTC**. Once you're done evaluating, drop the reserved agents back to 1:

```
# edit server/pcc-deploy.toml -> [scaling] min_agents = 1, then:
pc cloud organizations select -o tuilux
pc cloud deploy -y --config-file server/pcc-deploy.toml --build-dir server --dockerfile Dockerfile
```
