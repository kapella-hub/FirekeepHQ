"""`firekeep-maildex` — the human's four verbs over the email dex.

Thin on purpose. `accounts.py` owns the registry, `vault.py` owns the secret,
`sync.py` owns the orchestration, and this module owns nothing but argv, exit
codes and the words a person reads.

Three decisions genuinely live here, because they are about a human at a prompt
rather than about the data:

* **The password is read with `getpass`, never from argv (M3).** There is no
  `--password` flag and there never will be: a secret on a command line is in
  the shell history, in `ps`, and in whatever collects either. The one escape
  hatch is `--password-stdin`, which reads a pipe — that is how automation and
  this wheel's own tests supply one without it ever becoming an argument.
* **A bare `sync` means every account.** `run_sync` refuses to guess — right
  for a library, wrong for someone typing.
* **`remove` marks the account even when the Keep is unreachable.** Refusing
  would leave a mailbox the human asked to be gone still syncing on the next
  run. The mark stops that immediately; the next successful sync finishes it.

Nothing raises out of `main`: a traceback is not a user interface, and this
same entrypoint is what `firekeep maildex` in the client kit delegates to.
"""
from __future__ import annotations

import argparse
import datetime
import getpass
import sys

from . import accounts, state, sync, vault, wire


def _me(args) -> str:
    """How the user reached this code — `firekeep-maildex` or `firekeep
    maildex`. Every message a person reads is prefixed with it, and every
    command this output suggests is spelled with it, so nothing here ever tells
    someone to run a command in a form they do not have."""
    return getattr(args, "prog", None) or "firekeep-maildex"


def _client() -> wire.Client:
    """The ONE place this CLI reaches for a server connection, so a failure to
    build one is handled identically by every command — and so the tests
    replace exactly what production uses.

    The ingest timeout is applied HERE as well as in the background spawn: a
    person typing `sync` sends the same large messages to the same synchronous
    embedder, and giving the foreground path the transport's 10s default would
    make the interactive command report "unreachable" on exactly the mail the
    background one indexes fine.
    """
    return wire.Client(timeout=float(sync.ingest_timeout()))


def _unreachable(exc: Exception) -> str:
    return (f"cannot reach the Keep ({exc}) — is this machine enrolled? "
            f"`firekeep doctor` says which part is missing")


# --- add --------------------------------------------------------------------


def _read_password(args) -> str:
    """The password, from a TTY prompt or from a pipe. Never from argv."""
    if args.password_stdin:
        # `.readline()`, not `.read()`: a here-string or an `echo |` carries a
        # trailing newline, and a password with an invisible newline welded to
        # it fails authentication in a way nobody can see.
        return sys.stdin.readline().rstrip("\r\n")
    return getpass.getpass("App password (input hidden): ")


def cmd_add(args) -> int:
    password = _read_password(args)
    if not password:
        print(f"{_me(args)}: no password given — nothing was registered.",
              file=sys.stderr)
        return 1

    try:
        account = accounts.add(
            args.host, args.username, port=args.port,
            folders=args.folders, backfill_days=args.backfill_days,
        )
    except ValueError as exc:
        print(f"{_me(args)}: {exc}", file=sys.stderr)
        return 1

    try:
        vault.store(account.id, password, call_tool=getattr(args, "call_tool", None))
    except vault.VaultError as exc:
        # Roll the registration back. A mailbox registered with no secret can
        # never sync, and its failure message would be about a missing vault
        # key rather than about what actually went wrong here.
        accounts.rollback(account.id)
        print(f"{_me(args)}: the app password could not be stored in the Keep's "
              f"vault ({exc})\n"
              f"  nothing was registered. Storing a secret is an admin operation "
              f"— ask whoever runs your Keep.", file=sys.stderr)
        return 1
    finally:
        password = None
        del password

    print(f"{_me(args)}: connected {account.username} at {account.host}:{account.port}\n"
          f"  id {account.id}\n"
          f"  folders {', '.join(account.folders)} · last {account.backfill_days} days\n"
          f"  private to you — mail is never shared with your workspace\n"
          f"  read-only: maildex cannot send, flag, move or delete anything\n"
          f"  indexed on the next sync — `{_me(args)} sync` runs one now.")
    if not _dex_registered():
        # Connecting a mailbox is human-only and works whether or not the dex is
        # registered; the BACKGROUND sync does not. A human who adds a mailbox
        # and never registers gets silence, so the nudge belongs here, once,
        # rather than in a doctor row they may never run.
        print(f"{_me(args)}: maildex is not registered on this machine, so "
              f"nothing syncs it automatically — `firekeep dex add maildex`.")
    return 0


def _dex_registered() -> bool:
    """Whether the dex registry has maildex turned on. Never raises: an
    unreadable registry must not fail an `add` that has already succeeded."""
    try:
        from firekeep_client import dexes

        return "maildex" in dexes.read_registry()
    except Exception:  # noqa: BLE001 - a nudge is not worth an exit code
        return True


# --- list -------------------------------------------------------------------


def cmd_list(args) -> int:
    registered = accounts.list_accounts()
    if not registered:
        print(f"{_me(args)}: no mailboxes connected — connect one with "
              f"`{_me(args)} add <host> <username>`")
        return 0

    print(f"{_me(args)} — the mailboxes this machine indexes into the Keep\n")
    for account in registered:
        st = state.read_state(account.id)
        counts = st.counts()
        print(f"  {account.id}")
        print(f"      {account.username} at {account.host}:{account.port}")
        print(f"      private to you · {', '.join(account.folders)} · "
              f"{_plural(counts['messages'], 'message')} · "
              f"last sync {_ago(st.last_sync_at)}")
        detail = [
            (counts["failures"], _plural(counts["failures"], "failure")),
            (counts["unparsed"], _plural(counts["unparsed"], "unindexable message")),
            (counts["truncated"], _plural(counts["truncated"], "truncated message")),
        ]
        shown = [text for n, text in detail if n]
        if shown:
            print(f"      {' · '.join(shown)}")
        for warning in _warnings(account, st):
            print(f"      ! {warning}")
    # M5, said every time the feature is described rather than only in the docs.
    print("\n  Mail deleted at your provider stays in the corpus until you "
          "`remove` and re-`add` the mailbox.")
    return 0


def _warnings(account: accounts.Account, st: state.AccountState) -> list[str]:
    out = []
    for name, fs in sorted(st.folders.items()):
        if fs.rebaselined_at:
            out.append(f"{name}: your provider rebuilt this folder, so it was "
                       f"re-indexed from scratch — the previous copies stay in "
                       f"the corpus under their old names")
    if account.status == accounts.PENDING_DELETE:
        out.append("pending removal — the next sync deletes its replicas and "
                   "forgets it")
    return out


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _ago(iso: str | None) -> str:
    """Relative, not absolute: "3h ago" answers the question a person is
    actually asking of a sync timestamp."""
    if not iso:
        return "never"
    try:
        when = datetime.datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    seconds = (datetime.datetime.now(datetime.timezone.utc) - when).total_seconds()
    if seconds < 0:
        return "just now"  # a clock that moved backwards is not worth a lecture
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return "just now"


# --- sync -------------------------------------------------------------------


def cmd_sync(args) -> int:
    try:
        client = _client()
    except Exception as exc:  # noqa: BLE001 - an unconfigured kit is not a crash
        print(f"{_me(args)}: {_unreachable(exc)}", file=sys.stderr)
        return 1
    try:
        result = sync.run_sync(
            args.account, all_accounts=not args.account, quiet=args.quiet,
            client=client, call_tool=getattr(args, "call_tool", None),
        )
    except ValueError as exc:
        print(f"{_me(args)}: {exc}", file=sys.stderr)
        return 1
    return 0 if result["ok"] else 1


# --- remove -----------------------------------------------------------------


def cmd_remove(args) -> int:
    if accounts.get(args.account_id) is None:
        print(f"{_me(args)}: unknown account: {args.account_id}\n"
              f"  `{_me(args)} list` shows the ids.", file=sys.stderr)
        return 1
    try:
        client = _client()
    except Exception as exc:  # noqa: BLE001
        # Mark anyway. The human's decision is the durable part; reaching the
        # server is not, and an account left ACTIVE would keep indexing a
        # mailbox they have already said should be gone.
        accounts.remove_mark(args.account_id)
        print(f"{_me(args)}: {_unreachable(exc)}\n"
              f"  the mailbox is marked for removal and will be deleted on the "
              f"next sync.", file=sys.stderr)
        return 1

    summary = sync.remove_account(args.account_id, client=client,
                                  call_tool=getattr(args, "call_tool", None))
    if summary["status"] == "removed":
        print(f"{_me(args)}: removed {args.account_id} — "
              f"{_plural(summary['deleted'], 'corpus source')} deleted. "
              f"Your mailbox at the provider is untouched.")
        for warning in summary["warnings"]:
            print(f"{_me(args)}: {warning}", file=sys.stderr)
        return 0
    for warning in summary["warnings"]:
        print(f"{_me(args)}: {warning}", file=sys.stderr)
    return 1


# --- the entrypoint ---------------------------------------------------------


def _build_parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="A mailbox connected read-only and indexed into the Keep's corpus.",
        epilog="Mail is always private to you. maildex cannot send, flag, move "
               "or delete anything — every mailbox is opened read-only.",
    )
    sub = parser.add_subparsers(dest="action")

    add = sub.add_parser("add", help="connect a mailbox (prompts for the app password)")
    add.add_argument("host", help="IMAP host, e.g. imap.gmail.com")
    add.add_argument("username", help="usually your email address")
    add.add_argument("--port", type=int, default=accounts.DEFAULT_PORT,
                     help=f"IMAP over TLS (default {accounts.DEFAULT_PORT})")
    # `nargs="+"` because the `firekeep maildex` bridge builds
    # `["--folders", *folders]` from its own `nargs="+"` option. Commas inside
    # any element are split too, so `--folders INBOX Archive` and
    # `--folders INBOX,Archive` both mean the same two folders.
    add.add_argument("--folders", nargs="+", metavar="FOLDER", default=None,
                     help=f"folders to index, space- or comma-separated "
                          f"(default {' '.join(accounts.DEFAULT_FOLDERS)})")
    add.add_argument("--backfill-days", type=int, default=None,
                     help=f"how far back to index on the first sync "
                          f"(default {accounts.DEFAULT_BACKFILL_DAYS})")
    # NOT `--password`. This reads a pipe; it never becomes an argument, so it
    # never reaches the shell history or the process list.
    add.add_argument("--password-stdin", action="store_true",
                     help="read the app password from stdin instead of prompting")
    add.set_defaults(func=cmd_add)

    listing = sub.add_parser("list", help="mailboxes, folders, counts, failures, staleness")
    listing.set_defaults(func=cmd_list)

    syncing = sub.add_parser("sync", help="read registered mailboxes and index new mail now")
    syncing.add_argument("--account", help="sync one account by id")
    # Accepted and ignored: a bare `sync` already means every account, and the
    # detached background spawn passes --all explicitly. Rejecting the flag
    # would make the documented background command fail here.
    syncing.add_argument("--all", action="store_true", dest="all_accounts",
                         help="sync every registered account (the default)")
    syncing.add_argument("--quiet", action="store_true", help="print nothing")
    syncing.set_defaults(func=cmd_sync)

    removing = sub.add_parser(
        "remove", help="delete a mailbox and its corpus replicas (your mail is kept)")
    removing.add_argument("account_id", metavar="id", help="account id from `list`")
    removing.set_defaults(func=cmd_remove)
    return parser


def main(argv: list[str] | None = None, *, prog: str = "firekeep-maildex") -> int:
    """Every path returns an exit code. `prog` is what the user typed: the main
    CLI delegates here as `firekeep maildex`, and a usage line naming the console
    script would point them at a command they never ran."""
    parser = _build_parser(prog)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse's own --help / usage errors
        return int(exc.code or 0)

    if getattr(args, "func", None) is None:
        parser.print_usage(sys.stderr)
        return 2
    args.prog = prog
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 - a traceback is not a user interface
        from firekeep_client import hooklog

        hooklog.log_failure("maildex", f"{args.action} failed: {exc}")
        print(f"{prog}: {args.action} failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised via `main`
    sys.exit(main())
