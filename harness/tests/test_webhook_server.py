"""Tests for webhook_server.py."""
import asyncio
import json
import sys

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parents[1]))


# ── Helpers ──────────────────────────────────────────────────────────────────

VALID_SECRET = "test-secret-abc"
FAILING_PAYLOAD = {
    "event_type": "result.completed",
    "data": {
        "success_rate": 0.0,
        "runs": {
            "1": {
                "id": 1,
                "scenario": {"id": 272668, "name": "Test Scenario"},
                "success": False,
                "evaluation": {"metrics": []},
                "transcript_object": [],
                "error_message": "",
            }
        },
    },
}
ALL_PASS_PAYLOAD = {
    "event_type": "result.completed",
    "data": {"success_rate": 100.0, "runs": {}},
}
UNKNOWN_EVENT_PAYLOAD = {"event_type": "some.other.event", "data": {}}


@pytest.fixture
def server_module(monkeypatch):
    """Import webhook_server with CEKURA_WEBHOOK_SECRET patched."""
    import importlib
    monkeypatch.setenv("CEKURA_WEBHOOK_SECRET", VALID_SECRET)
    import webhook_server
    importlib.reload(webhook_server)
    return webhook_server


# ── Auth tests ────────────────────────────────────────────────────────────────

def test_missing_secret_header_returns_401(server_module):
    status = asyncio.run(server_module._process_webhook({}, FAILING_PAYLOAD))
    assert status == 401


def test_wrong_secret_returns_401(server_module):
    status = asyncio.run(
        server_module._process_webhook({"X-CEKURA-SECRET": "wrong"}, FAILING_PAYLOAD)
    )
    assert status == 401


def test_correct_secret_returns_200(server_module, monkeypatch):
    async def fake_enqueue(payload): pass
    monkeypatch.setattr(server_module, "_enqueue_failing_async", fake_enqueue)
    status = asyncio.run(
        server_module._process_webhook(
            {"X-CEKURA-SECRET": VALID_SECRET}, FAILING_PAYLOAD
        )
    )
    assert status == 200


# ── Event handling tests ──────────────────────────────────────────────────────

def test_all_pass_payload_not_enqueued(server_module, monkeypatch):
    enqueued = []

    async def fake_enqueue(payload):
        enqueued.append(payload)

    monkeypatch.setattr(server_module, "_enqueue_failing_async", fake_enqueue)
    asyncio.run(
        server_module._process_webhook(
            {"X-CEKURA-SECRET": VALID_SECRET}, ALL_PASS_PAYLOAD
        )
    )
    assert enqueued == []


def test_unknown_event_returns_200_and_ignored(server_module, monkeypatch):
    enqueued = []

    async def fake_enqueue(payload):
        enqueued.append(payload)

    monkeypatch.setattr(server_module, "_enqueue_failing_async", fake_enqueue)
    status = asyncio.run(
        server_module._process_webhook(
            {"X-CEKURA-SECRET": VALID_SECRET}, UNKNOWN_EVENT_PAYLOAD
        )
    )
    assert status == 200
    assert enqueued == []


# ── Dedup tests ───────────────────────────────────────────────────────────────

def test_duplicate_scenario_not_enqueued_twice(server_module):
    """Same scenario ID in two webhook payloads → only enqueued once."""
    async def run():
        server_module._queued.clear()
        # Drain the queue so previous test state doesn't interfere
        while not server_module._queue.empty():
            server_module._queue.get_nowait()

        await server_module._enqueue_failing_async(FAILING_PAYLOAD)
        first_size = len(server_module._queued)
        await server_module._enqueue_failing_async(FAILING_PAYLOAD)
        second_size = len(server_module._queued)
        return first_size, second_size

    first, second = asyncio.run(run())
    assert first == 1
    assert second == 1  # second webhook for same ID was ignored


# ── Infra error skip test ─────────────────────────────────────────────────────

def test_infra_error_runs_not_enqueued(server_module):
    """Runs with error_message and success=False are infra failures — skip self-heal."""
    infra_payload = {
        "event_type": "result.completed",
        "data": {
            "success_rate": 0.0,
            "runs": {
                "1": {
                    "id": 1,
                    "scenario": {"id": 999, "name": "Infra Test"},
                    "success": False,
                    "evaluation": {"metrics": []},
                    "transcript_object": [],
                    "error_message": "WebSocket handshake failed",
                }
            },
        },
    }

    async def run():
        server_module._queued.clear()
        await server_module._enqueue_failing_async(infra_payload)
        return list(server_module._queued)

    result = asyncio.run(run())
    assert 999 not in result, "Infra errors should not trigger self-heal"
