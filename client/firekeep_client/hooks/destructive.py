"""Deterministic, offline pre-flight guard for destructive shell commands.

Design: docs/superpowers/specs/2026-08-02-uncommitted-work-preservation-design.md

Deliberately NOT routed through the agent gateway. Sending every Bash call to
`POST /agent/action/before` would put a 5-second network timeout on the hottest tool, and
that gate fails open — so the single command that matters would sail through whenever
Cortex is slow or down. A guard that fails open is not a guard. This is pure-local
pattern matching plus one `git status`: no network, no LLM, no server opinion.

Posture is snapshot-then-ALLOW. Destructive git commands are frequently exactly what the
user wants; blocking them would fire constantly on intentional cleanups, and an agent
that cannot revert its own botched edit will thrash — which is how the 2026-08-02
incident began. The value on offer is recoverability, not prohibition.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from firekeep_client import worktree_snapshot

# Each entry is a command that can destroy uncommitted work. The false-POSITIVE cost is
# what shapes these: a guard that fires on `git checkout -b` gets switched off within a
# day, and then protects nothing. So they match the destructive spellings only.
_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bgit\s+checkout\s+(?:.*\s)?--(?:\s|$)", "git checkout --"),
    (r"\bgit\s+checkout\s+\.(?:\s|$)",          "git checkout ."),
    # The MODERN spelling of the same destruction. Matching only `git checkout --`
    # would leave the identical hole one synonym away.
    (r"\bgit\s+restore\b",                      "git restore"),
    (r"\bgit\s+reset\s+--hard\b",               "git reset --hard"),
    (r"\bgit\s+clean\s+-\w*f",                  "git clean -f"),
    (r"\bgit\s+stash\s+(?:drop|clear)\b",       "git stash drop/clear"),
    # Two of [rf] required: plain `rm -f one_file` is not the blast radius this exists
    # for, and matching it would add noise without adding protection.
    (r"\brm\s+-\w*[rf]\w*[rf]\w*",              "rm -rf"),
)
_COMPILED = tuple((re.compile(p), label) for p, label in _PATTERNS)


def _git(cwd: str | None, *args: str) -> subprocess.CompletedProcess:
    # Explicit utf-8: `text=True` uses the locale codec, and a decode error kills
    # subprocess's reader thread, silently emptying stdout. Here that would make a dirty
    # tree look clean and skip the snapshot entirely. See worktree_snapshot._git.
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          encoding="utf-8", errors="replace", check=False)


def _repo_root(cwd: str | None) -> Path | None:
    """Delegates to worktree_snapshot.repo_root — one implementation, not two."""
    return worktree_snapshot.repo_root(cwd)


def matches(command: str) -> str | None:
    """The destructive form this command takes, or None. Pure — no side effects."""
    if not command:
        return None
    for pattern, label in _COMPILED:
        if pattern.search(command):
            return label
    return None


def guard(command: str, cwd: str | None = None) -> str | None:
    """Snapshot uncommitted work if `command` would destroy it. Never raises, never blocks.

    Returns a one-line notice when a snapshot was taken, else None. Two conditions must
    both hold: the command is destructive AND the tree actually has uncommitted changes.
    Requiring real dirtiness is what keeps this quiet — on a clean tree these commands
    destroy nothing, so the guard says nothing and does not become noise.
    """
    try:
        label = matches(command)
        if not label:
            return None
        root = _repo_root(cwd)
        if root is None:
            return None
        dirty = (_git(str(root), "status", "--porcelain").stdout or "").strip()
        if not dirty:
            return None
        snap = worktree_snapshot.capture(root, reason=f"before {label}: {command[:160]}")
        if snap is None:
            return None
        return (f"firekeep: snapshotted uncommitted work before `{label}` — "
                f"restore with `firekeep restore --apply {snap.name}`")
    except Exception:  # noqa: BLE001 — a guard failure must never stop the command
        return None
