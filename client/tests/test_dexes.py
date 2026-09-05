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


def test_maildex_manifest_is_an_ingest_client_dex():
    """Registry consumer #3, on the docdex chassis (maildex spec §1): no MCP
    server, so `kind` is what tells the gateway there is nothing to mount."""
    m = dexes.KNOWN_DEXES["maildex"]
    assert m.id == "firekeep.maildex"
    assert m.name == "maildex"
    assert m.title == "Maildex"
    assert m.indexes == "email"
    assert m.kind == "ingest-client"
    assert m.console_script == "firekeep-maildex"
    assert m.import_probe == "firekeep_maildex"
    assert m.description


def test_the_maildex_register_states_the_two_invariants_a_person_must_know():
    """M1 (always member-private) and M2 (no send capability) are the two facts
    that decide whether connecting a mailbox is acceptable, so they belong in the
    one line `firekeep dex list` prints — not only in the guide."""
    description = dexes.KNOWN_DEXES["maildex"].description.lower()
    assert "private" in description
    assert "read-only" in description
    assert "send" in description


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
    # Written scrambled (and stored sorted): the order comes from KNOWN_DEXES,
    # never from the file, so the gateway mounts in a defined order whatever a
    # hand-edited registry happens to list first.
    dexes.write_registry({name: {} for name in reversed(list(dexes.KNOWN_DEXES))})
    assert [m.name for m in dexes.registered()] == list(dexes.KNOWN_DEXES)


def test_registered_ignores_names_it_does_not_know(registry_home):
    """A hand-edited file naming a dex this client has never heard of must not
    crash the gateway — it has no manifest, so there is nothing to mount."""
    dexes.write_registry({"symdex": {}, "webdex": {}})
    assert [m.name for m in dexes.registered()] == ["symdex"]


# --------------------------------------------------------------------------- #
# ensure_migrated — the migration rule (Task A3)                                #
# --------------------------------------------------------------------------- #

CONFIGURED = """\
[identity]
agent_id = tester

[server]
kind = ports
scheme = http
host = 10.0.0.5
verify_tls = false
"""

# A pre-[server] profile config — the shape resolver.load_config MIGRATES (backs
# up, rewrites, prints). ensure_migrated must never trigger that.
LEGACY_PROFILES = """\
[active]
profile = personal

[personal]
kind = ports
scheme = http
host = 198.51.100.7
verify_tls = false
agent_id = mogan
"""


def _write_config(home, text):
    path = home / "config"
    path.write_text(text, encoding="utf-8")
    return path


class TestMigration:
    """Default-on, and the off-switch sticks (ROADMAP §5, amendment of
    2026-08-19 evening — REVERSING the suggestion-not-default rule these tests
    used to pin: a fresh machine no longer gets `{}`, and the configured/fresh
    fork is gone entirely). One deterministic rule, two lines: no dexes.json
    means symdex + docdex; a dexes.json means whatever the user made it."""

    def test_configured_machine_gets_both_dexes(self, registry_home):
        """Was `test_configured_machine_grandfathers_symdex`, which asserted
        symdex ALONE. Grandfathering symdex is now a special case of the general
        rule rather than its own branch — an update still never removes a
        capability an install already has; it adds docdex alongside."""
        _write_config(registry_home, CONFIGURED)
        dexes.ensure_migrated()
        assert sorted(dexes.read_registry()) == ["docdex", "symdex"]

    def test_fresh_machine_gets_both_dexes(self, registry_home):
        """Was `test_fresh_machine_opts_in_by_writing_an_empty_registry`. The
        reversal in one assertion: Firekeep understands your code and your
        documents out of the box, with no third install question asked to get
        there."""
        dexes.ensure_migrated()
        assert sorted(dexes.read_registry()) == ["docdex", "symdex"]

    def test_the_default_set_stops_at_symdex_and_docdex(self, registry_home):
        """maildex is NOT default-on: a connector with no account indexes
        nothing, so it would buy a doctor row and no mail. `firekeep maildex
        add` is what registers it."""
        dexes.ensure_migrated()
        assert "maildex" not in dexes.read_registry()

    def test_the_seeded_entries_are_stamped_like_any_other(self, registry_home):
        """Seeded entries are ordinary registry entries — `dex remove` and the
        doctor rows must not be able to tell them from hand-added ones."""
        dexes.ensure_migrated()
        for entry in dexes.read_registry().values():
            assert entry["source"] == "bundled"
            assert entry["added_at"].endswith("Z")

    def test_existing_registry_is_never_touched(self, registry_home):
        """Including — especially — an EMPTY one. This is the line default-on
        rests on: a default nobody can turn off is not a default, it is a
        mandate."""
        _write_config(registry_home, CONFIGURED)
        path = dexes.registry_path()
        path.write_text("{}", encoding="utf-8")  # deliberately unformatted
        before = path.read_bytes()

        dexes.ensure_migrated()
        assert path.read_bytes() == before

    def test_a_removed_symdex_stays_removed(self, registry_home):
        """The off-switch, over an update: someone who ran `firekeep dex remove
        symdex` must not find it back on next start just because the default set
        now names it."""
        _write_config(registry_home, CONFIGURED)
        dexes.write_registry({"docdex": {}})
        dexes.ensure_migrated()
        assert list(dexes.read_registry()) == ["docdex"]

    def test_a_removed_docdex_stays_removed(self, registry_home):
        dexes.write_registry({"symdex": {}})
        dexes.ensure_migrated()
        assert list(dexes.read_registry()) == ["symdex"]

    def test_migration_does_not_read_the_config_at_all(self, registry_home, monkeypatch):
        """The configured-vs-fresh fork is gone, so nothing here has any business
        opening the config — and a rule that cannot read it cannot regrow the
        `load_config` hazard the old one had to route around."""
        monkeypatch.setattr(dexes.resolver, "_raw_config", _boom_config)
        dexes.ensure_migrated()
        assert sorted(dexes.read_registry()) == ["docdex", "symdex"]

    def test_migration_never_rewrites_the_users_config(self, registry_home):
        """Seeding the registry runs at every gateway start, so it must leave a
        profile-era config exactly as it found it. It no longer reads the config
        at all, which is the strongest form of that — but the guard stays: this
        is the outcome that mattered, and the old rule reached it the harder way
        (`_raw_config`, never `load_config`, because load_config MIGRATES —
        backup + atomic rewrite + stderr, and it can raise
        ConfigMigrationConflict)."""
        path = _write_config(registry_home, LEGACY_PROFILES)
        before = path.read_bytes()

        dexes.ensure_migrated()
        assert path.read_bytes() == before
        assert "[server]" not in path.read_text(encoding="utf-8")

    def test_migration_never_raises(self, registry_home, monkeypatch):
        _write_config(registry_home, CONFIGURED)
        monkeypatch.setattr(dexes, "write_registry", _boom)
        dexes.ensure_migrated()  # must not raise
        log = (registry_home / "logs" / "hooks.log").read_text(encoding="utf-8")
        assert "migration failed during gateway" in log

    def test_the_failure_log_names_the_caller(self, registry_home, monkeypatch):
        _write_config(registry_home, CONFIGURED)
        monkeypatch.setattr(dexes, "write_registry", _boom)
        dexes.ensure_migrated(installing=True)
        log = (registry_home / "logs" / "hooks.log").read_text(encoding="utf-8")
        assert "migration failed during install" in log


def _boom(entries):
    raise OSError("read-only home")


def _boom_config():
    raise AssertionError("ensure_migrated must not read the config")


# --------------------------------------------------------------------------- #
# The two call sites                                                            #
# --------------------------------------------------------------------------- #


def test_gateway_startup_migrates(registry_home):
    """The load fallback: an update that never re-ran `firekeep install` must
    still find symdex mounted on its first session. docdex is registered too but
    mounts nothing — it is an `ingest-client`, so its entry drives the sync
    trigger and the doctor row, never a backend."""
    from firekeep_client.gateway import Gateway

    _write_config(registry_home, CONFIGURED)
    assert "symdex" in [b.name for b in Gateway().backends]
    assert sorted(dexes.read_registry()) == ["docdex", "symdex"]


@pytest.fixture
def install_env(registry_home, monkeypatch):
    """The minimum stubbing that lets cmd_install run in-process: no venv, no
    pip, no adapter render, no PATH edits (mirrors tests/test_cli_install.py)."""
    from firekeep_client import cli

    class _Adapter:
        def render(self, *, venv_bin):
            pass

    monkeypatch.setattr("firekeep_client.state._private", lambda p: None)
    monkeypatch.setattr(cli, "_run", lambda cmd, **kw: None)
    monkeypatch.setattr(cli, "_kit_dir", lambda: None)  # "running from the installed venv"
    monkeypatch.setattr(cli, "get_adapter", lambda name: _Adapter())
    monkeypatch.setattr(cli.pathenv, "ensure_on_path", lambda home, venv_bin, **kw: [])
    return cli


def test_install_on_a_fresh_machine_registers_both_dexes(install_env, registry_home):
    """Was `..._leaves_the_registry_empty`, and it was THE ordering guard —
    `_bootstrap_home` writes a config SKELETON carrying a `[server]` section, so
    a migration running after it read every fresh machine as 'configured'. The
    fork it guarded is gone (ROADMAP §5, 2026-08-19: default-on), which makes
    the ordering unobservable and the outcome the same either way. What is worth
    pinning now is that `firekeep install` leaves a machine understanding code
    and documents without being asked a third question."""
    assert install_env.main(["install", "--runtime", "claude", "--non-interactive"]) == 0
    assert sorted(dexes.read_registry()) == ["docdex", "symdex"]


def test_install_on_a_configured_machine_registers_both_dexes(install_env, registry_home):
    _write_config(registry_home, CONFIGURED)
    assert install_env.main(["install", "--runtime", "claude", "--non-interactive"]) == 0
    assert sorted(dexes.read_registry()) == ["docdex", "symdex"]


def test_install_leaves_a_users_registry_alone(install_env, registry_home):
    """`firekeep install` is also what a re-render and an update run, so it is
    the likeliest way a removed dex would come back."""
    dexes.write_registry({"docdex": {}})
    assert install_env.main(["install", "--runtime", "claude", "--non-interactive"]) == 0
    assert list(dexes.read_registry()) == ["docdex"]


# --------------------------------------------------------------------------- #
# role — index vs. capability (Firekeep Hands PR1 Task 1)                       #
# --------------------------------------------------------------------------- #


def test_manifest_role_defaults_to_index():
    from firekeep_client import dexes
    assert dexes.DexManifest.__dataclass_fields__["role"].default == "index"
    assert dexes.KNOWN_DEXES["symdex"].role == "index"
    assert dexes.KNOWN_DEXES["docdex"].role == "index"


def test_hands_manifest_is_a_capability_mounted_as_mcp_stdio():
    from firekeep_client import dexes
    m = dexes.KNOWN_DEXES["hands"]
    assert (m.id, m.name, m.title, m.indexes) == ("firekeep.hands", "hands", "Hands", "desktop")
    assert m.kind == "mcp-stdio"
    assert m.console_script == "firekeep-hands"
    assert m.import_probe == "firekeep_hands"
    assert m.role == "capability"


def test_hands_is_never_seeded(registry_home):
    from firekeep_client import dexes
    dexes.ensure_migrated()
    assert set(dexes.read_registry()) == {"symdex", "docdex"}
