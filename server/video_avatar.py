#
# Copyright (c) 2024-2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Optional video avatar rendering for WebRTC calls.

The avatar layer is deliberately presentation-only: it renders video from the
bot's existing TTS audio and does not replace the Pipecat STT/LLM/tool pipeline.
"""

import os
from typing import Any

import aiohttp
from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    Frame,
    InterruptionFrame,
    OutputTransportReadyFrame,
    StartFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.ai_service import AIService

AVATAR_PROVIDER_NONE = "none"
SUPPORTED_AVATAR_PROVIDERS = {AVATAR_PROVIDER_NONE, "tavus", "simli"}


class AvatarConfigError(RuntimeError):
    """Raised when an explicitly enabled avatar provider is not configured."""


def get_avatar_provider() -> str:
    """Return the configured avatar provider.

    Defaults to ``none`` so existing audio-only, Twilio, Cekura, and harness
    flows remain unchanged unless video is explicitly enabled.
    """

    provider = os.getenv("AVATAR_PROVIDER", AVATAR_PROVIDER_NONE).strip().lower()
    provider = provider or AVATAR_PROVIDER_NONE
    if provider not in SUPPORTED_AVATAR_PROVIDERS:
        supported = ", ".join(sorted(SUPPORTED_AVATAR_PROVIDERS))
        raise AvatarConfigError(
            f"Unsupported AVATAR_PROVIDER={provider!r}. Expected one of: {supported}."
        )
    return provider


def avatar_video_transport_params(provider: str) -> dict[str, Any]:
    """Return TransportParams kwargs for avatar video output."""

    if provider == AVATAR_PROVIDER_NONE:
        return {}

    return {
        "video_out_enabled": True,
        "video_out_is_live": True,
        "video_out_width": _int_env("AVATAR_VIDEO_WIDTH", 1280),
        "video_out_height": _int_env("AVATAR_VIDEO_HEIGHT", 720),
        "video_out_framerate": _int_env("AVATAR_VIDEO_FRAMERATE", 30),
    }


def create_avatar_service(provider: str, *, session: aiohttp.ClientSession):
    """Create the optional avatar service for the selected provider."""

    if provider == AVATAR_PROVIDER_NONE:
        return None
    if provider == "tavus":
        return _create_tavus_service(session=session)
    if provider == "simli":
        return _create_simli_service()

    raise AvatarConfigError(f"Unsupported AVATAR_PROVIDER={provider!r}.")


def _create_tavus_service(*, session: aiohttp.ClientSession):
    api_key = _required_env("TAVUS_API_KEY")
    replica_id = _required_env("TAVUS_REPLICA_ID")
    persona_id = os.getenv("TAVUS_PERSONA_ID", "pipecat-stream").strip() or "pipecat-stream"

    try:
        from pipecat.services.tavus.video import TavusVideoService
    except ImportError as e:
        raise AvatarConfigError(
            "AVATAR_PROVIDER=tavus requires the pipecat Tavus extra. "
            "Install dependencies from pyproject.toml/uv.lock first."
        ) from e

    class SafeTavusVideoService(TavusVideoService):
        """Tavus renderer that cannot silence the core bot.

        Pipecat's stock TavusVideoService proxies TTS audio into Tavus and
        expects Tavus audio/video back. If Tavus setup fails, that consumes the
        bot's TTS frames and the browser hears nothing. For our use case, the
        browser should always hear the existing Gradium audio; Tavus is only a
        best-effort avatar video renderer.
        """

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._tavus_warning_logged = False

        async def _on_participant_audio_data(
            self, participant_id: str, audio: Any, audio_source: str
        ):
            # Keep the user-facing audio from our existing TTS path. Tavus audio
            # would duplicate it and can disappear if the avatar session fails.
            return

        async def cleanup(self):
            await AIService.cleanup(self)
            transport_client = getattr(self, "_client", None)
            if transport_client and self._tavus_client_ready():
                try:
                    await transport_client.cleanup()
                except Exception as e:
                    self._log_tavus_unavailable(e)
            self._client = None

        async def start(self, frame: StartFrame):
            if self._tavus_client_ready():
                try:
                    await super().start(frame)
                except Exception as e:
                    self._log_tavus_unavailable(e)
                return

            await AIService.start(self, frame)
            self._log_tavus_unavailable()

        async def _end_conversation(self):
            if not self._tavus_client_ready():
                return
            try:
                await super()._end_conversation()
            except Exception as e:
                self._log_tavus_unavailable(e)

        async def process_frame(self, frame: Frame, direction: FrameDirection):
            await AIService.process_frame(self, frame, direction)

            if isinstance(frame, InterruptionFrame):
                if self._tavus_client_ready():
                    await self._handle_interruptions()
                await self.push_frame(frame, direction)
            elif isinstance(frame, TTSAudioRawFrame):
                await self.push_frame(frame, direction)
                if self._tavus_client_ready():
                    try:
                        await self._handle_audio_frame(frame)
                    except Exception as e:
                        self._log_tavus_unavailable(e)
                else:
                    self._log_tavus_unavailable()
            elif isinstance(frame, OutputTransportReadyFrame):
                self._transport_ready = True
                await self.push_frame(frame, direction)
            elif isinstance(frame, TTSStartedFrame):
                await self.start_ttfb_metrics()
                await self.push_frame(frame, direction)
            elif isinstance(frame, BotStartedSpeakingFrame):
                await self.stop_ttfb_metrics()
                await self.push_frame(frame, direction)
            else:
                await self.push_frame(frame, direction)

        def _tavus_client_ready(self) -> bool:
            transport_client = getattr(self, "_client", None)
            daily_client = getattr(transport_client, "_client", None)
            return bool(daily_client)

        def _log_tavus_unavailable(self, exception: Exception | None = None):
            if self._tavus_warning_logged:
                return
            self._tavus_warning_logged = True
            if exception:
                logger.warning(
                    f"Tavus avatar unavailable; continuing with audio-only output: {exception}"
                )
            else:
                logger.warning("Tavus avatar unavailable; continuing with audio-only output.")

    return SafeTavusVideoService(
        api_key=api_key,
        replica_id=replica_id,
        persona_id=persona_id,
        session=session,
    )


def _create_simli_service():
    api_key = _required_env("SIMLI_API_KEY")
    face_id = _required_env("SIMLI_FACE_ID")

    try:
        from pipecat.services.simli.video import SimliVideoService
    except ImportError as e:
        raise AvatarConfigError(
            "AVATAR_PROVIDER=simli requires the pipecat Simli extra. "
            "Use AVATAR_PROVIDER=tavus for this build, or add pipecat-ai[simli]."
        ) from e

    return SimliVideoService(api_key=api_key, face_id=face_id)


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise AvatarConfigError(f"{name} is required when AVATAR_PROVIDER is enabled.")
    return value


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError as e:
        raise AvatarConfigError(f"{name} must be an integer, got {value!r}.") from e
    if parsed <= 0:
        raise AvatarConfigError(f"{name} must be positive, got {parsed}.")
    return parsed
