"""Background symdex auto-index: spawn a detached `python -m firekeep_symdex.reindex`
from the session_start hook when the staleness policy says the workspace needs one.
ON by default; opt out via FIREKEEP_NO_AUTO_INDEX or `[symdex] auto_index = false`.

The invariants worth guarding are the ones that make this safe to have ON by default:
it never blocks or fails a session, it never spawns twice for the same (folder, stamp),
it never indexes a directory that isn't a git tree, and it agrees with symdex about
where the index lives."""
import configparser
import json
import os
import subprocess

import pytest

from firekeep_client import symdexindex


def _cfg(text=""):
    c = configparser.ConfigParser()
    c.read_string(text)
    return c


@pytest.fixture
def repo(tmp_path):
    """A minimal git working tree."""
    d = tmp_path / "MyRepo"
    d.mkdir()
    (d / ".git").mkdir()
    return d


# --- enable / opt-out --------------------------------------------------------

def test_enabled_by_default():
    assert symdexindex.is_enabled(_cfg()) is True


def test_env_opt_out(monkeypatch):
    monkeypatch.setenv("FIREKEEP_NO_AUTO_INDEX", "1")
    assert symdexindex.is_enabled(_cfg()) is False


def test_env_falsey_does_not_opt_out(monkeypatch):
    monkeypatch.setenv("FIREKEEP_NO_AUTO_INDEX", "0")
    assert symdexindex.is_enabled(_cfg()) is True


def test_config_opt_out():
    assert symdexindex.is_enabled(_cfg("[symdex]\nauto_index = false\n")) is False
    assert symdexindex.is_enabled(_cfg("[symdex]\nauto_index = true\n")) is True


def test_blank_config_value_stays_enabled():
    # A half-edited `auto_index =` (blank) means 'unset' -> default ON, NOT disabled.
    assert symdexindex.is_enabled(_cfg("[symdex]\nauto_index =\n")) is True


# --- index location: must agree with symdex ----------------------------------

def test_index_root_honours_symdex_env(monkeypatch, tmp_path):
    """CODE_INDEX_PATH is symdex's own override (server.py threads it in as
    storage_path). Disagreeing would report 'not indexed' forever."""
    monkeypatch.setenv("CODE_INDEX_PATH", str(tmp_path / "elsewhere"))
    assert symdexindex.index_root() == tmp_path / "elsewhere"


def test_index_root_defaults_to_home(monkeypatch):
    monkeypatch.delenv("CODE_INDEX_PATH", raising=False)
    assert symdexindex.index_root().name == ".code-index"


def test_index_file_matches_indexstore_slug(monkeypatch, tmp_path, repo):
    """IndexStore._index_path for owner='local' is f"{owner}-{basename}.json"."""
    monkeypatch.setenv("CODE_INDEX_PATH", str(tmp_path))
    assert symdexindex.index_file(repo) == tmp_path / "local-MyRepo.json"


# --- eligibility guard -------------------------------------------------------

def test_is_indexable_requires_git(repo, tmp_path):
    assert symdexindex.is_indexable(repo) is True
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert symdexindex.is_indexable(plain) is False


def test_is_indexable_accepts_git_file_worktree(tmp_path):
    """A linked worktree or submodule has `.git` as a FILE, not a directory."""
    d = tmp_path / "linked"
    d.mkdir()
    (d / ".git").write_text("gitdir: ../.git/worktrees/linked\n")
    assert symdexindex.is_indexable(d) is True


def test_is_indexable_rejects_missing_and_file(tmp_path):
    assert symdexindex.is_indexable(tmp_path / "ghost") is False
    f = tmp_path / "afile"
    f.write_text("x")
    assert symdexindex.is_indexable(f) is False


# --- indexed_at reader -------------------------------------------------------

def test_read_indexed_at(tmp_path):
    idx = tmp_path / "local-X.json"
    idx.write_text(json.dumps({"indexed_at": "2026-07-29T21:35:01", "symbols": []}))
    assert symdexindex.read_indexed_at(idx) == "2026-07-29T21:35:01"


def test_read_indexed_at_tolerates_missing_and_corrupt(tmp_path):
    assert symdexindex.read_indexed_at(tmp_path / "ghost.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert symdexindex.read_indexed_at(bad) is None
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"symbols": []}))
    assert symdexindex.read_indexed_at(empty) is None


# --- staleness policy --------------------------------------------------------

def _head(repo, text):
    (repo / ".git" / "HEAD").write_text(text)


def test_missing_index_builds_unconditionally(repo, tmp_path):
    """The only case where the user is strictly worse off than before the feature:
    every symdex tool answers 'Repository not found'."""
    assert symdexindex.should_index(repo, tmp_path / "absent.json") == "bootstrap"


def test_existing_index_stamps_date_and_git_tip(repo, tmp_path):
    idx = tmp_path / "local-MyRepo.json"
    idx.write_text("{}")
    refs = repo / ".git" / "refs" / "heads"
    refs.mkdir(parents=True)
    (refs / "main").write_text("a" * 40)
    _head(repo, "ref: refs/heads/main\n")

    stamp = symdexindex.should_index(repo, idx)
    today = __import__("datetime").date.today().isoformat()
    assert stamp.startswith(today + ".")
    # Stable across calls -> the O_EXCL claim dedupes repeat session starts.
    assert stamp == symdexindex.should_index(repo, idx)


def test_new_commit_changes_the_stamp(repo, tmp_path):
    """A moved branch tip is the signal that source actually changed."""
    idx = tmp_path / "i.json"
    idx.write_text("{}")
    refs = repo / ".git" / "refs" / "heads"
    refs.mkdir(parents=True)
    tip = refs / "main"
    tip.write_text("a" * 40)
    _head(repo, "ref: refs/heads/main\n")

    before = symdexindex.should_index(repo, idx)
    os.utime(tip, (0, 0))  # simulate a commit moving the tip
    assert symdexindex.should_index(repo, idx) != before


def test_detached_head_uses_the_sha(repo, tmp_path):
    idx = tmp_path / "i.json"
    idx.write_text("{}")
    _head(repo, "b" * 40 + "\n")
    assert symdexindex.should_index(repo, idx).endswith("." + "b" * 12)


def test_unreadable_git_layout_degrades_to_daily_floor(repo, tmp_path):
    """packed-refs, a linked worktree, or a branch with no commits: still refresh
    daily rather than never."""
    idx = tmp_path / "i.json"
    idx.write_text("{}")
    _head(repo, "ref: refs/heads/nonexistent\n")
    today = __import__("datetime").date.today().isoformat()
    assert symdexindex.should_index(repo, idx) == today


def test_git_tip_stamp_returns_none_without_head(repo):
    assert symdexindex._git_tip_stamp(repo) is None


# --- claim keying ------------------------------------------------------------

def test_claim_path_is_a_single_safe_filename(repo):
    """A folder path carries ':' and '\\' on Windows and a stamp is caller-supplied;
    neither may produce a separator, or the claim escapes the scratch dir."""
    p = symdexindex._claim_path(repo, "../../escape/x")
    assert "/" not in p.name and "\\" not in p.name
    assert p.name.startswith("auto_index.")


def test_claim_path_differs_per_stamp_and_folder(repo, tmp_path):
    other = tmp_path / "Other"
    other.mkdir()
    a = symdexindex._claim_path(repo, "s1")
    assert a != symdexindex._claim_path(repo, "s2")
    assert a != symdexindex._claim_path(other, "s1")


# --- spawn behaviour ---------------------------------------------------------

def test_maybe_spawn_launches_detached_reindex(monkeypatch, repo, tmp_path):
    monkeypatch.setattr(symdexindex.state, "_scratch_file",
                        lambda n: tmp_path / n)
    seen = {}

    def fake_popen(argv, **kw):
        seen["argv"] = argv
        seen["kw"] = kw
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    assert symdexindex.maybe_spawn(_cfg(), repo, "2026-07-29") is True
    assert seen["argv"][1:] == ["-m", "firekeep_symdex.reindex", str(repo), "--incremental"]
    # Detached from the hook, and no stream inherited from the hook process.
    assert seen["kw"]["stdin"] is subprocess.DEVNULL
    assert seen["kw"]["stdout"] is subprocess.DEVNULL
    if os.name == "nt":
        # A HIDDEN console, not NO console: the venv launcher re-spawns the base
        # interpreter, and with DETACHED_PROCESS that child was handed a visible
        # console — a Windows Terminal window on every session start (2026-08-25).
        # See firekeep_client.background / tests/test_background.py.
        assert seen["kw"]["creationflags"] & subprocess.CREATE_NO_WINDOW
        assert not seen["kw"]["creationflags"] & subprocess.DETACHED_PROCESS
    else:
        assert seen["kw"]["start_new_session"] is True


def test_maybe_spawn_is_once_per_folder_stamp(monkeypatch, repo, tmp_path):
    """Two windows opening the same repo together must not both write the index."""
    monkeypatch.setattr(symdexindex.state, "_scratch_file", lambda n: tmp_path / n)
    calls = []
    monkeypatch.setattr(subprocess, "Popen",
                        lambda argv, **kw: calls.append(argv) or object())
    assert symdexindex.maybe_spawn(_cfg(), repo, "same") is True
    assert symdexindex.maybe_spawn(_cfg(), repo, "same") is True  # in flight, not re-spawned
    assert len(calls) == 1
    # A new stamp is a new claim.
    assert symdexindex.maybe_spawn(_cfg(), repo, "next") is True
    assert len(calls) == 2


def test_maybe_spawn_releases_claim_when_launch_fails(monkeypatch, repo, tmp_path):
    """A failed launch must be retryable by a later session, not permanently claimed."""
    monkeypatch.setattr(symdexindex.state, "_scratch_file", lambda n: tmp_path / n)

    def boom(argv, **kw):
        raise OSError("no exec for you")

    monkeypatch.setattr(subprocess, "Popen", boom)
    assert symdexindex.maybe_spawn(_cfg(), repo, "s") is False
    assert not symdexindex._claim_path(repo, "s").exists()


def test_maybe_spawn_respects_opt_out(monkeypatch, repo):
    monkeypatch.setenv("FIREKEEP_NO_AUTO_INDEX", "1")
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: pytest.fail("spawned while disabled"))
    assert symdexindex.maybe_spawn(_cfg(), repo, "s") is False


def test_maybe_spawn_never_raises(monkeypatch, repo):
    """Contract: an index is an optimisation and may never cost a session."""
    monkeypatch.setattr(symdexindex, "_claim_path",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert symdexindex.maybe_spawn(_cfg(), repo, "s") is False


# --- nudge composition -------------------------------------------------------

def test_nudge_silent_when_policy_declines(monkeypatch, repo):
    monkeypatch.setattr(symdexindex, "should_index", lambda folder, idx: None)
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: pytest.fail("spawned despite declining policy"))
    assert symdexindex.index_nudge(_cfg(), {"cwd": str(repo)}) == ""


def test_nudge_silent_for_non_repo(monkeypatch, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setattr(symdexindex, "should_index",
                        lambda folder, idx: pytest.fail("policy consulted for non-repo"))
    assert symdexindex.index_nudge(_cfg(), {"cwd": str(plain)}) == ""


def test_nudge_reports_build_vs_refresh(monkeypatch, repo, tmp_path):
    monkeypatch.setenv("CODE_INDEX_PATH", str(tmp_path))
    monkeypatch.setattr(symdexindex, "should_index", lambda folder, idx: "stamp")
    monkeypatch.setattr(symdexindex, "maybe_spawn", lambda *a: True)

    msg = symdexindex.index_nudge(_cfg(), {"cwd": str(repo)})
    assert "building" in msg and "MyRepo" in msg

    (tmp_path / "local-MyRepo.json").write_text("{}")
    msg = symdexindex.index_nudge(_cfg(), {"cwd": str(repo)})
    assert "refreshing" in msg


def test_nudge_falls_back_to_manual_command_when_spawn_fails(monkeypatch, repo):
    """Never claim an index is in flight when it isn't — same honesty rule as
    autoupdate's 'updating in background' vs 'run: firekeep update'."""
    monkeypatch.setattr(symdexindex, "should_index", lambda folder, idx: "stamp")
    monkeypatch.setattr(symdexindex, "maybe_spawn", lambda *a: False)
    msg = symdexindex.index_nudge(_cfg(), {"cwd": str(repo)})
    assert "firekeep_symdex.reindex" in msg
    assert "background" not in msg


def test_nudge_never_raises(monkeypatch):
    monkeypatch.setattr(symdexindex, "is_enabled",
                        lambda cfg: (_ for _ in ()).throw(RuntimeError("boom")))
    assert symdexindex.index_nudge(_cfg(), {"cwd": "/whatever"}) == ""
