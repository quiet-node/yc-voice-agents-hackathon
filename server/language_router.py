#
# Copyright (c) 2024-2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Lightweight language cue routing for the pharmacy voice agent."""

import re
import unicodedata
from dataclasses import dataclass

from loguru import logger
from pipecat.frames.frames import Frame, TranscriptionFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


@dataclass(frozen=True)
class LanguageCue:
    language: str
    code: str
    reason: str


_SPANISH_ALWAYS_PATTERNS = [
    re.compile(pattern)
    for pattern in (
        r"\b(spanish|espanol|castellano)\b",
        r"\b(en|a)\s+espanol\b",
        r"\b(hola|buenos|buenas|gracias|necesito|quiero|receta|medicina|medicamento|farmacia)\b",
    )
]

_ENGLISH_EXPLICIT_PATTERNS = [
    re.compile(pattern)
    for pattern in (
        r"\b(english|ingles)\b",
        r"\bin\s+english\b",
    )
]

_SELECTION_SPANISH_PATTERNS = [
    re.compile(pattern)
    for pattern in (
        r"^(2|two|dos)$",
        r"^(number|option|choice)\s+(2|two|dos)$",
        r"^(2|two|dos)\s+(please|por favor)$",
    )
]

_SELECTION_ENGLISH_PATTERNS = [
    re.compile(pattern)
    for pattern in (
        r"^(1|one|uno)$",
        r"^(number|option|choice)\s+(1|one|uno)$",
        r"^(1|one|uno)\s+(please|por favor)$",
    )
]


def detect_language_cue(
    text: str,
    *,
    selection_pending: bool,
) -> LanguageCue | None:
    """Detect English/Spanish preference from a short caller transcript."""

    normalized = _normalize_for_language_detection(text)
    if not normalized:
        return None

    if _matches_any(_SPANISH_ALWAYS_PATTERNS, normalized):
        return LanguageCue("Spanish", "es", "spanish_word")

    if _matches_any(_ENGLISH_EXPLICIT_PATTERNS, normalized):
        return LanguageCue("English", "en", "explicit_english")

    if selection_pending and len(normalized.split()) <= 4:
        if _matches_any(_SELECTION_SPANISH_PATTERNS, normalized):
            return LanguageCue("Spanish", "es", "language_menu_selection")
        if _matches_any(_SELECTION_ENGLISH_PATTERNS, normalized):
            return LanguageCue("English", "en", "language_menu_selection")

    return None


class LanguagePreferenceProcessor(FrameProcessor):
    """Inject language preference context before user turns reach the LLM."""

    def __init__(self, context: LLMContext, **kwargs):
        super().__init__(name="language_preference", **kwargs)
        self._context = context
        self._preferred_language: str | None = None
        self._selection_pending = True

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame):
            self._maybe_update_language(frame.text)

        await self.push_frame(frame, direction)

    def _maybe_update_language(self, text: str):
        cue = detect_language_cue(text, selection_pending=self._selection_pending)
        if not cue:
            return

        self._selection_pending = False
        if cue.language == self._preferred_language:
            return

        self._preferred_language = cue.language
        self._context.add_message(
            {
                "role": "system",
                "content": _language_context_message(cue.language),
            }
        )
        logger.info(
            f"Language preference set to {cue.language} from transcript cue "
            f"({cue.reason}): {text!r}"
        )


def _language_context_message(language: str) -> str:
    if language == "Spanish":
        return (
            "LANGUAGE_CONTEXT: The caller selected Spanish. Starting with your "
            "next response, speak Spanish only unless the caller asks to switch. "
            "Do not ask the language preference again. Briefly confirm in "
            "Spanish, then continue the pharmacy flow."
        )

    return (
        "LANGUAGE_CONTEXT: The caller selected English. Starting with your next "
        "response, speak English only unless the caller asks to switch. Do not "
        "ask the language preference again, then continue the pharmacy flow."
    )


def _normalize_for_language_detection(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _matches_any(patterns: list[re.Pattern[str]], text: str) -> bool:
    return any(pattern.search(text) for pattern in patterns)
