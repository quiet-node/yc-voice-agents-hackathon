#
# Copyright (c) 2024-2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Mount the local video-call demo UI without changing bot behavior."""

from pathlib import Path

from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from video_avatar import AvatarConfigError, avatar_runtime_config


def mount_demo_frontend() -> None:
    """Mount the video-call demo client on the Pipecat dev runner app."""

    from pipecat.runner.run import app

    demo_dir = Path(__file__).parent / "demo_client"
    if not demo_dir.exists():
        logger.warning(f"Demo frontend directory not found: {demo_dir}")
        return

    @app.get("/demo/config", include_in_schema=False)
    async def demo_config():
        try:
            avatar = avatar_runtime_config()
        except AvatarConfigError as e:
            avatar = {
                "provider": "invalid",
                "enabled": False,
                "configured": False,
                "missing_env": [],
                "error": str(e),
                "transport": {},
            }
        return {"avatar": avatar}

    app.mount("/demo", StaticFiles(directory=demo_dir, html=True), name="bayview-demo")

    @app.get("/", include_in_schema=False)
    async def demo_root_redirect():
        return RedirectResponse(url="/demo/")
