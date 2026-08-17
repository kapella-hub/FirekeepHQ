"""The folder registry: what a human told Firekeep it may understand."""
from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from firekeep_docdex import sources


def test_registry_lives_under_the_isolated_firekeep_home(firekeep_home):
    assert sources.sources_path() == firekeep_home / "docdex" / "sources.json"


def test_add_defaults_to_private(tmp_path):
    src = sources.add(tmp_path)
    assert src.visibility == "member"
    assert src.status == "active"
    assert sources.get(src.id).visibility == "member"


def test_add_shared_is_workspace_visible(tmp_path):
    src = sources.add(tmp_path, shared=True)
    assert src.visibility == "workspace"


def test_ids_are_unique_128_bit_hex(tmp_path):
    a = sources.add(_mk(tmp_path, "a"))
    b = sources.add(_mk(tmp_path, "b"))
    assert a.id != b.id
    assert len(a.id) == 32 and int(a.id, 16) >= 0


def _mk(root, name):
    d = root / name
    d.mkdir()
    return d


def test_path_is_expanded_and_resolved(tmp_path, monkeypatch):
    nested = _mk(tmp_path, "notes")
    src = sources.add(str(nested) + os.sep + "." + os.sep)
    assert src.path == str(nested.resolve())


def test_home_relative_path_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    _mk(tmp_path, "Notes")
    src = sources.add("~/Notes")
    assert src.path == str((tmp_path / "Notes").resolve())


def test_add_refuses_a_missing_folder(tmp_path):
    with pytest.raises(ValueError, match="no such folder"):
        sources.add(tmp_path / "nope")


def test_add_refuses_a_file(tmp_path):
    f = tmp_path / "a.md"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="not a folder"):
        sources.add(f)


def test_add_refuses_a_duplicate_active_path(tmp_path):
    d = _mk(tmp_path, "notes")
    sources.add(d)
    with pytest.raises(ValueError, match="already registered"):
        sources.add(d)


def test_list_reports_a_missing_path_and_never_drops_it(tmp_path):
    d = _mk(tmp_path, "notes")
    src = sources.add(d)
    assert sources.list_sources()[0].missing is False
    d.rmdir()
    listed = sources.list_sources()
    assert [s.id for s in listed] == [src.id]
    assert listed[0].missing is True


def test_pending_delete_lifecycle(tmp_path):
    src = sources.add(_mk(tmp_path, "notes"))
    marked = sources.remove_mark(src.id)
    assert marked.status == "pending_delete"
    assert sources.get(src.id).status == "pending_delete"
    sources.drop(src.id)
    assert sources.get(src.id) is None
    assert sources.list_sources() == []


def test_remove_mark_is_idempotent(tmp_path):
    src = sources.add(_mk(tmp_path, "notes"))
    sources.remove_mark(src.id)
    assert sources.remove_mark(src.id).status == "pending_delete"


def test_remove_mark_unknown_id_raises(tmp_path):
    with pytest.raises(ValueError, match="unknown source"):
        sources.remove_mark("deadbeef" * 4)


def test_drop_unknown_id_is_a_noop(tmp_path):
    sources.drop("deadbeef" * 4)  # no raise


def test_a_pending_delete_path_may_be_re_added(tmp_path):
    """The duplicate guard is about ACTIVE sources: once a source is on its way
    out, re-adding the same folder must not be refused forever."""
    d = _mk(tmp_path, "notes")
    first = sources.add(d)
    sources.remove_mark(first.id)
    second = sources.add(d)
    assert second.id != first.id


def test_registry_survives_a_round_trip_on_disk(tmp_path):
    src = sources.add(_mk(tmp_path, "notes"), shared=True)
    raw = json.loads(sources.sources_path().read_text(encoding="utf-8"))
    assert raw[src.id]["visibility"] == "workspace"
    assert raw[src.id]["path"] == src.path
    assert raw[src.id]["added_at"].endswith("+00:00")


def test_missing_registry_reads_as_empty(firekeep_home):
    assert sources.read_sources() == {}
    assert sources.list_sources() == []


def test_corrupt_registry_reads_as_empty_and_is_logged(firekeep_home, monkeypatch, tmp_path):
    monkeypatch.setenv("FIREKEEP_LOG_DIR", str(tmp_path / "logs"))
    p = sources.sources_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    assert sources.read_sources() == {}
    log = tmp_path / "logs" / "hooks.log"
    assert log.exists() and "sources.json" in log.read_text(encoding="utf-8")


def test_a_corrupt_registry_is_never_silently_overwritten_by_a_read(firekeep_home):
    p = sources.sources_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    sources.read_sources()
    assert p.read_text(encoding="utf-8") == "{not json"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_registry_file_is_0600(tmp_path):
    sources.add(_mk(tmp_path, "notes"))
    mode = stat.S_IMODE(sources.sources_path().stat().st_mode)
    assert mode == 0o600


def test_write_is_atomic_no_temp_files_left_behind(tmp_path):
    sources.add(_mk(tmp_path, "notes"))
    sources.add(_mk(tmp_path, "other"))
    leftovers = [p.name for p in sources.sources_path().parent.iterdir()
                 if p.name != "sources.json"]
    assert leftovers == []
