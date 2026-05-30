#!/usr/bin/env python3
"""Bayview self-healing webhook server.

Receives Cekura result.completed events, extracts failing scenario IDs,
and feeds them one at a time into the self_heal loop.

Usage:
    python3 harness/webhook_server.py
    python3 harness/webhook_server.py --port 8888
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import functools
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(Path(__file__).parent))

from self_heal import HealResult, run_heal  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("webhook")

# ---------------------------------------------------------------------------
# State (module-level, reset in tests via monkeypatch)
# ---------------------------------------------------------------------------

_queue: asyncio.Queue[int] = asyncio.Queue()
_queued: set[int] = set()       # IDs currently queued OR in-progress
_lock: asyncio.Lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------

def _webhook_secret() -> str:
    secret = os.environ.get("CEKURA_WEBHOOK_SECRET", "")
    if not secret:
        env_file = SERVER / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("CEKURA_WEBHOOK_SECRET="):
                    secret = line.split("=", 1)[1].strip()
                    break
    if not secret:
        raise RuntimeError("CEKURA_WEBHOOK_SECRET not found in environment or server/.env")
    return secret


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------

def _extract_failing_scenario_ids(data: dict[str, Any]) -> list[int]:
    """Return scenario IDs from runs where success=False and no error_message."""
    failing = []
    for run in data.get("runs", {}).values():
        if run.get("success"):
            continue
        if run.get("error_message"):
            scenario_name = run.get("scenario", {}).get("name", "?")
            log.warning(
                "Skipping infra error for scenario '%s': %s",
                scenario_name,
                run["error_message"][:100],
            )
            continue
        scenario_id = run.get("scenario", {}).get("id")
        if scenario_id is not None:
            failing.append(int(scenario_id))
    return failing


# ---------------------------------------------------------------------------
# Core logic (async, testable)
# ---------------------------------------------------------------------------

async def _enqueue_failing_async(payload: dict[str, Any]) -> None:
    """Extract failing IDs and push new ones onto the queue."""
    data = payload.get("data", {})
    success_rate: float = data.get("success_rate", 0.0)

    if success_rate >= 100.0:
        log.info("All scenarios passed (success_rate=%.1f) — nothing to heal.", success_rate)
        return

    failing_ids = _extract_failing_scenario_ids(data)
    if not failing_ids:
        log.info("No heal-eligible failures found in payload.")
        return

    async with _lock:
        for sid in failing_ids:
            if sid in _queued:
                log.info("Scenario %d already queued/in-progress — skipping.", sid)
                continue
            _queued.add(sid)
            await _queue.put(sid)
            log.info("Enqueued scenario %d for healing.", sid)


async def _process_webhook(
    headers: dict[str, str],
    payload: dict[str, Any],
) -> int:
    """Validate and handle a webhook payload. Returns HTTP status code."""
    incoming = headers.get("X-CEKURA-SECRET", "")
    try:
        expected = _webhook_secret()
    except RuntimeError as exc:
        log.error("Webhook secret misconfigured: %s", exc)
        return 500

    if not incoming or incoming != expected:
        log.warning("Rejected webhook — bad or missing X-CEKURA-SECRET.")
        return 401

    event_type = payload.get("event_type", "")
    if event_type != "result.completed":
        log.info("Ignoring unknown event_type=%r", event_type)
        return 200

    # Short-circuit before enqueue when everything passed
    success_rate: float = payload.get("data", {}).get("success_rate", 0.0)
    if success_rate >= 100.0:
        log.info("All scenarios passed (success_rate=%.1f) — nothing to heal.", success_rate)
        return 200

    await _enqueue_failing_async(payload)
    return 200


# ---------------------------------------------------------------------------
# aiohttp route handler
# ---------------------------------------------------------------------------

async def handle_webhook(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")

    headers = dict(request.headers)
    status = await _process_webhook(headers, payload)
    return web.Response(status=status)


# ---------------------------------------------------------------------------
# Serial worker
# ---------------------------------------------------------------------------

async def _worker(loop: asyncio.AbstractEventLoop) -> None:
    """Drain the queue one scenario at a time."""
    log.info("Worker started — waiting for scenarios.")
    while True:
        scenario_id = await _queue.get()
        log.info("▶  Healing scenario %d…", scenario_id)
        try:
            heal_fn = functools.partial(run_heal, scenario_id, auto_merge=True)
            result: HealResult = await loop.run_in_executor(None, heal_fn)
            _log_result(scenario_id, result)
        except Exception as exc:
            log.error("Heal failed for scenario %d: %s", scenario_id, exc)
        finally:
            _queued.discard(scenario_id)
            _queue.task_done()


def _log_result(scenario_id: int, result: HealResult) -> None:
    out_dir = (
        ROOT
        / "harness"
        / "runs"
        / f"webhook-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"scenario-{scenario_id}.json"
    out_file.write_text(
        json.dumps(dataclasses.asdict(result), indent=2, default=str),
        encoding="utf-8",
    )
    status = "✅ PASSED" if result.passed else "❌ no improvement"
    log.info(
        "◀  Scenario %d complete: %s (score=%s, pr=%s)",
        scenario_id,
        status,
        result.final_score,
        result.pr_url or "—",
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bayview self-healing webhook server.")
    parser.add_argument("--port", type=int, default=8888)
    return parser.parse_args()


async def main_async(port: int) -> None:
    loop = asyncio.get_running_loop()
    asyncio.create_task(_worker(loop))

    app = web.Application()
    app.router.add_post("/webhook/cekura", handle_webhook)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Webhook server listening on port %d", port)
    log.info("Route: POST /webhook/cekura")
    await asyncio.Event().wait()  # run forever


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(main_async(args.port))
    except KeyboardInterrupt:
        log.info("Shutting down.")


if __name__ == "__main__":
    main()
