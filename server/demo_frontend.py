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


def mount_demo_frontend() -> None:
    """Mount the Tavus-style demo client on the Pipecat dev runner app."""

    from pipecat.runner.run import app

    demo_dir = Path(__file__).parent / "demo_client"
    if not demo_dir.exists():
        logger.warning(f"Demo frontend directory not found: {demo_dir}")
        return

    app.mount("/demo", StaticFiles(directory=demo_dir, html=True), name="bayview-demo")

    @app.get("/", include_in_schema=False)
    async def demo_root_redirect():
        return RedirectResponse(url="/demo/")
