"""The four verbs a person types.

These drive `cli.main` through the REAL default seams — the vault reaches for
`firekeep_client.hooks._mcp.call_tool`, the IMAP layer reaches for
`imapio._default_connector` — with the fakes substituted at those seams rather
than at maildex's own function boundaries. A test that patched
`vault.retrieve` would pass while the production path was broken.
"""
from __future__ import annotations

import pytest
from conftest import PASSWORD, connector_for

from firekeep_maildex import accounts, cli, imapio, state, sync, vault, wire


@pytest.fixture(autouse=True)
def wired(monkeypatch, fake_vault, spy, endpoint, server):
    """Substitute the three things that would otherwise reach a network."""
    from firekeep_client.hooks import _mcp

    monkeypatch.setattr(_mcp, "call_tool", fake_vault)
    monkeypatch.setattr(imapio, "_default_connector", connector_for(spy))
    monkeypatch.setattr(
        cli, "_client",
        lambda: wire.Client(endpoint, post=server.post, delete=server.delete))
    monkeypatch.setattr(sync, "_make_client", cli._client)
    return fake_vault


@pytest.fixture
def typed(monkeypatch):
    """A password typed at the prompt."""
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": PASSWORD)


# --- add --------------------------------------------------------------------


def test_add_registers_the_mailbox_and_stores_the_secret(typed, fake_vault, capsys):
    assert cli.main(["add", "imap.example.com", "me@example.com"]) == 0
    account = accounts.list_accounts()[0]
    assert account.host == "imap.example.com"
    assert fake_vault.secrets[vault.vault_key(account.id)] == PASSWORD


def test_add_prompts_rather_than_taking_the_password_from_argv():
    """There is no `--password` flag, and there never will be: a secret on a
    command line is in the shell history and in `ps`."""
    parser = cli._build_parser("firekeep-maildex")
    add = parser._subparsers._group_actions[0].choices["add"]
    options = {o for action in add._actions for o in action.option_strings}
    assert "--password" not in options
    assert "--password-stdin" in options


def test_add_reads_a_piped_password_without_it_becoming_an_argument(
        monkeypatch, fake_vault, capsys):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert cli.main(["add", "imap.example.com", "me@example.com",
                     "--password-stdin"]) == 0
    account = accounts.list_accounts()[0]
    assert fake_vault.secrets[vault.vault_key(account.id)] == PASSWORD


def test_a_piped_password_keeps_no_trailing_newline(monkeypatch, fake_vault):
    """`echo | firekeep maildex add --password-stdin` carries one, and a
    password with an invisible newline welded to it fails authentication in a
    way nobody can see."""
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("s3cret\r\n"))
    cli.main(["add", "imap.example.com", "me@example.com", "--password-stdin"])
    assert list(fake_vault.secrets.values()) == ["s3cret"]


def test_an_empty_password_registers_nothing(monkeypatch, capsys):
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "")
    assert cli.main(["add", "imap.example.com", "me@example.com"]) == 1
    assert accounts.list_accounts() == []


def test_the_add_output_never_echoes_the_password(typed, capsys):
    cli.main(["add", "imap.example.com", "me@example.com"])
    captured = capsys.readouterr()
    assert PASSWORD not in captured.out + captured.err


def test_the_add_output_states_the_two_promises_that_matter(typed, capsys):
    """A person connecting a mailbox is deciding whether to trust this. The
    two things they need told are M1 and M2."""
    cli.main(["add", "imap.example.com", "me@example.com"])
    out = capsys.readouterr().out
    assert "private to you" in out
    assert "read-only" in out and "cannot send" in out


def test_add_honours_folders_and_backfill(typed):
    cli.main(["add", "imap.example.com", "me@example.com",
              "--folders", "INBOX,Archive", "--backfill-days", "30"])
    account = accounts.list_accounts()[0]
    assert account.folders == ("INBOX", "Archive")
    assert account.backfill_days == 30


def test_a_vault_refusal_rolls_the_registration_back(typed, fake_vault, capsys):
    """A mailbox registered with no secret can never sync, and its failure
    message would be about a missing vault key rather than about what actually
    went wrong."""
    fake_vault.refuse_store = True
    assert cli.main(["add", "imap.example.com", "me@example.com"]) == 1
    assert accounts.list_accounts() == []
    assert "admin" in capsys.readouterr().err


def test_a_duplicate_mailbox_is_refused_before_the_vault_is_touched(
        typed, fake_vault, capsys):
    cli.main(["add", "imap.example.com", "me@example.com"])
    stores = len([c for c in fake_vault.calls if c[1] == "vault_store"])
    assert cli.main(["add", "imap.example.com", "me@example.com"]) == 1
    assert len([c for c in fake_vault.calls if c[1] == "vault_store"]) == stores
    assert "already registered" in capsys.readouterr().err


def test_add_nudges_when_the_dex_is_not_registered(typed, monkeypatch, capsys):
    from firekeep_client import dexes

    monkeypatch.setattr(dexes, "read_registry", lambda: {})
    cli.main(["add", "imap.example.com", "me@example.com"])
    assert "firekeep dex add maildex" in capsys.readouterr().out


def test_an_unreadable_registry_does_not_fail_an_add_that_succeeded(
        typed, monkeypatch):
    from firekeep_client import dexes

    def boom():
        raise OSError("permission denied")

    monkeypatch.setattr(dexes, "read_registry", boom)
    assert cli.main(["add", "imap.example.com", "me@example.com"]) == 0


# --- list -------------------------------------------------------------------


def test_list_with_nothing_registered_says_how_to_start(capsys):
    assert cli.main(["list"]) == 0
    assert "no mailboxes connected" in capsys.readouterr().out


def test_list_shows_the_mailbox_its_folders_and_its_staleness(typed, capsys):
    cli.main(["add", "imap.example.com", "me@example.com"])
    capsys.readouterr()
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "me@example.com at imap.example.com:993" in out
    assert "INBOX, Sent" in out
    assert "last sync never" in out
    assert "private to you" in out


def test_list_discloses_the_m5_gap_every_time(typed, capsys):
    """Provider-side deletions are not mirrored in round 1. Saying it only in
    the docs is how a disclosed gap becomes an undisclosed one."""
    cli.main(["add", "imap.example.com", "me@example.com"])
    capsys.readouterr()
    cli.main(["list"])
    out = capsys.readouterr().out
    assert "deleted at your provider stays in the corpus" in out


def test_list_shows_counts_after_a_sync(typed, capsys):
    cli.main(["add", "imap.example.com", "me@example.com"])
    cli.main(["sync", "--quiet"])
    capsys.readouterr()
    cli.main(["list"])
    out = capsys.readouterr().out
    assert "3 messages" in out
    assert "last sync just now" in out


def test_list_explains_a_rebuilt_folder(typed, capsys):
    cli.main(["add", "imap.example.com", "me@example.com"])
    account = accounts.list_accounts()[0]
    st = state.read_state(account.id)
    state.reconcile(st, "INBOX", 900)
    state.reconcile(st, "INBOX", 901)
    state.write_state(account.id, st)
    capsys.readouterr()
    cli.main(["list"])
    assert "rebuilt this folder" in capsys.readouterr().out


def test_list_flags_a_mailbox_pending_removal(typed, capsys):
    cli.main(["add", "imap.example.com", "me@example.com"])
    accounts.remove_mark(accounts.list_accounts()[0].id)
    capsys.readouterr()
    cli.main(["list"])
    assert "pending removal" in capsys.readouterr().out


def test_list_never_shows_a_password(typed, fake_vault, capsys):
    cli.main(["add", "imap.example.com", "me@example.com"])
    capsys.readouterr()
    cli.main(["list"])
    captured = capsys.readouterr()
    assert PASSWORD not in captured.out + captured.err


# --- sync -------------------------------------------------------------------


def test_a_bare_sync_means_every_mailbox(typed, server, capsys):
    cli.main(["add", "imap.example.com", "me@example.com"])
    assert cli.main(["sync", "--quiet"]) == 0
    assert len(server.posts) == 3


def test_sync_can_target_one_mailbox(typed, server):
    cli.main(["add", "imap.example.com", "me@example.com"])
    account = accounts.list_accounts()[0]
    assert cli.main(["sync", "--account", account.id, "--quiet"]) == 0
    assert len(server.posts) == 3


def test_sync_of_an_unknown_account_is_an_error_not_a_traceback(capsys):
    assert cli.main(["sync", "--account", "nope", "--quiet"]) == 1
    assert "unknown account" in capsys.readouterr().err


def test_the_documented_background_command_is_accepted(typed):
    """`python -m firekeep_maildex.sync --all --quiet` is what the session-start
    trigger spawns; `--all` must not be rejected by the CLI's own parser."""
    cli.main(["add", "imap.example.com", "me@example.com"])
    assert cli.main(["sync", "--all", "--quiet"]) == 0


def test_a_failing_sync_returns_a_nonzero_exit_code(typed, server):
    from conftest import TransportFailure

    cli.main(["add", "imap.example.com", "me@example.com"])
    server.post_hook = lambda i, u, b: (_ for _ in ()).throw(
        TransportFailure("connection refused"))
    assert cli.main(["sync", "--quiet"]) == 1


# --- remove -----------------------------------------------------------------


def test_remove_deletes_the_replicas_and_says_the_mailbox_is_untouched(
        typed, server, capsys):
    cli.main(["add", "imap.example.com", "me@example.com"])
    account = accounts.list_accounts()[0]
    cli.main(["sync", "--quiet"])
    capsys.readouterr()
    assert cli.main(["remove", account.id]) == 0
    out = capsys.readouterr().out
    assert "3 corpus sources deleted" in out
    assert "untouched" in out
    assert accounts.get(account.id) is None


def test_remove_forgets_the_stored_password(typed, fake_vault):
    cli.main(["add", "imap.example.com", "me@example.com"])
    account = accounts.list_accounts()[0]
    cli.main(["remove", account.id])
    assert fake_vault.secrets == {}


def test_remove_of_an_unknown_id_points_at_list(capsys):
    assert cli.main(["remove", "nope"]) == 1
    assert "list` shows the ids" in capsys.readouterr().err


def test_remove_marks_the_mailbox_even_when_the_keep_is_unreachable(
        typed, monkeypatch, capsys):
    """Refusing would leave a mailbox the human asked to be gone still indexing
    on the next run."""
    cli.main(["add", "imap.example.com", "me@example.com"])
    account = accounts.list_accounts()[0]

    def no_keep():
        raise RuntimeError("this machine is not enrolled")

    monkeypatch.setattr(cli, "_client", no_keep)
    assert cli.main(["remove", account.id]) == 1
    assert accounts.get(account.id).status == accounts.PENDING_DELETE
    assert "marked for removal" in capsys.readouterr().err


# --- the entrypoint ---------------------------------------------------------


def test_a_bare_invocation_prints_usage():
    assert cli.main([]) == 2


def test_help_exits_zero(capsys):
    assert cli.main(["--help"]) == 0


def test_the_help_text_states_the_read_only_promise(capsys):
    cli.main(["--help"])
    out = capsys.readouterr().out
    assert "read-only" in out and "private to you" in out


def test_the_prog_name_is_what_the_user_typed(typed, capsys):
    """`firekeep maildex` delegates here; a usage line naming the console
    script would point them at a command they never ran."""
    cli.main(["list"], prog="firekeep maildex")
    assert "firekeep maildex" in capsys.readouterr().out


def test_nothing_raises_out_of_main(monkeypatch, capsys):
    """A traceback is not a user interface — and this same entrypoint is what
    `firekeep maildex` delegates to."""
    def boom(args):
        raise RuntimeError("something deep failed")

    # `_build_parser` resolves `cmd_list` as a module global at call time, so
    # patching the attribute is what the parser will hand to `main`.
    monkeypatch.setattr(cli, "cmd_list", boom)
    assert cli.main(["list"]) == 1
    assert "list failed" in capsys.readouterr().err
