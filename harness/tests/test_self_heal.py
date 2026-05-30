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
