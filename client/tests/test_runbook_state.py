"""Enforced Runbooks Phase B — the workspace-scoped bundle store in state.py.

Spec: docs/superpowers/specs/2026-08-15-enforced-runbooks-design.md ("Bundle").
The store's whole job is stated in two invariants: the write is ATOMIC (temp
file + os.replace — a reader sees a complete old bundle or a complete new one,
never a torn file), and a failed or invalid fetch keeps the LAST-KNOWN-GOOD
copy untouched — for a block-mode runbook that stored bundle is what decides
the fail-closed posture while the server is unreachable.
"""
from __future__ import annotations

import json
import os

import pytest

from firekeep_client import state


def _bundle(version="v1", workspace="ws-1", entries=None):
    return {
        "version": version,
        "workspace_id": workspace,
        "entries": entries if entries is not None else [
            {"skill_id": "deploy-vps", "step_id": "s1", "pattern": "git push*",
             "mode": "advise", "load_bearing": False, "fail_posture": "open"},
        ],
    }


class TestWriteReadRoundtrip:
    def test_roundtrip_preserves_entries_version_workspace(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path / "cache"))
        assert state.write_runbook_bundle(_bundle()) is True
        got = state.read_runbook_bundle()
        assert got["version"] == "v1"
        assert got["workspace_id"] == "ws-1"
        assert got["entries"][0]["pattern"] == "git push*"
        assert isinstance(got["fetched_at"], float)

    def test_empty_entries_is_valid_and_replaces(self, tmp_path, monkeypatch):
        """An empty entry list is the server retiring every runbook — a real
        answer, not a failure, so it must replace the previous bundle."""
        monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path / "cache"))
        state.write_runbook_bundle(_bundle())
        assert state.write_runbook_bundle(_bundle(version="v2", entries=[])) is True
        got = state.read_runbook_bundle()
        assert got["version"] == "v2"
        assert got["entries"] == []

    def test_non_dict_entry_items_are_dropped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path / "cache"))
        entries = [{"pattern": "ok*"}, "junk", 5, None]
        assert state.write_runbook_bundle(_bundle(entries=entries)) is True
        assert state.read_runbook_bundle()["entries"] == [{"pattern": "ok*"}]


class TestAtomicity:
    def test_write_goes_through_os_replace_not_in_place(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path / "cache"))
        replaced = []
        real_replace = os.replace

        def spy(src, dst):
            replaced.append((str(src), str(dst)))
            return real_replace(src, dst)

        monkeypatch.setattr(state.os, "replace", spy)
        assert state.write_runbook_bundle(_bundle()) is True
        # Both the bundle file and the pointer went through temp + os.replace,
        # and the temp lived in the SAME directory (os.replace across volumes
        # is not atomic).
        assert len(replaced) == 2
        for src, dst in replaced:
            assert ".tmp-" in src
            assert os.path.dirname(src) == os.path.dirname(dst)

    def test_no_temp_files_left_behind(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path / "cache"))
        state.write_runbook_bundle(_bundle())
        leftovers = [p.name for p in (state.cache_dir() / "runbooks").iterdir()
                     if ".tmp-" in p.name]
        assert leftovers == []

    def test_failed_replace_keeps_last_known_good_and_cleans_temp(
            self, tmp_path, monkeypatch):
        """The atomic write's reason to exist: a crash mid-write must leave the
        previous bundle byte-identical and no torn file for the reader."""
        monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path / "cache"))
        assert state.write_runbook_bundle(_bundle(version="good")) is True

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(state.os, "replace",
                       lambda src, dst: (_ for _ in ()).throw(OSError("disk full")))
            assert state.write_runbook_bundle(_bundle(version="torn")) is False

        got = state.read_runbook_bundle()
        assert got["version"] == "good"
        leftovers = [p.name for p in (state.cache_dir() / "runbooks").iterdir()
                     if ".tmp-" in p.name]
        assert leftovers == []


class TestLastKnownGoodOnInvalidPayload:
    @pytest.mark.parametrize("bad", [
        None,
        "a string",
        [],
        {},
        {"version": "v2", "entries": []},                        # no workspace_id
        {"workspace_id": "ws-1", "entries": []},                 # no version
        {"version": "", "workspace_id": "ws-1", "entries": []},  # empty version
        {"version": 7, "workspace_id": "ws-1", "entries": []},   # non-str version
        {"version": "v2", "workspace_id": "", "entries": []},    # empty workspace
        {"version": "v2", "workspace_id": "ws-1", "entries": "nope"},
        {"version": "v2", "workspace_id": "ws-1"},               # entries absent
    ])
    def test_invalid_payload_rejected_and_previous_bundle_kept(
            self, tmp_path, monkeypatch, bad):
        monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path / "cache"))
        assert state.write_runbook_bundle(_bundle(version="keep-me")) is True
        assert state.write_runbook_bundle(bad) is False
        assert state.read_runbook_bundle()["version"] == "keep-me"


class TestStaleness:
    def test_fresh_bundle_is_not_stale(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path / "cache"))
        state.write_runbook_bundle(_bundle())
        assert state.runbook_bundle_is_stale(state.read_runbook_bundle()) is False

    def test_bundle_older_than_ttl_is_stale(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path / "cache"))
        state.write_runbook_bundle(_bundle())
        got = state.read_runbook_bundle()
        got["fetched_at"] -= state.RUNBOOK_BUNDLE_TTL_SECONDS + 1
        assert state.runbook_bundle_is_stale(got) is True

    @pytest.mark.parametrize("bundle", [
        {"entries": []},                                # no fetched_at at all
        {"entries": [], "fetched_at": "garbage"},       # unreadable age
        {"entries": [], "fetched_at": None},
    ])
    def test_unknown_age_is_stale_not_fresh(self, bundle):
        assert state.runbook_bundle_is_stale(bundle) is True


class TestWorkspaceScoping:
    def test_two_workspaces_stored_side_by_side(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path / "cache"))
        state.write_runbook_bundle(_bundle(version="a1", workspace="ws-a"))
        state.write_runbook_bundle(_bundle(version="b1", workspace="ws-b"))
        # Pointer follows the most recent successful fetch...
        assert state.read_runbook_bundle()["version"] == "b1"
        # ...but the other workspace's last-known-good is still addressable.
        assert state.read_runbook_bundle("ws-a")["version"] == "a1"
        assert state.read_runbook_bundle("ws-b")["version"] == "b1"

    def test_hostile_workspace_id_cannot_escape_the_cache_dir(
            self, tmp_path, monkeypatch):
        monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path / "cache"))
        assert state.write_runbook_bundle(
            _bundle(version="evil", workspace="../../evil")) is True
        d = state.cache_dir() / "runbooks"
        # Everything landed INSIDE runbooks/ — nothing above it.
        outside = [p for p in tmp_path.rglob("*.json")
                   if d not in p.parents]
        assert outside == []
        assert state.read_runbook_bundle("../../evil")["version"] == "evil"

    def test_workspace_named_current_does_not_collide_with_pointer(
            self, tmp_path, monkeypatch):
        monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path / "cache"))
        assert state.write_runbook_bundle(
            _bundle(version="c1", workspace="current")) is True
        assert state.read_runbook_bundle()["version"] == "c1"
        assert state.read_runbook_bundle("current")["version"] == "c1"


class TestReadNeverRaises:
    def test_missing_store_reads_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path / "cache"))
        assert state.read_runbook_bundle() is None

    def test_corrupt_bundle_file_reads_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path / "cache"))
        state.write_runbook_bundle(_bundle())
        d = state.cache_dir() / "runbooks"
        (d / "ws-1.json").write_text("{ not json", encoding="utf-8")
        assert state.read_runbook_bundle() is None

    def test_bundle_file_without_entries_list_reads_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path / "cache"))
        state.write_runbook_bundle(_bundle())
        d = state.cache_dir() / "runbooks"
        (d / "ws-1.json").write_text(json.dumps({"version": "x", "entries": "no"}),
                                     encoding="utf-8")
        assert state.read_runbook_bundle() is None

    def test_dangling_pointer_reads_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path / "cache"))
        state.write_runbook_bundle(_bundle())
        (state.cache_dir() / "runbooks" / "ws-1.json").unlink()
        assert state.read_runbook_bundle() is None

    def test_empty_pointer_reads_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path / "cache"))
        state.write_runbook_bundle(_bundle())
        (state.cache_dir() / "runbooks" / "current").write_text("", encoding="utf-8")
        assert state.read_runbook_bundle() is None
