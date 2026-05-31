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

_STATUS_FILE = ROOT / "harness" / "runs" / "webhook-status.json"

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
_queued: set[int] = set()              # IDs currently queued OR in-progress
_queued_names: dict[int, str] = {}     # ID → display name
_healed_success: set[int] = set()     # IDs that already passed healing this session
_lock: asyncio.Lock = asyncio.Lock()
_heal_state: dict[str, Any] = {
    "in_progress": None,
    "last_result": None,
}


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
# Status file
# ---------------------------------------------------------------------------

def _write_status() -> None:
    """Persist current heal state to disk so the dashboard can read it."""
    queued_items = [
        {"scenario_id": sid, "scenario_name": _queued_names.get(sid, "")}
        for sid in _queued
    ]
    status = {
        "updated_at": datetime.now(UTC).isoformat(),
        "queue_depth": _queue.qsize(),
        "queued_items": queued_items,
        "in_progress": _heal_state["in_progress"],
        "last_result": _heal_state["last_result"],
    }
    try:
        _STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATUS_FILE.write_text(json.dumps(status, default=str), encoding="utf-8")
    except Exception as exc:
        log.warning("Failed to write heal status: %s", exc)


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------

def _extract_failing_scenarios(data: dict[str, Any]) -> list[tuple[int, str]]:
    """Return (scenario_id, scenario_name) for runs where success=False and no error_message."""
    failing = []
    for run in data.get("runs", {}).values():
        if run.get("success"):
            continue
        scenario = run.get("scenario", {})
        if run.get("error_message"):
            log.warning(
                "Skipping infra error for scenario '%s': %s",
                scenario.get("name", "?"),
                run["error_message"][:100],
            )
            continue
        scenario_id = scenario.get("id")
        scenario_name = scenario.get("name", "")
        if scenario_id is not None:
            failing.append((int(scenario_id), scenario_name))
    return failing


# ---------------------------------------------------------------------------
# Core logic (async, testable)
# ---------------------------------------------------------------------------

async def _enqueue_failing_async(payload: dict[str, Any]) -> None:
    """Extract failing scenarios and push new ones onto the queue."""
    data = payload.get("data", {})
    success_rate: float = data.get("success_rate", 0.0)

    if success_rate >= 100.0:
        log.info("All scenarios passed (success_rate=%.1f) — nothing to heal.", success_rate)
        return

    failing = _extract_failing_scenarios(data)
    if not failing:
        log.info("No heal-eligible failures found in payload.")
        return

    async with _lock:
        for sid, name in failing:
            if sid in _healed_success:
                log.info("Scenario %d already healed and passed this session — skipping.", sid)
                continue
            if sid in _queued:
                log.info("Scenario %d already queued/in-progress — skipping.", sid)
                continue
            _queued.add(sid)
            _queued_names[sid] = name
            await _queue.put(sid)
            log.info("Enqueued scenario %d (%r) for healing.", sid, name)
        _write_status()


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


async def handle_internal_enqueue(request: web.Request) -> web.Response:
    """Loopback-only endpoint: manually enqueue a single scenario for healing."""
    try:
        body = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")

    scenario_id = body.get("scenario_id")
    scenario_name = body.get("scenario_name", "")
    if not isinstance(scenario_id, int):
        return web.Response(status=400, text="scenario_id must be an integer")

    async with _lock:
        if scenario_id in _healed_success:
            return web.json_response({"ok": True, "message": "already healed and passed"})
        if scenario_id in _queued:
            return web.json_response({"ok": True, "message": "already queued"})
        _queued.add(scenario_id)
        _queued_names[scenario_id] = scenario_name
        await _queue.put(scenario_id)
        log.info("Manually enqueued scenario %d (%r).", scenario_id, scenario_name)
        _write_status()

    return web.json_response({"ok": True, "scenario_id": scenario_id})


# ---------------------------------------------------------------------------
# Serial worker
# ---------------------------------------------------------------------------

async def _worker(loop: asyncio.AbstractEventLoop) -> None:
    """Drain the queue one scenario at a time."""
    log.info("Worker started — waiting for scenarios.")
    while True:
        scenario_id = await _queue.get()
        scenario_name = _queued_names.get(scenario_id, "")
        log.info("▶  Healing scenario %d (%r)…", scenario_id, scenario_name)
        result: HealResult | None = None
        _heal_state["in_progress"] = {
            "scenario_id": scenario_id,
            "scenario_name": scenario_name,
            "started_at": datetime.now(UTC).isoformat(),
            "steps": [],
        }
        _write_status()

        def _step_cb(text: str) -> None:
            state = _heal_state.get("in_progress")
            if state is not None:
                state.setdefault("steps", []).append({
                    "ts": datetime.now(UTC).isoformat(),
                    "text": text,
                })
                _write_status()

        try:
            heal_fn = functools.partial(run_heal, scenario_id, auto_merge=True, step_callback=_step_cb)
            result = await loop.run_in_executor(None, heal_fn)
            _log_result(scenario_id, result)
        except Exception as exc:
            log.error("Heal failed for scenario %d: %s", scenario_id, exc)
        finally:
            _heal_state["in_progress"] = None
            _heal_state["last_result"] = {
                "scenario_id": scenario_id,
                "scenario_name": scenario_name,
                "passed": result.passed if result is not None else False,
                "pr_url": result.pr_url if result is not None else None,
                "completed_at": datetime.now(UTC).isoformat(),
            }
            if result is not None and result.passed:
                _healed_success.add(scenario_id)
            _queued.discard(scenario_id)
            _queued_names.pop(scenario_id, None)
            _queue.task_done()
            _write_status()


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
    app.router.add_post("/internal/enqueue", handle_internal_enqueue)

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
