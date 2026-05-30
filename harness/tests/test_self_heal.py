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
