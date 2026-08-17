"""`firekeep docdex add|list|sync|remove` — the bridge onto the docdex wheel.

The bridge is deliberately thin: it translates the client CLI's
positional-choices shape into the wheel's own argv and delegates. What these
tests pin is everything that CAN drift between the two sides —

* the argv translation itself (a dropped `--shared` publishes a member's
  private notes to their whole workspace);
* the import is LAZY, so a kit whose docdex wheel is absent still has a working
  `firekeep` for every other command;
* the human CLI works whether or not docdex is REGISTERED as a dex —
  registration gates the background trigger and the doctor accounting, never a
  human's control over their own folders.
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

from firekeep_client import cli, dexes

# In a monorepo checkout the docdex wheel may not be pip-installed. Its sibling
# source dir is stdlib-only at every module this bridge touches (pypdf and
# python-docx are imported inside the extractors), so a path fallback lets these
# tests exercise the REAL package rather than a stub that could drift from it.
if importlib.util.find_spec("firekeep_docdex") is None:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "docdex" / "src"))

NOT_INSTALLED = ("docdex is not installed — reinstall with the bootstrap or "
                 "`firekeep dex add docdex` on a bundled install")


@pytest.fixture
def docdex_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("FIREKEEP_LOG_DIR", str(tmp_path / "logs"))
    return tmp_path


@pytest.fixture
def delegated(monkeypatch):
    """Record what the bridge hands the wheel, without running it."""
    from firekeep_docdex import cli as docdex_cli

    calls = []
    monkeypatch.setattr(docdex_cli, "main",
                        lambda argv, **kw: calls.append((argv, kw)) or 0)
    return calls


def _out(capsys):
    captured = capsys.readouterr()
    return captured.out + captured.err


# --- argv translation -------------------------------------------------------


def test_bare_docdex_lists(docdex_home, delegated):
    assert cli.main(["docdex"]) == 0
    assert delegated[0][0] == ["list"]


def test_add_passes_the_path(docdex_home, delegated, tmp_path):
    assert cli.main(["docdex", "add", str(tmp_path)]) == 0
    assert delegated[0][0] == ["add", str(tmp_path)]


def test_add_shared_carries_the_flag(docdex_home, delegated, tmp_path):
    """A dropped --shared is the one translation bug with a privacy blast
    radius: the folder would silently land as member-private instead of shared
    (or, reversed, the other way)."""
    assert cli.main(["docdex", "add", str(tmp_path), "--shared"]) == 0
    assert delegated[0][0] == ["add", str(tmp_path), "--shared"]


def test_list_takes_no_arguments(docdex_home, delegated):
    assert cli.main(["docdex", "list"]) == 0
    assert delegated[0][0] == ["list"]


def test_sync_without_a_source_syncs_everything(docdex_home, delegated):
    assert cli.main(["docdex", "sync"]) == 0
    assert delegated[0][0] == ["sync"]


def test_sync_carries_a_source_id(docdex_home, delegated):
    assert cli.main(["docdex", "sync", "--source", "abc123"]) == 0
    assert delegated[0][0] == ["sync", "--source", "abc123"]


def test_remove_passes_the_id(docdex_home, delegated):
    assert cli.main(["docdex", "remove", "abc123"]) == 0
    assert delegated[0][0] == ["remove", "abc123"]


def test_the_wheel_is_told_how_the_user_invoked_it(docdex_home, delegated):
    """Usage and error lines must name `firekeep docdex`, not the console
    script the user never typed."""
    cli.main(["docdex", "list"])
    assert delegated[0][1] == {"prog": "firekeep docdex"}


def test_the_exit_code_comes_straight_from_the_wheel(docdex_home, monkeypatch):
    from firekeep_docdex import cli as docdex_cli

    monkeypatch.setattr(docdex_cli, "main", lambda argv, **kw: 3)
    assert cli.main(["docdex", "sync"]) == 3


# --- usage errors the bridge answers itself ---------------------------------


def test_add_without_a_path_is_a_usage_error(docdex_home, capsys):
    assert cli.main(["docdex", "add"]) == 2
    out = _out(capsys)
    assert "folder" in out.lower()


def test_remove_without_an_id_is_a_usage_error(docdex_home, capsys):
    assert cli.main(["docdex", "remove"]) == 2
    assert "id" in _out(capsys).lower()


def test_an_unknown_action_is_rejected_by_the_parser(docdex_home):
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(["docdex", "enable"])


# --- the wheel is optional ---------------------------------------------------


def test_without_the_wheel_it_fails_with_the_repair_not_a_traceback(
    docdex_home, monkeypatch, capsys
):
    monkeypatch.setitem(sys.modules, "firekeep_docdex", None)
    assert cli.main(["docdex", "list"]) == 1
    assert NOT_INSTALLED in _out(capsys)


def test_the_import_is_lazy(docdex_home):
    """`firekeep_docdex` is an optional sibling wheel, so a module-level import
    in cli.py would take out every OTHER firekeep command on a kit that does not
    have it — and would break the stdlib-only client spine besides."""
    source = Path(cli.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    module_level = set()
    for node in tree.body:  # top level ONLY — nested imports are the point
        if isinstance(node, ast.Import):
            module_level.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            module_level.add(node.module.split(".")[0])
    assert "firekeep_docdex" not in module_level


# --- registration gates the background, never the human ----------------------


def test_the_human_cli_works_when_docdex_is_not_registered(docdex_home, delegated):
    assert dexes.read_registry() == {}
    assert cli.main(["docdex", "list"]) == 0
    assert delegated


def test_the_human_cli_works_when_docdex_is_registered(docdex_home, delegated):
    dexes.add("docdex")
    assert cli.main(["docdex", "list"]) == 0
    assert delegated


# --- against the real wheel --------------------------------------------------


def test_add_then_list_round_trips_through_the_real_wheel(
    docdex_home, tmp_path, capsys
):
    """No stub: the argv the bridge builds has to be argv the wheel accepts."""
    from firekeep_docdex import sources

    folder = tmp_path / "notes"
    folder.mkdir()
    assert cli.main(["docdex", "add", str(folder)]) == 0
    registered = sources.list_sources()
    assert len(registered) == 1
    assert registered[0].visibility == sources.MEMBER

    capsys.readouterr()
    assert cli.main(["docdex", "list"]) == 0
    assert registered[0].id in _out(capsys)


def test_shared_really_reaches_the_registry(docdex_home, tmp_path):
    from firekeep_docdex import sources

    folder = tmp_path / "runbooks"
    folder.mkdir()
    assert cli.main(["docdex", "add", str(folder), "--shared"]) == 0
    assert sources.list_sources()[0].visibility == sources.WORKSPACE


def test_a_bad_path_is_the_wheels_rc_1(docdex_home, tmp_path, capsys):
    assert cli.main(["docdex", "add", str(tmp_path / "nope")]) == 1
    assert "nope" in _out(capsys)
