"""Background docdex sync at session start: the symdexindex twin, same invariants.

Failing-first for plan Task C1 (docs/superpowers/plans/2026-08-17-dex-registry-and-docdex.md).
The cases worth pinning are the ones that make a background upload safe to have on
a session-start hook at all: it is OFF unless a human registered the dex AND
registered a folder, it never spawns twice for one interval, it never blocks or
fails a session, and private-session mode stops it before it starts.

The reads here go through the module's OWN path helpers rather than importing
`firekeep_docdex` — that boundary is the feature (see the module docstring), so the
fixtures write `sources.json` and the state files by hand, exactly as the wheel does.
"""
from __future__ import annotations

import datetime
import io
import json
import os
import subprocess
import sys
import textwrap
import configparser

import pytest

from firekeep_client import dexes, docdexsync

SOURCE_A = "a" * 32
SOURCE_B = "b" * 32


def _cfg(text: str = "") -> configparser.ConfigParser:
    c = configparser.ConfigParser()
    c.read_string(text)
    return c


@pytest.fixture
def docdex_home(tmp_path, monkeypatch):
    """A configured kit home. FIREKEEP_CONFIG isolates the docdex dir exactly as
    it isolates the config, the registry and the personal marker — the trigger
    derives its paths from the same place `firekeep_docdex.firekeep_home` does."""
    home = tmp_path / ".firekeep"
    home.mkdir()
    (home / "config").write_text(textwrap.dedent("""\
        [identity]
        agent_id = tester
        [server]
        kind = ports
        scheme = http
        host = 127.0.0.1
        verify_tls = false
    """), encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(home / "config"))
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("FIREKEEP_LOG_DIR", str(tmp_path / "logs"))
    for var in ("FIREKEEP_NO_AUTO_SYNC", "FIREKEEP_DOCDEX_SYNC_INTERVAL_HOURS",
                "FIREKEEP_BYPASS"):
        monkeypatch.delenv(var, raising=False)
    return home


def _register_docdex() -> None:
    dexes.write_registry({"docdex": {"added_at": "2026-08-17T00:00:00Z",
                                     "source": "bundled"}})


def _entry(path: str = "/notes", status: str = "active") -> dict:
    return {"path": path, "visibility": "member",
            "added_at": "2026-08-17T00:00:00+00:00", "status": status}


def _write_sources(entries) -> None:
    path = docdexsync.sources_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries), encoding="utf-8")


def _stamp_sync(source_id: str, *, hours_ago: float) -> None:
    when = (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=hours_ago))
    path = docdexsync.state_file(source_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "version": 1, "files": {}, "last_sync_at": when.isoformat(),
        "last_walk_completed": True,
    }), encoding="utf-8")


def _ready() -> None:
    """Registered dex + one active source: the only state in which this fires."""
    _register_docdex()
    _write_sources({SOURCE_A: _entry()})


def _record_spawns(monkeypatch) -> list:
    """Record docdex sync launches, delegating every OTHER Popen to the real one.

    A blanket stub would also swallow `state._private`, which shells out to
    `icacls` on Windows through this same call — a spawn guard that counts those
    is counting the wrong thing on one platform only."""
    real = subprocess.Popen
    seen: list = []

    def spy(argv, **kw):
        if "firekeep_docdex.sync" in argv:
            seen.append({"argv": argv, "kw": kw})
            return object()
        return real(argv, **kw)

    monkeypatch.setattr(subprocess, "Popen", spy)
    return seen


def _forbid_spawn(monkeypatch, why: str) -> None:
    real = subprocess.Popen

    def guard(argv, **kw):
        if "firekeep_docdex.sync" in argv:
            pytest.fail(why)
        return real(argv, **kw)

    monkeypatch.setattr(subprocess, "Popen", guard)


# --- enable / opt-out --------------------------------------------------------


def test_disabled_when_nothing_is_set_up(docdex_home):
    assert docdexsync.is_enabled(_cfg()) is False


def test_disabled_when_the_dex_is_not_registered(docdex_home):
    """Registration gates ACTIVITY: the wheel is always installed, and a machine
    that never ran `firekeep dex add docdex` must never upload a document."""
    _write_sources({SOURCE_A: _entry()})
    assert docdexsync.is_enabled(_cfg()) is False


def test_disabled_when_no_folder_is_registered(docdex_home):
    _register_docdex()
    assert docdexsync.is_enabled(_cfg()) is False


def test_enabled_when_registered_with_an_active_source(docdex_home):
    _ready()
    assert docdexsync.is_enabled(_cfg()) is True


def test_pending_delete_sources_do_not_count_as_active(docdex_home):
    """A folder on its way out is not a reason to wake a sync up."""
    _register_docdex()
    _write_sources({SOURCE_A: _entry(status="pending_delete")})
    assert docdexsync.is_enabled(_cfg()) is False


def test_env_opt_out_wins_over_everything(docdex_home, monkeypatch):
    _ready()
    monkeypatch.setenv("FIREKEEP_NO_AUTO_SYNC", "1")
    assert docdexsync.is_enabled(_cfg()) is False


def test_env_falsey_does_not_opt_out(docdex_home, monkeypatch):
    _ready()
    monkeypatch.setenv("FIREKEEP_NO_AUTO_SYNC", "0")
    assert docdexsync.is_enabled(_cfg()) is True


def test_config_opt_out(docdex_home):
    _ready()
    assert docdexsync.is_enabled(_cfg("[docdex]\nauto_sync = false\n")) is False
    assert docdexsync.is_enabled(_cfg("[docdex]\nauto_sync = true\n")) is True


def test_blank_config_value_stays_enabled(docdex_home):
    # A half-edited `auto_sync =` means 'unset' -> the default, NOT disabled.
    _ready()
    assert docdexsync.is_enabled(_cfg("[docdex]\nauto_sync =\n")) is True


# --- reading the wheel's files without importing it --------------------------


def test_paths_live_under_the_configured_kit_home(docdex_home):
    assert docdexsync.docdex_dir() == docdex_home / "docdex"
    assert docdexsync.sources_file() == docdex_home / "docdex" / "sources.json"
    assert docdexsync.state_file(SOURCE_A) == (
        docdex_home / "docdex" / "state" / f"{SOURCE_A}.json")


def test_asking_never_creates_the_docdex_dir(docdex_home):
    """'Has a human registered a folder?' must not leave evidence that they have."""
    assert docdexsync.active_source_ids() == []
    assert not (docdex_home / "docdex").exists()


def test_active_source_ids_tolerates_corruption(docdex_home):
    _register_docdex()
    path = docdexsync.sources_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    for junk in ("{not json", "[]", '"a string"', ""):
        path.write_text(junk, encoding="utf-8")
        assert docdexsync.active_source_ids() == []
        assert docdexsync.is_enabled(_cfg()) is False


def test_active_source_ids_skips_non_dict_entries(docdex_home):
    _write_sources({SOURCE_A: "nonsense", SOURCE_B: _entry()})
    assert docdexsync.active_source_ids() == [SOURCE_B]


def test_missing_status_counts_as_active(docdex_home):
    """Agrees with firekeep_docdex.sources._to_source, which defaults the same
    way — the two readers cannot ask each other, so they must not drift."""
    _write_sources({SOURCE_A: {"path": "/notes", "visibility": "member"}})
    assert docdexsync.active_source_ids() == [SOURCE_A]


# --- staleness ---------------------------------------------------------------


def test_never_synced_is_stale(docdex_home):
    _ready()
    assert docdexsync.should_sync([SOURCE_A]) is not None


def test_corrupt_state_reads_as_never_synced(docdex_home):
    _ready()
    path = docdexsync.state_file(SOURCE_A)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert docdexsync.read_last_sync(SOURCE_A) is None
    assert docdexsync.should_sync([SOURCE_A]) is not None


def test_aborted_run_leaves_no_stamp_and_stays_stale(docdex_home):
    """sync.py does not stamp `last_sync_at` on an aborted run — a state file
    with files but no stamp must read as 'never synced', not as fresh."""
    _ready()
    path = docdexsync.state_file(SOURCE_A)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "files": {"a.md": {}},
                                "last_sync_at": None}), encoding="utf-8")
    assert docdexsync.read_last_sync(SOURCE_A) is None


@pytest.mark.parametrize("stamp", [
    "2026-08-17T04:00:00+00:00",   # what docdex writes
    "2026-08-17T04:00:00Z",        # fromisoformat only learned `Z` in 3.11
    "2026-08-17T04:00:00",         # naive: UTC, never the machine's local offset
])
def test_last_sync_is_read_as_utc_in_every_spelling(docdex_home, stamp):
    _ready()
    path = docdexsync.state_file(SOURCE_A)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last_sync_at": stamp}), encoding="utf-8")
    expected = datetime.datetime(2026, 8, 17, 4, tzinfo=datetime.timezone.utc)
    assert docdexsync.read_last_sync(SOURCE_A) == expected.timestamp()


def test_unparseable_last_sync_reads_as_never_synced(docdex_home):
    _ready()
    path = docdexsync.state_file(SOURCE_A)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last_sync_at": "yesterday-ish"}), encoding="utf-8")
    assert docdexsync.read_last_sync(SOURCE_A) is None


def test_fresh_sync_declines(docdex_home):
    _ready()
    _stamp_sync(SOURCE_A, hours_ago=0.1)
    assert docdexsync.should_sync([SOURCE_A]) is None


def test_staleness_boundary_is_the_interval(docdex_home):
    _ready()
    _stamp_sync(SOURCE_A, hours_ago=5.9)
    assert docdexsync.should_sync([SOURCE_A]) is None
    _stamp_sync(SOURCE_A, hours_ago=6.1)
    assert docdexsync.should_sync([SOURCE_A]) is not None


def test_interval_env_override(docdex_home, monkeypatch):
    _ready()
    _stamp_sync(SOURCE_A, hours_ago=2)
    assert docdexsync.should_sync([SOURCE_A]) is None  # default 6h
    monkeypatch.setenv("FIREKEEP_DOCDEX_SYNC_INTERVAL_HOURS", "1")
    assert docdexsync.sync_interval_hours() == 1
    assert docdexsync.should_sync([SOURCE_A]) is not None


@pytest.mark.parametrize("raw", ["", "  ", "soon", "0", "-3"])
def test_unparseable_interval_falls_back_to_the_documented_default(
        docdex_home, monkeypatch, raw):
    """A typo must not silently turn a disclosed cadence into 'always' or 'never'."""
    monkeypatch.setenv("FIREKEEP_DOCDEX_SYNC_INTERVAL_HOURS", raw)
    assert docdexsync.sync_interval_hours() == docdexsync.DEFAULT_SYNC_INTERVAL_HOURS


def test_the_oldest_source_decides(docdex_home):
    """The spawn is `--all`, so the question is 'is ANYTHING stale?'. A folder
    added a minute ago must not wait six hours because a sibling synced on time."""
    _register_docdex()
    _write_sources({SOURCE_A: _entry("/notes"), SOURCE_B: _entry("/runbooks")})
    _stamp_sync(SOURCE_A, hours_ago=0.1)
    assert docdexsync.should_sync([SOURCE_A, SOURCE_B]) is not None
    _stamp_sync(SOURCE_B, hours_ago=0.1)
    assert docdexsync.should_sync([SOURCE_A, SOURCE_B]) is None


def test_stamp_is_stable_within_a_bucket_and_moves_between_them(docdex_home):
    """The stamp IS the dedupe key, so its granularity is the cadence."""
    _ready()
    interval = docdexsync.sync_interval_hours() * 3600.0
    now = 1_000_000_000.0
    first = docdexsync.should_sync([SOURCE_A], now=now)
    assert first == docdexsync.should_sync([SOURCE_A], now=now + 60)
    assert first != docdexsync.should_sync([SOURCE_A], now=now + interval + 60)


# --- claim keying ------------------------------------------------------------


def test_claim_path_is_a_single_safe_filename(docdex_home):
    p = docdexsync._claim_path("../../escape/x")
    assert "/" not in p.name and "\\" not in p.name
    assert p.name.startswith("docdex_sync.")


# --- spawn behaviour ---------------------------------------------------------


def test_maybe_spawn_launches_detached_sync(docdex_home, monkeypatch):
    _ready()
    seen = _record_spawns(monkeypatch)
    assert docdexsync.maybe_spawn(_cfg(), "42") is True
    assert len(seen) == 1
    argv, kw = seen[0]["argv"], seen[0]["kw"]
    assert argv[0] == sys.executable
    assert argv[1:] == ["-m", "firekeep_docdex.sync", "--all", "--quiet"]
    # Detached from the hook, and no stream inherited from the hook process (I6).
    assert kw["stdin"] is subprocess.DEVNULL
    assert kw["stdout"] is subprocess.DEVNULL
    assert kw["stderr"] is subprocess.DEVNULL
    if os.name == "nt":
        # A HIDDEN console, not NO console — DETACHED_PROCESS made the venv launcher's
        # re-spawned interpreter open a Windows Terminal window on every session start
        # (2026-08-25). See firekeep_client.background / tests/test_background.py.
        assert kw["creationflags"] & subprocess.CREATE_NO_WINDOW
        assert not kw["creationflags"] & subprocess.DETACHED_PROCESS
    else:
        assert kw["start_new_session"] is True


def test_maybe_spawn_is_once_per_stamp(docdex_home, monkeypatch):
    """Three windows opening together must not launch three uploads of one folder."""
    _ready()
    seen = _record_spawns(monkeypatch)
    assert docdexsync.maybe_spawn(_cfg(), "42") is True
    assert docdexsync.maybe_spawn(_cfg(), "42") is True  # in flight, not re-spawned
    assert len(seen) == 1
    assert docdexsync.maybe_spawn(_cfg(), "43") is True  # a new interval is a new claim
    assert len(seen) == 2


def test_maybe_spawn_releases_claim_when_launch_fails(docdex_home, monkeypatch):
    """A failed launch must be retryable by a later session, not claimed forever."""
    _ready()
    docdexsync._claim_path("42")  # realise the scratch dir before Popen is stubbed
    real = subprocess.Popen

    def boom(argv, **kw):
        if "firekeep_docdex.sync" in argv:
            raise OSError("no exec for you")
        return real(argv, **kw)

    monkeypatch.setattr(subprocess, "Popen", boom)
    assert docdexsync.maybe_spawn(_cfg(), "42") is False
    assert not docdexsync._claim_path("42").exists()


def test_maybe_spawn_respects_opt_out(docdex_home, monkeypatch):
    _ready()
    monkeypatch.setenv("FIREKEEP_NO_AUTO_SYNC", "1")
    _forbid_spawn(monkeypatch, "spawned while disabled")
    assert docdexsync.maybe_spawn(_cfg(), "42") is False


def test_maybe_spawn_never_raises(docdex_home, monkeypatch):
    """Contract: a sync is an optimisation and may never cost a session."""
    _ready()
    monkeypatch.setattr(docdexsync, "_claim_path",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert docdexsync.maybe_spawn(_cfg(), "42") is False


# --- nudge composition -------------------------------------------------------


def test_nudge_silent_when_disabled(docdex_home, monkeypatch):
    _forbid_spawn(monkeypatch, "spawned while disabled")
    assert docdexsync.sync_nudge(_cfg()) == ""


def test_nudge_silent_when_everything_is_fresh(docdex_home, monkeypatch):
    _ready()
    _stamp_sync(SOURCE_A, hours_ago=0.1)
    _forbid_spawn(monkeypatch, "spawned despite a fresh sync")
    assert docdexsync.sync_nudge(_cfg()) == ""


def test_nudge_reports_a_background_sync(docdex_home, monkeypatch):
    _register_docdex()
    _write_sources({SOURCE_A: _entry("/notes"), SOURCE_B: _entry("/runbooks")})
    monkeypatch.setattr(docdexsync, "maybe_spawn", lambda *a: True)
    msg = docdexsync.sync_nudge(_cfg())
    assert "syncing 2 document sources in the background" in msg
    assert "FIREKEEP_NO_AUTO_SYNC" in msg


def test_nudge_counts_one_source_in_the_singular(docdex_home, monkeypatch):
    _ready()
    monkeypatch.setattr(docdexsync, "maybe_spawn", lambda *a: True)
    assert "syncing 1 document source in the background" in docdexsync.sync_nudge(_cfg())


def test_nudge_falls_back_to_the_manual_command_when_spawn_fails(docdex_home, monkeypatch):
    """Never claim a sync is in flight when it isn't — the same honesty rule as
    autoupdate's 'updating in background' vs 'run: firekeep update'."""
    _ready()
    monkeypatch.setattr(docdexsync, "maybe_spawn", lambda *a: False)
    msg = docdexsync.sync_nudge(_cfg())
    assert "firekeep docdex sync" in msg
    assert "background" not in msg


def test_nudge_never_raises(docdex_home, monkeypatch):
    monkeypatch.setattr(docdexsync, "is_enabled",
                        lambda cfg: (_ for _ in ()).throw(RuntimeError("boom")))
    assert docdexsync.sync_nudge(_cfg()) == ""


# --- wiring into the session_start core --------------------------------------


def test_session_start_appends_the_sync_nudge(docdex_home, monkeypatch):
    from firekeep_client import transport
    from firekeep_client.hooks import _mcp, session_start

    monkeypatch.setattr(transport, "get_json", lambda *a, **k: {"rendered": "BRIEF"})
    monkeypatch.setattr(transport, "post_json",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    monkeypatch.setattr(_mcp, "call_tool", lambda *a, **k: {})
    monkeypatch.setattr(session_start.symdexindex, "index_nudge", lambda cfg, p: "")
    monkeypatch.setattr(session_start.docdexsync, "sync_nudge", lambda cfg: "|DOCDEX|")

    out = session_start.run({})
    assert out["systemMessage"].startswith("BRIEF")
    assert out["systemMessage"].endswith("|DOCDEX|")


def test_private_session_mode_stops_the_trigger_at_the_dispatcher(docdex_home, monkeypatch):
    """I3: bypass suspends sync. The dispatcher short-circuits session_start
    before the core runs, so the trigger needs no check of its own — this pins
    that the short-circuit is genuinely what prevents the spawn."""
    from firekeep_client import resolver
    from firekeep_client.hooks import __main__ as dispatcher

    _ready()
    assert docdexsync.is_enabled(_cfg()) is True  # it WOULD have fired
    resolver.set_personal(True)
    _forbid_spawn(monkeypatch, "spawned in private-session mode")
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))

    assert dispatcher.main(["session_start"]) == 0
    scratch = docdexsync._claim_path("probe").parent
    assert [p.name for p in scratch.glob("docdex_sync.*")] == []
