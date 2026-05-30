# Self-Healing Infrastructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a webhook-driven self-healing loop that detects Cekura evaluation failures, patches the bot prompt via GPT-5.5, deploys, verifies, and auto-merges — one scenario at a time.

**Architecture:** An aiohttp webhook server receives `result.completed` events from Cekura, enqueues failing scenario IDs into a serial `asyncio.Queue`, and a single worker drains the queue one at a time via `run_in_executor` → `run_heal()`. Auto-merge only fires after a full Cekura suite re-run confirms no regressions.

**Tech Stack:** Python 3.12, aiohttp 3.13, asyncio, openai SDK (token router), `gh` CLI, `pc` CLI, ngrok static domain.

**Spec:** `docs/superpowers/specs/2026-05-30-self-healing-infra-design.md`

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `harness/self_heal.py` | Replace `sys.exit()` → `RuntimeError`; add `run_heal()`; add `auto_merge` to `open_pr()` |
| Create | `harness/webhook_server.py` | aiohttp server, auth, serial queue worker, result logging |
| Create | `harness/start.sh` | Sources `.env`, starts ngrok + webhook server |
| Create | `harness/tests/test_self_heal.py` | Unit tests for `run_heal()` and modified helpers |
| Create | `harness/tests/test_webhook_server.py` | Unit tests for auth, enqueue logic, dedup |

---

## Task 1: Replace `sys.exit()` with `RuntimeError` in `self_heal.py`

Five `sys.exit()` calls must become `raise RuntimeError(...)` so they don't kill the
webhook server process when called from a thread. The CLI `main()` gets a
`try/except RuntimeError` wrapper to preserve existing behaviour.

**Files:**
- Modify: `harness/self_heal.py:94-132` (three env helpers)
- Modify: `harness/self_heal.py:554-562` (two guards in `self_heal()`)
- Modify: `harness/self_heal.py:719-721` (`main()`)
- Create: `harness/tests/test_self_heal.py`

- [ ] **Step 1: Create test file and write failing tests**

```bash
mkdir -p harness/tests
touch harness/tests/__init__.py
```

Create `harness/tests/test_self_heal.py`:

```python
"""Tests for self_heal.py — env helpers and run_heal()."""
import pytest
import sys
import os

# Make harness importable
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parents[1]))

import self_heal


def test_cekura_key_raises_runtime_error_not_system_exit(monkeypatch):
    """_cekura_key() must raise RuntimeError, not SystemExit, when key is missing."""
    monkeypatch.delenv("CEKURA_API_KEY", raising=False)
    # Point to a non-existent .env so file fallback also fails
    monkeypatch.setattr(self_heal, "SERVER", __import__("pathlib").Path("/nonexistent"))
    with pytest.raises(RuntimeError, match="CEKURA_API_KEY"):
        self_heal._cekura_key()


def test_token_router_key_raises_runtime_error(monkeypatch):
    monkeypatch.delenv("TOKEN_ROUTER_API_KEY", raising=False)
    monkeypatch.setattr(self_heal, "SERVER", __import__("pathlib").Path("/nonexistent"))
    with pytest.raises(RuntimeError, match="TOKEN_ROUTER_API_KEY"):
        self_heal._token_router_key()


def test_token_router_base_url_raises_runtime_error(monkeypatch):
    monkeypatch.delenv("TOKEN_ROUTER_BASE_URL", raising=False)
    monkeypatch.setattr(self_heal, "SERVER", __import__("pathlib").Path("/nonexistent"))
    with pytest.raises(RuntimeError, match="TOKEN_ROUTER_BASE_URL"):
        self_heal._token_router_base_url()


def test_self_heal_raises_on_detached_head(monkeypatch):
    """self_heal() must raise RuntimeError (not SystemExit) on detached HEAD."""
    monkeypatch.setattr(self_heal, "current_branch", lambda: "HEAD")
    import argparse
    args = argparse.Namespace(scenario=1, max_iterations=1, dry_run=False, no_deploy=True)
    with pytest.raises(RuntimeError, match="Detached HEAD"):
        self_heal.self_heal(args)


def test_self_heal_raises_on_dirty_tree(monkeypatch, tmp_path):
    monkeypatch.setattr(self_heal, "current_branch", lambda: "main")
    monkeypatch.setattr(self_heal, "has_uncommitted_changes", lambda: True)
    import argparse
    args = argparse.Namespace(scenario=1, max_iterations=1, dry_run=False, no_deploy=True)
    with pytest.raises(RuntimeError, match="Uncommitted changes"):
        self_heal.self_heal(args)
```

- [ ] **Step 2: Run tests — expect FAIL (sys.exit raises SystemExit, not RuntimeError)**

```bash
cd /Users/vicky/yc-voice-agents-hackathon
uv run --directory server pytest harness/tests/test_self_heal.py -v
```

Expected: 5 failures like `Failed: DID NOT RAISE <class 'RuntimeError'>` or `SystemExit` raised instead.

- [ ] **Step 3: Replace `sys.exit()` in the three env helpers (`self_heal.py:104, 118, 132`)**

In `harness/self_heal.py`, change:

```python
# Line 104 — _cekura_key()
    if not key:
        sys.exit("CEKURA_API_KEY not found in environment or server/.env")
    return key
```
→
```python
    if not key:
        raise RuntimeError("CEKURA_API_KEY not found in environment or server/.env")
    return key
```

Same pattern for `_token_router_key()` (line 118) and `_token_router_base_url()` (line 132):
```python
    if not key:
        raise RuntimeError("TOKEN_ROUTER_API_KEY not found in environment or server/.env.")
    return key
```
```python
    if not url:
        raise RuntimeError("TOKEN_ROUTER_BASE_URL not found in environment or server/.env.")
    return url
```

- [ ] **Step 4: Replace `sys.exit()` in `self_heal()` guards (`self_heal.py:554-562`)**

```python
# Before
    if base_branch == "HEAD":
        sys.exit("Detached HEAD — check out a named branch first.")

    if has_uncommitted_changes() and not dry_run:
        sys.exit(
            "Uncommitted changes in working tree.\n"
            "Commit or stash them before running self_heal so each patch is isolated.\n"
            "Use --dry-run to preview without touching the tree."
        )
```
→
```python
    if base_branch == "HEAD":
        raise RuntimeError("Detached HEAD — check out a named branch first.")

    if has_uncommitted_changes() and not dry_run:
        raise RuntimeError(
            "Uncommitted changes in working tree.\n"
            "Commit or stash them before running self_heal so each patch is isolated.\n"
            "Use --dry-run to preview without touching the tree."
        )
```

- [ ] **Step 5: Wrap `main()` to preserve CLI behaviour (`self_heal.py:719-721`)**

```python
# Before
def main() -> None:
    args = parse_args()
    result = self_heal(args)
```
→
```python
def main() -> None:
    args = parse_args()
    try:
        result = self_heal(args)
    except RuntimeError as exc:
        sys.exit(str(exc))
```

- [ ] **Step 6: Run tests — expect all 5 PASS**

```bash
uv run --directory server pytest harness/tests/test_self_heal.py -v
```

Expected: `5 passed`

- [ ] **Step 7: Commit**

```bash
git add harness/self_heal.py harness/tests/__init__.py harness/tests/test_self_heal.py
git commit -m "refactor: replace sys.exit with RuntimeError in self_heal helpers"
```

---

## Task 2: Add `run_heal()` wrapper to `self_heal.py`

A plain callable entry point that `webhook_server.py` can invoke from a thread without
touching `argparse`.

**Files:**
- Modify: `harness/self_heal.py` (add after `self_heal()` function, before `parse_args()`)
- Modify: `harness/tests/test_self_heal.py` (add test)

- [ ] **Step 1: Write failing test**

Add to `harness/tests/test_self_heal.py`:

```python
def test_run_heal_passes_args_correctly(monkeypatch):
    """run_heal() should call self_heal() with the right Namespace."""
    captured = {}

    def fake_self_heal(args):
        captured["args"] = args
        return self_heal.HealResult(
            scenario_id=args.scenario,
            iterations=0,
            final_score=100,
            passed=True,
            pr_url=None,
        )

    monkeypatch.setattr(self_heal, "self_heal", fake_self_heal)
    result = self_heal.run_heal(42, max_iterations=2, dry_run=True, no_deploy=True)

    assert captured["args"].scenario == 42
    assert captured["args"].max_iterations == 2
    assert captured["args"].dry_run is True
    assert captured["args"].no_deploy is True
    assert result.scenario_id == 42
```

- [ ] **Step 2: Run test — expect FAIL (`AttributeError: module has no attribute 'run_heal'`)**

```bash
uv run --directory server pytest harness/tests/test_self_heal.py::test_run_heal_passes_args_correctly -v
```

- [ ] **Step 3: Add `run_heal()` to `self_heal.py`** (insert after `self_heal()`, before the `parse_args()` section comment block)

```python
def run_heal(
    scenario_id: int,
    max_iterations: int = 3,
    dry_run: bool = False,
    no_deploy: bool = False,
) -> HealResult:
    """Callable entry point for webhook_server — no argparse required."""
    ns = argparse.Namespace(
        scenario=scenario_id,
        max_iterations=max_iterations,
        dry_run=dry_run,
        no_deploy=no_deploy,
    )
    return self_heal(ns)
```

- [ ] **Step 4: Run test — expect PASS**

```bash
uv run --directory server pytest harness/tests/test_self_heal.py -v
```

Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add harness/self_heal.py harness/tests/test_self_heal.py
git commit -m "feat: add run_heal() callable wrapper to self_heal"
```

---

## Task 3: Add `auto_merge` flag to `open_pr()` with regression gate

When `auto_merge=True` and score reached 100%, trigger a full Cekura suite re-run and
only merge if no regressions are detected.

**Files:**
- Modify: `harness/self_heal.py:458-464` (`open_pr()` signature)
- Modify: `harness/self_heal.py:665-667` (call site in `self_heal()`)
- Modify: `harness/tests/test_self_heal.py`

- [ ] **Step 1: Write failing tests**

Add to `harness/tests/test_self_heal.py`:

```python
def test_run_heal_propagates_auto_merge(monkeypatch):
    """run_heal(auto_merge=True) must set args.auto_merge=True in the Namespace."""
    captured = {}

    def fake_self_heal(args):
        captured["args"] = args
        return self_heal.HealResult(
            scenario_id=args.scenario,
            iterations=0,
            final_score=100,
            passed=True,
            pr_url=None,
        )

    monkeypatch.setattr(self_heal, "self_heal", fake_self_heal)
    self_heal.run_heal(99, auto_merge=True)
    assert captured["args"].auto_merge is True


def test_open_pr_auto_merge_not_called_by_default(monkeypatch):
    """auto_merge defaults to False — gh pr merge must NOT be called."""
    merge_called = []

    def fake_run_cmd(args, **kwargs):
        if "merge" in args:
            merge_called.append(args)
        result = __import__("subprocess").CompletedProcess(args, 0)
        result.stdout = "https://github.com/org/repo/pull/1"
        result.stderr = ""
        return result

    monkeypatch.setattr(self_heal, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(self_heal, "git", lambda *a, **k: __import__("subprocess").CompletedProcess(a, 0, stdout="", stderr=""))

    self_heal.open_pr(
        proposals=[],
        scenario_name="test-scenario",
        before_score=50,
        after_score=100,
        base_branch="main",
        # auto_merge not passed — defaults to False
    )
    assert merge_called == [], "gh pr merge should not be called when auto_merge=False"


def test_open_pr_auto_merge_merges_when_suite_passes(monkeypatch):
    """auto_merge=True should call gh pr merge when full suite re-run passes."""
    merge_called = []

    def fake_run_cmd(args, **kwargs):
        if "merge" in args:
            merge_called.append(True)
        result = __import__("subprocess").CompletedProcess(args, 0)
        result.stdout = "https://github.com/org/repo/pull/2"
        result.stderr = ""
        return result

    monkeypatch.setattr(self_heal, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(self_heal, "git", lambda *a, **k: __import__("subprocess").CompletedProcess(a, 0, stdout="", stderr=""))
    monkeypatch.setattr(self_heal, "_run_full_suite_and_check", lambda: True)

    self_heal.open_pr(
        proposals=[],
        scenario_name="test",
        before_score=50,
        after_score=100,
        base_branch="main",
        auto_merge=True,
    )
    assert merge_called, "gh pr merge should be called when auto_merge=True and suite passes"


def test_open_pr_auto_merge_skipped_when_suite_regresses(monkeypatch):
    """auto_merge=True should NOT call gh pr merge when full suite re-run shows regression."""
    merge_called = []

    def fake_run_cmd(args, **kwargs):
        if "merge" in args:
            merge_called.append(True)
        result = __import__("subprocess").CompletedProcess(args, 0)
        result.stdout = "https://github.com/org/repo/pull/3"
        result.stderr = ""
        return result

    monkeypatch.setattr(self_heal, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(self_heal, "git", lambda *a, **k: __import__("subprocess").CompletedProcess(a, 0, stdout="", stderr=""))
    monkeypatch.setattr(self_heal, "_run_full_suite_and_check", lambda: False)

    self_heal.open_pr(
        proposals=[],
        scenario_name="test",
        before_score=50,
        after_score=100,
        base_branch="main",
        auto_merge=True,
    )
    assert merge_called == [], "gh pr merge must NOT be called when suite regresses"
```

- [ ] **Step 2: Run tests — expect FAIL**

```bash
uv run --directory server pytest harness/tests/test_self_heal.py -k "auto_merge or propagates" -v
```

- [ ] **Step 3: Add `auto_merge` param and `_run_full_suite_and_check()` to `self_heal.py`**

Add the helper function (insert before `open_pr()`):

```python
def _run_full_suite_and_check() -> bool:
    """Trigger a full Cekura suite re-run and return True if all scenarios pass."""
    print("  🔄 Running full Cekura suite to check for regressions…")
    try:
        result = cekura_post("/results/run_all/", {"agent": CEKURA_AGENT_ID})
        run_id = result["id"]
        deadline = time.time() + 600  # 10 min timeout for full suite
        data: dict = {}
        while time.time() < deadline:
            data = cekura_get(f"/results/{run_id}/")
            if data.get("status") == "completed":
                break
            print(".", end="", flush=True)
            time.sleep(POLL_INTERVAL_S)
        print()
        success_rate: float = data.get("success_rate", 0.0)
        if success_rate >= 100.0:
            print(f"  ✅ Full suite passed: {success_rate}%")
            return True
        print(f"  ⚠  Regression detected: {success_rate}% — not all scenarios pass")
        return False
    except Exception as exc:
        print(f"  ⚠  Full suite re-run failed: {exc}")
        return False
```

Update `open_pr()` signature — add `auto_merge` only (no `baseline_pass_rate`; the gate checks 100% full suite):

```python
def open_pr(
    proposals: list[PatchProposal],
    scenario_name: str,
    before_score: int | None,
    after_score: int,
    base_branch: str,
    auto_merge: bool = False,           # NEW
) -> str | None:
```

At the end of `open_pr()`, after the `gh pr create` call succeeds, add:

```python
    if auto_merge and pr_url:
        print("  🔍 Checking for regressions before auto-merge…")
        if _run_full_suite_and_check():
            try:
                run_cmd(["gh", "pr", "merge", pr_url,
                         "--squash", "--delete-branch", "--yes"])
                print(f"  ✅ Auto-merged: {pr_url}")
            except subprocess.CalledProcessError as exc:
                print(f"  ⚠  Auto-merge failed: {(exc.stderr or '').strip()[:200]}")
                print("     PR left open for manual merge.")
        else:
            print("  ⚠  Regression detected — PR left open for manual review.")
```

Update the call site in `self_heal()` (line ~667) to pass `auto_merge`.
Add `auto_merge: bool = False` to `self_heal()`'s args reading and to `run_heal()`'s signature:

In `self_heal()`, after reading `no_deploy`:
```python
    auto_merge: bool = getattr(args, "auto_merge", False)
```

Update the `open_pr()` call:
```python
        pr_url = open_pr(
            patches_applied,
            baseline.scenario_name,
            before_score,
            final_score,
            base_branch,
            auto_merge=auto_merge,
        )
```

Update `run_heal()`:
```python
def run_heal(
    scenario_id: int,
    max_iterations: int = 3,
    dry_run: bool = False,
    no_deploy: bool = False,
    auto_merge: bool = False,
) -> HealResult:
    ns = argparse.Namespace(
        scenario=scenario_id,
        max_iterations=max_iterations,
        dry_run=dry_run,
        no_deploy=no_deploy,
        auto_merge=auto_merge,
    )
    return self_heal(ns)
```

- [ ] **Step 4: Run all tests — expect PASS**

```bash
uv run --directory server pytest harness/tests/test_self_heal.py -v
```

Expected: all tests pass (10 total: 5 from Task 1, 1 from Task 2, 4 from Task 3).

- [ ] **Step 5: Commit**

```bash
git add harness/self_heal.py harness/tests/test_self_heal.py
git commit -m "feat: add auto_merge flag with regression gate to open_pr"
```

---

## Task 4: Create `harness/webhook_server.py`

The aiohttp server: auth, enqueue, serial worker, result logging.

**Files:**
- Create: `harness/webhook_server.py`
- Create: `harness/tests/test_webhook_server.py`

- [ ] **Step 1: Write failing tests**

Create `harness/tests/test_webhook_server.py`:

```python
"""Tests for webhook_server.py."""
import asyncio
import json
import pytest
import sys
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
```

- [ ] **Step 2: Run tests — expect FAIL (module not found)**

```bash
uv run --directory server pytest harness/tests/test_webhook_server.py -v 2>&1 | head -20
```

- [ ] **Step 3: Create `harness/webhook_server.py`**

```python
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

from self_heal import run_heal, HealResult  # noqa: E402

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
            log.warning("Skipping infra error for scenario '%s': %s",
                        scenario_name, run["error_message"][:100])
            continue
        scenario_id = run.get("scenario", {}).get("id")
        if scenario_id is not None:
            failing.append(int(scenario_id))
    return failing


# ---------------------------------------------------------------------------
# Core logic (sync-friendly wrappers for testing)
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


def _enqueue_failing(payload: dict[str, Any]) -> None:
    """Sync wrapper used in tests."""
    loop = asyncio.get_event_loop()
    loop.run_until_complete(_enqueue_failing_async(payload))


async def _process_webhook(
    headers: dict[str, str],
    payload: dict[str, Any],
) -> int:
    """Validate and handle a webhook payload. Returns HTTP status code."""
    # Auth
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
            # run_in_executor only accepts positional args; functools.partial lets us
            # use keyword args so parameter order changes don't silently break auto_merge.
            heal_fn = functools.partial(run_heal, scenario_id, auto_merge=True)
            result: HealResult = await loop.run_in_executor(None, heal_fn)
            _log_result(scenario_id, result)
        except Exception as exc:
            log.error("Heal failed for scenario %d: %s", scenario_id, exc)
        finally:
            async with _lock:
                _queued.discard(scenario_id)
            _queue.task_done()


def _log_result(scenario_id: int, result: HealResult) -> None:
    out_dir = ROOT / "harness" / "runs" / f"webhook-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"scenario-{scenario_id}.json"
    import dataclasses
    out_file.write_text(
        json.dumps(dataclasses.asdict(result), indent=2, default=str),
        encoding="utf-8",
    )
    status = "✅ PASSED" if result.passed else "❌ no improvement"
    log.info("◀  Scenario %d complete: %s (score=%s, pr=%s)",
             scenario_id, status, result.final_score, result.pr_url or "—")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bayview self-healing webhook server.")
    parser.add_argument("--port", type=int, default=8888)
    return parser.parse_args()


async def main_async(port: int) -> None:
    loop = asyncio.get_event_loop()
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
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
uv run --directory server pytest harness/tests/test_webhook_server.py -v
```

Expected: all 7 tests pass.

- [ ] **Step 5: Smoke-test the server manually**

```bash
# Terminal 1
cd /Users/vicky/yc-voice-agents-hackathon
uv run --directory server python3 harness/webhook_server.py &
sleep 1

# Terminal 2 — test auth rejection
curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:8888/webhook/cekura \
  -H "Content-Type: application/json" \
  -d '{"event_type":"result.completed","data":{"success_rate":100.0,"runs":{}}}' 
# Expected: 401

# Test with correct secret (all-pass → no queue)
CEKURA_WEBHOOK_SECRET=$(grep CEKURA_WEBHOOK_SECRET server/.env | cut -d= -f2)
curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:8888/webhook/cekura \
  -H "X-CEKURA-SECRET: $CEKURA_WEBHOOK_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"event_type":"result.completed","data":{"success_rate":100.0,"runs":{}}}'
# Expected: 200

kill %1  # stop the background server
```

- [ ] **Step 6: Commit**

```bash
git add harness/webhook_server.py harness/tests/test_webhook_server.py
git commit -m "feat: add webhook_server with serial heal queue"
```

---

## Task 5: Create `harness/start.sh`

One command that starts everything and prints the webhook URL.

**Files:**
- Create: `harness/start.sh`

- [ ] **Step 1: Create `harness/start.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
PORT=8888

# Load server/.env so webhook_server.py inherits all secrets
set -a
# shellcheck source=/dev/null
source "$ROOT/server/.env"
set +a

DOMAIN="${NGROK_DOMAIN:?NGROK_DOMAIN not set in server/.env}"

# Start ngrok with static domain in background
ngrok http --domain="$DOMAIN" "$PORT" --log=stdout > /tmp/ngrok-bayview.log 2>&1 &
NGROK_PID=$!
sleep 3  # give ngrok time to establish tunnel

echo ""
echo "╔══════════════════════════════════════════════════════════════════╗"
echo "  🔗 Webhook URL → https://$DOMAIN/webhook/cekura"
echo "  📋 Configure in: Cekura → Agent Settings → Webhook URL"
echo "  🔑 Secret already set in server/.env as CEKURA_WEBHOOK_SECRET"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""

# Trap to clean up ngrok on exit
trap 'kill $NGROK_PID 2>/dev/null; echo "Stopped."' EXIT INT TERM

# Start webhook server (foreground)
cd "$ROOT"
uv run --directory server python3 harness/webhook_server.py --port "$PORT"
```

- [ ] **Step 2: Make executable**

```bash
chmod +x harness/start.sh
```

- [ ] **Step 3: Verify it prints correctly (dry-run — don't let it block)**

```bash
# Just check the script is valid bash and the domain loads correctly
bash -n harness/start.sh && echo "syntax OK"
source server/.env && echo "Domain: $NGROK_DOMAIN"
```

Expected output:
```
syntax OK
Domain: prefashioned-jaspa-dillon.ngrok-free.dev
```

- [ ] **Step 4: Commit**

```bash
git add harness/start.sh
git commit -m "feat: add start.sh to launch ngrok + webhook server"
```

---

## Task 6: End-to-end smoke test

Verify the full loop without burning Cekura credits — use a fake webhook payload.

**Files:** No new files.

- [ ] **Step 1: Start the server**

```bash
# In one terminal:
./harness/start.sh
```

Wait for: `Webhook server listening on port 8888`

- [ ] **Step 2: Send a test payload with a real (known-failing) scenario ID**

```bash
CEKURA_WEBHOOK_SECRET=$(grep CEKURA_WEBHOOK_SECRET server/.env | cut -d= -f2)

curl -s -w "\nHTTP %{http_code}\n" -X POST http://localhost:8888/webhook/cekura \
  -H "X-CEKURA-SECRET: $CEKURA_WEBHOOK_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "event_type": "result.completed",
    "data": {
      "success_rate": 0.0,
      "runs": {
        "1": {
          "id": 1,
          "scenario": {"id": 272668, "name": "Identity verification"},
          "success": false,
          "evaluation": {"metrics": []},
          "transcript_object": [],
          "error_message": ""
        }
      }
    }
  }'
```

Expected:
```
HTTP 200
```

Server log should show:
```
Enqueued scenario 272668 for healing.
▶  Healing scenario 272668…
```

- [ ] **Step 3: Send the same payload again — verify dedup**

Re-run the same `curl` from Step 2 while the heal is in progress.

Server log should show:
```
Scenario 272668 already queued/in-progress — skipping.
```

- [ ] **Step 4: Commit run log if any result was produced**

```bash
ls harness/runs/webhook-*/scenario-*.json 2>/dev/null && \
  git add harness/runs/ && git commit -m "chore: add first webhook heal run log" || \
  echo "No run log yet (heal still in progress)"
```

---

## Running the Full Test Suite

```bash
cd /Users/vicky/yc-voice-agents-hackathon
uv run --directory server pytest harness/tests/ -v
```

Expected: all tests pass.
