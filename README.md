# Bayview Pharmacy Video Agent

An accessible, video-first pharmacy assistant for prescription refills and recovery — with a **self-improving eval loop** underneath that turns every Cekura failure into a deployed, verified fix.

🎥 **[Watch the demo video](https://www.youtube.com/watch?v=FH_uwZJSRJw)**

Built for the **YC Voice Agents Hackathon** hosted by [Cekura](https://cekura.com) and [Daily](https://daily.co), with [NVIDIA](https://nvidia.com), [AWS](https://aws.amazon.com), and [Twilio](https://twilio.com).

**Hackathon themes, all three covered:** evaluate + improve agent performance (Cekura self-heal loop) · open-weights models (NVIDIA Nemotron) · voice (Pipecat).

## Try it yourself

📞 **Call the agent: +1 (628) 300-0587**

The agent verifies your identity by **name + date of birth** before it discloses or refills anything. Use one of the sample patients below:

| Patient | Date of birth | Prescriptions (refills left · pickup status) |
|---|---|---|
| **Jane Doe** | 1985-04-12 | Lisinopril 10mg (2 · ready) · Atorvastatin 20mg (0 · not ready) |
| **John Smith** | 1972-09-30 | Metformin 500mg (5 · not ready) |
| **Maria Garcia** | 1990-11-23 | Levothyroxine 50mcg (1 · ready) · Albuterol inhaler (3 · not ready) |
| **David Lee** | 1968-02-07 | Amlodipine 5mg (0 · not ready) |
| **Susan Brown** | 1995-07-19 | Sertraline 50mg (4 · ready) |

Try a refill ("refill my Lisinopril"), a status check ("is my prescription ready?"), a no-refills-left case (Atorvastatin / Amlodipine), or test the privacy guardrail by asking for meds **before** giving your DOB.

---

## Why it matters

For elderly patients, stroke survivors, and people with speech or hearing challenges, a prescription refill call means long phone trees, repeating sensitive data, and parsing medication instructions through audio alone. Bayview turns that call into a **video conversation with a visible AI pharmacist** — speaking, listening, showing a face and lip movement, supporting English + Spanish, and reading visual cues — so the millions underserved by audio-only call centers get a more human way to manage critical medication access.

## What we built

A secure prescription-refill agent that **verifies identity before revealing any prescription data or placing a refill** (enforced in tool code, not just the prompt). Backend is mocked, so it runs on AI keys alone.

- **Google Meet-style video UI** — pre-join camera/mic checks, in-call device controls, scroll-contained transcript, active-speaker indicators, equal participant tiles.
- **Multilingual onboarding** — agent opens with "Signify 1 for English, 2 for Spanish"; spoken cues ("Hola", "2", "dos") or a gesture switch the conversation to Spanish.
- **Visual cue + gesture recognition** — MediaPipe Tasks Vision in the browser; showing a cup simulates an empty pill bottle and prompts refill help. Gestures drive language selection and low-friction accessibility flows.
- **AI video avatar layer** — rendered through Pipecat as a presentation layer, with graceful fallback to the audio path if video is unavailable.
- **Twilio voice support** — the same agent answers normal PSTN phone calls over a Twilio media stream.
- **Self-improvement harness** — Cekura failures auto-patch the bot, redeploy, re-run, and open a PR only when the score improves (below).

## Built during the hackathon

We forked the starter's flower-shop ordering bot and rebuilt it end to end. **New this hackathon:** the entire Bayview pharmacy product (persona, secure identity-gated tools, mock backend), the video-call client (`server/demo_client/`), MediaPipe vision + gesture and multilingual routing, the optional Nemotron/Parakeet integration, and the full self-improvement harness (`harness/`). The original starter shipped none of this.

## How we used Cekura, Nemotron, and Pipecat

- **Cekura — eval + self-improvement (the centerpiece).** Cekura runs real scored conversations against the deployed agent. We wired its failure webhooks into an automated loop: a failed scenario becomes a regression test, GPT-5.5 proposes a targeted patch, we redeploy, and Cekura **re-runs the same scenario**. A PR opens **only when the re-run score improves** — so every merged change is a measured gain, not a guess. We aimed Cekura at the security-critical behaviors (no prescription data before identity verification, correct refill handling, DOB-mismatch refusal): across a dozen eval batteries on the deployed agent, our first grounded 8-scenario run passed **4/8** expected outcomes (50%), and after the failures drove a code-level identity guardrail plus turn-taking fixes the post-fix battery passed **6/8 (75%)**.
- **NVIDIA Nemotron — open weights.** Nemotron 3 Super 120B is the primary LLM (GPT-4.1 is the fallback), and we added an optional NVIDIA Parakeet websocket STT path. Weights stay out of the container — the bot consumes them as hosted services, so it deploys to Pipecat Cloud with no local GPU.
- **Pipecat — voice + video orchestration.** One pipeline (transport → STT → language router → LLM + tools → TTS → optional avatar) runs identically across local WebRTC, Daily on Pipecat Cloud, and Twilio phone calls.

## The self-improvement loop

```
Cekura detects failure
   → webhook_server.py receives + dedupes the event
   → self_heal.py asks GPT-5.5 (via token router) for a targeted patch to bot-nemotron.py
   → patch applied → pc cloud deploy
   → Cekura re-runs the same scenario
   → score improved? open a PR.  no change? discard.
```

A live dashboard (`http://localhost:8765`) shows KPIs, failure clusters, an evidence explorer, and a fix queue — and auto-queues open items for healing on every data refresh.

## Architecture

```
Browser / Phone caller
   │  SmallWebRTC (local) · Daily (Pipecat Cloud) · Twilio websocket (phone)
   ▼
STT            Gradium (default) · NVIDIA Parakeet websocket (optional, 16 kHz)
   ▼
Language router   detects English/Spanish from speech + gesture cues
   ▼
LLM            Nemotron 3 Super 120B (primary) · GPT-4.1 (fallback)
   ▼
Pharmacy tools    verify_identity · get_prescriptions · refill_prescription · end_call  → mock_backend.py
   ▼
Gradium TTS  →  optional video avatar  →  caller
```

| Layer                      | Service                                                                    |
| -------------------------- | -------------------------------------------------------------------------- |
| **STT**                    | [Gradium](https://gradium.ai) default · NVIDIA Parakeet websocket optional |
| **LLM**                    | Nemotron 3 Super 120B (NVIDIA/AWS) · GPT-4.1 fallback                      |
| **TTS**                    | [Gradium](https://gradium.ai)                                              |
| **Transport**              | SmallWebRTC (local) · Daily (Pipecat Cloud) · Twilio (phone)               |
| **Orchestration / Deploy** | [Pipecat](https://pipecat.ai) · [Pipecat Cloud](https://pipecat.daily.co)  |
| **Eval / Healing**         | [Cekura](https://cekura.com) · GPT-5.5 patches (via token router)          |
| **Video UI / Vision**      | WebRTC client in `server/demo_client/` · MediaPipe Tasks Vision            |

Runtime surfaces: **`server/`** (Pipecat agent — `bot-nemotron.py` primary, `bot-gpt.py` fallback), **`server/demo_client/`** (browser video client), the **Twilio websocket path** (8 kHz, avatar disabled), and **`harness/`** (the self-heal loop).

## Quickstart

```bash
git clone https://github.com/quiet-node/yc-voice-agents-hackathon.git
cd yc-voice-agents-hackathon/server
cp .env.example .env          # fill GRADIUM_API_KEY, GRADIUM_VOICE_ID, OPENAI_API_KEY…
# set ENV=local  (required — disables the Cloud-only Krisp filter)
uv sync
uv run bot-nemotron.py        # primary (NVIDIA stack) · or: uv run bot-gpt.py
```

Open **http://localhost:7860** and click **Join now** (first launch ~20s while Pipecat downloads VAD + turn-detection models). You'll get the video UI with device selectors, transcript, vision/gesture badges, avatar status, and language selection as the agent's first question.

<details>
<summary><strong>Environment variables</strong></summary>

| Variable                                     | Purpose                                                            |
| -------------------------------------------- | ------------------------------------------------------------------ |
| `GRADIUM_API_KEY` / `GRADIUM_VOICE_ID`       | STT + TTS / TTS voice                                              |
| `OPENAI_API_KEY`                             | GPT-4.1 LLM (bot-gpt only)                                         |
| `STT_PROVIDER`                               | `gradium` (default) · `parakeet` for NVIDIA Parakeet websocket STT |
| `PARAKEET_STT_URL` / `NVIDIA_ASR_URL`        | Parakeet websocket URL (falls back to `NVIDIA_ASR_URL`)            |
| `NEMOTRON_LLM_URL` / `NEMOTRON_LLM_MODEL`    | Nemotron endpoint / model ID                                       |
| `NEMOTRON_ENABLE_THINKING`                   | Keep `false` for voice (adds latency, leaks into speech)           |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN`   | Optional Twilio call-metadata lookup                               |
| `ENV`                                        | Set `local` for local dev — required                               |
| `CEKURA_API_KEY`                             | Self-healing harness (Cekura runs)                                 |
| `TOKEN_ROUTER_API_KEY` / `TOKEN_ROUTER_BASE_URL` | GPT-5.5 patch proposals (OpenAI-compatible token router)       |
| `NGROK_DOMAIN` / `CEKURA_WEBHOOK_SECRET`     | Static ngrok domain / webhook auth                                 |
| `AVATAR_PROVIDER`                            | Optional Pipecat avatar layer; `none` = audio-only default         |
| `AVATAR_VIDEO_WIDTH` / `AVATAR_VIDEO_HEIGHT` | Avatar video track dimensions                                      |

NVIDIA endpoints (provided during the hackathon):

```bash
STT_PROVIDER=parakeet
NVIDIA_ASR_URL=ws://44.241.251.184:8080
NEMOTRON_LLM_URL=http://nemotron-fleet-alb-1322439314.us-west-2.elb.amazonaws.com/v1
NEMOTRON_LLM_MODEL=nvidia/nemotron-3-super
```

</details>

## Test with Cekura

```bash
/plugin marketplace add cekura-ai/cekura-skills
/plugin install cekura@cekura-skills
/cekura-report          # select Pipecat as the provider
```

Cekura runs scored conversations against the agent and tells you what failed. Sign up at **[dashboard.cekura.ai](https://dashboard.cekura.ai)**. Docs: [Pipecat integration](https://docs.cekura.ai/documentation/integrations/pipecat/automated).

<details>
<summary><strong>Deploy to Pipecat Cloud + wire up Twilio</strong></summary>

```bash
uv tool install pipecat-ai-cli
pc cloud auth login
cd server
pc cloud secrets set bayview-pharmacy-secrets --file .env
pc cloud deploy
```

The `Dockerfile` copies `bot-nemotron.py` → `bot.py`, so every deploy picks up your latest prompt and tool changes.

**Twilio (optional, for phone calls):** buy a voice-capable number, get your org name (`pc cloud organizations list`), create a TwiML Bin, and attach it under **Voice Configuration**:

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

</details>

<details>
<summary><strong>Run the self-improvement harness</strong></summary>

```
harness/
├── self_heal.py          # run scenario → patch → deploy → re-run → PR
├── webhook_server.py     # receives Cekura events, queues heals serially
├── serve_dashboard.py    # serves dashboard + /api/heal-status
├── generate_dashboard.py # builds index.html + report.json + fix_plan.md
└── start.sh              # launcher: ngrok + webhook server
```

**Terminal 1 — webhook server** (receives Cekura events, runs heals):

```bash
cd server
uv run python3 ../harness/webhook_server.py --port 8888
# or: bash ../harness/start.sh   (ngrok + webhook server; needs NGROK_DOMAIN in .env)
```

`start.sh` prints the public URL to set in **Cekura → Agent Settings → Webhook URL**.

**Terminal 2 — dashboard** at `http://localhost:8765` (KPIs, failure clusters, evidence explorer, fix queue; auto-queues open items, polls heal status every 3s):

```bash
cd server
uv run python3 ../harness/serve_dashboard.py
```

**Run a heal manually:**

```bash
uv run python3 ../harness/self_heal.py --scenario <ID>                  # full loop
uv run python3 ../harness/self_heal.py --scenario <ID> --max-iterations 3
uv run python3 ../harness/self_heal.py --scenario <ID> --dry-run        # no deploy
uv run python3 ../harness/self_heal.py --scenario <ID> --no-deploy      # patch only
```

**Generate the dashboard from a Cekura result:**

```bash
cd server
uv run python3 ../harness/generate_dashboard.py \
  --cekura-result-id latest --cekura-agent-id 18021 --out ../harness/runs/latest-cekura
# or --input ../harness/examples/bayview_cekura_report_sample.json for sample data
```

**ngrok:** `ngrok config add-authtoken <token>`, set `NGROK_DOMAIN` in `server/.env`, then `bash harness/start.sh`.

</details>

## Feedback for the sponsors

**NVIDIA — Nemotron.** Solid open-weights default: Nemotron 3 Super (120B) handled the pharmacy flow well — reliable tool-calling for identity verification and refills, good enough to run as the primary path over GPT-4.1. Watch-outs we hit: with `NEMOTRON_ENABLE_THINKING=true`, reasoning tokens add latency and can leak into spoken TTS when the serving stack has no reasoning parser (we keep it `false` for voice); and Parakeet STT expects 16 kHz mono PCM, so Twilio's 8 kHz audio needs resampling or a Gradium fallback.

**Cekura — self-improvement loops.** The eval → webhook → self-heal loop is the heart of this project and it worked: failure transcripts were detailed enough to drive targeted GPT-5.5 patches, and webhooks fired reliably. Friction worth fixing: scenario runs wouldn't go past **~3 concurrent** (anything beyond stalled or failed), runs were **slow** to complete, and the **Cekura Claude Code skill repeatedly asked us to re-authenticate** within a single session.

**Pipecat, Pipecat Cloud & Twilio.** Simple and easy to use — Pipecat's pipeline model was intuitive and Claude integrated with it easily and seamlessly with no issues, and deploying to Pipecat Cloud was painless. Twilio was the same story: wiring up phone calls was smooth and Claude handled the integration cleanly.

## References

[Pipecat docs](https://docs.pipecat.ai/) · [Pipecat Cloud](https://docs.pipecat.ai/pipecat-cloud/introduction) · [examples](https://github.com/pipecat-ai/pipecat-examples) · [Discord](https://discord.gg/pipecat) — [Cekura docs](https://docs.cekura.ai) · [Claude Code guide](https://docs.cekura.ai/mcp/claude-code-guide) · [Pipecat integration](https://docs.cekura.ai/documentation/integrations/pipecat/automated) — [Twilio dev hub](https://www.twilio.com/en-us/developers) · [Dev Phone](https://www.twilio.com/docs/labs/dev-phone)
