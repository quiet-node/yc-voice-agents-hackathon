#!/usr/bin/env python3
"""Bayview Pharmacy auto-fix loop.

Reads fix_queue from a harness report.json, applies targeted code/prompt patches
to server/bot-nemotron.py and server/bot-gpt.py, commits each fix on a dedicated
branch, and opens a GitHub PR for human review.

Nothing is merged to main automatically — every fix requires explicit PR approval.

Usage:
    python3 harness/auto_fix.py
    python3 harness/auto_fix.py --report harness/runs/latest-cekura/report.json
    python3 harness/auto_fix.py --dry-run
    python3 harness/auto_fix.py --verify       # Cekura re-run after each fix (costs credits)
    python3 harness/auto_fix.py --max-fixes 1
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
BOT_FILES = [SERVER / "bot-nemotron.py", SERVER / "bot-gpt.py"]

DEFAULT_REPORT = ROOT / "harness" / "runs" / "latest-cekura" / "report.json"
BRANCH_PREFIX = "auto-fix"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class FixResult:
    applied: bool
    description: str
    files_changed: list[Path] = field(default_factory=list)
    skip_reason: str = ""


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug[:50] or "fix"


def run_cmd(
    args: list[str],
    *,
    check: bool = True,
    capture: bool = True,
    cwd: Path = ROOT,
) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=check, capture_output=capture, text=True, cwd=cwd)


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return run_cmd(["git", *args], check=check)


def current_branch() -> str:
    return git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def has_uncommitted_changes() -> bool:
    return bool(git("status", "--porcelain").stdout.strip())


def create_branch(name: str, base: str) -> bool:
    try:
        git("checkout", "-b", name, base)
        return True
    except subprocess.CalledProcessError as exc:
        print(f"  ⚠  Could not create branch '{name}': {exc.stderr.strip()}")
        return False


def checkout_branch(name: str) -> None:
    git("checkout", name)


def commit_files(files: list[Path], message: str) -> bool:
    try:
        git("add", *[str(f) for f in files])
        git("commit", "-m", message)
        return True
    except subprocess.CalledProcessError as exc:
        print(f"  ⚠  Commit failed: {exc.stderr.strip()}")
        return False


def push_branch(branch: str) -> bool:
    try:
        git("push", "-u", "origin", branch)
        return True
    except subprocess.CalledProcessError as exc:
        print(f"  ⚠  Push failed: {exc.stderr.strip()}")
        return False


def create_github_pr(title: str, body: str, branch: str, base: str) -> str | None:
    try:
        result = run_cmd(
            ["gh", "pr", "create", "--title", title, "--body", body, "--base", base, "--head", branch]
        )
        return result.stdout.strip()
    except FileNotFoundError:
        print("  ⚠  'gh' CLI not found — install it from https://cli.github.com to auto-create PRs.")
        return None
    except subprocess.CalledProcessError as exc:
        print(f"  ⚠  PR creation failed: {(exc.stderr or '').strip()[:200]}")
        return None


def revert_files(files: list[Path]) -> None:
    for f in files:
        git("checkout", "--", str(f), check=False)


# ---------------------------------------------------------------------------
# Line-based injection helper
# ---------------------------------------------------------------------------


def inject_before_line(
    content: str,
    *,
    after_marker: str,
    before_line_containing: str,
    code_lines: list[str],
    idempotency_marker: str,
) -> tuple[str, bool]:
    """Insert code_lines before the first line containing before_line_containing
    that appears after a line containing after_marker.

    Skips if idempotency_marker is already present in content.
    Returns (new_content, was_modified).
    """
    if idempotency_marker in content:
        return content, False

    lines = content.splitlines(keepends=True)
    saw_marker = False
    injected = False
    result: list[str] = []

    for line in lines:
        if not saw_marker and after_marker in line:
            saw_marker = True

        if saw_marker and not injected and before_line_containing in line:
            indent = " " * (len(line) - len(line.lstrip()))
            for code_line in code_lines:
                result.append(f"{indent}{code_line}\n" if code_line.strip() else "\n")
            injected = True

        result.append(line)

    return "".join(result), injected


# ---------------------------------------------------------------------------
# Fixer: code guardrail
# ---------------------------------------------------------------------------

_GET_RX_GUARD = [
    'if not call_state["verified"]:',
    '    await params.result_callback(',
    '        {"error": "not_verified",',
    '         "note": "Identity must be verified before accessing prescription information."}',
    "    )",
    "    return",
]

_REFILL_RX_GUARD = [
    'if not call_state["verified"]:',
    "    await params.result_callback(",
    '        {"ok": False, "reason": "Identity must be verified before refilling prescriptions."}',
    "    )",
    "    return",
]


def apply_code_guardrail(fix_item: dict[str, Any]) -> FixResult:
    """Add call_state["verified"] guards to get_prescriptions and refill_prescription."""
    changed: list[Path] = []

    for bot_file in BOT_FILES:
        if not bot_file.exists():
            continue
        content = bot_file.read_text(encoding="utf-8")
        new_content = content

        new_content, mod1 = inject_before_line(
            new_content,
            after_marker="async def get_prescriptions(",
            before_line_containing="patient = find_patient_by_name",
            code_lines=_GET_RX_GUARD,
            idempotency_marker='"not_verified"',
        )

        new_content, mod2 = inject_before_line(
            new_content,
            after_marker="async def refill_prescription(",
            before_line_containing="patient = find_patient_by_name",
            code_lines=_REFILL_RX_GUARD,
            idempotency_marker='"must be verified before refilling"',
        )

        if mod1 or mod2:
            bot_file.write_text(new_content, encoding="utf-8")
            changed.append(bot_file)

    if not changed:
        return FixResult(
            applied=False,
            description="Guardrails already present or injection points not found in any bot file.",
        )
    names = ", ".join(f.name for f in changed)
    return FixResult(
        applied=True,
        description=f"Added identity verification guardrails to get_prescriptions and refill_prescription in: {names}",
        files_changed=changed,
    )


# ---------------------------------------------------------------------------
# Fixer: prompt patch (keepalive / responsiveness)
# ---------------------------------------------------------------------------

_KEEPALIVE_RULE_LINES = [
    '"If more than 3 seconds have passed since the caller last spoke and you haven\'t responded yet, "',
    '"say \'Just a moment\' once — but only once per wait. Prioritize responding immediately "',
    '"to the caller\'s first message.\\n\\n"',
]


def apply_prompt_patch(fix_item: dict[str, Any]) -> FixResult:
    """Append a first-response / keepalive rule to the system_instruction."""
    changed: list[Path] = []

    for bot_file in BOT_FILES:
        if not bot_file.exists():
            continue
        content = bot_file.read_text(encoding="utf-8")

        # Inject just before the "Today is ..." closing line of system_instruction.
        # Both bot files end system_instruction with this f-string.
        # Falls back to injecting before the last string segment if landmark absent.
        landmark = "f\"Today is {date.today().strftime"
        if landmark not in content:
            # Fallback: inject before the last line of system_instruction block
            landmark = "end_call without saying goodbye"
            if landmark not in content:
                continue

        new_content, injected = inject_before_line(
            content,
            after_marker="system_instruction = (",
            before_line_containing=landmark,
            code_lines=_KEEPALIVE_RULE_LINES,
            idempotency_marker="Just a moment",
        )

        if injected:
            bot_file.write_text(new_content, encoding="utf-8")
            changed.append(bot_file)

    if not changed:
        return FixResult(
            applied=False,
            description="Keepalive rule already present or landmark not found.",
        )
    names = ", ".join(f.name for f in changed)
    return FixResult(
        applied=True,
        description=f"Appended keepalive / first-response rule to system_instruction in: {names}",
        files_changed=changed,
    )


# ---------------------------------------------------------------------------
# Fixer: skip with human-actionable message
# ---------------------------------------------------------------------------

_INFRA_CHECKLIST = [
    "1. Check Pipecat Cloud capacity  →  pc cloud status",
    "2. Confirm NVIDIA STT WebSocket is up  →  curl -I $NVIDIA_ASR_URL",
    "3. Measure cold-start: time from 'Starting bot' log to first LLMRunFrame",
    "4. In Twilio console → Monitor → Logs: look for error codes 31920 / 31921",
    "5. Add a TTS pre-warm: queue a silent frame at on_client_connected before LLMRunFrame",
    "6. Verify ENV=local in server/.env for local runs (prevents Krisp import crash)",
]

_CATEGORY_NOTES: dict[str, list[str]] = {
    "Infra Issue": _INFRA_CHECKLIST,
    "Tool/Backend Issue": [
        "1. Check mock_backend.py has data matching the failing scenario's patient/drug",
        "2. Review the Cekura scenario's expected_outcome — the evaluator expectation may be wrong",
        "3. Add the missing patient/drug combo to PATIENTS in mock_backend.py if it should exist",
    ],
    "Metric or Prompt Ambiguity": [
        "1. Open the Cekura dashboard and inspect the failing metric's explanation text",
        "2. If the metric is ambiguous, update the metric definition in Cekura",
        "3. If the prompt is under-specified, add a narrow clarification to system_instruction",
    ],
}


def skip_with_warning(fix_item: dict[str, Any]) -> FixResult:
    category = fix_item.get("category", "")
    change_type = fix_item.get("change_type", "")
    title = fix_item.get("title", "")
    action = fix_item.get("action", "")
    target = fix_item.get("target", "")

    width = 70
    border = "─" * (width - 4)
    print()
    print(f"  ┌─ ACTION REQUIRED (cannot auto-fix) {border[:width - 38]}")
    print(f"  │  Issue  : {title}")
    print(f"  │  Type   : {category} / {change_type}")
    print(f"  │  Target : {target}")
    print(f"  │  Action : {action}")

    notes = _CATEGORY_NOTES.get(category)
    if notes:
        print("  │")
        print(f"  │  {category.upper()} CHECKLIST:")
        for note in notes:
            print(f"  │    {note}")

    print(f"  └{'─' * (width - 3)}")

    return FixResult(
        applied=False,
        description=f"Skipped — manual action required: {title}",
        skip_reason=f"{category} requires human intervention",
    )


# ---------------------------------------------------------------------------
# Fixer registry
# ---------------------------------------------------------------------------

FIXER_REGISTRY: dict[str, Callable[[dict[str, Any]], FixResult]] = {
    "code guardrail": apply_code_guardrail,
    "prompt + latency handling": apply_prompt_patch,
    # These need LLM-assisted generation or manual review — skip with guidance
    "prompt": skip_with_warning,
    "prompt + orchestration": skip_with_warning,
    "deployment/config": skip_with_warning,
    "mock data or evaluator": skip_with_warning,
    "manual review": skip_with_warning,
}


# ---------------------------------------------------------------------------
# Cekura verification (--verify mode)
# ---------------------------------------------------------------------------


def run_cekura_verification(
    agent_id: int,
    base_pass_rate: float,
    out_dir: Path,
) -> tuple[bool, float]:
    """Re-run generate_dashboard against the latest Cekura result.

    Returns (improved_or_same, new_pass_rate).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        run_cmd(
            [
                sys.executable,
                str(ROOT / "harness" / "generate_dashboard.py"),
                "--cekura-result-id", "latest",
                "--cekura-agent-id", str(agent_id),
                "--out", str(out_dir),
            ]
        )
        new_report = out_dir / "report.json"
        if not new_report.exists():
            print("  ⚠  Verification: report.json not produced.")
            return False, base_pass_rate
        model = json.loads(new_report.read_text(encoding="utf-8"))
        new_rate: float = model.get("summary", {}).get("pass_rate", 0.0)
        return new_rate >= base_pass_rate, new_rate
    except subprocess.CalledProcessError as exc:
        print(f"  ⚠  Cekura verification failed: {(exc.stderr or '').strip()[:200]}")
        return False, base_pass_rate


def post_pr_comment(pr_url: str, body: str) -> None:
    try:
        run_cmd(["gh", "pr", "comment", pr_url, "--body", body])
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass  # Non-critical — the comment is cosmetic


# ---------------------------------------------------------------------------
# Per-fix orchestration
# ---------------------------------------------------------------------------


def process_fix(
    fix_item: dict[str, Any],
    args: argparse.Namespace,
    base_branch: str,
    base_pass_rate: float,
) -> dict[str, Any]:
    priority = fix_item.get("priority", "?")
    title = fix_item.get("title", "unknown")
    change_type = fix_item.get("change_type", "manual review")
    category = fix_item.get("category", "")
    severity = fix_item.get("severity", "medium")
    action = fix_item.get("action", "")
    target = fix_item.get("target", "")
    affected = fix_item.get("affected_runs", 0)
    regression_risk = fix_item.get("regression_risk", "")

    print(f"\n{'─' * 72}")
    print(f"  P{priority} [{severity.upper()}]  {title}")
    print(f"  change_type={change_type}  affected_runs={affected}")

    fixer = FIXER_REGISTRY.get(change_type, skip_with_warning)

    if args.dry_run:
        print(f"  [DRY RUN] Would call: {fixer.__name__}")
        return {"priority": priority, "title": title, "status": "dry_run", "pr": None}

    fix_result = fixer(fix_item)

    if not fix_result.applied:
        reason = fix_result.skip_reason or fix_result.description
        print(f"  → Skipped: {reason}")
        return {"priority": priority, "title": title, "status": "skipped", "pr": None}

    print(f"  ✓ Applied: {fix_result.description}")

    # Branch name: auto-fix/p1-code-guardrail-privacy-risk
    branch_slug = slugify(f"p{priority}-{change_type}-{category}")
    branch_name = f"{BRANCH_PREFIX}/{branch_slug}"

    if not create_branch(branch_name, base_branch):
        revert_files(fix_result.files_changed)
        return {"priority": priority, "title": title, "status": "error", "pr": None}

    commit_msg = (
        f"fix: {title}\n\n"
        f"Auto-fix by harness/auto_fix.py\n"
        f"Category: {category}\n"
        f"Change type: {change_type}\n"
        f"Target: {target}\n"
        f"Action: {action}\n"
        f"Severity: {severity} | Affected runs: {affected}\n"
    )

    if not commit_files(fix_result.files_changed, commit_msg):
        checkout_branch(base_branch)
        git("branch", "-D", branch_name, check=False)
        return {"priority": priority, "title": title, "status": "error", "pr": None}

    pr_url: str | None = None
    pushed = push_branch(branch_name)

    if pushed:
        pr_body = (
            f"## Auto-fix: {title}\n\n"
            f"| Field | Value |\n"
            f"|---|---|\n"
            f"| **Severity** | `{severity}` |\n"
            f"| **Category** | `{category}` |\n"
            f"| **Change type** | `{change_type}` |\n"
            f"| **Affected runs** | {affected} |\n"
            f"| **Confidence** | `{fix_item.get('confidence', '?')}` |\n"
            f"| **Regression risk** | {regression_risk} |\n\n"
            f"### What was changed\n"
            f"{fix_result.description}\n\n"
            f"### Action taken\n"
            f"{action}\n\n"
            f"### Target\n"
            f"`{target}`\n\n"
            f"### Review checklist\n"
            f"- [ ] Re-run the Cekura suite against this branch before merging\n"
            f"- [ ] Pass rate improves or stays the same vs. baseline\n"
            f"- [ ] Conversation flow is still natural on happy-path scenarios\n"
            f"- [ ] No new privacy risk introduced\n\n"
            f"---\n"
            f"*Generated by `harness/auto_fix.py` at {now_iso()}*\n"
            f"*Base branch: `{base_branch}` | Base pass rate: {base_pass_rate}%*"
        )
        pr_url = create_github_pr(
            title=f"[auto-fix] P{priority}: {title}",
            body=pr_body,
            branch=branch_name,
            base=base_branch,
        )
        if pr_url:
            print(f"  ✓ PR created: {pr_url}")
        else:
            print(f"  ⚠  PR creation failed. Branch '{branch_name}' is pushed — open PR manually.")
    else:
        print(f"  ⚠  Push failed. Fix is committed locally on '{branch_name}'.")

    # Optional Cekura verification
    verify_status = "not_run"
    if args.verify:
        print("  ↻ Running Cekura verification (this will take a few minutes)…")
        verify_out = ROOT / "harness" / "runs" / f"post-fix-p{priority}"
        improved, new_rate = run_cekura_verification(args.cekura_agent_id, base_pass_rate, verify_out)
        if improved:
            verify_status = f"✓ improved {base_pass_rate}% → {new_rate}%"
            print(f"  {verify_status}")
        else:
            verify_status = f"✗ regression {base_pass_rate}% → {new_rate}%"
            print(f"  {verify_status} — review carefully before merging")
            if pr_url:
                post_pr_comment(
                    pr_url,
                    f"⚠️ **Cekura verification showed regression**: "
                    f"{base_pass_rate}% → {new_rate}%. "
                    f"Do not merge until the regression is investigated.",
                )

    checkout_branch(base_branch)
    print(f"  ← Back on '{base_branch}'")

    return {
        "priority": priority,
        "title": title,
        "status": "pr_created" if pr_url else ("pushed" if pushed else "committed"),
        "pr": pr_url,
        "verify": verify_status,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bayview auto-fix loop: apply fix queue → branch → PR.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT,
        help="Path to report.json from the harness dashboard.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without writing files or creating branches.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help=(
            "Re-run Cekura after each fix and flag regressions on the PR. "
            "Off by default — each verification run costs voice call credits."
        ),
    )
    parser.add_argument(
        "--max-fixes",
        type=int,
        default=None,
        metavar="N",
        help="Stop after processing N fixes (default: process all).",
    )
    parser.add_argument(
        "--cekura-agent-id",
        type=int,
        default=18021,
        help="Agent ID passed to generate_dashboard.py when --verify is set.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    report_path = args.report.resolve()
    if not report_path.exists():
        sys.exit(
            f"Report not found: {report_path}\n"
            "Run generate_dashboard.py first, e.g.:\n"
            "  python3 harness/generate_dashboard.py --cekura-result-id latest "
            "--out harness/runs/latest-cekura"
        )

    model = json.loads(report_path.read_text(encoding="utf-8"))
    fix_queue: list[dict[str, Any]] = model.get("fix_queue", [])
    base_pass_rate: float = model.get("summary", {}).get("pass_rate", 0.0)
    total_runs: int = model.get("summary", {}).get("total_runs", 0)

    if not fix_queue:
        print("Nothing to fix — fix_queue is empty. The agent may already be passing all scenarios.")
        return

    if has_uncommitted_changes() and not args.dry_run:
        sys.exit(
            "Uncommitted changes detected in the working tree.\n"
            "Please commit or stash them before running auto_fix to keep each\n"
            "fix on a clean, isolated branch.\n\n"
            "Run with --dry-run to preview without touching the working tree."
        )

    base_branch = current_branch()
    if base_branch == "HEAD":
        sys.exit("Detached HEAD state — check out a named branch before running auto_fix.")

    queue = fix_queue[: args.max_fixes] if args.max_fixes else fix_queue

    print("\nBayview Auto-Fix Loop")
    print(f"  Report     : {report_path.relative_to(ROOT)}")
    print(f"  Base branch: {base_branch}")
    print(f"  Pass rate  : {base_pass_rate}% ({total_runs} runs)")
    print(f"  Fix queue  : {len(queue)} of {len(fix_queue)} item(s)")
    if args.dry_run:
        print("  Mode       : DRY RUN — no files or branches will be changed")
    if args.verify:
        print("  Verify     : ON — Cekura will re-run after each applied fix (costs credits)")

    results = []
    for fix_item in queue:
        result = process_fix(fix_item, args, base_branch, base_pass_rate)
        results.append(result)

    # Summary table
    print(f"\n{'═' * 72}")
    print("  RESULTS")
    print(f"{'═' * 72}")
    col = "{:>3}  {:<20}  {:<12}  {}"
    print(col.format("P", "Status", "PR", "Title"))
    print(col.format("─" * 3, "─" * 20, "─" * 12, "─" * 36))
    for r in results:
        pr_label = "yes" if r.get("pr") else "—"
        verify_label = r.get("verify", "")
        title_str = r["title"][:48]
        if verify_label and verify_label != "not_run":
            title_str += f"  [{verify_label}]"
        print(col.format(str(r["priority"]), r["status"], pr_label, title_str))

    n_applied = sum(1 for r in results if r["status"] not in ("skipped", "dry_run", "error"))
    n_skipped = sum(1 for r in results if r["status"] == "skipped")
    n_prs = sum(1 for r in results if r.get("pr"))

    print()
    print(f"  Applied: {n_applied}  |  Skipped (manual): {n_skipped}  |  PRs created: {n_prs}")

    if n_applied:
        print()
        print("  Next steps:")
        print("  1. Review each PR on GitHub — check the diff and the review checklist.")
        print("  2. Run the Cekura suite against the PR branch before merging.")
        print("  3. Merge only if pass rate improves or is unchanged.")
        print(f"  4. After merging, regenerate the dashboard to track progress.")


if __name__ == "__main__":
    main()
