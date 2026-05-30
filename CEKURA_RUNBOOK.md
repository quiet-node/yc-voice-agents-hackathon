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

## What to expect

- ~8 serial voice calls, ~10–12 min total, ~5 Cekura credits/min.
- Report saved as `result_<id>_report.md`.
- Residual risk: an occasional turn may still flag >10s (raw Nemotron inference latency, not turn-taking). That's a model-latency lever, not a config bug.

## After the event — revert the warm floor (it bills continuously)

Cekura credits expire **2026-05-31 18:00 UTC**. Once you're done evaluating, drop the reserved agents back to 1:

```
# edit server/pcc-deploy.toml -> [scaling] min_agents = 1, then:
pc cloud organizations select -o tuilux
pc cloud deploy -y --config-file server/pcc-deploy.toml --build-dir server --dockerfile Dockerfile
```
