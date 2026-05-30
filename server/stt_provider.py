#
# Copyright (c) 2024-2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Speech-to-text provider selection.

Gradium remains the default production-safe path. NVIDIA Parakeet can be enabled
for WebRTC/Daily calls by setting ``STT_PROVIDER=parakeet`` and pointing
``PARAKEET_STT_URL`` or ``NVIDIA_ASR_URL`` at the hackathon Parakeet websocket.
"""

import asyncio
import json
import os
from typing import Any

import websockets
from loguru import logger
from pipecat.services.gradium.stt import GradiumSTTService
from pipecat.transcriptions.language import Language

from nvidia_stt import NVidiaWebSocketSTTService

STT_PROVIDER_GRADIUM = "gradium"
STT_PROVIDER_PARAKEET = "parakeet"
SUPPORTED_STT_PROVIDERS = {
    STT_PROVIDER_GRADIUM,
    STT_PROVIDER_PARAKEET,
    "nvidia",
    "nvidia-parakeet",
    "nvidia_parakeet",
}


class STTConfigError(RuntimeError):
    """Raised when an explicitly selected STT provider cannot be configured."""


def get_stt_provider() -> str:
    provider = os.getenv("STT_PROVIDER", STT_PROVIDER_GRADIUM).strip().lower()
    provider = provider or STT_PROVIDER_GRADIUM
    if provider in {"nvidia", "nvidia-parakeet", "nvidia_parakeet"}:
        return STT_PROVIDER_PARAKEET
    if provider not in SUPPORTED_STT_PROVIDERS:
        supported = ", ".join(sorted(SUPPORTED_STT_PROVIDERS))
        raise STTConfigError(f"Unsupported STT_PROVIDER={provider!r}. Expected one of: {supported}.")
    return provider


async def create_stt_service(*, audio_in_sample_rate: int):
    """Create the configured STT service.

    Parakeet expects 16 kHz mono PCM. WebRTC and Daily use this path; Twilio's
    8 kHz media stream falls back to Gradium unless explicitly overridden.
    """

    provider = get_stt_provider()
    if provider == STT_PROVIDER_GRADIUM:
        return _create_gradium_stt()

    if provider == STT_PROVIDER_PARAKEET:
        return await _create_parakeet_stt(audio_in_sample_rate=audio_in_sample_rate)

    raise STTConfigError(f"Unsupported STT provider {provider!r}.")


def _create_gradium_stt():
    return GradiumSTTService(
        api_key=os.environ["GRADIUM_API_KEY"],
        settings=GradiumSTTService.Settings(
            language=Language.EN,
        ),
    )


async def _create_parakeet_stt(*, audio_in_sample_rate: int):
    if audio_in_sample_rate != 16000 and not _bool_env("PARAKEET_STT_ALLOW_NON_16K", False):
        message = (
            f"Parakeet STT requires 16 kHz input, but this transport is "
            f"{audio_in_sample_rate} Hz."
        )
        if _bool_env("PARAKEET_STT_FALLBACK_TO_GRADIUM", True):
            logger.warning(f"{message} Falling back to Gradium STT.")
            return _create_gradium_stt()
        raise STTConfigError(message)

    url = (
        os.getenv("PARAKEET_STT_URL", "").strip()
        or os.getenv("NVIDIA_ASR_URL", "").strip()
        or "ws://localhost:8080"
    )
    if _bool_env("PARAKEET_STT_PREFLIGHT", True):
        available = await _preflight_parakeet_websocket(url)
        if not available:
            message = f"Parakeet STT websocket is not reachable at {url!r}."
            if _bool_env("PARAKEET_STT_FALLBACK_TO_GRADIUM", True):
                logger.warning(f"{message} Falling back to Gradium STT.")
                return _create_gradium_stt()
            raise STTConfigError(message)

    logger.info(f"Using NVIDIA Parakeet STT websocket: {url}")
    return NVidiaWebSocketSTTService(
        url=url,
        sample_rate=16000,
        strip_interim_prefix=_bool_env("PARAKEET_STT_STRIP_INTERIM_PREFIX", False),
        preroll_seconds=_float_env("PARAKEET_STT_PREROLL_SECONDS", 1.0),
    )


async def _preflight_parakeet_websocket(url: str) -> bool:
    timeout = _float_env("PARAKEET_STT_PREFLIGHT_TIMEOUT", 2.5)
    try:
        async with websockets.connect(url, open_timeout=timeout, ping_interval=None) as websocket:
            try:
                message = await asyncio.wait_for(websocket.recv(), timeout=timeout)
                data: Any = json.loads(message)
                if isinstance(data, dict) and data.get("type") == "error":
                    logger.warning(f"Parakeet STT preflight returned error: {data}")
                    return False
            except TimeoutError:
                # Existing NVIDIA websocket servers may not always send "ready"
                # before audio. A successful TCP/websocket handshake is enough.
                pass
            return True
    except Exception as e:
        logger.warning(f"Parakeet STT preflight failed: {e}")
        return False


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        parsed = float(value)
    except ValueError as e:
        raise STTConfigError(f"{name} must be a number, got {value!r}.") from e
    if parsed <= 0:
        raise STTConfigError(f"{name} must be positive, got {parsed}.")
    return parsed
