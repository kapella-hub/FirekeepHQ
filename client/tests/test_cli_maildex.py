"""`firekeep maildex add|list|sync|remove` — the bridge onto the maildex wheel.

The bridge is deliberately thin: it translates the client CLI's positional-choices
shape into the wheel's own argv and delegates. What these tests pin is everything that
CAN drift between the two sides —

* the argv translation itself (a dropped `--folders` silently indexes the default
  folders instead of the ones a person asked for, and a password appearing in argv at
  all would violate M3);
* the import is LAZY, so a kit whose maildex wheel is absent still has a working
  `firekeep` for every other command;
* the human CLI works whether or not maildex is REGISTERED as a dex — registration
  gates the background trigger and the doctor accounting, never a human's control over
  their own mailboxes — and `add` registers it on the way past, so that gate is never
  something a person has to find out about.

Unlike test_cli_docdex, this suite delegates to a STUB rather than the real wheel: the
client must not import `firekeep_maildex` outside the lazy block, and the wheel is not
a dependency of this package — the deps-free client CI job has no copy of it. The stub
is installed in `sys.modules`, so it wins even where the real wheel happens to be
present, which keeps the argv assertions deterministic on every machine.
"""
from __future__ import annotations

import ast
import sys
import types
from pathlib import Path

import pytest

from firekeep_client import cli, dexes

NOT_INSTALLED = ("maildex is not installed — reinstall with the bootstrap or "
                 "`firekeep dex add maildex` on a bundled install")


@pytest.fixture
def maildex_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("FIREKEEP_LOG_DIR", str(tmp_path / "logs"))
    return tmp_path


@pytest.fixture
def delegated(monkeypatch):
    """Record what the bridge hands the wheel, without running it."""
    calls: list = []
    package = types.ModuleType("firekeep_maildex")
    module = types.ModuleType("firekeep_maildex.cli")
    module.main = lambda argv, **kw: calls.append((argv, kw)) or 0
    package.cli = module
    monkeypatch.setitem(sys.modules, "firekeep_maildex", package)
    monkeypatch.setitem(sys.modules, "firekeep_maildex.cli", module)
    return calls


def _out(capsys):
    captured = capsys.readouterr()
    return captured.out + captured.err


# --- argv translation -------------------------------------------------------


def test_bare_maildex_lists(maildex_home, delegated):
    assert cli.main(["maildex"]) == 0
    assert delegated[0][0] == ["list"]


def test_add_passes_the_host_and_username(maildex_home, delegated):
    assert cli.main(["maildex", "add", "imap.example.com", "you@example.com"]) == 0
    assert delegated[0][0] == ["add", "imap.example.com", "you@example.com"]


def test_add_never_carries_a_password(maildex_home):
    """M3: the app password is prompted for by the wheel and stored in the vault.
    The bridge has no flag that could put it in argv — where it would land in
    every process listing and shell history on the machine."""
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(
            ["maildex", "add", "imap.example.com", "you@example.com",
             "--password", "hunter2"])


def test_add_carries_the_folders(maildex_home, delegated):
    """A dropped --folders silently indexes INBOX + Sent instead of what the
    person asked for — the same class of translation bug as docdex's --shared."""
    assert cli.main(["maildex", "add", "imap.example.com", "you@example.com",
                     "--folders", "INBOX", "Archive"]) == 0
    assert delegated[0][0] == ["add", "imap.example.com", "you@example.com",
                               "--folders", "INBOX", "Archive"]


def test_add_carries_the_backfill_horizon(maildex_home, delegated):
    assert cli.main(["maildex", "add", "imap.example.com", "you@example.com",
                     "--backfill-days", "30"]) == 0
    assert delegated[0][0] == ["add", "imap.example.com", "you@example.com",
                               "--backfill-days", "30"]


def test_add_without_caps_leaves_the_defaults_to_the_wheel(maildex_home, delegated):
    """No flag means no flag: the disclosed defaults (INBOX + Sent, 90 days) are
    the wheel's to apply, and a bridge that pre-filled them would make the two
    sides two places to change one documented cap."""
    cli.main(["maildex", "add", "imap.example.com", "you@example.com"])
    assert "--folders" not in delegated[0][0]
    assert "--backfill-days" not in delegated[0][0]


def test_list_takes_no_arguments(maildex_home, delegated):
    assert cli.main(["maildex", "list"]) == 0
    assert delegated[0][0] == ["list"]


def test_sync_without_an_account_syncs_everything(maildex_home, delegated):
    assert cli.main(["maildex", "sync"]) == 0
    assert delegated[0][0] == ["sync"]


def test_sync_carries_an_account_id(maildex_home, delegated):
    assert cli.main(["maildex", "sync", "--account", "abc123"]) == 0
    assert delegated[0][0] == ["sync", "--account", "abc123"]


def test_remove_passes_the_id(maildex_home, delegated):
    assert cli.main(["maildex", "remove", "abc123"]) == 0
    assert delegated[0][0] == ["remove", "abc123"]


def test_the_wheel_is_told_how_the_user_invoked_it(maildex_home, delegated):
    """Usage and error lines must name `firekeep maildex`, not the console
    script the user never typed."""
    cli.main(["maildex", "list"])
    assert delegated[0][1] == {"prog": "firekeep maildex"}


def test_the_exit_code_comes_straight_from_the_wheel(maildex_home, monkeypatch):
    package = types.ModuleType("firekeep_maildex")
    module = types.ModuleType("firekeep_maildex.cli")
    module.main = lambda argv, **kw: 3
    package.cli = module
    monkeypatch.setitem(sys.modules, "firekeep_maildex", package)
    assert cli.main(["maildex", "sync"]) == 3


def test_a_wheel_that_exits_instead_of_returning_still_yields_an_int(
        maildex_home, monkeypatch):
    """argparse inside the wheel raises SystemExit; `firekeep` hands back an int
    from every command, and dispatch is what calls sys.exit."""
    package = types.ModuleType("firekeep_maildex")
    module = types.ModuleType("firekeep_maildex.cli")

    def exiting(argv, **kw):
        raise SystemExit(2)

    module.main = exiting
    package.cli = module
    monkeypatch.setitem(sys.modules, "firekeep_maildex", package)
    assert cli.main(["maildex", "list"]) == 2


# --- usage errors the bridge answers itself ---------------------------------


def test_add_without_a_host_is_a_usage_error(maildex_home, capsys):
    assert cli.main(["maildex", "add"]) == 2
    assert "host" in _out(capsys).lower()


def test_add_without_a_username_is_a_usage_error(maildex_home, capsys):
    """One positional short is the likeliest typo, and it must not reach the
    wheel as a half-formed account."""
    assert cli.main(["maildex", "add", "imap.example.com"]) == 2
    assert "username" in _out(capsys).lower()


def test_remove_without_an_id_is_a_usage_error(maildex_home, capsys):
    assert cli.main(["maildex", "remove"]) == 2
    assert "id" in _out(capsys).lower()


def test_an_unknown_action_is_rejected_by_the_parser(maildex_home):
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(["maildex", "enable"])


# --- the wheel is optional ---------------------------------------------------


def test_without_the_wheel_it_fails_with_the_repair_not_a_traceback(
    maildex_home, monkeypatch, capsys
):
    monkeypatch.setitem(sys.modules, "firekeep_maildex", None)
    assert cli.main(["maildex", "list"]) == 1
    assert NOT_INSTALLED in _out(capsys)


def test_the_import_is_lazy(maildex_home):
    """`firekeep_maildex` is an optional sibling wheel, so a module-level import
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
    assert "firekeep_maildex" not in module_level


# --- registration gates the background, never the human ----------------------


def test_the_human_cli_works_when_maildex_is_not_registered(maildex_home, delegated):
    assert dexes.read_registry() == {}
    assert cli.main(["maildex", "list"]) == 0
    assert delegated


def test_the_human_cli_works_when_maildex_is_registered(maildex_home, delegated):
    dexes.add("maildex")
    assert cli.main(["maildex", "list"]) == 0
    assert delegated


# --- add registers the dex (ROADMAP §5, 2026-08-19: ceremony retired) --------
#
# maildex is deliberately not in the default set symdex and docdex are now in:
# a connector with no account indexes nothing. Connecting the account is the
# moment it stops being inert, so that is where the registration goes — nobody
# should have to learn what a dex is to get the mail they just connected.


def test_add_registers_the_dex(maildex_home, delegated):
    assert dexes.read_registry() == {}
    assert cli.main(["maildex", "add", "imap.example.com", "you@example.com"]) == 0
    assert "maildex" in dexes.read_registry()


def test_add_registers_before_it_delegates(maildex_home, monkeypatch):
    """The ordering is what makes the wheel's own 'not registered, nothing syncs
    automatically' nudge disappear: the wheel asks the live registry at the end
    of its `add`, so registering afterwards would print the nudge and then make
    it false."""
    seen: list = []
    package = types.ModuleType("firekeep_maildex")
    module = types.ModuleType("firekeep_maildex.cli")
    module.main = lambda argv, **kw: seen.append(dict(dexes.read_registry())) or 0
    package.cli = module
    monkeypatch.setitem(sys.modules, "firekeep_maildex", package)
    monkeypatch.setitem(sys.modules, "firekeep_maildex.cli", module)

    assert cli.main(["maildex", "add", "imap.example.com", "you@example.com"]) == 0
    assert "maildex" in seen[0]


def test_add_says_it_registered_once(maildex_home, delegated, capsys):
    """On the run that changes something, and not on the ones that do not — a
    line repeated on every `add` is noise about a fact already acted on."""
    cli.main(["maildex", "add", "imap.example.com", "you@example.com"])
    assert "registered maildex" in _out(capsys)

    cli.main(["maildex", "add", "imap.example.com", "other@example.com"])
    assert "registered maildex" not in _out(capsys)


def test_add_keeps_the_original_stamp_on_a_second_account(maildex_home, delegated):
    cli.main(["maildex", "add", "imap.example.com", "you@example.com"])
    first = dexes.read_registry()["maildex"]["added_at"]
    cli.main(["maildex", "add", "imap.example.com", "other@example.com"])
    assert dexes.read_registry()["maildex"]["added_at"] == first


def test_add_leaves_other_dexes_alone(maildex_home, delegated):
    dexes.add("symdex")
    cli.main(["maildex", "add", "imap.example.com", "you@example.com"])
    assert sorted(dexes.read_registry()) == ["maildex", "symdex"]


def test_list_and_sync_do_not_register(maildex_home, delegated):
    """Only connecting a mailbox is consent to index one. Reading the accounts
    you already have, or syncing them by hand, is not — and a `sync` that
    registered would quietly re-enable the background job someone turned off."""
    assert cli.main(["maildex", "list"]) == 0
    assert cli.main(["maildex", "sync"]) == 0
    assert dexes.read_registry() == {}


def test_remove_does_not_register(maildex_home, delegated):
    assert cli.main(["maildex", "remove", "abc123"]) == 0
    assert dexes.read_registry() == {}


def test_a_usage_error_registers_nothing(maildex_home, capsys):
    """`maildex add` with a missing positional never reaches the wheel, so it
    must not leave a registration behind either."""
    assert cli.main(["maildex", "add", "imap.example.com"]) == 2
    assert dexes.read_registry() == {}


def test_a_missing_wheel_registers_nothing(maildex_home, monkeypatch, capsys):
    """Registering a dex whose wheel is absent buys a doctor `warn` row and a
    backend that cannot start — the exact state `firekeep dex add` refuses to
    create."""
    monkeypatch.setitem(sys.modules, "firekeep_maildex", None)
    assert cli.main(["maildex", "add", "imap.example.com", "you@example.com"]) == 1
    assert NOT_INSTALLED in _out(capsys)
    assert dexes.read_registry() == {}


def test_a_registry_that_cannot_be_written_does_not_block_the_account(
    maildex_home, delegated, monkeypatch, capsys
):
    """A read-only home costs the BACKGROUND sync, which one command repairs. It
    must not cost the human the thing they actually asked for — unlike `firekeep
    dex add`, where writing the registry IS the whole job."""
    def _boom(entries):
        raise OSError("read-only home")

    monkeypatch.setattr(dexes, "write_registry", _boom)
    assert cli.main(["maildex", "add", "imap.example.com", "you@example.com"]) == 0
    assert delegated[0][0] == ["add", "imap.example.com", "you@example.com"]
    out = _out(capsys)
    assert "could not register maildex" in out
    assert "firekeep dex add maildex" in out  # names the repair
