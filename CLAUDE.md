# CLAUDE.md

Guidance for Claude Code and teammates working in this repo. Keep it current — if a command or gotcha changes, fix it here.

## What this is

A real-time voice AI agent built on **Pipecat**, forked from the official YC Voice Agents Hackathon starter (`pipecat-ai/yc-voice-agents-hackathon`).

The starter ships a flower-shop ordering bot ("Field & Flower"). We're repurposing it into our own agent.

> **What we're building:** Bayview Pharmacy, a secure prescription-refill voice agent we evaluate and continuously improve with **Cekura** — the point of the event is a production-grade agent with a real eval loop, not a one-off demo.

All application code lives in **`server/`**. It's Python.

## The pipeline (mental model)

```
caller audio → STT → LLM (+ tool calls) → TTS → caller audio
              Pipecat orchestrates everything: VAD, turn-taking, barge-in, streaming
```

Two interchangeable bot variants (same pipeline shape, different services; the product logic is identical between them):

- **`bot-nemotron.py`** — NVIDIA Nemotron Speech STT + Nemotron-3-Super LLM + Gradium TTS. (Primary — uses the sponsor's open models.)
- **`bot-gpt.py`** — Gradium STT + OpenAI GPT-4.1 + Gradium TTS. (Fastest to get talking; good fallback.)

Transports: **SmallWebRTC** (local/browser) and **Twilio** (phone). Deploy target: **Pipecat Cloud**.

## Key files (`server/`)

| File | What it is |
|---|---|
| `bot-nemotron.py` / `bot-gpt.py` | Bot entrypoints. The **pipeline, `system_instruction`, and tool functions** live here. This is where the product logic is. |
| `mock_backend.py` | The "database" — in-memory dicts the tools read from. Swap this to change the domain. |
| `nvidia_stt.py` | Custom Pipecat STT service for NVIDIA streaming ASR (WebSocket). Don't edit unless debugging STT. |
| `nemotron_llm.py` | Thin OpenAI-compatible LLM service for Nemotron via vLLM. Don't edit unless debugging LLM. |
| `.env` | Secrets + endpoints. **Never commit.** Copy from `.env.example`. |
| `pcc-deploy.toml` | Pipecat Cloud deploy config. |
| `pyproject.toml` | Dependencies (managed by `uv`). |

## Run it locally

From **`server/`**:

```bash
uv sync                    # install deps (first time only)
uv run bot-nemotron.py     # or: uv run bot-gpt.py
```

Open **http://localhost:7860** (the **Pipecat Playground**, a built-in dev/test client) and click **Connect**. First launch takes ~20s while Pipecat downloads VAD + turn-detection models.

The Playground is a generic debugging UI — not our product demo. A custom demo UI would be a **separate app** (Pipecat `client-react` over RTVI) on its own port, pointed at the same running bot.

## Environment / config (`server/.env`)

| Var | For | Needed by |
|---|---|---|
| `GRADIUM_API_KEY`, `GRADIUM_VOICE_ID` | TTS | both bots |
| `OPENAI_API_KEY` | GPT-4.1 LLM | `bot-gpt` only |
| `NVIDIA_ASR_URL` | Nemotron STT endpoint | `bot-nemotron` |
| `NEMOTRON_LLM_URL`, `NEMOTRON_LLM_MODEL` | Nemotron LLM endpoint + model id | `bot-nemotron` |
| `NEMOTRON_ENABLE_THINKING` | `true`/`false` — keep **`false`** for voice (reasoning adds latency and can leak into speech) | `bot-nemotron` |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | Phone transport | phone only |
| `ENV=local` | **Required for local dev** (see gotchas) | local |

NVIDIA endpoints are provided by the event and can change — get current values from the organizers.

## Gotchas (these will bite)

- **Set `ENV=local`** in `.env` for local runs. Without it the bot imports the Krisp noise filter (Pipecat Cloud-only, not installed locally) and **crashes the moment you click Connect**.
- **Restart the bot after editing `.env`** — env is loaded once at startup.
- **Keep `NEMOTRON_ENABLE_THINKING=false`** for voice. Reasoning tokens add seconds and can be spoken aloud if the server lacks a reasoning parser.
- **Sample rates:** WebRTC is 16 kHz in / 24 kHz out. Twilio media arrives as 8 kHz mu-law, but the Twilio branch should resample inbound audio to 16 kHz for NVIDIA STT and keep outbound audio at 8 kHz for phone playback.
- **One bot per port (7860).** Kill the old instance before restarting, or you'll get "address already in use."

## Where the product lives (how to customize)

To change what the agent does, edit the bot file (`bot-nemotron.py` is primary):

1. **`system_instruction`** — persona, rules, conversation style.
2. **Tool functions** (the `async def` handlers inside `run_bot`) + the `tool_functions` list — what the agent can *do*.
3. **`mock_backend.py`** — the data those tools read.
4. Register each tool with `llm.register_direct_function(fn)` and include it in `ToolsSchema` (existing code does both — follow the pattern). Tools return via `await params.result_callback({...})`.

**Enforce hard rules in code, not just the prompt.** If the agent "must verify before X," put an `if`-check in the tool handler. Prompts can be talked around; code can't.

## Evaluate with Cekura (the event's whole point)

Drive Cekura from Claude Code:

```bash
/plugin marketplace add cekura-ai/cekura-skills
/plugin install cekura@cekura-skills
/cekura-report     # runs scenarios against the agent → transcripts + scores
```

Select **Pipecat** as the provider when connecting. Failed scenarios become regression tests: fix the agent, re-run, watch the score climb. Keep test calls short — voice testing burns credits fast (~5 credits/min).

## Deploy (Pipecat Cloud)

```bash
uv tool install pipecat-ai-cli
pc cloud auth login
pc cloud secrets set <secret-name> --file .env   # upload secrets (name per pcc-deploy.toml)
pc cloud deploy
```

For phone: buy a Twilio number and point a TwiML Bin at `wss://api.pipecat.daily.co/ws/twilio` with the `_pipecatCloudServiceHost` parameter. Full steps are in `README.md`. **Get it working locally over WebRTC before deploying.**

## Engineering principles

Hackathon reality: ship a working, demoable agent by 6 PM. These keep the code sane without slowing you down.

- **Simplicity first / YAGNI.** Build the smallest thing that works — no speculative abstractions, no config nobody asked for, no error handling for impossible cases. In a 9-hour build, over-engineering is the #1 self-inflicted wound. If 50 lines do it, don't write 200.
- **Make it work, then make it good.** Get the happy path talking first; harden the rules, edge cases, and eval loop second.
- **Think before non-trivial changes.** For anything past a small edit, state the approach in one line first so a teammate can course-correct cheaply.
- **Surgical changes, no dead code.** Touch only what the task needs; don't refactor working code or "improve" adjacent files; remove anything your change leaves unused. Match the existing style even if you'd do it differently.
- **Verify fast-moving APIs, don't guess.** Pipecat / NVIDIA / Cekura / Gradium change often — check current docs or the installed version instead of recalling an API shape.

## Conventions

- **Python 3.12+**, **`uv`** for everything (`uv run`, `uv sync`). No bare `pip` / `python`.
- **Ruff** for lint + format: line length 100, import-sort + pyupgrade. Run `uv run ruff format` and `uv run ruff check` before committing.
- Tools are `async def`, returning via `await params.result_callback({...})` — follow the existing pattern.
- **Commits:** small and descriptive, using **your own git identity** (don't copy another person's author/signoff).
- **Secrets stay in `.env`** (gitignored). Never put keys in code, commits, screenshots, or chat.
- Don't commit `.env`, `.venv/`, or `__pycache__/`.

## Reference

- Pipecat docs: https://docs.pipecat.ai
- Cekura docs: https://docs.cekura.ai
- Full setup, deploy, and Cekura walkthrough: this repo's `README.md`.
