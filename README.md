# Voice Agent Self-Improvement Harness

A production-grade voice AI agent with a fully autonomous self-healing loop — built for the YC Voice Agents Hackathon hosted by [Cekura](https://cekura.com) and [Daily](https://daily.co), in partnership with [NVIDIA](https://nvidia.com), [AWS](https://aws.amazon.com), and [Twilio](https://twilio.com).

## What we built

**Bayview Pharmacy** — a secure prescription-refill voice agent that verifies caller identity before revealing any prescription data, checking refill status, or placing a refill. All backend calls are mocked so it runs with only AI service keys.

On top of the agent, we built a **self-improvement harness** that closes the loop between Cekura evaluations and code changes — automatically:

```
Cekura detects failure
       ↓
Webhook receives event
       ↓
Claude proposes a targeted patch to bot-nemotron.py
       ↓
Patch applied, pc cloud deploy runs
       ↓
Cekura re-runs the same scenario
       ↓
PR opened if score improved / no change if not
```

The dashboard shows live healing status, failure clusters, an evidence explorer, and a fix queue — and auto-queues all open items for healing the moment new data loads.

---

## Tech stack

| Layer | Service |
|---|---|
| **STT** | [Gradium](https://gradium.ai) (default) · NVIDIA Parakeet websocket (optional) |
| **LLM** | Nemotron 3 Super 120B (NVIDIA/AWS) · GPT-4.1 (fallback) |
| **TTS** | [Gradium](https://gradium.ai) |
| **Transport** | SmallWebRTC (local) · Daily (Pipecat Cloud) · Twilio (phone) |
| **Orchestration** | [Pipecat](https://pipecat.ai) |
| **Deploy** | [Pipecat Cloud](https://pipecat.daily.co) |
| **Eval** | [Cekura](https://cekura.com) |
| **Healing** | Claude (claude-sonnet-4-6 via Anthropic API) |

Bot files in `server/`:
- `bot-nemotron.py` — primary (Gradium STT + Nemotron LLM + Gradium TTS)
- `bot-gpt.py` — fallback (Gradium STT + GPT-4.1 + Gradium TTS)

---

## Local development

### Prerequisites

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/) package manager
- API keys: Gradium + OpenAI (for `bot-gpt`) or NVIDIA endpoints (for `bot-nemotron`)

### Setup

```bash
git clone https://github.com/quiet-node/yc-voice-agents-hackathon.git
cd yc-voice-agents-hackathon/server
cp .env.example .env
# Fill in GRADIUM_API_KEY, GRADIUM_VOICE_ID, OPENAI_API_KEY, etc.
# Set ENV=local (required — disables Krisp noise filter which is Cloud-only)
uv sync
```

### Run the bot

```bash
cd server
uv run bot-nemotron.py   # NVIDIA stack (primary)
# or
uv run bot-gpt.py        # OpenAI stack (fallback)
```

Open **http://localhost:7860** and click **Connect**. First launch takes ~20s while Pipecat downloads VAD + turn-detection models.

### Key `.env` variables

| Variable | Purpose |
|---|---|
| `GRADIUM_API_KEY` | STT + TTS |
| `GRADIUM_VOICE_ID` | TTS voice |
| `OPENAI_API_KEY` | GPT-4.1 LLM (bot-gpt only) |
| `STT_PROVIDER` | `gradium` by default; set `parakeet` for NVIDIA Parakeet websocket STT |
| `PARAKEET_STT_URL` | Optional Parakeet websocket URL; falls back to `NVIDIA_ASR_URL` |
| `NVIDIA_ASR_URL` | NVIDIA Parakeet STT WebSocket |
| `NEMOTRON_LLM_URL` | Nemotron LLM endpoint |
| `NEMOTRON_LLM_MODEL` | Model ID |
| `NEMOTRON_ENABLE_THINKING` | Keep `false` for voice (adds latency, leaks into speech) |
| `ENV` | Set `local` for local dev — required |
| `CEKURA_API_KEY` | For self-healing harness |
| `ANTHROPIC_API_KEY` | For Claude patch proposals |
| `NGROK_DOMAIN` | Static ngrok domain for Cekura webhooks |
| `CEKURA_WEBHOOK_SECRET` | Shared secret for webhook auth |

NVIDIA endpoints (available during the hackathon):

```bash
STT_PROVIDER=parakeet
NVIDIA_ASR_URL=ws://44.241.251.184:8080
NEMOTRON_LLM_URL=http://nemotron-fleet-alb-1322439314.us-west-2.elb.amazonaws.com/v1
NEMOTRON_LLM_MODEL=nvidia/nemotron-3-super
```

---

## Deploy to Pipecat Cloud

### Install CLI and log in

```bash
uv tool install pipecat-ai-cli
pc cloud auth login
```

### Upload secrets and deploy

```bash
cd server
pc cloud secrets set bayview-pharmacy-secrets --file .env
pc cloud deploy
```

The `Dockerfile` copies `bot-nemotron.py` as `bot.py`, so every deploy picks up your latest prompt and tool changes.

### Wire up Twilio (optional, for phone calls)

1. [Buy a Twilio number](https://help.twilio.com/articles/223135247) with voice capability.
2. Get your Pipecat Cloud org name: `pc cloud organizations list`
3. Create a TwiML Bin:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="wss://api.pipecat.daily.co/ws/twilio">
      <Parameter name="_pipecatCloudServiceHost" value="bayview-pharmacy.YOUR_ORG_NAME"/>
    </Stream>
  </Connect>
</Response>
```

4. Attach the TwiML Bin to your number under **Voice Configuration**.

---

## Self-Improvement Harness

The harness lives in `harness/` and has three components that work together:

```
harness/
├── self_heal.py          # Core loop: run scenario → patch → deploy → re-run → PR
├── webhook_server.py     # aiohttp server; receives Cekura events, queues heals serially
├── serve_dashboard.py    # Local HTTP server; serves dashboard + exposes /api/heal-status
├── generate_dashboard.py # Builds index.html + report.json + fix_plan.md from Cekura data
└── start.sh              # One-command launcher: ngrok + webhook server
```

### How the healing loop works

1. **Cekura** runs scenarios against the deployed Pipecat Cloud agent and sends webhook events on failure.
2. **`webhook_server.py`** receives the event (authenticated via `CEKURA_WEBHOOK_SECRET`), deduplicates, and enqueues the scenario.
3. **`self_heal.py`** picks up the queued scenario, calls Claude with the failure transcript to propose a targeted patch to `bot-nemotron.py`, applies it, runs `pc cloud deploy`, waits for the deployment to go live, and re-runs the Cekura scenario.
4. If the score improves, a GitHub PR is opened. Otherwise the change is discarded.
5. **`serve_dashboard.py`** serves a live dashboard at `http://localhost:8765` that polls for heal status and auto-queues new failures.

### Run the webhook server + dashboard

**Terminal 1 — webhook server** (receives Cekura events, runs heals):

```bash
cd yc-voice-agents-hackathon/server
uv run python3 ../harness/webhook_server.py --port 8888
```

Or use `start.sh` to launch ngrok + webhook server together (requires `NGROK_DOMAIN` in `server/.env`):

```bash
bash harness/start.sh
```

The script prints the public webhook URL to configure in Cekura → Agent Settings → Webhook URL.

**Terminal 2 — dashboard server**:

```bash
cd yc-voice-agents-hackathon/server
uv run python3 ../harness/serve_dashboard.py
```

Open **http://localhost:8765**. The dashboard:
- Shows KPIs, failure clusters, evidence explorer, and a fix queue
- Auto-queues all open fix items for healing on load and after each data refresh
- Polls for heal status every 3 seconds and auto-refreshes data when a heal completes
- Matrix and Runs tabs show the full Cekura scenario breakdown

### Run a heal manually

```bash
cd yc-voice-agents-hackathon/server
uv run python3 ../harness/self_heal.py --scenario <SCENARIO_ID>
uv run python3 ../harness/self_heal.py --scenario <SCENARIO_ID> --max-iterations 3
uv run python3 ../harness/self_heal.py --scenario <SCENARIO_ID> --dry-run     # no deploy
uv run python3 ../harness/self_heal.py --scenario <SCENARIO_ID> --no-deploy   # patch only
```

### Generate the dashboard from a Cekura result

```bash
cd yc-voice-agents-hackathon/server

# From local sample data
uv run python3 ../harness/generate_dashboard.py \
  --input ../harness/examples/bayview_cekura_report_sample.json \
  --out ../harness/runs/demo

# From the latest real Cekura run
uv run python3 ../harness/generate_dashboard.py \
  --cekura-result-id latest \
  --cekura-agent-id 18021 \
  --out ../harness/runs/latest-cekura
```

Output: `index.html` (dashboard), `report.json` (machine-readable), `fix_plan.md` (human-readable).

### ngrok setup

The webhook server needs a public HTTPS URL so Cekura can reach it. We use a static ngrok domain:

1. Install ngrok and authenticate: `ngrok config add-authtoken <token>`
2. Set `NGROK_DOMAIN=your-subdomain.ngrok-free.app` in `server/.env`
3. Run `bash harness/start.sh` — it starts ngrok and the webhook server together

Configure the printed URL in **Cekura → Agent Settings → Webhook URL**.

---

## Test with Cekura

[Cekura](https://cekura.com) runs real conversations against your agent, scores them, and tells you what failed.

### Sign up

Create your account at **[dashboard.cekura.ai](https://dashboard.cekura.ai)**.

### Drive Cekura from Claude Code

```bash
/plugin marketplace add cekura-ai/cekura-skills
/plugin install cekura@cekura-skills
/cekura-report
```

Select **Pipecat** as the provider. Full details: [docs.cekura.ai → Pipecat](https://docs.cekura.ai/documentation/integrations/pipecat/automated).

---

## References

### Pipecat
- [Documentation](https://docs.pipecat.ai/)
- [Pipecat Cloud](https://docs.pipecat.ai/pipecat-cloud/introduction)
- [Examples](https://github.com/pipecat-ai/pipecat-examples)
- [Discord](https://discord.gg/pipecat)

### Cekura
- [Claude Code guide](https://docs.cekura.ai/mcp/claude-code-guide)
- [Cekura skills](https://docs.cekura.ai/mcp/skills)
- [Pipecat integration](https://docs.cekura.ai/documentation/integrations/pipecat/automated)
- [Docs](https://docs.cekura.ai) · [Dashboard](https://dashboard.cekura.ai)

### Twilio
- [Developer Hub](https://www.twilio.com/en-us/developers)
- [Dev Phone](https://www.twilio.com/docs/labs/dev-phone)
