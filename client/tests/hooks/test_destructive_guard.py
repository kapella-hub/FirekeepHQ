"""Pre-flight guard for destructive shell commands.

Design: docs/superpowers/specs/2026-08-02-uncommitted-work-preservation-design.md

Regression guard for 2026-08-02: `git checkout -- cortex/app/` destroyed nine files of
uncommitted work. `pre_tool` already mapped Bash -> "run_command"; only the adapter's
`^(Edit|Write)$` matcher kept that branch unreachable, so the blocking gate never saw the
command while PostToolUse watched it execute.

Real git repos, not mocks: the guard's entire claim is about what git does to a working
tree, and a mocked git cannot falsify it.
"""
from __future__ import annotations

import subprocess

import pytest


def _git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=str(repo), capture_output=True, text=True, check=False,
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "proj"
    r.mkdir()
    _git(r, "init", "-q")
    (r / "a.py").write_text("committed\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    monkeypatch.setenv("FIREKEEP_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    return r


def _dirty(repo):
    (repo / "a.py").write_text("uncommitted work\n", encoding="utf-8")


class TestDestructiveGuard:
    def test_snapshots_before_git_checkout_on_a_dirty_tree(self, repo):
        """The exact command from the incident."""
        from firekeep_client import worktree_snapshot as ws
        from firekeep_client.hooks import destructive

        _dirty(repo)
        note = destructive.guard("git checkout -- cortex/app/", cwd=str(repo))
        assert note, "a destructive command on a dirty tree must announce a snapshot"
        assert len(ws.list_snapshots(repo)) == 1

    def test_silent_on_a_clean_tree(self, repo):
        """Requiring real dirtiness is what stops this becoming noise that gets
        disabled — on a clean tree these commands destroy nothing."""
        from firekeep_client import worktree_snapshot as ws
        from firekeep_client.hooks import destructive

        assert destructive.guard("git checkout -- .", cwd=str(repo)) is None
        assert ws.list_snapshots(repo) == []

    def test_git_restore_is_matched_too(self, repo):
        """`git restore` is the MODERN spelling of the same destruction. Matching only
        `git checkout --` would leave the identical hole one synonym away."""
        from firekeep_client.hooks import destructive

        _dirty(repo)
        assert destructive.guard("git restore src/", cwd=str(repo))

    @pytest.mark.parametrize("cmd", [
        "git reset --hard HEAD~1",
        "git clean -fd",
        "rm -rf build/",
        "git stash clear",
    ])
    def test_other_destructive_forms_are_matched(self, repo, cmd):
        from firekeep_client.hooks import destructive
        _dirty(repo)
        assert destructive.guard(cmd, cwd=str(repo)), f"not matched: {cmd}"

    @pytest.mark.parametrize("cmd", [
        "git status", "git checkout -b feature", "git diff", "ls -la", "pytest tests/",
    ])
    def test_harmless_commands_are_ignored(self, repo, cmd):
        """`git checkout -b` creates a branch and destroys nothing — a guard that fired
        on it would be turned off within a day."""
        from firekeep_client.hooks import destructive
        _dirty(repo)
        assert destructive.guard(cmd, cwd=str(repo)) is None, f"false positive: {cmd}"

    def test_never_raises_outside_a_repo(self, tmp_path, monkeypatch):
        from firekeep_client.hooks import destructive
        monkeypatch.setenv("FIREKEEP_SNAPSHOT_DIR", str(tmp_path / "snaps"))
        plain = tmp_path / "plain"
        plain.mkdir()
        assert destructive.guard("rm -rf .", cwd=str(plain)) is None


class TestPreToolWiring:
    def test_bash_never_blocks_even_when_destructive(self, repo, client_env, monkeypatch):
        """Approved posture is snapshot-then-ALLOW. Blocking would fire on intentional
        cleanups, and an agent unable to revert its own bad edit will thrash — which is
        how the incident started."""
        from firekeep_client.hooks import pre_tool

        _dirty(repo)
        monkeypatch.chdir(repo)
        rc = pre_tool.run({"tool_name": "Bash",
                           "tool_input": {"command": "git checkout -- ."}})
        assert rc == 0

    def test_bash_guard_runs_without_any_network(self, repo, client_env, monkeypatch):
        """No agent-gateway round trip: a 5s timeout that fails open would wave through
        the one command that matters whenever Cortex is slow or down."""
        from firekeep_client import transport
        from firekeep_client.hooks import pre_tool

        def no_network(*a, **k):
            raise AssertionError("the destructive guard must not touch the network")

        monkeypatch.setattr(transport, "post_json", no_network)
        _dirty(repo)
        monkeypatch.chdir(repo)
        assert pre_tool.run({"tool_name": "Bash",
                             "tool_input": {"command": "git checkout -- ."}}) == 0
