"""`firekeep dex list|add|remove` — the off-switch (ROADMAP §5).

Originally the suggestion-not-default FUNNEL: nothing was registered until a
person asked for it. The 2026-08-19 amendment reversed the default (symdex +
docdex register themselves) and left the command doing the same job from the
other side — it is now how you turn a dex OFF, and how you put one back.

Two things these pin that are easy to get wrong: `add` must PROVE the wheel is importable before writing a
registry entry (otherwise the next session mounts a backend that cannot start,
and the user's evidence is a silent missing tool), and every mutation must say
that it takes effect on the NEXT agent session — the gateway reads the registry
at startup, so nothing changes in the session doing the typing.
"""
from __future__ import annotations

import pytest

from firekeep_client import cli, dexes


@pytest.fixture
def dex_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("FIREKEEP_LOG_DIR", str(tmp_path / "logs"))
    # Default the wheel probe to "present" so tests that are about the REGISTRY
    # do not depend on what happens to be installed in the host venv (docdex's
    # wheel does not exist yet). The probe itself is tested separately.
    monkeypatch.setattr(dexes, "is_installed", lambda manifest: True)
    return tmp_path


@pytest.fixture
def registry_home(tmp_path, monkeypatch):
    """Like `dex_home`, but leaves `is_installed` at its real probe, for a test
    that wants to choose its own answer per manifest rather than take
    `dex_home`'s blanket True. A test using this fixture should still stub the
    probe for any dex whose state it asserts on: what is installed in the venv
    running the suite is not something a test can assume."""
    monkeypatch.setenv("FIREKEEP_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("FIREKEEP_LOG_DIR", str(tmp_path / "logs"))
    return tmp_path


def _out(capsys):
    captured = capsys.readouterr()
    return captured.out + captured.err


# --------------------------------------------------------------------------- #
# list                                                                          #
# --------------------------------------------------------------------------- #


def test_bare_dex_lists(dex_home, capsys):
    assert cli.main(["dex"]) == 0
    out = _out(capsys)
    assert "symdex" in out and "docdex" in out


def test_list_shows_both_states(dex_home, capsys):
    dexes.add("symdex")
    assert cli.main(["dex", "list"]) == 0
    lines = {line.split()[0]: line for line in _out(capsys).splitlines() if line.strip()}
    assert "registered" in lines["symdex"]
    assert "available" in lines["docdex"]


def test_list_carries_what_each_dex_indexes_and_why(dex_home, capsys):
    cli.main(["dex", "list"])
    out = _out(capsys)
    assert "code" in out and "documents" in out
    assert dexes.KNOWN_DEXES["docdex"].description.split("—")[0].strip() in out


def test_list_carries_maildex_and_what_it_indexes(dex_home, capsys):
    """A new dex must reach `dex list` with no change to the command: the list
    is KNOWN_DEXES, and a dex a person cannot see is a dex nobody turns on."""
    cli.main(["dex", "list"])
    out = _out(capsys)
    lines = {line.split()[0]: line for line in out.splitlines() if line.strip()}
    assert "email" in lines["maildex"]
    assert "available" in lines["maildex"]


def test_add_maildex_registers_it(dex_home, capsys):
    assert cli.main(["dex", "add", "maildex"]) == 0
    assert list(dexes.read_registry()) == ["maildex"]
    assert "next agent session" in _out(capsys)


def test_add_maildex_without_the_wheel_registers_nothing(dex_home, capsys, monkeypatch):
    monkeypatch.setattr(dexes, "is_installed", lambda manifest: False)
    assert cli.main(["dex", "add", "maildex"]) == 1
    assert "firekeep_maildex" in _out(capsys)
    assert dexes.read_registry() == {}


def test_list_frames_an_empty_registry_as_the_off_switch(dex_home, capsys):
    """Was `..._suggests_symdex_when_nothing_is_registered`. symdex and docdex
    now register themselves (ROADMAP §5, 2026-08-19), so an empty registry is
    something the user did, and `list` says so and names the way back — the same
    sentence `firekeep doctor` prints, from the same constant."""
    cli.main(["dex", "list"])
    out = _out(capsys)
    assert cli.EMPTY_REGISTRY_HINT in out
    assert "you removed them" in out
    assert "firekeep dex add symdex" in out and "firekeep dex add docdex" in out


def test_list_is_honest_about_a_missing_wheel(dex_home, capsys, monkeypatch):
    monkeypatch.setattr(dexes, "is_installed", lambda manifest: manifest.name != "docdex")
    cli.main(["dex", "list"])
    lines = {line.split()[0]: line for line in _out(capsys).splitlines() if line.strip()}
    assert "not installed" in lines["docdex"]


def test_list_reports_registry_entries_it_does_not_know(dex_home, capsys):
    """A `dex list` that hid a name actually sitting in dexes.json would be
    lying about the file it is reporting on."""
    dexes.write_registry({"webdex": {}})
    cli.main(["dex", "list"])
    assert "webdex" in _out(capsys)


# --------------------------------------------------------------------------- #
# add                                                                           #
# --------------------------------------------------------------------------- #


def test_add_registers_and_says_when_it_takes_effect(dex_home, capsys):
    assert cli.main(["dex", "add", "symdex"]) == 0
    assert list(dexes.read_registry()) == ["symdex"]
    assert "next agent session" in _out(capsys)


def test_add_is_idempotent_and_says_so(dex_home, capsys):
    cli.main(["dex", "add", "symdex"])
    capsys.readouterr()
    assert cli.main(["dex", "add", "symdex"]) == 0
    assert "already registered" in _out(capsys)


def test_add_unknown_name_is_rc_1_and_names_the_known_dexes(dex_home, capsys):
    assert cli.main(["dex", "add", "webdex"]) == 1
    out = _out(capsys)
    assert "webdex" in out and "symdex" in out and "docdex" in out
    assert dexes.read_registry() == {}


def test_add_without_the_wheel_fails_loudly_and_registers_nothing(
    dex_home, capsys, monkeypatch
):
    """The honest failure docdex will give until its wheel lands: refuse, name
    the fix, and leave the registry alone. Registering a dex whose code is
    absent would trade a clear error now for a mystery next session."""
    monkeypatch.setattr(dexes, "is_installed", lambda manifest: False)
    assert cli.main(["dex", "add", "docdex"]) == 1
    out = _out(capsys)
    assert "firekeep_docdex" in out
    assert "install" in out.lower()  # names the repair
    assert dexes.read_registry() == {}


def test_add_without_a_name_is_a_usage_error(dex_home, capsys):
    assert cli.main(["dex", "add"]) == 2
    assert dexes.read_registry() == {}


# --------------------------------------------------------------------------- #
# remove                                                                        #
# --------------------------------------------------------------------------- #


def test_remove_deregisters(dex_home, capsys):
    dexes.add("docdex")
    assert cli.main(["dex", "remove", "docdex"]) == 0
    assert dexes.read_registry() == {}
    assert "next agent session" in _out(capsys)


def test_remove_symdex_warns_about_what_is_lost_but_obeys(dex_home, capsys):
    """Names the capability being given up (code intelligence, in the plan's
    words) and then does it anyway — this is a choice, not a mistake to block."""
    dexes.add("symdex")
    assert cli.main(["dex", "remove", "symdex"]) == 0
    out = _out(capsys).lower()
    assert "index code" in out
    assert "brings it back" in out  # and how to undo it
    assert dexes.read_registry() == {}


def test_remove_does_not_require_the_wheel(dex_home, capsys, monkeypatch):
    """Removing must work on exactly the machine most likely to need it: one
    whose wheel is broken or gone."""
    dexes.add("symdex")
    monkeypatch.setattr(dexes, "is_installed", lambda manifest: False)
    assert cli.main(["dex", "remove", "symdex"]) == 0
    assert dexes.read_registry() == {}


def test_remove_unregistered_dex_is_a_no_op(dex_home, capsys):
    assert cli.main(["dex", "remove", "docdex"]) == 0
    assert "not registered" in _out(capsys)


def test_remove_unknown_name_is_rc_1(dex_home, capsys):
    assert cli.main(["dex", "remove", "webdex"]) == 1


def test_remove_without_a_name_is_a_usage_error(dex_home):
    assert cli.main(["dex", "remove"]) == 2


# --------------------------------------------------------------------------- #
# the wheel probe itself                                                        #
# --------------------------------------------------------------------------- #


def test_is_installed_probes_the_manifest_import_name():
    """Tested against real modules, since every other test here stubs it."""
    present = dexes.DexManifest(
        id="x", name="x", title="X", indexes="y", kind="mcp-stdio",
        console_script="x", import_probe="json", description="d",
    )
    absent = dexes.DexManifest(
        id="x", name="x", title="X", indexes="y", kind="mcp-stdio",
        console_script="x", import_probe="firekeep_no_such_dex", description="d",
    )
    assert dexes.is_installed(present) is True
    assert dexes.is_installed(absent) is False


def test_is_installed_never_raises_on_a_broken_probe():
    broken = dexes.DexManifest(
        id="x", name="x", title="X", indexes="y", kind="mcp-stdio",
        console_script="x", import_probe="", description="d",
    )
    assert dexes.is_installed(broken) is False


# --------------------------------------------------------------------------- #
# parser wiring                                                                 #
# --------------------------------------------------------------------------- #


def test_parser_rejects_an_unknown_action(dex_home):
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(["dex", "enable", "symdex"])


# --------------------------------------------------------------------------- #
# role — capabilities are labeled differently from indexes                     #
# --------------------------------------------------------------------------- #


def test_dex_list_labels_capabilities(registry_home, monkeypatch, capsys):
    """The point of this test is the VERB — `operates desktop` for a capability
    against `indexes code` for a dex — not which wheels happen to be in the
    venv running it.

    The wheel probe is therefore stubbed rather than trusted. It used to be
    left real, on the reasoning that hands' wheel "is not present": true on a
    clean CI runner, false on any machine with `hands/src` on `PYTHONPATH` or
    an editable install, where `_dex_state` reports `available` and the
    assertion failed for a reason that has nothing to do with what is being
    tested. Stubbing it also means the day CI installs the wheel — which the
    hands CI job now does — this test does not start failing.
    """
    from firekeep_client import cli
    monkeypatch.setattr(dexes, "is_installed", lambda manifest: manifest.name != "hands")
    cli.cmd_dex(type("A", (), {"action": "list"})())
    out = capsys.readouterr().out
    assert "hands  [not installed]  operates desktop" in out
    assert "symdex  [" in out and "indexes code" in out
