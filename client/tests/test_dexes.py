"""The dex registry: known manifests + the installed-registry file.

Failing-first for dex milestone 1 Task A1 (plan
docs/superpowers/plans/2026-08-17-dex-registry-and-docdex.md). The registry is
what replaces the gateway's hardcoded LOCAL_SERVERS tuple, so these tests pin
the two things every later consumer depends on: the manifest SHAPE (designed as
if public — SDK ladder rung 1) and the file's read/write contract (never raises
on a corrupt or missing file; writes atomically and privately).
"""
from __future__ import annotations

import json
import os
import sys

import pytest

from firekeep_client import dexes


@pytest.fixture
def registry_home(tmp_path, monkeypatch):
    """FIREKEEP_CONFIG isolates the registry exactly as it isolates the config —
    registry_path() derives from the same home dir (resolver._config_path())."""
    monkeypatch.setenv("FIREKEEP_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("FIREKEEP_LOG_DIR", str(tmp_path / "logs"))
    return tmp_path


# --------------------------------------------------------------------------- #
# Manifests                                                                     #
# --------------------------------------------------------------------------- #


def test_symdex_manifest_is_an_mcp_stdio_dex():
    m = dexes.KNOWN_DEXES["symdex"]
    assert m.id == "firekeep.symdex"
    assert m.name == "symdex"
    assert m.title == "Symdex"
    assert m.indexes == "code"
    assert m.kind == "mcp-stdio"
    assert m.console_script == "firekeep-symdex"
    assert m.import_probe == "firekeep_symdex"
    assert m.description


def test_docdex_manifest_is_an_ingest_client_dex():
    """kind is exactly the field that tells the gateway 'nothing to mount here'
    (docdex spec §2) — an ingest-client has no MCP server."""
    m = dexes.KNOWN_DEXES["docdex"]
    assert m.id == "firekeep.docdex"
    assert m.name == "docdex"
    assert m.title == "Docdex"
    assert m.indexes == "documents"
    assert m.kind == "ingest-client"
    assert m.console_script == "firekeep-docdex"
    assert m.import_probe == "firekeep_docdex"
    assert m.description


def test_manifest_name_is_the_registry_key_everywhere():
    """DexManifest.name IS the registry key (plan: type consistency). A manifest
    whose name drifts from its dict key would make add()/remove()/registered()
    disagree about which dex they are talking about."""
    for key, manifest in dexes.KNOWN_DEXES.items():
        assert manifest.name == key


def test_manifests_are_frozen():
    with pytest.raises(Exception):
        dexes.KNOWN_DEXES["symdex"].kind = "ingest-client"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# registry_path / read_registry                                                 #
# --------------------------------------------------------------------------- #


def test_registry_path_sits_beside_the_config(registry_home):
    assert dexes.registry_path() == (registry_home / "dexes.json").resolve()


def test_read_registry_missing_file_is_empty(registry_home):
    assert dexes.read_registry() == {}


def test_read_registry_corrupt_file_is_empty_and_logged(registry_home):
    """A hand-mangled registry must never take a session down with it — but it
    must also never vanish silently, or a user whose dexes stopped mounting has
    no trace to follow."""
    path = dexes.registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert dexes.read_registry() == {}
    log = (registry_home / "logs" / "hooks.log").read_text(encoding="utf-8")
    assert "dexes" in log


def test_read_registry_non_object_json_is_empty(registry_home):
    """Valid JSON that is not an object (a list, a string) is still not a
    registry — treat it as unreadable rather than iterating it by accident."""
    path = dexes.registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('["symdex"]', encoding="utf-8")
    assert dexes.read_registry() == {}


def test_read_registry_never_raises_on_an_unreadable_path(registry_home):
    path = dexes.registry_path()
    path.mkdir(parents=True)  # a DIRECTORY where the file should be
    assert dexes.read_registry() == {}


# --------------------------------------------------------------------------- #
# write_registry / add / remove                                                 #
# --------------------------------------------------------------------------- #


def test_add_then_read_round_trip_stamps_the_entry(registry_home):
    manifest = dexes.add("symdex")
    assert manifest is dexes.KNOWN_DEXES["symdex"]

    entries = dexes.read_registry()
    assert set(entries) == {"symdex"}
    assert entries["symdex"]["source"] == "bundled"
    assert entries["symdex"]["added_at"].endswith("Z")


def test_add_is_idempotent_and_keeps_the_original_stamp(registry_home):
    dexes.add("symdex")
    first = dexes.read_registry()["symdex"]["added_at"]
    dexes.add("symdex")
    assert dexes.read_registry()["symdex"]["added_at"] == first


def test_add_preserves_other_entries(registry_home):
    dexes.add("symdex")
    dexes.add("docdex")
    assert set(dexes.read_registry()) == {"symdex", "docdex"}


def test_remove_round_trip(registry_home):
    dexes.add("symdex")
    dexes.add("docdex")
    assert dexes.remove("symdex") is dexes.KNOWN_DEXES["symdex"]
    assert set(dexes.read_registry()) == {"docdex"}


def test_remove_is_idempotent(registry_home):
    assert dexes.remove("symdex") is dexes.KNOWN_DEXES["symdex"]
    assert dexes.read_registry() == {}


def test_add_unknown_name_raises_value_error(registry_home):
    with pytest.raises(ValueError) as exc:
        dexes.add("webdex")
    assert "webdex" in str(exc.value)


def test_remove_unknown_name_raises_value_error(registry_home):
    with pytest.raises(ValueError):
        dexes.remove("webdex")


def test_write_leaves_no_temp_file_behind(registry_home):
    dexes.add("symdex")
    assert sorted(p.name for p in registry_home.iterdir() if p.is_file()) == ["dexes.json"]


def test_written_registry_is_valid_json_object(registry_home):
    dexes.add("symdex")
    assert isinstance(json.loads(dexes.registry_path().read_text(encoding="utf-8")), dict)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits (Windows uses ACLs)")
def test_registry_file_is_private(registry_home):
    dexes.add("symdex")
    assert (os.stat(dexes.registry_path()).st_mode & 0o777) == 0o600


# --------------------------------------------------------------------------- #
# registered()                                                                  #
# --------------------------------------------------------------------------- #


def test_registered_is_empty_without_a_file(registry_home):
    assert dexes.registered() == []


def test_registered_returns_manifests_in_known_order(registry_home):
    dexes.write_registry({"docdex": {}, "symdex": {}})
    assert [m.name for m in dexes.registered()] == list(dexes.KNOWN_DEXES)


def test_registered_ignores_names_it_does_not_know(registry_home):
    """A hand-edited file naming a dex this client has never heard of must not
    crash the gateway — it has no manifest, so there is nothing to mount."""
    dexes.write_registry({"symdex": {}, "webdex": {}})
    assert [m.name for m in dexes.registered()] == ["symdex"]
