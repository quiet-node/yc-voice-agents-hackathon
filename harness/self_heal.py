#!/usr/bin/env python3
"""Bayview self-healing loop.

Runs a single Cekura scenario, calls GPT-5.5 to propose a targeted prompt patch
if it fails, applies the patch to bot-nemotron.py, deploys to Pipecat Cloud,
re-runs the same scenario, and opens a GitHub PR when the score improves.

Nothing is merged automatically — every fix requires PR approval.

Usage:
    python3 harness/self_heal.py --scenario 272668
    python3 harness/self_heal.py --scenario 272678 --max-iterations 3
    python3 harness/self_heal.py --scenario 272668 --dry-run
    python3 harness/self_heal.py --scenario 272668 --no-deploy   # skip pc cloud deploy
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
BOT_FILE = SERVER / "bot-nemotron.py"

CEKURA_API_BASE = "https://api.cekura.ai/test_framework/v1"
CEKURA_AGENT_ID = 18021
PIPECAT_AGENT_NAME = "bayview-pharmacy"

POLL_INTERVAL_S = 15
DEPLOY_POLL_INTERVAL_S = 20
DEPLOY_TIMEOUT_S = 300


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    run_id: int
    scenario_name: str
    eo_score: int | None
    passed: bool
    transcript: list[dict[str, Any]]
    failure_explanations: list[str]
    main_agent_turns: int


@dataclass
class PatchProposal:
    find: str
    replace: str
    rationale: str
    iteration: int


@dataclass
class HealResult:
    scenario_id: int
    iterations: int
    final_score: int | None
    passed: bool
    pr_url: str | None
    patches_applied: list[PatchProposal] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug[:50] or "fix"


def _cekura_key() -> str:
    key = os.environ.get("CEKURA_API_KEY", "")
    if not key:
        env_file = SERVER / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("CEKURA_API_KEY="):
                    key = line.split("=", 1)[1].strip()
                    break
    if not key:
        raise RuntimeError("CEKURA_API_KEY not found in environment or server/.env")
    return key


def _token_router_key() -> str:
    key = os.environ.get("TOKEN_ROUTER_API_KEY", "")
    if not key:
        env_file = SERVER / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("TOKEN_ROUTER_API_KEY="):
                    key = line.split("=", 1)[1].strip()
                    break
    if not key:
        raise RuntimeError("TOKEN_ROUTER_API_KEY not found in environment or server/.env")
    return key


def _token_router_base_url() -> str:
    url = os.environ.get("TOKEN_ROUTER_BASE_URL", "")
    if not url:
        env_file = SERVER / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("TOKEN_ROUTER_BASE_URL="):
                    url = line.split("=", 1)[1].strip()
                    break
    if not url:
        raise RuntimeError("TOKEN_ROUTER_BASE_URL not found in environment or server/.env")
    return url


def cekura_get(path: str) -> Any:
    url = f"{CEKURA_API_BASE}{path}"
    req = urllib.request.Request(url, headers={"X-CEKURA-API-KEY": _cekura_key()})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def cekura_post(path: str, payload: dict[str, Any]) -> Any:
    url = f"{CEKURA_API_BASE}{path}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "X-CEKURA-API-KEY": _cekura_key(),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def run_cmd(args: list[str], *, check: bool = True, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=check, capture_output=True, text=True, cwd=cwd)


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return run_cmd(["git", *args], check=check)


def current_branch() -> str:
    return git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def has_uncommitted_changes() -> bool:
    return bool(git("status", "--porcelain").stdout.strip())


# ---------------------------------------------------------------------------
# Cekura: run scenario + poll
# ---------------------------------------------------------------------------


def run_scenario(scenario_id: int) -> int:
    # Pipecat Cloud agents use pipecat_v2 — Cekura provisions sessions internally.
    # The old /run_scenarios/ endpoint is for phone/SIP and requires room URLs.
    result = cekura_post(
        "/scenarios/run_scenarios_pipecat_v2/",
        {"scenarios": [{"scenario": scenario_id}], "frequency": 1},
    )
    run_id: int = result["id"]
    print(f"  → Cekura run started: ID {run_id}")
    return run_id


def poll_run(run_id: int) -> RunResult:
    print(f"  ↻ Polling run {run_id}", end="", flush=True)
    while True:
        data = cekura_get(f"/results/{run_id}/")
        if data.get("status") == "completed":
            print(" done")
            break
        print(".", end="", flush=True)
        time.sleep(POLL_INTERVAL_S)

    runs = data.get("runs", {})
    if not runs:
        return RunResult(
            run_id=run_id,
            scenario_name="unknown",
            eo_score=None,
            passed=False,
            transcript=[],
            failure_explanations=["No run data returned"],
            main_agent_turns=0,
        )

    r = next(iter(runs.values())) if isinstance(runs, dict) else runs[0]
    transcript = r.get("transcript_object") or r.get("transcript", [])
    if isinstance(transcript, str):
        transcript = []
    eo = r.get("expected_outcome") or {}
    score: int | None = eo.get("score")
    explanations: list[str] = eo.get("explanation", [])
    main_turns = sum(1 for t in transcript if "Main" in t.get("role", ""))

    return RunResult(
        run_id=run_id,
        scenario_name=r.get("scenario", {}).get("name", "unknown"),
        eo_score=score,
        passed=score == 100,
        transcript=transcript,
        failure_explanations=explanations,
        main_agent_turns=main_turns,
    )


# ---------------------------------------------------------------------------
# GPT-5.5: propose prompt patch
# ---------------------------------------------------------------------------

_PATCH_PROMPT = """\
You are improving the system prompt for Bayview Pharmacy, a voice AI agent.

## Current system_instruction (excerpt around the problem area)
{system_instruction_excerpt}

## Scenario that is failing
{scenario_name}

## Expected outcomes (what should happen)
{expected_outcomes}

## What actually went wrong (Cekura evaluation)
{failure_explanations}

## Transcript
{transcript}

## Your task
Propose ONE minimal, targeted change to the system_instruction text above that
would fix the failure WITHOUT breaking the passing scenarios.

Rules:
- Only edit the exact text shown — do not invent new sections or restructure.
- Keep the change to 1–3 sentences.
- Do not remove safety rules (identity verification, privacy protections).
- Be surgical: the smallest change that addresses the root cause.

Respond with a JSON object ONLY (no markdown fences):
{{
  "find": "exact substring to replace (must exist verbatim in the excerpt)",
  "replace": "new text to substitute in",
  "rationale": "one sentence explaining why this fixes the failure"
}}
"""


def _build_transcript_text(transcript: list[dict[str, Any]]) -> str:
    lines = []
    for t in transcript[:30]:
        role = t.get("role", "?")
        content = t.get("content") or t.get("message", "")
        if isinstance(content, list):
            content = " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
        lines.append(f"[{role}]: {str(content)[:150]}")
    return "\n".join(lines) if lines else "(no transcript)"


def _read_system_instruction(bot_file: Path) -> str:
    content = bot_file.read_text(encoding="utf-8")
    # Extract from the system_instruction = ( ... ) block
    match = re.search(r'system_instruction\s*=\s*\((.*?)\)\s*\n', content, re.DOTALL)
    if match:
        raw = match.group(1)
        # Remove string delimiters and concatenation artifacts
        cleaned = re.sub(r'"\s*\n\s*"', "", raw)
        cleaned = re.sub(r'f?"', "", cleaned)
        return cleaned[:4000]
    # Fallback: return surrounding lines if regex doesn't match
    lines = content.splitlines()
    start = next((i for i, l in enumerate(lines) if "system_instruction" in l), 0)
    return "\n".join(lines[start : start + 80])


def _fetch_scenario_expected_outcomes(scenario_id: int) -> str:
    try:
        data = cekura_get(f"/scenarios/{scenario_id}/")
        eo = data.get("expected_outcome_prompt") or data.get("expected_outcome", "")
        return str(eo)[:800] if eo else "(not available)"
    except Exception:
        return "(not available)"


def propose_patch(
    scenario_id: int,
    result: RunResult,
    iteration: int,
    step_callback: Callable[[str], None] | None = None,
) -> PatchProposal | None:
    system_excerpt = _read_system_instruction(BOT_FILE)
    expected_outcomes = _fetch_scenario_expected_outcomes(scenario_id)
    transcript_text = _build_transcript_text(result.transcript)
    failure_text = "\n".join(f"- {e}" for e in result.failure_explanations)

    prompt = _PATCH_PROMPT.format(
        system_instruction_excerpt=system_excerpt,
        scenario_name=result.scenario_name,
        expected_outcomes=expected_outcomes,
        failure_explanations=failure_text,
        transcript=transcript_text,
    )

    print("  🤖 Calling gpt-5.5 via token router to propose a fix…")
    if step_callback:
        step_callback(f"🤖 Asking LLM for a fix (iter {iteration})…")
    from openai import OpenAI

    client = OpenAI(base_url=_token_router_base_url(), api_key=_token_router_key())
    response = client.chat.completions.create(
        model="openai/gpt-5.5",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = (response.choices[0].message.content or "").strip()

    # Strip markdown fences if the model added them despite instructions
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        proposal = json.loads(raw)
    except json.JSONDecodeError:
        print(f"  ⚠  Model returned non-JSON: {raw[:200]}")
        return None

    find = proposal.get("find", "")
    replace = proposal.get("replace", "")
    rationale = proposal.get("rationale", "")

    if not find or not replace:
        print("  ⚠  Model proposal missing 'find' or 'replace' field.")
        return None

    bot_content = BOT_FILE.read_text(encoding="utf-8")
    if find not in bot_content:
        print(f"  ⚠  'find' text not found verbatim in {BOT_FILE.name}.")
        print(f"     find={find[:100]!r}")
        return None

    print(f"  💡 Rationale: {rationale}")
    if step_callback:
        step_callback(f"💡 {rationale}")
    return PatchProposal(find=find, replace=replace, rationale=rationale, iteration=iteration)


# ---------------------------------------------------------------------------
# Apply patch to bot file
# ---------------------------------------------------------------------------


def apply_patch(proposal: PatchProposal) -> bool:
    content = BOT_FILE.read_text(encoding="utf-8")
    if proposal.find not in content:
        print(f"  ⚠  Patch target no longer present in {BOT_FILE.name} (already applied?).")
        return False
    new_content = content.replace(proposal.find, proposal.replace, 1)
    BOT_FILE.write_text(new_content, encoding="utf-8")
    print(f"  ✓ Patched {BOT_FILE.name}")
    return True


def revert_patch(proposal: PatchProposal) -> None:
    content = BOT_FILE.read_text(encoding="utf-8")
    if proposal.replace in content:
        reverted = content.replace(proposal.replace, proposal.find, 1)
        BOT_FILE.write_text(reverted, encoding="utf-8")
        print(f"  ↩ Reverted patch in {BOT_FILE.name}")


# ---------------------------------------------------------------------------
# Deploy to Pipecat Cloud + wait for new deployment
# ---------------------------------------------------------------------------


def _get_active_deployment_id() -> str:
    result = run_cmd(["pc", "cloud", "agent", "status", PIPECAT_AGENT_NAME])
    match = re.search(r"Active Deployment ID:\s+(\S+)", result.stdout)
    return match.group(1) if match else ""


def deploy_and_wait(dry_run: bool = False) -> bool:
    if dry_run:
        print("  [DRY RUN] Would run: pc cloud deploy && wait for new deployment")
        return True

    print("  🚀 Deploying to Pipecat Cloud…")
    old_deployment = _get_active_deployment_id()

    try:
        proc = subprocess.Popen(
            ["pc", "cloud", "deploy"],
            cwd=SERVER,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for line in proc.stdout or []:
            print(f"    {line}", end="", flush=True)
        proc.wait()
        if proc.returncode != 0:
            print(f"  ⚠  Deploy failed (exit {proc.returncode})")
            return False
    except FileNotFoundError:
        print("  ⚠  'pc' CLI not found — cannot deploy automatically.")
        print("     Run 'pc cloud deploy' manually, then re-run with --no-deploy.")
        return False

    # Wait for a new deployment to go active
    print("  ⏳ Waiting for new deployment to become active…", end="", flush=True)
    deadline = time.time() + DEPLOY_TIMEOUT_S
    while time.time() < deadline:
        new_deployment = _get_active_deployment_id()
        if new_deployment and new_deployment != old_deployment:
            print(f" ready ({new_deployment[:8]})")
            # Give the new agents a few seconds to fully start
            time.sleep(10)
            return True
        print(".", end="", flush=True)
        time.sleep(DEPLOY_POLL_INTERVAL_S)

    print(" timeout")
    print("  ⚠  New deployment not detected within timeout — run may test old version.")
    return False


# ---------------------------------------------------------------------------
# Git: branch + commit + PR
# ---------------------------------------------------------------------------


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


def open_pr(
    proposals: list[PatchProposal],
    scenario_name: str,
    before_score: int | None,
    after_score: int,
    base_branch: str,
    auto_merge: bool = False,
) -> str | None:
    slug = slugify(f"self-heal-{scenario_name}")
    branch_name = f"self-heal/{slug}-{int(time.time())}"

    try:
        git("checkout", "-b", branch_name, base_branch)
    except subprocess.CalledProcessError as exc:
        print(f"  ⚠  Branch creation failed: {exc.stderr.strip()}")
        return None

    try:
        git("add", str(BOT_FILE))
        patch_summary = "\n".join(
            f"- Iteration {p.iteration}: {p.rationale}" for p in proposals
        )
        commit_msg = (
            f"fix(self-heal): improve {scenario_name}\n\n"
            f"Self-healing loop applied {len(proposals)} patch(es):\n"
            f"{patch_summary}\n\n"
            f"Score: {before_score}% → {after_score}%\n"
        )
        git("commit", "-m", commit_msg)
    except subprocess.CalledProcessError as exc:
        print(f"  ⚠  Commit failed: {exc.stderr.strip()}")
        git("checkout", base_branch, check=False)
        git("branch", "-D", branch_name, check=False)
        return None

    try:
        git("push", "-u", "origin", branch_name)
    except subprocess.CalledProcessError as exc:
        print(f"  ⚠  Push failed: {exc.stderr.strip()}")
        git("checkout", base_branch, check=False)
        return None

    patch_table = "\n".join(
        f"| {p.iteration} | {p.rationale} |" for p in proposals
    )
    pr_body = (
        f"## Self-heal: {scenario_name}\n\n"
        f"The self-healing loop improved this scenario from **{before_score}%** → **{after_score}%**.\n\n"
        f"### Patches applied\n\n"
        f"| Iteration | Rationale |\n"
        f"|---|---|\n"
        f"{patch_table}\n\n"
        f"### Diff summary\n"
        f"Only `system_instruction` text was modified in `{BOT_FILE.name}`.\n\n"
        f"### Review checklist\n"
        f"- [ ] Re-run Cekura against this branch before merging\n"
        f"- [ ] Pass rate does not regress on other scenarios\n"
        f"- [ ] Conversation flow reads naturally\n"
        f"- [ ] No safety rules were weakened\n\n"
        f"---\n"
        f"*Generated by `harness/self_heal.py` at {now_iso()}*\n"
        f"*Base branch: `{base_branch}`*"
    )

    try:
        result = run_cmd(
            ["gh", "pr", "create",
             "--title", f"[self-heal] {scenario_name}: {before_score}% → {after_score}%",
             "--body", pr_body,
             "--base", base_branch,
             "--head", branch_name]
        )
        pr_url = result.stdout.strip()
        print(f"  ✓ PR created: {pr_url}")
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
        return pr_url
    except FileNotFoundError:
        print("  ⚠  'gh' not found — branch pushed, open PR manually.")
        return None
    except subprocess.CalledProcessError as exc:
        print(f"  ⚠  PR creation failed: {(exc.stderr or '').strip()[:200]}")
        return None
    finally:
        git("checkout", base_branch, check=False)


# ---------------------------------------------------------------------------
# Main self-healing loop
# ---------------------------------------------------------------------------


def self_heal(args: argparse.Namespace, step_callback: Callable[[str], None] | None = None) -> HealResult:
    scenario_id: int = args.scenario
    max_iterations: int = args.max_iterations
    dry_run: bool = args.dry_run
    no_deploy: bool = args.no_deploy
    auto_merge: bool = getattr(args, "auto_merge", False)
    skip_clean_check: bool = getattr(args, "skip_clean_check", False)

    def step(msg: str) -> None:
        if step_callback:
            step_callback(msg)

    base_branch = current_branch()
    if base_branch == "HEAD":
        raise RuntimeError("Detached HEAD — check out a named branch first.")

    if has_uncommitted_changes() and not dry_run and not skip_clean_check:
        raise RuntimeError(
            "Uncommitted changes in working tree.\n"
            "Commit or stash them before running self_heal so each patch is isolated.\n"
            "Use --dry-run to preview without touching the tree."
        )

    print(f"\nBayview Self-Heal Loop")
    print(f"  Scenario ID : {scenario_id}")
    print(f"  Max iters   : {max_iterations}")
    print(f"  Base branch : {base_branch}")
    if dry_run:
        print("  Mode        : DRY RUN")
    if no_deploy:
        print("  Deploy      : SKIPPED (--no-deploy)")
    print()

    # ── Baseline run ────────────────────────────────────────────────────────
    print("── Step 1: Baseline run")
    step("▶ Running baseline scenario…")
    run_id = run_scenario(scenario_id)
    baseline = poll_run(run_id)
    print(f"  Scenario : {baseline.scenario_name}")
    print(f"  EO score : {baseline.eo_score}%  |  main turns: {baseline.main_agent_turns}")

    score_str = f"{baseline.eo_score}%" if baseline.eo_score is not None else "?"
    step(f"Baseline: {score_str} — {len(baseline.failure_explanations)} failure(s)")
    for exp in baseline.failure_explanations[:3]:
        step(f"  · {exp[:120]}")

    if baseline.passed:
        print("  ✅ Already passing — nothing to do.")
        step("✅ Already passing — nothing to do.")
        return HealResult(
            scenario_id=scenario_id,
            iterations=0,
            final_score=baseline.eo_score,
            passed=True,
            pr_url=None,
        )

    if baseline.main_agent_turns == 0:
        print(
            "  ⚠  Agent produced 0 turns — this is an infrastructure/cold-start issue, "
            "not a prompt issue. Check deployment and retry."
        )
        step("⚠ 0 agent turns — infra/cold-start issue, cannot heal")
        return HealResult(
            scenario_id=scenario_id,
            iterations=0,
            final_score=baseline.eo_score,
            passed=False,
            pr_url=None,
        )

    print(f"  Failures:")
    for line in baseline.failure_explanations:
        print(f"    {line}")

    patches_applied: list[PatchProposal] = []
    before_score = baseline.eo_score
    current_result = baseline

    # ── Iterative fix loop ───────────────────────────────────────────────────
    for iteration in range(1, max_iterations + 1):
        print(f"\n── Step {iteration + 1}: Fix iteration {iteration}/{max_iterations}")

        proposal = propose_patch(scenario_id, current_result, iteration, step_callback=step_callback)
        if proposal is None:
            print("  ⚠  Could not generate a valid patch — stopping.")
            step("⚠ LLM could not generate a valid patch — stopping.")
            break

        print(f"  find    : {proposal.find[:80]!r}")
        print(f"  replace : {proposal.replace[:80]!r}")

        if dry_run:
            print("  [DRY RUN] Would apply patch, deploy, and rerun.")
            patches_applied.append(proposal)
            continue

        if not apply_patch(proposal):
            step(f"⚠ Patch text not found in {BOT_FILE.name} — stopping.")
            break

        patches_applied.append(proposal)
        step(f"✓ Patch applied to {BOT_FILE.name}")

        # Deploy unless skipped
        if not no_deploy:
            step("🚀 Deploying to Pipecat Cloud…")
            deployed = deploy_and_wait(dry_run=False)
            if not deployed:
                print("  ⚠  Deploy failed — reverting patch and stopping.")
                step("⚠ Deploy failed — reverting patch.")
                revert_patch(proposal)
                patches_applied.pop()
                break
            step("✓ Deployment ready")
        else:
            print("  ⏭  Skipping deploy (--no-deploy)")

        # Re-run the scenario
        print(f"  ↻ Re-running scenario {scenario_id}…")
        step("↻ Re-running scenario…")
        run_id = run_scenario(scenario_id)
        current_result = poll_run(run_id)
        print(
            f"  EO score : {current_result.eo_score}%  |  main turns: {current_result.main_agent_turns}"
        )
        iter_score = f"{current_result.eo_score}%" if current_result.eo_score is not None else "?"

        if current_result.passed:
            print(f"  ✅ Passed on iteration {iteration}!")
            step(f"Score: {iter_score} ✅ passed!")
            break

        step(f"Score: {iter_score} — still failing")
        for exp in current_result.failure_explanations[:2]:
            step(f"  · {exp[:120]}")
        print(f"  Still failing:")
        for line in current_result.failure_explanations:
            print(f"    {line}")

    # ── Result ───────────────────────────────────────────────────────────────
    pr_url: str | None = None
    final_score = current_result.eo_score

    if current_result.passed and patches_applied and not dry_run:
        print(f"\n── Opening PR: {before_score}% → {final_score}%")
        step(f"📝 Opening PR ({before_score}% → {final_score}%)…")
        pr_url = open_pr(patches_applied, baseline.scenario_name, before_score, final_score, base_branch, auto_merge=auto_merge)
        if pr_url:
            step(f"✅ PR created: {pr_url}")
    elif not current_result.passed and patches_applied and not dry_run:
        print(
            f"\n  ⚠  Score did not reach 100% after {len(patches_applied)} iteration(s). "
            f"Final: {final_score}%."
        )
        step(f"❌ No improvement (final: {final_score}%) — reverting patches.")
        print("  Reverting patches — manual intervention needed.")
        for p in reversed(patches_applied):
            revert_patch(p)

    return HealResult(
        scenario_id=scenario_id,
        iterations=len(patches_applied),
        final_score=final_score,
        passed=current_result.passed,
        pr_url=pr_url,
        patches_applied=patches_applied,
    )


# ---------------------------------------------------------------------------
# Callable entry point (no argparse — for use by webhook_server, etc.)
# ---------------------------------------------------------------------------


def run_heal(
    scenario_id: int,
    max_iterations: int = 3,
    dry_run: bool = False,
    no_deploy: bool = False,
    auto_merge: bool = False,
    step_callback: Callable[[str], None] | None = None,
) -> HealResult:
    """Callable entry point for webhook_server — no argparse required."""
    ns = argparse.Namespace(
        scenario=scenario_id,
        max_iterations=max_iterations,
        dry_run=dry_run,
        no_deploy=no_deploy,
        auto_merge=auto_merge,
        skip_clean_check=True,  # webhook server runs continuously, unclean tree is normal
    )
    return self_heal(ns, step_callback=step_callback)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bayview self-healing loop: run → GPT-5.5 patches prompt → deploy → rerun → PR.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--scenario", type=int, required=True, help="Cekura scenario ID to fix.")
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=3,
        metavar="N",
        help="Max fix-deploy-rerun cycles (default: 3).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show proposed patches without writing files, deploying, or creating branches.",
    )
    parser.add_argument(
        "--no-deploy",
        action="store_true",
        help="Skip 'pc cloud deploy' (useful if you deploy manually or test locally).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        result = self_heal(args)
    except RuntimeError as exc:
        sys.exit(str(exc))

    print(f"\n{'═' * 60}")
    print("  SELF-HEAL SUMMARY")
    print(f"{'═' * 60}")
    print(f"  Scenario  : {result.scenario_id}")
    print(f"  Iterations: {result.iterations}")
    print(f"  Final EO  : {result.final_score}%")
    print(f"  Passed    : {'✅ yes' if result.passed else '❌ no'}")
    print(f"  PR        : {result.pr_url or '—'}")
    if result.patches_applied:
        print(f"  Patches   :")
        for p in result.patches_applied:
            print(f"    [{p.iteration}] {p.rationale}")


if __name__ == "__main__":
    main()
