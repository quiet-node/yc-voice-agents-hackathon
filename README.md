# Bayview Pharmacy Video Agent

An accessible video-first pharmacy assistant for prescription recovery, refill support, and medication-status calls — with a self-improving voice-agent harness underneath.

Built for the YC Voice Agents Hackathon hosted by [Cekura](https://cekura.com) and [Daily](https://daily.co), in partnership with [NVIDIA](https://nvidia.com), [AWS](https://aws.amazon.com), and [Twilio](https://twilio.com).

## Why this matters

For many families, prescription recovery is not a simple phone call. Elderly patients, stroke survivors, people with speech or hearing challenges, and family caregivers often have to navigate long phone trees, repeat sensitive information, and understand medication instructions through audio alone. That experience is especially hard when word recognition, recall, or confidence on the phone has been affected by disability or age.

**Bayview Pharmacy** turns that call into a video conversation with a visible AI pharmacy assistant. The agent can speak, listen, show a human-like visual presence, support English and Spanish, and use video cues to understand the caller's context. A visual face and lip movement can make speech easier to follow for patients who rely on visual word recognition, while video and gesture support reduce the burden of navigating a phone-only system.

Our goal is to make prescription access easier for millions of disabled and elderly patients who are underserved by traditional call centers. Instead of forcing every patient through the same audio-only workflow, Bayview gives them a more human, multimodal, multilingual way to recover and manage critical medication access.

## What we built

**Bayview Pharmacy** is a secure prescription-refill and recovery agent that verifies caller identity before revealing any prescription data, checking refill status, or placing a refill. All backend calls are mocked so it runs with only AI service keys.

The product layer includes:

- **Google Meet-style video call UI** with pre-join camera/microphone checks, in-call device controls, scroll-contained transcript, active-speaker indicators, and equal participant video tiles.
- **Twilio voice-call support** for patients who still need a normal phone number. The same Pipecat agent logic can answer PSTN calls through a Twilio media stream.
- **AI video avatar layer** rendered through Pipecat as a presentation layer, while preserving the existing bot brain and audio path if video rendering is unavailable.
- **Multilingual onboarding** where the agent first asks: "Signify 1 for English, 2 for Spanish." Gesture selection and spoken cues like "Hola" or "2" switch the conversation into Spanish.
- **Visual cue detection** using MediaPipe-style browser vision. For the hackathon demo, showing a cup can simulate an empty prescription bottle and prompt refill assistance.
- **Gesture recognition** for language selection and low-friction accessibility workflows.
- **Secure pharmacy flow** with identity verification before prescription disclosure or refill actions.

Underneath the product, we built a **self-improvement harness** that closes the loop between Cekura evaluations and code changes — automatically:

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

## Infrastructure Overview

The application is split into three deployable/runtime surfaces:

1. **Pipecat agent runtime** in `server/`
   - `bot-nemotron.py` is the primary agent.
   - `bot-gpt.py` is the OpenAI fallback.
   - The bot is a Pipecat pipeline: transport input -> STT -> language router -> user context aggregator -> LLM -> TTS -> optional video avatar renderer -> transport output.
   - The pharmacy tools are registered directly on the LLM service and call the mocked backend in `mock_backend.py`.
   - The same business logic runs across video calls, Pipecat Cloud sessions, and Twilio voice calls.

2. **Browser video-call client** in `server/demo_client/`
   - Served locally by `demo_frontend.py` through the Pipecat runner app.
   - Uses WebRTC for media, an RTVI data channel for transcript/control messages, and browser APIs for camera, microphone, speaker selection, and audio-level checks.
   - Runs MediaPipe Tasks Vision in the browser for object and gesture recognition. The server does not need to receive raw camera frames for the demo cue logic.

3. **Twilio voice-call path**
   - Twilio calls connect to the same Pipecat agent through `FastAPIWebsocketTransport`.
   - `parse_telephony_websocket` extracts the stream and call identifiers.
   - `TwilioFrameSerializer` handles Twilio media stream framing.
   - The voice path uses 8 kHz input/output sample rates and disables the video/avatar layer because PSTN calls are audio-only.

4. **Self-improvement harness** in `harness/`
   - `generate_dashboard.py` normalizes Cekura run output into `report.json`, `fix_plan.md`, and a static dashboard.
   - `serve_dashboard.py` serves the dashboard and local API endpoints for refresh, transcripts, generated tests, and healing status.
   - `webhook_server.py` receives Cekura webhook failures.
   - `self_heal.py` applies a targeted fix, deploys through Pipecat Cloud, re-runs the same Cekura scenario, and opens a PR only when the result improves.

### Runtime Pipeline

```text
Browser / Phone caller
        |
        v
Pipecat transport
  - SmallWebRTC locally
  - Daily in Pipecat Cloud
  - Twilio websocket for phone calls
        |
        v
Speech-to-text
  - Gradium by default
  - optional NVIDIA Parakeet websocket service
        |
        v
LanguagePreferenceProcessor
  - detects English/Spanish selection from speech cues
  - supports "Hola", "2", "dos", "Spanish", "English", "1"
        |
        v
LLM
  - Nemotron 3 Super 120B primary path
  - GPT-4.1 fallback path
        |
        v
Pharmacy tools
  - verify_identity
  - get_prescriptions
  - refill_prescription
  - end_call
        |
        v
Gradium TTS -> Pipecat output -> caller
```

### Hugging Face / NVIDIA Model Integration

We did not put large Hugging Face model weights inside the bot container. The bot stays lightweight and calls model services over the network.

For speech recognition, we implemented an optional NVIDIA Parakeet STT path:

- `stt_provider.py` selects the STT backend from `STT_PROVIDER`.
- `STT_PROVIDER=gradium` uses Gradium as the production-safe default.
- `STT_PROVIDER=parakeet` creates `NVidiaWebSocketSTTService` from `nvidia_stt.py`.
- `nvidia_stt.py` streams Pipecat `UserAudioRawFrame` audio to the NVIDIA Parakeet websocket endpoint and converts websocket transcripts back into Pipecat `TranscriptionFrame` and `InterimTranscriptionFrame` objects.
- The service expects 16 kHz mono PCM, so WebRTC/Daily calls use it directly. Twilio's 8 kHz path falls back to Gradium unless explicitly overridden.

This is the Hugging Face/NVIDIA implementation pattern: the Parakeet-family ASR model is available through Hugging Face/NVIDIA resources, but our application consumes it as a hosted websocket ASR service. Pipecat handles audio transport, frame processing, turn aggregation, LLM orchestration, and TTS. That kept the demo deployable on Pipecat Cloud without shipping local GPU model weights in the application image.

### Pipecat Execution Model

Local execution uses the Pipecat runner:

```bash
cd server
STT_PROVIDER=parakeet uv run bot-nemotron.py
```

The app mounts the video UI at `http://localhost:7860/demo/` and exposes Pipecat's local `/start` flow for a SmallWebRTC call. The browser creates the offer, Pipecat starts the session, and the bot pipeline begins once the WebRTC transport connects.

Cloud execution uses Pipecat Cloud:

- `Dockerfile` starts from `dailyco/pipecat-base:latest`.
- Pipecat Cloud expects `bot.py`, so the image copies `bot-nemotron.py` to `bot.py`.
- Supporting modules are copied into the image: `mock_backend.py`, `nemotron_llm.py`, `nvidia_stt.py`, `stt_provider.py`, `language_router.py`, and `video_avatar.py`.
- Pipecat Cloud starts WebRTC sessions over Daily rooms; the same bot code handles `DailyRunnerArguments`.
- Twilio phone calls use the Pipecat websocket transport and the same core agent logic. The audio stream is serialized through `TwilioFrameSerializer`, sample-rate overrides are set to 8 kHz, and video/avatar output is disabled for the PSTN path.

---

## Tech Stack

| Layer | Service |
|---|---|
| **STT** | [Gradium](https://gradium.ai) default · NVIDIA Parakeet websocket service optional |
| **LLM** | Nemotron 3 Super 120B (NVIDIA/AWS) · GPT-4.1 (fallback) |
| **TTS** | [Gradium](https://gradium.ai) |
| **Transport** | SmallWebRTC (local video) · Daily (Pipecat Cloud) · Twilio (phone) |
| **Orchestration** | [Pipecat](https://pipecat.ai) |
| **Deploy** | [Pipecat Cloud](https://pipecat.daily.co) |
| **Eval** | [Cekura](https://cekura.com) |
| **Healing** | Claude (claude-sonnet-4-6 via Anthropic API) |
| **Video UI** | Browser WebRTC demo client in `server/demo_client/` |
| **Vision/Gestures** | MediaPipe Tasks Vision in the browser |
| **Avatar layer** | Optional Pipecat video renderer; audio-only fallback by default |

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

Open **http://localhost:7860** and click **Join now**. First launch takes ~20s while Pipecat downloads VAD + turn-detection models.

The local demo opens a video-call interface with:
- Camera, microphone, and speaker selectors before and during the call
- Transcript/chat on the right side
- Visual detection and gesture recognition badges
- AI avatar status
- Language selection as the first agent question

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
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` | Optional Twilio REST lookup for call metadata |
| `ENV` | Set `local` for local dev — required |
| `CEKURA_API_KEY` | For self-healing harness |
| `ANTHROPIC_API_KEY` | For Claude patch proposals |
| `NGROK_DOMAIN` | Static ngrok domain for Cekura webhooks |
| `CEKURA_WEBHOOK_SECRET` | Shared secret for webhook auth |
| `AVATAR_PROVIDER` | Optional Pipecat video avatar layer; defaults to `none` for audio-only |
| `AVATAR_VIDEO_WIDTH` / `AVATAR_VIDEO_HEIGHT` | Output dimensions for optional avatar video tracks |

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

The dashboard sidebar also includes hard-coded sample agents for demo storytelling:
- **Bayview Pharmacy** — real current report data
- **Voice Agent Auto-Improvement** — sample self-healing metrics
- **Voice Agent Scammer Detection** — sample fraud-detection metrics

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
