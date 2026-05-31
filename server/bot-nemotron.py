#
# Copyright (c) 2024–2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Bayview Pharmacy — secure prescription refill voice agent.

A caller phones in; the bot verifies their identity (full name + date of birth)
before revealing any prescription information or taking any action, then handles
refills and status questions. All backend calls are mocked (see mock_backend.py),
so it runs with no external dependencies beyond the AI services.

Pipeline: Nemotron Speech Streaming STT → Nemotron-3-Super-120B LLM → Gradium TTS, with direct
function tools registered on the LLM context.

Run the bot using::

    uv run bot-nemotron.py
"""

import copy
import json
import os
import random
import re
from datetime import UTC, date, datetime
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from loguru import logger
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import EndTaskFrame, FunctionCallResultProperties, LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.runner.types import (
    DailyRunnerArguments,
    RunnerArguments,
    SmallWebRTCRunnerArguments,
    WebSocketRunnerArguments,
)
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.gradium.tts import GradiumTTSService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.daily.transport import DailyParams, DailyTransport
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.turns.user_turn_completion_mixin import UserTurnCompletionConfig
from pipecat.turns.user_turn_strategies import FilterIncompleteUserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from language_router import LanguagePreferenceProcessor
from mock_backend import PATIENTS
from nemotron_llm import VLLMOpenAILLMService
from stt_provider import create_stt_service, get_stt_provider
from video_avatar import (
    AVATAR_PROVIDER_NONE,
    AvatarConfigError,
    avatar_runtime_config,
    avatar_video_transport_params,
    create_avatar_service,
    get_avatar_provider,
)

load_dotenv(override=True)


# --- Date-of-birth parsing ----------------------------------------------------
# Nemotron mis-converts spoken dates ("April twelfth nineteen eighty-five" ->
# "1985-12-12") and narrates the conversion aloud. So we take the date of birth
# from the caller verbatim and normalize it to YYYY-MM-DD here in code instead of
# trusting the model to do it.

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12, "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}  # fmt: skip

# Cardinal + ordinal words 0–19, plus the round ordinals used for days.
_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "first": 1, "second": 2, "third": 3,
    "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9,
    "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
    "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18,
    "nineteenth": 19, "twentieth": 20, "thirtieth": 30,
}  # fmt: skip
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}  # fmt: skip
_NUMBER_WORDS = set(_UNITS) | set(_TENS) | {"hundred", "thousand", "and", "oh", "o"}


def _word_num(tokens: list[str]) -> int | None:
    """Additive spelled-number -> int (ones/teens/tens/hundred/thousand)."""
    total, current, seen = 0, 0, False
    for t in tokens:
        if t in _UNITS:
            current += _UNITS[t]
        elif t in _TENS:
            current += _TENS[t]
        elif t == "hundred":
            current = (current or 1) * 100
        elif t == "thousand":
            total += (current or 1) * 1000
            current = 0
        elif t in ("and", "oh", "o"):
            continue
        else:
            return None
        seen = True
    return total + current if seen else None


def _word_year(tokens: list[str]) -> int | None:
    """Spelled year -> int, including the "nineteen eighty-five" pair idiom."""
    if not tokens:
        return None
    if "thousand" in tokens or "hundred" in tokens:
        return _word_num(tokens)
    head = tokens[0]
    if head in _UNITS and 10 <= _UNITS[head] <= 19:
        century = _UNITS[head]
    elif head in _TENS:
        century = _TENS[head]
    else:
        return _word_num(tokens)
    rest = _word_num(tokens[1:]) if len(tokens) > 1 else 0
    return century * 100 + (rest or 0)


def _fmt_date(year: int, month: int, day: int) -> str | None:
    if 1 <= month <= 12 and 1 <= day <= 31 and 1900 <= year <= 2100:
        return f"{year:04d}-{month:02d}-{day:02d}"
    return None


def _parse_dob(spoken: str) -> str | None:
    """Normalize a spoken or written date of birth to ISO YYYY-MM-DD, or None."""
    if not spoken:
        return None
    txt = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", spoken.strip().lower())

    if m := re.search(r"\b(\d{4})\D(\d{1,2})\D(\d{1,2})\b", txt):  # ISO-ish
        return _fmt_date(int(m[1]), int(m[2]), int(m[3]))
    if m := re.search(r"\b(\d{1,2})\D(\d{1,2})\D(\d{4})\b", txt):  # US M/D/Y
        return _fmt_date(int(m[3]), int(m[1]), int(m[2]))
    if m := re.search(r"\b(\d{8})\b", txt):  # YYYYMMDD or MMDDYYYY
        b = m[1]
        return _fmt_date(int(b[:4]), int(b[4:6]), int(b[6:])) or _fmt_date(
            int(b[4:]), int(b[:2]), int(b[2:4])
        )

    month = next((n for name, n in _MONTHS.items() if re.search(rf"\b{name}\b", txt)), None)
    if month is None:
        return None
    toks = [t for t in re.split(r"[^a-z0-9]+", txt) if t]

    def _day_from(prefix: list[str]) -> int | None:
        d = next((int(t) for t in prefix if t.isdigit() and 1 <= int(t) <= 31), None)
        if d is None:
            dw = [t for t in prefix if t in _UNITS or t in _TENS]
            d = _word_num(dw) if dw else None
        return d if d and 1 <= d <= 31 else None

    # Year is an explicit 4-digit token or a trailing run of number-words. A day
    # word ("nineteenth") can look like a year's century, so accept the earliest
    # split that yields BOTH a valid year and a valid day from the leftovers.
    candidates: list[tuple[int, tuple[int, int]]] = []
    candidates += [
        (int(t), (i, i + 1))
        for i, t in enumerate(toks)
        if t.isdigit() and len(t) == 4 and 1900 <= int(t) <= 2025
    ]
    for start in range(len(toks)):
        run = toks[start:]
        if run and all(t in _NUMBER_WORDS for t in run):
            y = _word_year(run)
            if y and 1900 <= y <= 2025:
                candidates.append((y, (start, len(toks))))

    for year, (a, b) in candidates:
        day = _day_from([t for i, t in enumerate(toks) if not (a <= i < b)])
        if day is not None:
            return _fmt_date(year, month, day)
    return None


async def get_call_info(call_sid: str) -> dict:
    """Fetch call information from Twilio REST API using aiohttp.

    Args:
        call_sid: The Twilio call SID

    Returns:
        Dictionary containing call information including from_number, to_number, status, etc.
    """
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")

    if not account_sid or not auth_token:
        logger.warning("Missing Twilio credentials, cannot fetch call info")
        return {}

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}.json"

    try:
        # Use HTTP Basic Auth with aiohttp
        auth = aiohttp.BasicAuth(account_sid, auth_token)

        async with aiohttp.ClientSession() as session:
            async with session.get(url, auth=auth) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"Twilio API error ({response.status}): {error_text}")
                    return {}

                data = await response.json()

                call_info = {
                    "from_number": data.get("from"),
                    "to_number": data.get("to"),
                }

                return call_info

    except Exception as e:
        logger.error(f"Error fetching call info from Twilio: {e}")
        return {}


async def run_bot(
    transport: BaseTransport,
    from_number: str | None = None,
    audio_in_sample_rate: int = 16000,
    audio_out_sample_rate: int = 24000,
    avatar_provider: str = AVATAR_PROVIDER_NONE,
):
    """Main bot logic.

    Args:
        transport: The transport to use.
        from_number: Caller's phone number (Twilio path only).
        audio_in_sample_rate: Input audio sample rate in Hz. Defaults to 16000 (WebRTC).
        audio_out_sample_rate: Output audio sample rate in Hz. Defaults to 24000 (WebRTC).
        avatar_provider: Optional video avatar renderer for WebRTC calls.
    """
    logger.info("Starting bot")

    # Track call start time for transcript saving
    _call_info: dict = {"start": None}

    # Per-call state. Closed over by the tool functions below so each call gets
    # its own isolated session. `verified` flips True ONLY on a successful
    # verify_identity; `failed_attempts` counts verification misses.
    call_state: dict = {"verified": False, "verified_name": None, "failed_attempts": 0}

    # Each call gets its own deep copy of the patient records. refill_prescription
    # mutates a prescription in place (decrements refills_remaining, clears
    # `ready`); PATIENTS is a module-level dict and the worker process is reused
    # across calls, so mutating the global would leak one caller's refill into the
    # next call on the same worker (e.g. a later status check reporting 1 refill /
    # not ready instead of 2 / ready). The copy keeps each call deterministic.
    patients = copy.deepcopy(PATIENTS)

    def _norm_name(s: str) -> str:
        # Lowercase, drop non-letters, collapse whitespace — tolerant of STT noise.
        return re.sub(r"\s+", " ", re.sub(r"[^a-z ]", " ", s.lower())).strip()

    def _name_matches(spoken: str, record_name: str) -> bool:
        a, b = _norm_name(spoken), _norm_name(record_name)
        if not a:
            return False
        # Equal, or one is a prefix of the other (handles STT truncation like
        # "Jane Do" for "Jane Doe"), or every spoken token is in the record name.
        if a == b or b.startswith(a) or a.startswith(b):
            return True
        return set(a.split()).issubset(set(b.split()))

    def _dob_matches(spoken: str, record_dob: str) -> bool:
        # Parse the spoken date in code (the model mis-converts dates), then
        # require an exact match against the record — DOB stays a hard check.
        parsed = _parse_dob(spoken)
        if parsed is not None:
            return parsed == record_dob
        # Fallback: the model may have passed an already-correct ISO date.
        return re.sub(r"\D", "", spoken) == re.sub(r"\D", "", record_dob)

    def _find_patient(full_name: str, date_of_birth: str | None = None) -> dict | None:
        for (patient_name, patient_dob), record in patients.items():
            if date_of_birth is not None and not _dob_matches(date_of_birth, patient_dob):
                continue
            if _name_matches(full_name, patient_name):
                return record
        return None

    def find_patient_by_name(full_name: str) -> dict | None:
        return _find_patient(full_name)

    # --- Tools the LLM can call ---------------------------------------------

    async def verify_identity(
        params: FunctionCallParams,
        full_name: str,
        date_of_birth: str,
    ) -> None:
        """Verify the caller's identity against pharmacy records. You MUST call
        this and receive verified=true BEFORE revealing any prescription details
        or refilling anything.

        Args:
            full_name: The caller's full name, first and last.
            date_of_birth: The caller's date of birth, passed through exactly as
                they said it (e.g. "April twelfth, nineteen eighty-five"). Do not
                convert, reformat, or reorder it — the system normalizes it.
        """
        # DOB must match exactly (digit-for-digit); the name is matched tolerantly
        # so a mis-transcribed name ("Jane Do" for "Jane Doe") still verifies.
        if _find_patient(full_name, date_of_birth) is not None:
            call_state["verified"] = True
            call_state["verified_name"] = full_name.strip()
            await params.result_callback({"verified": True})
            return
        call_state["failed_attempts"] += 1
        await params.result_callback(
            {
                "verified": False,
                "failed_attempts": call_state["failed_attempts"],
                "note": (
                    "Name and date of birth did not match our records. Ask the caller "
                    "to repeat them. After 2 failed attempts, tell them you'll have a "
                    "pharmacist call them back, then call end_call."
                ),
            }
        )

    async def get_prescriptions(params: FunctionCallParams, full_name: str) -> None:
        """Look up a caller's prescriptions — medication, refills remaining, and
        whether each is ready for pickup. Use this to answer "what are my
        medications", "is my prescription ready", and "how many refills are left".

        Args:
            full_name: The caller's full name, first and last.
        """
        patient = find_patient_by_name(full_name)
        if not patient:
            await params.result_callback(
                {"error": "not_found", "note": f"No account found for '{full_name}'."}
            )
            return
        await params.result_callback({"prescriptions": patient["prescriptions"]})

    async def refill_prescription(
        params: FunctionCallParams,
        full_name: str,
        drug_name: str,
    ) -> None:
        """Refill one of the caller's prescriptions. Only call this after the
        caller confirms which medication they want refilled.

        Args:
            full_name: The caller's full name, first and last.
            drug_name: The medication to refill, e.g. "Lisinopril 10mg".
        """
        patient = find_patient_by_name(full_name)
        if not patient:
            await params.result_callback(
                {"ok": False, "reason": f"No account found for '{full_name}'."}
            )
            return
        rx = next(
            (p for p in patient["prescriptions"] if drug_name.strip().lower() in p["drug"].lower()),
            None,
        )
        if not rx:
            await params.result_callback(
                {"ok": False, "reason": f"No prescription found matching '{drug_name}'."}
            )
            return
        if rx["refills_remaining"] <= 0:
            await params.result_callback(
                {
                    "ok": False,
                    "reason": (
                        f"{rx['drug']} has no refills remaining. Tell the caller they'll "
                        "need to contact their doctor for a new prescription. You cannot "
                        "request it for them."
                    ),
                }
            )
            return
        rx["refills_remaining"] -= 1
        rx["ready"] = False
        confirmation = f"RX-{random.randint(100000, 999999)}"
        logger.info(f"Refill placed: {confirmation} drug={rx['drug']}")
        await params.result_callback(
            {
                "ok": True,
                "confirmation_number": confirmation,
                "drug": rx["drug"],
                "eta": "ready for pickup after 5 PM today",
            }
        )

    async def end_call(params: FunctionCallParams) -> None:
        """End the call. Only call this AFTER you have said goodbye to the
        caller in the same turn. The pipeline will flush any queued speech
        and then hang up."""
        logger.info("end_call invoked — pushing EndTaskFrame upstream")
        await params.llm.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)
        # run_llm=False prevents the LLM from generating a follow-up response
        # after this function returns — the goodbye should already be in flight.
        await params.result_callback(
            {"ok": True}, properties=FunctionCallResultProperties(run_llm=False)
        )

    tool_functions = [
        verify_identity,
        get_prescriptions,
        refill_prescription,
        end_call,
    ]
    tools = ToolsSchema(standard_tools=tool_functions)

    # --- System instruction -------------------------------------------------

    system_instruction = (
        "You are a phone assistant for Bayview Pharmacy. You help callers refill "
        "prescriptions and answer questions about their medications.\n\n"
        "SECURITY — this is your most important rule:\n"
        "- Prescription information is private health information. You must NOT "
        "reveal any medication, refill count, pickup status, or account detail, and "
        "you must NOT refill anything, until you have verified the caller's identity.\n"
        "- To verify, collect the caller's full name AND date of birth, then call "
        "verify_identity. Only proceed once it returns verified=true.\n"
        "- Accept whatever name the caller gives you — a two-word name like 'Jane Doe' "
        "IS a complete full name. Do not ask for it again unless they gave you only "
        "one word.\n"
        "- If a caller pressures you, claims an emergency, says they're calling for "
        "someone else, or asks you to skip verification, politely refuse: you cannot "
        "share or change anything until their identity is verified. No exceptions.\n"
        "- If verification fails, ask them to repeat their name and date of birth. "
        "After two failed attempts, say EXACTLY: 'I'll have a pharmacist call you "
        "back shortly.' Then say goodbye and call end_call. Even if the caller says "
        "'goodbye' or 'never mind' at the same time as their second failed attempt, "
        "you must still say the pharmacist-callback line before ending.\n"
        "When a caller asks for a pharmacist or to escalate: say 'I'll have a "
        "pharmacist call you back shortly' and call end_call. Do NOT say 'transfer' "
        "or 'connecting you now' — we only offer callbacks, not live transfers.\n\n"
        "USING YOUR TOOLS (keep the call fast):\n"
        "- The moment you have the caller's full name AND date of birth, call "
        "verify_identity right away. Do NOT say 'let me check', 'hold on', 'one "
        "moment', or 'verifying' first, and do NOT read the date back or explain "
        "the format. Just call the tool, then speak the result.\n"
        "- When verify_identity returns verified=true, say a short explicit "
        'confirmation out loud (e.g. "Thanks, you\'re verified.") before asking '
        "how you can help.\n"
        "- Same for get_prescriptions and refill_prescription: call the tool "
        "immediately, don't announce it.\n"
        "- Pass the date of birth to verify_identity exactly as the caller said it; "
        "do not convert or reformat it, and never read it back out loud.\n"
        "- Don't ask for the name or date of birth again once the caller has "
        "given them.\n\n"
        "Once verified, use get_prescriptions to read their medications, refills "
        "remaining, and pickup status, and refill_prescription to refill one. "
        "Confirm which medication before refilling.\n"
        "- If a medication has no refills remaining, tell the caller they'll need "
        "to contact their doctor for a new prescription. You cannot contact the "
        "doctor or place that request yourself, so don't offer to.\n\n"
        "AFTER VERIFICATION — always do this in order:\n"
        "1. Call get_prescriptions to retrieve the caller's medication list.\n"
        "2. Read out their medications and status (ready/not ready, refills remaining).\n"
        "3. Then ask what they'd like to do (refill, status check, etc.).\n"
        "Never skip straight to 'which medication would you like to refill?' without "
        "reading the list first.\n\n"
        "LANGUAGE HANDLING:\n"
        "- The first thing you ask is the English/Spanish preference prompt. If the "
        "caller says 'one', '1', 'English', or 'ingles', continue in English. If "
        "the caller says 'two', '2', 'dos', 'Spanish', 'espanol', or uses Spanish "
        "words such as 'hola', continue in Spanish immediately.\n"
        "- A spoken Spanish cue is enough to select Spanish. Do not ask the language "
        "question again after a language is selected.\n"
        "- If Spanish is selected, every spoken response must be in Spanish unless "
        "the caller asks to switch languages. Keep the same security, verification, "
        "and tool-use rules.\n\n"
        "Talk like a real pharmacy clerk on the phone — not a chatbot:\n"
        "- Keep it to 1–2 short sentences per turn.\n"
        "- Ask ONE thing at a time. Get the name, then in your next spoken turn ask for the date of birth; if they gave a two-word full name, treat it as complete and move on to date of birth.\n"
        "- While waiting for a tool to finish, say nothing — do not fill silence with "
        "'One moment...', 'Let me check...', or 'Almost done.' Just wait.\n"
        '- Skip filler openers like "Absolutely!", "Of course!", "I\'d be happy to" '
        "— go straight to the point.\n"
        "- Use contractions. Fragments are fine.\n"
        "- Responses are spoken aloud. No bullet points, no emojis. Read numbers and "
        'dates in words ("two refills", "April twelfth").\n\n'
        "Ending the call: only call end_call after the caller's request has been "
        "fully answered AND they've said goodbye or confirmed there's nothing else. "
        "Never call end_call right after verifying identity, and never end while the "
        "caller still has an unanswered question. When they're done or say goodbye: "
        'say a short closing line (e.g. "Thanks, take care!") AND call end_call in '
        "the same turn. Never call end_call without saying goodbye first.\n\n"
        f"Today is {date.today().strftime('%A, %B %d, %Y')}."
    )

    # Speech-to-text service. Gradium remains the default. Set
    # STT_PROVIDER=parakeet to use the NVIDIA Parakeet websocket when available.
    stt = await create_stt_service(audio_in_sample_rate=audio_in_sample_rate)
    logger.info(f"Speech-to-text provider requested: {get_stt_provider()}")

    # LLM service — Nemotron-3-Super-120B served by vLLM (OpenAI-compatible chat
    # completions at /v1). vLLM exposes the Chat Completions API, not the Responses
    # API, so we use OpenAILLMService (not OpenAIResponsesLLMService). The live
    # endpoint serves the model as "nemotron-3-super" (per its /v1/models).
    #
    # Reasoning ("thinking") toggle — Nemotron is controlled per-request via
    # chat_template_kwargs.enable_thinking, forwarded through the OpenAI client's
    # extra_body (the request-body convention confirmed against this endpoint in
    # ../aiewf-eval traces). Default OFF for low-latency voice. To ENABLE, set
    # NEMOTRON_ENABLE_THINKING=true; to DISABLE, leave unset/false.
    #
    # CAUTION for voice: reasoning is only kept out of the spoken `content` if the
    # vLLM server runs a reasoning parser (e.g. --reasoning-parser nemotron_v3, which
    # routes it to a separate `reasoning_content` field). This live endpoint did NOT
    # surface reasoning_content in testing, so if thinking is enabled and the server
    # lacks a parser, chain-of-thought would appear inline in `content` and get
    # spoken. Keep thinking OFF for voice unless the parser is confirmed active.
    # VLLMOpenAILLMService is a thin OpenAILLMService subclass that reports TTFB to
    # the first NON-THINKING token (so the metric reflects time-to-first-spoken-word
    # when reasoning is enabled, not time-to-first-reasoning-token). No-op when
    # thinking is off. See server/nemotron_llm.py.
    # Route LLM through the token router (fast, reliable) when available.
    # Falls back to the hackathon Nemotron endpoint if token router isn't configured.
    _tr_url = os.getenv("TOKEN_ROUTER_BASE_URL", "").strip()
    _tr_key = os.getenv("TOKEN_ROUTER_API_KEY", "").strip()
    if _tr_url and _tr_key:
        llm_base_url = _tr_url
        llm_api_key = _tr_key
        llm_model = os.getenv("FAST_LLM_MODEL", "openai/gpt-4o-mini")
        llm_extra: dict = {}
        logger.info(f"LLM: token router → {llm_model}")
    else:
        enable_thinking = os.getenv("NEMOTRON_ENABLE_THINKING", "false").lower() == "true"
        llm_base_url = os.getenv(
            "NEMOTRON_LLM_URL",
            "http://nemotron-fleet-alb-1322439314.us-west-2.elb.amazonaws.com/v1",
        )
        llm_api_key = os.getenv("NEMOTRON_LLM_API_KEY", "EMPTY")
        llm_model = os.getenv("NEMOTRON_LLM_MODEL", "nvidia/nemotron-3-super")
        llm_extra = {"extra_body": {"chat_template_kwargs": {"enable_thinking": enable_thinking}}}
        logger.info(f"LLM: Nemotron endpoint → {llm_model}")

    llm = VLLMOpenAILLMService(
        api_key=llm_api_key,
        base_url=llm_base_url,
        settings=VLLMOpenAILLMService.Settings(
            model=llm_model,
            system_instruction=system_instruction,
            extra=llm_extra,
        ),
    )

    # Text-to-Speech service
    tts = GradiumTTSService(
        api_key=os.environ["GRADIUM_API_KEY"],
        settings=GradiumTTSService.Settings(
            voice=os.getenv("GRADIUM_VOICE_ID", "Eu9iL_CYe8N-Gkx_"),
        ),
    )

    # ToolsSchema describes the tools to the LLM; register_direct_function
    # wires the actual handlers the LLM will invoke. Both are required.
    for fn in tool_functions:
        llm.register_direct_function(fn)

    context = LLMContext(tools=tools)
    language_preference = LanguagePreferenceProcessor(context)
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            # start_secs=0.3 so brief backchannels ("okay") don't register as a
            # turn start and interrupt the bot mid-reply.
            vad_analyzer=SileroVADAnalyzer(params=VADParams(start_secs=0.3)),
            # Nemotron over-tags short verify answers (a name, a date of birth) as
            # incomplete; the default 5s/10s waits caused multi-second dead air
            # before every reply. Cut them so the bot answers promptly.
            user_turn_strategies=FilterIncompleteUserTurnStrategies(
                config=UserTurnCompletionConfig(
                    incomplete_short_timeout=1.5,
                    incomplete_long_timeout=2.5,
                ),
            ),
        ),
    )

    avatar_session: aiohttp.ClientSession | None = None
    avatar_service = None
    try:
        if avatar_provider != AVATAR_PROVIDER_NONE:
            avatar_session = aiohttp.ClientSession()
            avatar_service = create_avatar_service(avatar_provider, session=avatar_session)
            logger.info(f"Video avatar enabled: provider={avatar_provider}")
    except Exception as e:
        if avatar_session and not avatar_session.closed:
            await avatar_session.close()
        avatar_session = None
        avatar_service = None
        logger.warning(f"Video avatar disabled; continuing audio-only: {e}")

    # Pipeline - assembled from reusable components. The avatar service is an
    # optional renderer after TTS; it does not replace the bot brain or tools.
    pipeline_steps = [
        transport.input(),
        stt,
        language_preference,
        user_aggregator,
        llm,
        tts,
    ]
    if avatar_service:
        pipeline_steps.append(avatar_service)
    pipeline_steps.extend([transport.output(), assistant_aggregator])

    pipeline = Pipeline(pipeline_steps)

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
            audio_in_sample_rate=audio_in_sample_rate,
            audio_out_sample_rate=audio_out_sample_rate,
        ),
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        _call_info["start"] = datetime.now(UTC)
        logger.info("Client connected")
        # Kick off the conversation
        context.add_message(
            {
                "role": "user",
                "content": (
                    "A caller just connected. First ask exactly: 'Thanks for calling "
                    "Bayview Pharmacy. Do you prefer English or Spanish? Signify 1 "
                    "for English, 2 for Spanish. Prefiere ingles o espanol? "
                    "Indique 1 para ingles, 2 para espanol.' Do not ask how you "
                    "can help until the language "
                    "preference is selected."
                ),
            }
        )
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=False)

    try:
        await runner.add_workers(worker)
        await runner.run()
    finally:
        if avatar_session and not avatar_session.closed:
            await avatar_session.close()

        # Save transcript for the Call History dashboard
        end_time = datetime.now(UTC)
        start_time = _call_info.get("start") or end_time
        duration_s = int((end_time - start_time).total_seconds())

        turns = []
        for message in context.messages:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            content = message.get("content")
            if role in ("user", "assistant") and isinstance(content, str) and content:
                turns.append({"role": role, "content": content})

        if turns:
            ts = end_time.strftime("%Y%m%dT%H%M%S")
            transcript_dir = (
                Path(__file__).resolve().parents[1] / "harness" / "runs" / "transcripts"
            )
            transcript_dir.mkdir(parents=True, exist_ok=True)
            out = transcript_dir / f"{ts}-live.json"
            out.write_text(
                json.dumps(
                    {
                        "source": "live",
                        "timestamp": end_time.isoformat(),
                        "duration_s": duration_s,
                        "transcript": turns,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            logger.info(f"Transcript saved: {out}")


async def bot(runner_args: RunnerArguments):
    """Main bot entry point."""

    from_number: str | None = None
    transport_overrides: dict = {}
    try:
        avatar_provider = get_avatar_provider()
        avatar_config = avatar_runtime_config()
    except AvatarConfigError as e:
        logger.warning(f"Invalid video avatar configuration; continuing audio-only: {e}")
        avatar_provider = AVATAR_PROVIDER_NONE
        avatar_config = {"configured": False}
    run_avatar_provider = AVATAR_PROVIDER_NONE

    # Krisp is available when deployed to Pipecat Cloud
    if os.environ.get("ENV") != "local":
        from pipecat.audio.filters.krisp_viva_filter import KrispVivaFilter

        krisp_filter = KrispVivaFilter()
    else:
        krisp_filter = None

    match runner_args:
        case SmallWebRTCRunnerArguments():
            webrtc_connection: SmallWebRTCConnection = runner_args.webrtc_connection
            if avatar_provider != AVATAR_PROVIDER_NONE and avatar_config.get("configured"):
                run_avatar_provider = avatar_provider
            elif avatar_provider != AVATAR_PROVIDER_NONE:
                missing_avatar_env = avatar_config.get("missing_env", [])
                missing_avatar_text = (
                    ", ".join(missing_avatar_env)
                    if isinstance(missing_avatar_env, list)
                    else "unknown"
                )
                logger.warning(
                    f"Video avatar disabled; missing provider config: {missing_avatar_text}"
                )

            transport = SmallWebRTCTransport(
                webrtc_connection=webrtc_connection,
                params=TransportParams(
                    audio_in_enabled=True,
                    audio_in_filter=krisp_filter,
                    audio_out_enabled=True,
                    **avatar_video_transport_params(run_avatar_provider),
                ),
            )
        case WebSocketRunnerArguments():
            if avatar_provider != AVATAR_PROVIDER_NONE:
                logger.info(
                    f"Ignoring AVATAR_PROVIDER={avatar_provider} for Twilio; "
                    "video avatar rendering is WebRTC-only."
                )
            # Twilio media streams are 8 kHz μ-law in both directions.
            # (No upsample needed — Gradium STT handles 8 kHz directly.)
            transport_overrides["audio_in_sample_rate"] = 8000
            transport_overrides["audio_out_sample_rate"] = 8000

            # Parse Twilio websocket and fetch call information
            _, call_data = await parse_telephony_websocket(runner_args.websocket)

            # Fetch the caller's number from the Twilio REST API.
            call_info = await get_call_info(call_data["call_id"])
            if call_info:
                from_number = call_info.get("from_number")
                logger.info(f"Call from: {from_number} to: {call_info.get('to_number')}")

            serializer = TwilioFrameSerializer(
                stream_sid=call_data["stream_id"],
                call_sid=call_data["call_id"],
                account_sid=os.getenv("TWILIO_ACCOUNT_SID", ""),
                auth_token=os.getenv("TWILIO_AUTH_TOKEN", ""),
            )

            transport = FastAPIWebsocketTransport(
                websocket=runner_args.websocket,
                params=FastAPIWebsocketParams(
                    audio_in_enabled=True,
                    audio_in_filter=krisp_filter,
                    audio_out_enabled=True,
                    add_wav_header=False,
                    serializer=serializer,
                ),
            )
        case DailyRunnerArguments():
            # Pipecat Cloud starts WebRTC sessions (e.g. the playground and
            # Cekura's pipecat_v2 test runs) over a Daily room. Same 16 kHz in /
            # 24 kHz out defaults as SmallWebRTC, so no sample-rate overrides.
            transport = DailyTransport(
                runner_args.room_url,
                runner_args.token,
                "Bayview Pharmacy",
                params=DailyParams(
                    audio_in_enabled=True,
                    # Krisp is for real mic noise; Cekura test agent uses clean TTS —
                    # keep filter off so synthetic audio isn't suppressed by the voice model.
                    audio_in_filter=None,
                    audio_out_enabled=True,
                    vad_analyzer=SileroVADAnalyzer(
                        params=VADParams(start_secs=0.2, stop_secs=0.5)
                    ),
                ),
            )
        case _:
            logger.error(f"Unsupported runner arguments type: {type(runner_args)}")
            return

    await run_bot(
        transport,
        from_number=from_number,
        avatar_provider=run_avatar_provider,
        **transport_overrides,
    )


if __name__ == "__main__":
    from pipecat.runner.run import main

    from demo_frontend import mount_demo_frontend

    mount_demo_frontend()
    main()
