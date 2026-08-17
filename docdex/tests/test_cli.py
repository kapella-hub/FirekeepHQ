"""`firekeep-docdex` — the console script a human drives.

The CLI is a thin wrapper over `sources.py` and `sync.py`, so what these tests
pin is the WRAPPING: exit codes, what gets printed, and the handful of places
the CLI decides something the modules underneath deliberately do not.

Two of those decisions are load-bearing:

* **A bare `sync` means every source.** `run_sync` refuses to guess (it raises
  without a source id or `all_sources`), which is right for a library and wrong
  for a human at a prompt.
* **`remove` with no reachable server still marks the source.** Refusing would
  leave a folder the human asked to be gone still syncing on the next run; the
  mark stops it immediately and the next successful sync finishes the job.
"""
from __future__ import annotations

import pytest

from firekeep_docdex import cli, sources, state, wire

# Captured before the autouse fixture below replaces it, so the one test that
# is ABOUT `_client` can still reach the real thing.
_REAL_CLIENT = cli._client


@pytest.fixture(autouse=True)
def wired(monkeypatch, client):
    """Every command that talks to the server goes through `cli._client`."""
    monkeypatch.setattr(cli, "_client", lambda: client)
    return client


def _out(capsys):
    captured = capsys.readouterr()
    return captured.out + captured.err


def _folder(tmp_path, name="notes", files=None):
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    for rel, body in (files or {"a.md": "alpha", "b.md": "beta"}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return root


# --- add --------------------------------------------------------------------


def test_add_registers_a_folder_privately_by_default(tmp_path, capsys):
    root = _folder(tmp_path)
    assert cli.main(["add", str(root)]) == 0
    registered = sources.list_sources()
    assert len(registered) == 1
    assert registered[0].visibility == sources.MEMBER
    out = _out(capsys)
    assert registered[0].id in out       # the id `remove` will need
    assert "private" in out.lower()


def test_add_shared_says_who_can_see_it(tmp_path, capsys):
    root = _folder(tmp_path)
    assert cli.main(["add", str(root), "--shared"]) == 0
    assert sources.list_sources()[0].visibility == sources.WORKSPACE
    assert "workspace" in _out(capsys).lower()


def test_add_a_missing_folder_fails_and_registers_nothing(tmp_path, capsys):
    assert cli.main(["add", str(tmp_path / "nope")]) == 1
    assert sources.read_sources() == {}
    assert "nope" in _out(capsys)


def test_add_the_same_folder_twice_is_refused(tmp_path, capsys):
    root = _folder(tmp_path)
    cli.main(["add", str(root)])
    capsys.readouterr()
    assert cli.main(["add", str(root)]) == 1
    assert len(sources.read_sources()) == 1
    assert "already registered" in _out(capsys)


def test_add_nudges_when_docdex_is_not_registered_as_a_dex(tmp_path, capsys):
    """Folder control is human-only and works unregistered — but a human who
    adds a folder and never registers the dex gets no background sync, and the
    symptom is silence. Say so once, here."""
    assert cli.main(["add", str(_folder(tmp_path))]) == 0
    assert "firekeep dex add docdex" in _out(capsys)


def test_add_does_not_nudge_once_the_dex_is_registered(tmp_path, capsys):
    from firekeep_client import dexes

    dexes.add("docdex")
    assert cli.main(["add", str(_folder(tmp_path))]) == 0
    assert "firekeep dex add docdex" not in _out(capsys)


# --- list -------------------------------------------------------------------


def test_list_of_nothing_says_how_to_add_something(capsys):
    assert cli.main(["list"]) == 0
    assert "docdex add" in _out(capsys)


def test_list_shows_the_id_path_visibility_and_counts(tmp_path, capsys):
    root = _folder(tmp_path)
    src = sources.add(root)
    cli.main(["sync", "--source", src.id, "--quiet"])
    capsys.readouterr()

    assert cli.main(["list"]) == 0
    out = _out(capsys)
    assert src.id in out          # FULL id — it is what `remove` takes
    assert str(root) in out
    assert "private" in out.lower()
    assert "2 files" in out


def test_list_flags_a_missing_folder_and_says_nothing_was_deleted(tmp_path, capsys):
    root = _folder(tmp_path)
    src = sources.add(root)
    cli.main(["sync", "--source", src.id, "--quiet"])
    for child in root.iterdir():
        child.unlink()
    root.rmdir()
    capsys.readouterr()

    assert cli.main(["list"]) == 0
    out = _out(capsys).lower()
    assert "missing" in out
    assert "deleted" in out  # ...and that its replicas were NOT


def test_list_reports_a_source_awaiting_removal(tmp_path, capsys):
    src = sources.add(_folder(tmp_path))
    sources.remove_mark(src.id)
    cli.main(["list"])
    assert "pending" in _out(capsys).lower()


def test_list_explains_an_incomplete_walk(tmp_path, capsys):
    """The one honest answer to "why did nothing get deleted?" — recorded by
    state precisely so `list` can give it."""
    src = sources.add(_folder(tmp_path))
    st = state.read_state(src.id)
    st.last_sync_at = state.now()
    st.last_walk_completed = False
    state.write_state(src.id, st)

    cli.main(["list"])
    out = _out(capsys).lower()
    assert "did not complete" in out


def test_list_counts_failures_and_pending_deletes(tmp_path, capsys):
    src = sources.add(_folder(tmp_path))
    st = state.read_state(src.id)
    state.record_failure(st, "a.md", "deadbeef", "503 busy")
    state.record_ingested(st, "b.md", "cafe")
    state.mark_pending_delete(st, "b.md")
    state.write_state(src.id, st)

    cli.main(["list"])
    out = _out(capsys)
    assert "1 failure" in out
    assert "1 pending delete" in out


# --- sync -------------------------------------------------------------------


def test_a_bare_sync_syncs_every_source(tmp_path, server):
    """`run_sync` refuses to guess; a human at a prompt should not have to."""
    sources.add(_folder(tmp_path, "one", {"a.md": "alpha"}))
    sources.add(_folder(tmp_path, "two", {"b.md": "beta"}))
    assert cli.main(["sync", "--quiet"]) == 0
    assert len(server.posts) == 2


def test_sync_one_source_touches_only_that_source(tmp_path, server):
    one = sources.add(_folder(tmp_path, "one", {"a.md": "alpha"}))
    sources.add(_folder(tmp_path, "two", {"b.md": "beta"}))
    assert cli.main(["sync", "--source", one.id, "--quiet"]) == 0
    assert len(server.posts) == 1


def test_quiet_sync_prints_nothing(tmp_path, capsys):
    sources.add(_folder(tmp_path))
    cli.main(["sync", "--quiet"])
    assert _out(capsys) == ""


def test_a_loud_sync_reports_each_source(tmp_path, capsys):
    src = sources.add(_folder(tmp_path))
    cli.main(["sync"])
    out = _out(capsys)
    assert src.id[:8] in out
    assert "ingested 2" in out


def test_sync_of_an_unknown_source_is_rc_1_not_a_traceback(capsys):
    assert cli.main(["sync", "--source", "0" * 32]) == 1
    assert "unknown source" in _out(capsys)


def test_sync_reports_a_failing_run_as_rc_1(tmp_path, server, monkeypatch):
    from firekeep_client import transport

    def boom(*_a, **_kw):
        raise transport.TransportError("POST ... failed: 503 busy", status=503)

    server.post_hook = lambda *a: boom()
    sources.add(_folder(tmp_path))
    assert cli.main(["sync", "--quiet"]) == 1


def test_sync_without_a_reachable_keep_is_rc_1_and_names_the_repair(
    tmp_path, monkeypatch, capsys
):
    def unconfigured():
        raise RuntimeError("no [server] section in ~/.firekeep/config")

    monkeypatch.setattr(cli, "_client", unconfigured)
    sources.add(_folder(tmp_path))
    assert cli.main(["sync"]) == 1
    out = _out(capsys)
    assert "no [server] section" in out
    assert "firekeep doctor" in out


# --- remove -----------------------------------------------------------------


def test_remove_deletes_the_replicas_and_forgets_the_source(tmp_path, capsys, server):
    src = sources.add(_folder(tmp_path))
    cli.main(["sync", "--quiet"])
    capsys.readouterr()

    assert cli.main(["remove", src.id]) == 0
    assert sources.get(src.id) is None
    assert not state.state_path(src.id).exists()
    assert any("dex-sources" in d["url"] for d in server.deletes)
    assert "removed" in _out(capsys).lower()


def test_remove_an_unknown_id_is_rc_1(capsys):
    assert cli.main(["remove", "0" * 32]) == 1
    assert "unknown source" in _out(capsys)


def test_remove_with_no_reachable_keep_still_marks_the_source(
    tmp_path, monkeypatch, capsys
):
    """The mark is the part that must not be lost: it stops the next sync from
    re-uploading a folder the human already asked to be gone."""
    src = sources.add(_folder(tmp_path))

    def unconfigured():
        raise RuntimeError("no [server] section in ~/.firekeep/config")

    monkeypatch.setattr(cli, "_client", unconfigured)
    assert cli.main(["remove", src.id]) == 1
    still = sources.get(src.id)
    assert still is not None and still.status == sources.PENDING_DELETE
    assert "next sync" in _out(capsys)


def test_remove_the_server_will_not_confirm_stays_pending(tmp_path, server, capsys):
    from firekeep_client import transport

    src = sources.add(_folder(tmp_path))

    def refuse(*_a, **_kw):
        raise transport.TransportError("DELETE ... failed: 503 busy", status=503)

    server.delete_hook = lambda *a: refuse()
    assert cli.main(["remove", src.id]) == 1
    still = sources.get(src.id)
    assert still is not None and still.status == sources.PENDING_DELETE
    assert "next sync" in _out(capsys)


def test_remove_while_a_sync_holds_the_lock_reports_it(tmp_path, capsys):
    from firekeep_docdex import sync as sync_mod

    src = sources.add(_folder(tmp_path))
    with sync_mod.source_lock(src.id):
        assert cli.main(["remove", src.id]) == 1
    assert "sync" in _out(capsys).lower()


# --- the entrypoint ---------------------------------------------------------


def test_no_subcommand_prints_usage_and_is_rc_2(capsys):
    assert cli.main([]) == 2
    assert "usage" in _out(capsys).lower()


def test_an_unexpected_failure_is_a_message_never_a_traceback(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(cli.sources, "list_sources",
                        lambda: (_ for _ in ()).throw(RuntimeError("disk on fire")))
    assert cli.main(["list"]) == 1
    assert "disk on fire" in _out(capsys)


def test_the_prog_name_follows_how_it_was_invoked(capsys):
    """The main CLI delegates here as `firekeep docdex ...`; a usage line that
    named the console script would send a user to a command they never typed."""
    assert cli.main(["add"], prog="firekeep docdex") == 2
    assert "firekeep docdex" in _out(capsys)


def test_the_console_script_points_at_this_module():
    """The declaration in pyproject.toml is what makes `firekeep-docdex` exist
    on PATH — and what the detached background spawn and the bootstrap both
    assume. A rename here without a rename there is a silent dead command."""
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    scripts = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["scripts"]
    assert scripts["firekeep-docdex"] == "firekeep_docdex.cli:main"
    module, _, attr = scripts["firekeep-docdex"].partition(":")
    assert module == cli.__name__ and getattr(cli, attr) is cli.main


def test_wire_client_is_what_the_cli_builds(monkeypatch):
    """`_client` is the only place the CLI reaches for a server connection —
    every test above replaces exactly this seam, so it must be the real one."""
    monkeypatch.setattr(wire, "Client", lambda: "sentinel")
    assert _REAL_CLIENT() == "sentinel"
