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
import time
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

_AVATAR_RUNTIME_STATE: dict[str, Any] = {
    "provider": AVATAR_PROVIDER_NONE,
    "status": "disabled",
    "message": "Audio-only mode",
    "last_error": None,
    "conversation_id": None,
    "updated_at": None,
}


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


def avatar_runtime_config() -> dict[str, Any]:
    """Return non-secret avatar status for local/demo UI diagnostics."""

    provider = get_avatar_provider()
    env_issues = _avatar_env_issues(provider)
    missing_env = [issue["name"] for issue in env_issues]
    configured = provider == AVATAR_PROVIDER_NONE or not env_issues
    runtime = dict(_AVATAR_RUNTIME_STATE)

    status = runtime["status"] if runtime.get("provider") == provider else "configured"
    message = runtime["message"] if runtime.get("provider") == provider else "Provider configured"

    if provider == AVATAR_PROVIDER_NONE:
        status = "disabled"
        message = "Audio-only mode"
    elif not configured:
        status = "missing_config"
        message = "Missing or placeholder avatar credentials"

    return {
        "provider": provider,
        "enabled": provider != AVATAR_PROVIDER_NONE,
        "configured": configured,
        "missing_env": missing_env,
        "env_issues": env_issues,
        "status": status,
        "message": message,
        "last_error": runtime.get("last_error") if runtime.get("provider") == provider else None,
        "conversation_id": runtime.get("conversation_id") if runtime.get("provider") == provider else None,
        "updated_at": runtime.get("updated_at") if runtime.get("provider") == provider else None,
        "transport": avatar_video_transport_params(provider) if configured else {},
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
        from pipecat.transports.tavus.transport import (
            TavusCallbacks,
            TavusParams,
            TavusTransportClient,
        )
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
            _set_avatar_runtime_state(
                "tavus",
                "configured",
                "Tavus configured; waiting for a WebRTC call",
            )

        async def setup(self, setup):
            _set_avatar_runtime_state(
                "tavus",
                "starting",
                "Creating Tavus conversation",
            )
            await AIService.setup(self, setup)
            try:
                callbacks = TavusCallbacks(
                    on_joined=self._on_joined,
                    on_participant_joined=self._on_participant_joined,
                    on_participant_left=self._on_participant_left,
                )
                self._client = TavusTransportClient(
                    bot_name="Pipecat",
                    callbacks=callbacks,
                    api_key=self._api_key,
                    replica_id=self._replica_id,
                    persona_id=self._persona_id,
                    session=self._session,
                    params=TavusParams(
                        audio_in_enabled=True,
                        video_in_enabled=True,
                        audio_out_enabled=True,
                        microphone_out_enabled=False,
                    ),
                )
                await self._client.setup(setup)
            except Exception as e:
                self._log_tavus_unavailable(e)
                return

            if self._tavus_client_ready():
                conversation_id = getattr(getattr(self, "_client", None), "_conversation_id", None)
                _set_avatar_runtime_state(
                    "tavus",
                    "ready",
                    "Tavus room created; waiting for avatar media",
                    conversation_id=conversation_id,
                )
            else:
                self._log_tavus_unavailable(
                    RuntimeError(
                        "Tavus did not create a Daily client. Check TAVUS_API_KEY and "
                        "TAVUS_REPLICA_ID, then retry the call."
                    )
                )

        async def _on_joined(self, data):
            conversation_id = getattr(getattr(self, "_client", None), "_conversation_id", None)
            _set_avatar_runtime_state(
                "tavus",
                "room_joined",
                "Tavus room joined; waiting for replica",
                conversation_id=conversation_id,
            )
            await super()._on_joined(data)

        async def _on_participant_joined(self, participant):
            conversation_id = getattr(getattr(self, "_client", None), "_conversation_id", None)
            _set_avatar_runtime_state(
                "tavus",
                "avatar_joined",
                "Tavus replica joined; waiting for video frames",
                conversation_id=conversation_id,
            )
            await super()._on_participant_joined(participant)

        async def _on_participant_video_frame(
            self, participant_id: str, video_frame: Any, video_source: str
        ):
            conversation_id = getattr(getattr(self, "_client", None), "_conversation_id", None)
            _set_avatar_runtime_state(
                "tavus",
                "video_active",
                "Tavus avatar video is live",
                conversation_id=conversation_id,
            )
            await super()._on_participant_video_frame(participant_id, video_frame, video_source)

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
            _set_avatar_runtime_state(
                "tavus",
                "configured",
                "Tavus configured; waiting for a WebRTC call",
            )

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
            _set_avatar_runtime_state(
                "tavus",
                "unavailable",
                "Tavus unavailable; using existing audio-only bot output",
                error=str(exception) if exception else None,
            )
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


def _set_avatar_runtime_state(
    provider: str,
    status: str,
    message: str,
    *,
    error: str | None = None,
    conversation_id: str | None = None,
) -> None:
    _AVATAR_RUNTIME_STATE.update(
        {
            "provider": provider,
            "status": status,
            "message": message,
            "last_error": error,
            "conversation_id": conversation_id,
            "updated_at": time.time(),
        }
    )


def _avatar_env_issues(provider: str) -> list[dict[str, str]]:
    required_env: list[str] = []
    if provider == "tavus":
        required_env = ["TAVUS_API_KEY", "TAVUS_REPLICA_ID"]
    elif provider == "simli":
        required_env = ["SIMLI_API_KEY", "SIMLI_FACE_ID"]

    issues: list[dict[str, str]] = []
    for name in required_env:
        value = os.getenv(name, "").strip()
        if not value:
            issues.append({"name": name, "reason": "missing"})
        elif _is_placeholder_env_value(value):
            issues.append({"name": name, "reason": "placeholder"})
    return issues


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise AvatarConfigError(f"{name} is required when AVATAR_PROVIDER is enabled.")
    if _is_placeholder_env_value(value):
        raise AvatarConfigError(
            f"{name} still has a placeholder value; replace it with a real provider value."
        )
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


def _is_placeholder_env_value(value: str) -> bool:
    normalized = value.strip().lower()
    placeholder_values = {
        "<api-key>",
        "<tavus_api_key>",
        "<your_tavus_api_key>",
        "<your_replica_id>",
        "api-key",
        "change_me",
        "changeme",
        "replace_me",
        "todo",
        "your_api_key",
        "your_replica_id",
        "your_tavus_api_key",
    }
    return (
        normalized in placeholder_values
        or normalized.startswith("your_")
        or normalized.startswith("<your_")
    )
