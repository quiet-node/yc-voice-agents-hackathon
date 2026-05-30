"""Tests for self_heal.py — env helpers and run_heal()."""
import sys
from pathlib import Path

import pytest

# Make harness importable
sys.path.insert(0, str(Path(__file__).parents[1]))

import self_heal


def test_cekura_key_raises_runtime_error_not_system_exit(monkeypatch):
    """_cekura_key() must raise RuntimeError, not SystemExit, when key is missing."""
    monkeypatch.delenv("CEKURA_API_KEY", raising=False)
    # Point to a non-existent .env so file fallback also fails
    monkeypatch.setattr(self_heal, "SERVER", Path("/nonexistent"))
    with pytest.raises(RuntimeError, match="CEKURA_API_KEY"):
        self_heal._cekura_key()


def test_token_router_key_raises_runtime_error(monkeypatch):
    monkeypatch.delenv("TOKEN_ROUTER_API_KEY", raising=False)
    monkeypatch.setattr(self_heal, "SERVER", Path("/nonexistent"))
    with pytest.raises(RuntimeError, match="TOKEN_ROUTER_API_KEY"):
        self_heal._token_router_key()


def test_token_router_base_url_raises_runtime_error(monkeypatch):
    monkeypatch.delenv("TOKEN_ROUTER_BASE_URL", raising=False)
    monkeypatch.setattr(self_heal, "SERVER", Path("/nonexistent"))
    with pytest.raises(RuntimeError, match="TOKEN_ROUTER_BASE_URL"):
        self_heal._token_router_base_url()


def test_self_heal_raises_on_detached_head(monkeypatch):
    """self_heal() must raise RuntimeError (not SystemExit) on detached HEAD."""
    monkeypatch.setattr(self_heal, "current_branch", lambda: "HEAD")
    import argparse
    args = argparse.Namespace(scenario=1, max_iterations=1, dry_run=False, no_deploy=True)
    with pytest.raises(RuntimeError, match="Detached HEAD"):
        self_heal.self_heal(args)


def test_self_heal_raises_on_dirty_tree(monkeypatch):
    monkeypatch.setattr(self_heal, "current_branch", lambda: "main")
    monkeypatch.setattr(self_heal, "has_uncommitted_changes", lambda: True)
    import argparse
    args = argparse.Namespace(scenario=1, max_iterations=1, dry_run=False, no_deploy=True)
    with pytest.raises(RuntimeError, match="Uncommitted changes"):
        self_heal.self_heal(args)


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
