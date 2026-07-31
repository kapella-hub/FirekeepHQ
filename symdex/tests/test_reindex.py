"""Headless one-shot index entry point (`python -m firekeep_symdex.reindex`).

This is the callable surface a lifecycle hook can actually reach — `server:main` is a
stdio MCP server and needs a client on the other end, which a SessionStart hook does
not have. The client kit's background auto-index spawns this module by `-m`, so its
argv contract and its exit codes are an interface, not an implementation detail.
"""
import json
import subprocess
import sys

import pytest

from firekeep_symdex import reindex


def _run(*args):
    """Invoke via -m the same way firekeep_client.symdexindex.maybe_spawn does."""
    return subprocess.run(
        [sys.executable, "-m", "firekeep_symdex.reindex", *args],
        capture_output=True, text=True,
    )


# --- argv contract -----------------------------------------------------------

def test_module_is_runnable_by_dash_m():
    """The client spawns `-m firekeep_symdex.reindex`; if that stops resolving, the
    auto-index silently no-ops in a detached process nobody sees."""
    r = _run("--help")
    assert r.returncode == 0
    assert "--incremental" in r.stdout


def test_path_is_required():
    r = _run()
    assert r.returncode != 0


# --- exit codes are the interface --------------------------------------------

def test_success_returns_zero_and_prints_json(tmp_path, capsys):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "mod.py").write_text("def hello():\n    return 1\n")
    rc = reindex.main([str(src), "--storage-path", str(tmp_path / "idx")])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True


def test_missing_folder_returns_one_not_a_traceback(tmp_path, capsys):
    rc = reindex.main([str(tmp_path / "ghost"), "--storage-path", str(tmp_path / "idx")])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is False
    assert "not found" in payload["error"].lower()


def test_unexpected_exception_returns_two_and_stays_json(monkeypatch, tmp_path, capsys):
    """A detached caller can only ever see the exit code and stdout — a traceback on
    stderr would be lost, so failures must be structured."""
    import firekeep_symdex.tools.index_folder as m

    monkeypatch.setattr(m, "index_folder",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    rc = reindex.main([str(tmp_path)])
    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is False
    assert "RuntimeError" in payload["error"]


# --- defaults differ from the MCP tool on purpose ----------------------------

def test_ai_summaries_default_off(monkeypatch, tmp_path):
    """The MCP tool defaults use_ai_summaries=True, which bills an API key per index.
    A background index the user did not ask for must not spend money."""
    seen = {}
    import firekeep_symdex.tools.index_folder as m

    def spy(**kw):
        seen.update(kw)
        return {"success": True}

    monkeypatch.setattr(m, "index_folder", spy)
    reindex.main([str(tmp_path)])
    assert seen["use_ai_summaries"] is False
    assert seen["incremental"] is False  # matches index_folder's own default


def test_no_false_truncation_note_below_the_cap(tmp_path, capsys):
    """Regression: the note's threshold and message were hardcoded 500 while the real
    cap is DEFAULT_MAX_FILES (1500), so any repo over 500 files was told its index was
    truncated when it was whole. The JSON line is the ONLY diagnostic a detached
    background index leaves, so a false alarm there is expensive."""
    from firekeep_symdex.tools._utils import DEFAULT_MAX_FILES

    src = tmp_path / "proj"
    src.mkdir()
    for i in range(12):
        (src / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n")

    rc = reindex.main([str(src), "--storage-path", str(tmp_path / "idx")])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["file_count"] == 12
    assert DEFAULT_MAX_FILES > 12
    assert "note" not in payload, f"false truncation note: {payload.get('note')!r}"


def test_incremental_flag_is_threaded(monkeypatch, tmp_path):
    seen = {}
    import firekeep_symdex.tools.index_folder as m

    monkeypatch.setattr(m, "index_folder", lambda **kw: seen.update(kw) or {"success": True})
    reindex.main([str(tmp_path), "--incremental"])
    assert seen["incremental"] is True
