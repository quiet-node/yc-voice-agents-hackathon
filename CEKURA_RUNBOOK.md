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
   Want `min_agents` ≥ the *effective* concurrency and status showing that many "agents Ready." Currently `min_agents = 1` (reverted after the eval to stop billing; bump it up in `server/pcc-deploy.toml` + redeploy before a run if you want a warm floor).

## The 2 questions the skill will ask, and what to answer

| Prompt | Answer |
|---|---|
| **How many scenarios?** (it proposes 12–15) | Say **8** — keeps the run ~10–12 min and credits low. |
| **Mock data handling?** | Pick **"Get a list of mock data to add"**, then reply *"already seeded in mock_backend.py, proceed."* Do **NOT** pick "Skip" — that deletes the refill/verify scenarios. |
| Connection mode (only if it still asks) | **pipecat-v2**. |

Grounding is already handled: the agent description pins every passing scenario to Jane Doe / 1985-04-12, so autogen won't invent mismatched identities.

## Concurrency — let the org limit throttle it

The bottleneck is the **shared NVIDIA STT + Nemotron LLM endpoints** (event GPUs): too many simultaneous calls queue → some turns stall >10s and trip "Infrastructure Issues" (looks like the agent went silent). Keep effective concurrency around **3**.

There is **no per-run or per-project concurrency parameter** (`scenarios_run_pipecat_v2` has none; project 5875 has no such field). Cekura enforces a **max-parallel limit at the org level** (org 4841, set to 3 by the Cekura team). If that's active for pipecat-v2, just **submit the whole suite in one run call** and it self-throttles to 3 at a time.

**Verify it's actually throttling (free — read it off the run you're already doing):** submit ≥4 scenarios, then check each run's `call_started_at`. If only 3 start together and the rest are staggered → the org limit works, submit all at once from now on. If all start within the same ~second → it isn't gating pipecat-v2, fall back to manual batching: pass **3 scenario IDs per `scenarios_run_pipecat_v2` call**, poll `results_retrieve` to `completed`, repeat. (Just tell Claude "run these in batches of 3".)

## What to expect

- Each `scenarios_run_pipecat_v2` call is one `result_<id>`; if you chunk the suite, the report aggregates across them.
- With effective concurrency ≤3, the >10s "Infrastructure Issues" flags disappear (proven: a solo call shows "No infrastructure issues detected"); at 8-at-once they reappear.

## After the event — revert the warm floor (it bills continuously)

Cekura credits expire **2026-05-31 18:00 UTC**. Once you're done evaluating, drop the reserved agents back to 1:

```
# edit server/pcc-deploy.toml -> [scaling] min_agents = 1, then:
pc cloud organizations select -o tuilux
pc cloud deploy -y --config-file server/pcc-deploy.toml --build-dir server --dockerfile Dockerfile
```
