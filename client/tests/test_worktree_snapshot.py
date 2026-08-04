"""Local uncommitted-work snapshots — see
docs/superpowers/specs/2026-08-02-uncommitted-work-preservation-design.md

These tests drive a REAL git repository in tmp_path rather than mocking subprocess. The
whole feature is a claim about what git does to a working tree, and a mocked git cannot
falsify it: the round-trip test below is the only thing that proves recovery works, and
it only means something against real `git checkout`.
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
    """A real git repo with one commit, plus an isolated snapshot store."""
    r = tmp_path / "proj"
    r.mkdir()
    _git(r, "init", "-q")
    (r / "kept.py").write_text("original\n", encoding="utf-8")
    (r / "sub").mkdir()
    (r / "sub" / "mod.py").write_text("mod original\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    monkeypatch.setenv("FIREKEEP_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    return r


def test_round_trip_restores_a_destroyed_working_tree(repo):
    """THE load-bearing test, and a direct regression guard for the 2026-08-02 incident:
    `git checkout -- <dir>` reverted nine files of uncommitted work with no way back.

    Dirty the tree, snapshot, destroy exactly the way it was destroyed, restore, and
    assert the tree is byte-identical. Anything weaker asserts that files were written,
    not that the work came back."""
    from firekeep_client import worktree_snapshot as ws

    (repo / "kept.py").write_text("MY UNCOMMITTED WORK\n", encoding="utf-8")
    (repo / "sub" / "mod.py").write_text("also mine\n", encoding="utf-8")
    _git(repo, "add", "sub/mod.py")          # partially staged, as a real tree often is
    before = {
        "kept.py": (repo / "kept.py").read_text(encoding="utf-8"),
        "sub/mod.py": (repo / "sub" / "mod.py").read_text(encoding="utf-8"),
    }

    snap = ws.capture(repo, reason="test")
    assert snap is not None

    _git(repo, "checkout", "--", ".")        # the exact destructive command
    assert (repo / "kept.py").read_text(encoding="utf-8") == "original\n"

    ws.apply_snapshot(snap, repo)

    assert (repo / "kept.py").read_text(encoding="utf-8") == before["kept.py"]
    assert (repo / "sub" / "mod.py").read_text(encoding="utf-8") == before["sub/mod.py"]


def test_untracked_files_survive_git_clean(repo):
    """`git diff` contains nothing about untracked files, so a patch alone cannot restore
    what `git clean -fd` deletes. They are copied, not diffed."""
    from firekeep_client import worktree_snapshot as ws

    (repo / "brand_new.py").write_text("never committed\n", encoding="utf-8")
    snap = ws.capture(repo, reason="test")

    _git(repo, "clean", "-fdq")
    assert not (repo / "brand_new.py").exists()

    ws.apply_snapshot(snap, repo)
    assert (repo / "brand_new.py").read_text(encoding="utf-8") == "never committed\n"


def test_capture_is_a_silent_noop_outside_a_git_repo(tmp_path, monkeypatch):
    """Mirrors symdexindex.is_indexable()'s precedent: not-a-repo is normal, not an
    error, and must never surface as one."""
    from firekeep_client import worktree_snapshot as ws

    monkeypatch.setenv("FIREKEEP_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    plain = tmp_path / "not_a_repo"
    plain.mkdir()
    assert ws.capture(plain, reason="test") is None


def test_capture_never_raises_when_git_is_unusable(repo, monkeypatch):
    """A snapshot failure must never be the reason a command or a hook stops."""
    from firekeep_client import worktree_snapshot as ws

    def boom(*a, **k):
        raise OSError("git vanished")

    monkeypatch.setattr(ws.subprocess, "run", boom)
    assert ws.capture(repo, reason="test") is None


def test_clean_tree_captures_nothing(repo):
    """No changes means no snapshot — otherwise the store fills with empty directories
    on every prompt tick and rotation evicts the ones that mattered."""
    from firekeep_client import worktree_snapshot as ws
    assert ws.capture(repo, reason="test") is None


def test_rotation_keeps_the_newest_and_prunes_the_rest(repo, monkeypatch):
    """Bounded disk. Pruning the NEWEST would be worse than not rotating at all."""
    from firekeep_client import worktree_snapshot as ws

    monkeypatch.setenv("FIREKEEP_SNAPSHOT_KEEP", "3")
    made = []
    for i in range(5):
        (repo / "kept.py").write_text(f"rev {i}\n", encoding="utf-8")
        made.append(ws.capture(repo, reason=f"r{i}"))
    live = ws.list_snapshots(repo)
    assert len(live) == 3
    assert {s["id"] for s in live} == {p.name for p in made[-3:]}


def test_truncation_is_recorded_not_silent(repo, monkeypatch):
    """A snapshot that silently dropped content is worse than none: it looks like a
    safety net and fails on use. Same publish-your-own-yield rule the archmap collectors
    follow."""
    import json
    from firekeep_client import worktree_snapshot as ws

    monkeypatch.setenv("FIREKEEP_SNAPSHOT_MAX_BYTES", "200")
    (repo / "kept.py").write_text("x" * 50_000 + "\n", encoding="utf-8")
    (repo / "big_untracked.bin").write_text("y" * 50_000, encoding="utf-8")
    snap = ws.capture(repo, reason="test")
    assert snap is not None
    meta = json.loads((snap / "meta.json").read_text(encoding="utf-8"))
    assert meta["truncated"], "over-cap content must be reported in meta.json"


def test_non_locale_decodable_content_still_snapshots(repo):
    """subprocess.run(text=True) decodes with the LOCALE codec — cp1252 on Windows —
    and git output containing e.g. UTF-8 Cyrillic (0xD0 0x90) raises UnicodeDecodeError
    in a reader thread. capture() swallows exceptions, so the failure mode is a SILENT
    no-snapshot: protection quietly absent exactly when someone is editing non-ASCII
    content. Found by running the guard against the real repo."""
    from firekeep_client import worktree_snapshot as ws

    # U+0410 encodes to D0 90, and 0x90 is UNDEFINED in cp1252 — that exact byte is
    # what raised against the real repo. A first draft of this test used "Привет",
    # whose bytes are all cp1252-decodable, so it passed against the broken code and
    # proved nothing.
    (repo / "kept.py").write_text("# А АА\nvalue = 1\n", encoding="utf-8")
    snap = ws.capture(repo, reason="unicode")
    assert snap is not None, "non-ASCII content must not silently defeat the snapshot"
    assert (snap / "files" / "kept.py").exists()
    # The decode error kills subprocess's READER THREAD, so run() returns with silently
    # EMPTY stdout instead of raising — worse than a crash, because files can go missing
    # from a snapshot with no signal at all. An empty patch is the observable symptom.
    import json
    meta = json.loads((snap / "meta.json").read_text(encoding="utf-8"))
    assert meta["patch_bytes"] > 0, "git output was silently lost to a decode error"
