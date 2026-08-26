"""Background maildex sync at session start: the docdexsync twin, same invariants.

Plan Task I (docs/superpowers/plans/2026-08-19-maildex.md), spec §3. The cases worth
pinning are the ones that make a background IMAP fetch safe to have on a session-start
hook at all: it is OFF unless a human registered the dex AND connected a mailbox, it
never spawns twice for one interval, it never blocks or fails a session, and
private-session mode stops it before it starts.

The reads here go through the module's OWN path helpers rather than importing
`firekeep_maildex` — that boundary is the feature (see the module docstring), so the
fixtures write `accounts.json` and the state files by hand, exactly as the wheel does.
That also keeps this suite runnable before/without the wheel, which is the point of the
seam: nothing here may depend on maildex being installed.
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

from firekeep_client import dexes, maildexsync

ACCOUNT_A = "a" * 32
ACCOUNT_B = "b" * 32


def _cfg(text: str = "") -> configparser.ConfigParser:
    c = configparser.ConfigParser()
    c.read_string(text)
    return c


@pytest.fixture
def maildex_home(tmp_path, monkeypatch):
    """A configured kit home. FIREKEEP_CONFIG isolates the maildex dir exactly as
    it isolates the config, the registry and the personal marker — the trigger
    derives its paths from the same place the wheel does."""
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
    for var in ("FIREKEEP_NO_AUTO_SYNC", "FIREKEEP_MAILDEX_SYNC_INTERVAL_HOURS",
                "FIREKEEP_BYPASS"):
        monkeypatch.delenv(var, raising=False)
    return home


def _register_maildex() -> None:
    dexes.write_registry({"maildex": {"added_at": "2026-08-19T00:00:00Z",
                                      "source": "bundled"}})


def _entry(host: str = "imap.example.com", status: str = "active") -> dict:
    return {"host": host, "port": 993, "username": "you@example.com",
            "folders": ["INBOX", "Sent"], "backfill_days": 90,
            "added_at": "2026-08-19T00:00:00+00:00", "status": status}


def _write_accounts(entries) -> None:
    path = maildexsync.accounts_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries), encoding="utf-8")


def _stamp_sync(account_id: str, *, hours_ago: float) -> None:
    when = (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=hours_ago))
    path = maildexsync.state_file(account_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "version": 1, "folders": {}, "messages": {},
        "last_sync_at": when.isoformat(),
    }), encoding="utf-8")


def _ready() -> None:
    """Registered dex + one active account: the only state in which this fires."""
    _register_maildex()
    _write_accounts({ACCOUNT_A: _entry()})


def _record_spawns(monkeypatch) -> list:
    """Record maildex sync launches, delegating every OTHER Popen to the real one.

    A blanket stub would also swallow `state._private`, which shells out to
    `icacls` on Windows through this same call — a spawn guard that counts those
    is counting the wrong thing on one platform only."""
    real = subprocess.Popen
    seen: list = []

    def spy(argv, **kw):
        if "firekeep_maildex.sync" in argv:
            seen.append({"argv": argv, "kw": kw})
            return object()
        return real(argv, **kw)

    monkeypatch.setattr(subprocess, "Popen", spy)
    return seen


def _forbid_spawn(monkeypatch, why: str) -> None:
    real = subprocess.Popen

    def guard(argv, **kw):
        if "firekeep_maildex.sync" in argv:
            pytest.fail(why)
        return real(argv, **kw)

    monkeypatch.setattr(subprocess, "Popen", guard)


# --- enable / opt-out --------------------------------------------------------


def test_disabled_when_nothing_is_set_up(maildex_home):
    assert maildexsync.is_enabled(_cfg()) is False


def test_disabled_when_the_dex_is_not_registered(maildex_home):
    """Registration gates ACTIVITY: the wheel is always installed, and a machine
    that never ran `firekeep dex add maildex` must never open a mailbox."""
    _write_accounts({ACCOUNT_A: _entry()})
    assert maildexsync.is_enabled(_cfg()) is False


def test_disabled_when_no_account_is_connected(maildex_home):
    _register_maildex()
    assert maildexsync.is_enabled(_cfg()) is False


def test_enabled_when_registered_with_an_active_account(maildex_home):
    _ready()
    assert maildexsync.is_enabled(_cfg()) is True


def test_inactive_accounts_do_not_count(maildex_home):
    """An account on its way out is not a reason to wake a sync up."""
    _register_maildex()
    _write_accounts({ACCOUNT_A: _entry(status="pending_delete")})
    assert maildexsync.is_enabled(_cfg()) is False


def test_one_env_switch_suspends_both_ingest_dexes(maildex_home, monkeypatch):
    """FIREKEEP_NO_AUTO_SYNC is deliberately the SAME variable docdexsync reads.
    A person who sets it wants the background uploads to stop — discovering a
    second per-dex variable afterwards is how a 'disabled' sync keeps running."""
    from firekeep_client import docdexsync

    _ready()
    monkeypatch.setenv("FIREKEEP_NO_AUTO_SYNC", "1")
    assert maildexsync.is_enabled(_cfg()) is False
    assert docdexsync.is_enabled(_cfg()) is False


def test_env_falsey_does_not_opt_out(maildex_home, monkeypatch):
    _ready()
    monkeypatch.setenv("FIREKEEP_NO_AUTO_SYNC", "0")
    assert maildexsync.is_enabled(_cfg()) is True


def test_config_opt_out(maildex_home):
    _ready()
    assert maildexsync.is_enabled(_cfg("[maildex]\nauto_sync = false\n")) is False
    assert maildexsync.is_enabled(_cfg("[maildex]\nauto_sync = true\n")) is True


def test_docdex_opt_out_does_not_disable_maildex(maildex_home):
    """The config switch is per-dex even though the env one is shared: turning
    off document sync must not silently turn off mail sync too."""
    _ready()
    assert maildexsync.is_enabled(_cfg("[docdex]\nauto_sync = false\n")) is True


def test_blank_config_value_stays_enabled(maildex_home):
    # A half-edited `auto_sync =` means 'unset' -> the default, NOT disabled.
    _ready()
    assert maildexsync.is_enabled(_cfg("[maildex]\nauto_sync =\n")) is True


# --- reading the wheel's files without importing it --------------------------


def test_paths_live_under_the_configured_kit_home(maildex_home):
    assert maildexsync.maildex_dir() == maildex_home / "maildex"
    assert maildexsync.accounts_file() == maildex_home / "maildex" / "accounts.json"
    assert maildexsync.state_file(ACCOUNT_A) == (
        maildex_home / "maildex" / "state" / f"{ACCOUNT_A}.json")


def test_asking_never_creates_the_maildex_dir(maildex_home):
    """'Has a human connected a mailbox?' must not leave evidence that they have."""
    assert maildexsync.active_account_ids() == []
    assert not (maildex_home / "maildex").exists()


def test_the_trigger_never_imports_the_wheel(maildex_home):
    """The subprocess IS the boundary: maildex is the module that holds a mailbox
    password in memory during a sync, and it must not be dragged into every
    session-start hook to answer 'is anything stale?'."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(maildexsync.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "firekeep_maildex" not in imported


def test_active_account_ids_tolerates_corruption(maildex_home):
    _register_maildex()
    path = maildexsync.accounts_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    for junk in ("{not json", "[]", '"a string"', ""):
        path.write_text(junk, encoding="utf-8")
        assert maildexsync.active_account_ids() == []
        assert maildexsync.is_enabled(_cfg()) is False


def test_active_account_ids_skips_non_dict_entries(maildex_home):
    _write_accounts({ACCOUNT_A: "nonsense", ACCOUNT_B: _entry()})
    assert maildexsync.active_account_ids() == [ACCOUNT_B]


def test_missing_status_counts_as_active(maildex_home):
    """Agrees with how the wheel reads the same file — the two readers cannot ask
    each other, so they must not drift."""
    _write_accounts({ACCOUNT_A: {"host": "imap.example.com",
                                 "username": "you@example.com"}})
    assert maildexsync.active_account_ids() == [ACCOUNT_A]


# --- staleness ---------------------------------------------------------------


def test_never_synced_is_stale(maildex_home):
    _ready()
    assert maildexsync.should_sync([ACCOUNT_A]) is not None


def test_corrupt_state_reads_as_never_synced(maildex_home):
    _ready()
    path = maildexsync.state_file(ACCOUNT_A)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert maildexsync.read_last_sync(ACCOUNT_A) is None
    assert maildexsync.should_sync([ACCOUNT_A]) is not None


def test_aborted_run_leaves_no_stamp_and_stays_stale(maildex_home):
    """The wheel does not stamp `last_sync_at` on an aborted run — a state file
    with messages but no stamp must read as 'never synced', not as fresh."""
    _ready()
    path = maildexsync.state_file(ACCOUNT_A)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "messages": {"1": {}},
                                "last_sync_at": None}), encoding="utf-8")
    assert maildexsync.read_last_sync(ACCOUNT_A) is None


@pytest.mark.parametrize("stamp", [
    "2026-08-19T04:00:00+00:00",   # what maildex writes
    "2026-08-19T04:00:00Z",        # fromisoformat only learned `Z` in 3.11
    "2026-08-19T04:00:00",         # naive: UTC, never the machine's local offset
])
def test_last_sync_is_read_as_utc_in_every_spelling(maildex_home, stamp):
    _ready()
    path = maildexsync.state_file(ACCOUNT_A)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last_sync_at": stamp}), encoding="utf-8")
    expected = datetime.datetime(2026, 8, 19, 4, tzinfo=datetime.timezone.utc)
    assert maildexsync.read_last_sync(ACCOUNT_A) == expected.timestamp()


def test_unparseable_last_sync_reads_as_never_synced(maildex_home):
    _ready()
    path = maildexsync.state_file(ACCOUNT_A)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last_sync_at": "yesterday-ish"}), encoding="utf-8")
    assert maildexsync.read_last_sync(ACCOUNT_A) is None


def test_fresh_sync_declines(maildex_home):
    _ready()
    _stamp_sync(ACCOUNT_A, hours_ago=0.1)
    assert maildexsync.should_sync([ACCOUNT_A]) is None


def test_staleness_boundary_is_the_interval(maildex_home):
    _ready()
    _stamp_sync(ACCOUNT_A, hours_ago=5.9)
    assert maildexsync.should_sync([ACCOUNT_A]) is None
    _stamp_sync(ACCOUNT_A, hours_ago=6.1)
    assert maildexsync.should_sync([ACCOUNT_A]) is not None


def test_interval_env_override(maildex_home, monkeypatch):
    _ready()
    _stamp_sync(ACCOUNT_A, hours_ago=2)
    assert maildexsync.should_sync([ACCOUNT_A]) is None  # default 6h (spec M6)
    monkeypatch.setenv("FIREKEEP_MAILDEX_SYNC_INTERVAL_HOURS", "1")
    assert maildexsync.sync_interval_hours() == 1
    assert maildexsync.should_sync([ACCOUNT_A]) is not None


def test_the_interval_is_maildexs_own_env_var(maildex_home, monkeypatch):
    """Docdex's variable must not move mail's cadence: the two caps are disclosed
    separately (spec M6) and a shared knob would make one of the tables a lie."""
    monkeypatch.setenv("FIREKEEP_DOCDEX_SYNC_INTERVAL_HOURS", "1")
    assert maildexsync.sync_interval_hours() == maildexsync.DEFAULT_SYNC_INTERVAL_HOURS


@pytest.mark.parametrize("raw", ["", "  ", "soon", "0", "-3"])
def test_unparseable_interval_falls_back_to_the_documented_default(
        maildex_home, monkeypatch, raw):
    """A typo must not silently turn a disclosed cadence into 'always' or 'never'."""
    monkeypatch.setenv("FIREKEEP_MAILDEX_SYNC_INTERVAL_HOURS", raw)
    assert maildexsync.sync_interval_hours() == maildexsync.DEFAULT_SYNC_INTERVAL_HOURS


def test_the_oldest_account_decides(maildex_home):
    """The spawn is `--all`, so the question is 'is ANYTHING stale?'. A mailbox
    connected a minute ago must not wait six hours because a sibling synced."""
    _register_maildex()
    _write_accounts({ACCOUNT_A: _entry(), ACCOUNT_B: _entry("imap.other.example")})
    _stamp_sync(ACCOUNT_A, hours_ago=0.1)
    assert maildexsync.should_sync([ACCOUNT_A, ACCOUNT_B]) is not None
    _stamp_sync(ACCOUNT_B, hours_ago=0.1)
    assert maildexsync.should_sync([ACCOUNT_A, ACCOUNT_B]) is None


def test_stamp_is_stable_within_a_bucket_and_moves_between_them(maildex_home):
    """The stamp IS the dedupe key, so its granularity is the cadence."""
    _ready()
    interval = maildexsync.sync_interval_hours() * 3600.0
    now = 1_000_000_000.0
    first = maildexsync.should_sync([ACCOUNT_A], now=now)
    assert first == maildexsync.should_sync([ACCOUNT_A], now=now + 60)
    assert first != maildexsync.should_sync([ACCOUNT_A], now=now + interval + 60)


# --- failure counting (the doctor row's number) ------------------------------


def test_failure_count_is_zero_without_a_state_file(maildex_home):
    assert maildexsync.read_failure_count(ACCOUNT_A) == 0


def test_failure_count_counts_messages_carrying_an_error(maildex_home):
    """Keys are `<folder>|<uidvalidity>|<uid>` (M7 keeps generations apart), and
    the number this reader produces must equal the wheel's own
    `AccountState.counts()["failures"]` — truthy `error`, nothing else."""
    path = maildexsync.state_file(ACCOUNT_A)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"messages": {
        "INBOX|900|1": {"ingested_at": "2026-08-19T00:00:00+00:00", "error": None},
        "INBOX|900|2": {"ingested_at": None, "error": "503 busy"},
        "Sent|31|9": {"ingested_at": "2026-08-19T00:00:00+00:00", "error": None},
    }}), encoding="utf-8")
    assert maildexsync.read_failure_count(ACCOUNT_A) == 1


def test_an_unparsed_message_is_not_a_failure(maildex_home):
    """The terminal half of the seen/ingested split: broken MIME and image-only
    mail carry a `note` with no `error`, because the same bytes parse the same
    way in six hours. Counting those would make doctor warn forever about mail
    nothing will ever retry."""
    path = maildexsync.state_file(ACCOUNT_A)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"messages": {
        "INBOX|900|1": {"ingested_at": None, "error": None,
                        "note": "no text parts"},
    }}), encoding="utf-8")
    assert maildexsync.read_failure_count(ACCOUNT_A) == 0


@pytest.mark.parametrize("junk", ["{not json", "[]", '{"messages": 3}', "{}"])
def test_failure_count_degrades_to_zero_on_an_unknown_shape(maildex_home, junk):
    """This number feeds a doctor row. A state file this reader does not
    recognise must cost the number, never the doctor run."""
    path = maildexsync.state_file(ACCOUNT_A)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(junk, encoding="utf-8")
    assert maildexsync.read_failure_count(ACCOUNT_A) == 0


# --- claim keying ------------------------------------------------------------


def test_claim_path_is_a_single_safe_filename(maildex_home):
    p = maildexsync._claim_path("../../escape/x")
    assert "/" not in p.name and "\\" not in p.name
    assert p.name.startswith("maildex_sync.")


def test_the_two_ingest_dexes_do_not_share_a_claim(maildex_home):
    """Same bucket, same interval, different dex: a shared claim key would let
    whichever ran first silence the other for six hours."""
    from firekeep_client import docdexsync

    assert maildexsync._claim_path("42") != docdexsync._claim_path("42")


# --- spawn behaviour ---------------------------------------------------------


def test_maybe_spawn_launches_detached_sync(maildex_home, monkeypatch):
    _ready()
    seen = _record_spawns(monkeypatch)
    assert maildexsync.maybe_spawn(_cfg(), "42") is True
    assert len(seen) == 1
    argv, kw = seen[0]["argv"], seen[0]["kw"]
    assert argv[0] == sys.executable
    assert argv[1:] == ["-m", "firekeep_maildex.sync", "--all", "--quiet"]
    # Detached from the hook, and no stream inherited from the hook process.
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


def test_maybe_spawn_is_once_per_stamp(maildex_home, monkeypatch):
    """Three windows opening together must not open three IMAP sessions."""
    _ready()
    seen = _record_spawns(monkeypatch)
    assert maildexsync.maybe_spawn(_cfg(), "42") is True
    assert maildexsync.maybe_spawn(_cfg(), "42") is True  # in flight, not re-spawned
    assert len(seen) == 1
    assert maildexsync.maybe_spawn(_cfg(), "43") is True  # a new interval, a new claim
    assert len(seen) == 2


def test_maybe_spawn_releases_claim_when_launch_fails(maildex_home, monkeypatch):
    """A failed launch must be retryable by a later session, not claimed forever."""
    _ready()
    maildexsync._claim_path("42")  # realise the scratch dir before Popen is stubbed
    real = subprocess.Popen

    def boom(argv, **kw):
        if "firekeep_maildex.sync" in argv:
            raise OSError("no exec for you")
        return real(argv, **kw)

    monkeypatch.setattr(subprocess, "Popen", boom)
    assert maildexsync.maybe_spawn(_cfg(), "42") is False
    assert not maildexsync._claim_path("42").exists()


def test_maybe_spawn_respects_opt_out(maildex_home, monkeypatch):
    _ready()
    monkeypatch.setenv("FIREKEEP_NO_AUTO_SYNC", "1")
    _forbid_spawn(monkeypatch, "spawned while disabled")
    assert maildexsync.maybe_spawn(_cfg(), "42") is False


def test_maybe_spawn_never_raises(maildex_home, monkeypatch):
    """Contract: a sync is an optimisation and may never cost a session."""
    _ready()
    monkeypatch.setattr(maildexsync, "_claim_path",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert maildexsync.maybe_spawn(_cfg(), "42") is False


# --- nudge composition -------------------------------------------------------


def test_nudge_silent_when_disabled(maildex_home, monkeypatch):
    _forbid_spawn(monkeypatch, "spawned while disabled")
    assert maildexsync.sync_nudge(_cfg()) == ""


def test_nudge_silent_when_everything_is_fresh(maildex_home, monkeypatch):
    _ready()
    _stamp_sync(ACCOUNT_A, hours_ago=0.1)
    _forbid_spawn(monkeypatch, "spawned despite a fresh sync")
    assert maildexsync.sync_nudge(_cfg()) == ""


def test_nudge_reports_a_background_sync(maildex_home, monkeypatch):
    _register_maildex()
    _write_accounts({ACCOUNT_A: _entry(), ACCOUNT_B: _entry("imap.other.example")})
    monkeypatch.setattr(maildexsync, "maybe_spawn", lambda *a: True)
    msg = maildexsync.sync_nudge(_cfg())
    assert "syncing 2 mail accounts in the background" in msg
    assert "FIREKEEP_NO_AUTO_SYNC" in msg


def test_nudge_counts_one_account_in_the_singular(maildex_home, monkeypatch):
    _ready()
    monkeypatch.setattr(maildexsync, "maybe_spawn", lambda *a: True)
    assert "syncing 1 mail account in the background" in maildexsync.sync_nudge(_cfg())


def test_nudge_falls_back_to_the_manual_command_when_spawn_fails(
        maildex_home, monkeypatch):
    """Never claim a sync is in flight when it isn't — the same honesty rule as
    autoupdate's 'updating in background' vs 'run: firekeep update'."""
    _ready()
    monkeypatch.setattr(maildexsync, "maybe_spawn", lambda *a: False)
    msg = maildexsync.sync_nudge(_cfg())
    assert "firekeep maildex sync" in msg
    assert "background" not in msg


def test_nudge_never_raises(maildex_home, monkeypatch):
    monkeypatch.setattr(maildexsync, "is_enabled",
                        lambda cfg: (_ for _ in ()).throw(RuntimeError("boom")))
    assert maildexsync.sync_nudge(_cfg()) == ""


# --- wiring into the session_start core --------------------------------------


def test_session_start_appends_the_sync_nudge(maildex_home, monkeypatch):
    from firekeep_client import transport
    from firekeep_client.hooks import _mcp, session_start

    monkeypatch.setattr(transport, "get_json", lambda *a, **k: {"rendered": "BRIEF"})
    monkeypatch.setattr(transport, "post_json",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    monkeypatch.setattr(_mcp, "call_tool", lambda *a, **k: {})
    monkeypatch.setattr(session_start.symdexindex, "index_nudge", lambda cfg, p: "")
    monkeypatch.setattr(session_start.docdexsync, "sync_nudge", lambda cfg: "")
    monkeypatch.setattr(session_start.maildexsync, "sync_nudge", lambda cfg: "|MAILDEX|")

    out = session_start.run({})
    assert out["systemMessage"].startswith("BRIEF")
    assert out["systemMessage"].endswith("|MAILDEX|")


def test_session_start_keeps_both_ingest_nudges_in_registry_order(
        maildex_home, monkeypatch):
    """Adding mail must not displace documents: both lines appear, documents
    first, because that is the order the registry lists them in."""
    from firekeep_client import transport
    from firekeep_client.hooks import _mcp, session_start

    monkeypatch.setattr(transport, "get_json", lambda *a, **k: {"rendered": "BRIEF"})
    monkeypatch.setattr(transport, "post_json",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    monkeypatch.setattr(_mcp, "call_tool", lambda *a, **k: {})
    monkeypatch.setattr(session_start.symdexindex, "index_nudge", lambda cfg, p: "")
    monkeypatch.setattr(session_start.docdexsync, "sync_nudge", lambda cfg: "|DOCDEX|")
    monkeypatch.setattr(session_start.maildexsync, "sync_nudge", lambda cfg: "|MAILDEX|")

    msg = session_start.run({})["systemMessage"]
    assert msg == "BRIEF|DOCDEX||MAILDEX|"


def test_private_session_mode_stops_the_trigger_at_the_dispatcher(
        maildex_home, monkeypatch):
    """Bypass suspends sync. The dispatcher short-circuits session_start before
    the core runs, so the trigger needs no check of its own — this pins that the
    short-circuit is genuinely what prevents the spawn."""
    from firekeep_client import resolver
    from firekeep_client.hooks import __main__ as dispatcher

    _ready()
    assert maildexsync.is_enabled(_cfg()) is True  # it WOULD have fired
    resolver.set_personal(True)
    _forbid_spawn(monkeypatch, "spawned in private-session mode")
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))

    assert dispatcher.main(["session_start"]) == 0
    scratch = maildexsync._claim_path("probe").parent
    assert [p.name for p in scratch.glob("maildex_sync.*")] == []
