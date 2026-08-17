"""`firekeep-docdex` — the human's four verbs over the documents dex.

Thin on purpose. `sources.py` owns the folder registry, `sync.py` owns the
orchestration, and this module owns nothing but argv, exit codes and the words
a person reads. Anything decided here that is not one of those three is a bug
in the layering.

Three decisions do genuinely live here, because they are about a human at a
prompt rather than about the data:

* **A bare `sync` means every source.** `run_sync` refuses to guess — right for
  a library, wrong for someone typing.
* **`remove` marks the source even when the Keep is unreachable.** Refusing
  would leave a folder the human asked to be gone still syncing on the next
  run. The mark stops that immediately; the next successful sync finishes the
  removal.
* **Nothing raises out of `main`.** A traceback is not a user interface, and
  this same entrypoint is what the main CLI delegates to.

`firekeep docdex ...` in the client kit is a bridge onto this module (it passes
its own `prog`), so the behaviour is defined once and cannot drift between the
two ways a person can reach it.
"""
from __future__ import annotations

import argparse
import datetime
import sys

from . import sources, state, sync, wire

# What a visibility means, in the words a human chose it with.
_VISIBILITY = {
    sources.MEMBER: "private (only you, even on a shared Keep)",
    sources.WORKSPACE: "shared with your workspace",
}


def _me(args) -> str:
    """How the user reached this code — `firekeep-docdex` or `firekeep docdex`.

    Every message a person reads is prefixed with it, and every command this
    output suggests is spelled with it, so nothing here ever tells someone to
    run a command in a form they do not have."""
    return getattr(args, "prog", None) or "firekeep-docdex"


def _client() -> wire.Client:
    """The ONE place this CLI reaches for a server connection.

    A single seam so a failure to build one is handled identically by every
    command — and so the tests replace exactly what production uses."""
    return wire.Client()


def _unreachable(exc: Exception) -> str:
    return (f"cannot reach the Keep ({exc}) — is this machine enrolled? "
            f"`firekeep doctor` says which part is missing")


# --- add --------------------------------------------------------------------


def cmd_add(args) -> int:
    try:
        src = sources.add(args.path, shared=args.shared)
    except ValueError as exc:
        print(f"{_me(args)}: {exc}", file=sys.stderr)
        return 1
    print(f"{_me(args)}: added {src.path}\n"
          f"  id {src.id}\n"
          f"  {_VISIBILITY.get(src.visibility, src.visibility)}\n"
          f"  indexed on the next sync — `{_me(args)} sync` runs one now.")
    if not _dex_registered():
        # Folder control is human-only and works whether or not the dex is
        # registered; the BACKGROUND sync does not. A human who adds a folder
        # and never registers gets silence, so the nudge belongs here, once,
        # rather than in a doctor row they may never run.
        print(f"{_me(args)}: docdex is not registered on this machine, so "
              f"nothing syncs it automatically — `firekeep dex add docdex`.")
    return 0


def _dex_registered() -> bool:
    """Whether the dex registry has docdex turned on. Never raises: an
    unreadable registry must not fail an `add` that has already succeeded."""
    try:
        from firekeep_client import dexes

        return "docdex" in dexes.read_registry()
    except Exception:  # noqa: BLE001 - a nudge is not worth an exit code
        return True


# --- list -------------------------------------------------------------------


def cmd_list(args) -> int:
    registered = sources.list_sources()
    if not registered:
        print(f"{_me(args)}: no folders registered — add one with "
              f"`{_me(args)} add <path>`")
        return 0

    print(f"{_me(args)} — the folders this machine indexes into the Keep\n")
    for src in registered:
        st = state.read_state(src.id)
        counts = st.counts()
        print(f"  {src.id}")
        print(f"      {src.path}")
        print(f"      {_VISIBILITY.get(src.visibility, src.visibility)} · "
              f"{_plural(counts['files'], 'file')} · last sync {_ago(st.last_sync_at)}")
        detail = [
            _plural(counts["pending_deletes"], "pending delete"),
            _plural(counts["failures"], "failure"),
            _plural(counts["truncated"], "truncated file"),
        ]
        shown = [d for d, n in zip(detail, (counts["pending_deletes"],
                                            counts["failures"],
                                            counts["truncated"])) if n]
        if shown:
            print(f"      {' · '.join(shown)}")
        for warning in _warnings(src, st):
            print(f"      ! {warning}")
    return 0


def _warnings(src: sources.Source, st: state.SourceState) -> list[str]:
    out = []
    if src.missing:
        # The single most important thing `list` can say. A missing folder is
        # exactly the state a naive implementation would read as "every file
        # was deleted", and this one deliberately does not.
        out.append("the folder is MISSING right now — nothing has been deleted, "
                   "because a folder that cannot be read is not evidence that "
                   "its documents are gone")
    elif st.last_sync_at and not st.last_walk_completed:
        # The honest answer to "why did nothing get removed?", recorded by
        # state for precisely this line.
        out.append("the last walk did not complete — no deletions could be "
                   "inferred from it")
    if src.status == sources.PENDING_DELETE:
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
            args.source, all_sources=not args.source, quiet=args.quiet, client=client
        )
    except ValueError as exc:
        print(f"{_me(args)}: {exc}", file=sys.stderr)
        return 1
    return 0 if result["ok"] else 1


# --- remove -----------------------------------------------------------------


def cmd_remove(args) -> int:
    if sources.get(args.source_id) is None:
        print(f"{_me(args)}: unknown source: {args.source_id}\n"
              f"  `{_me(args)} list` shows the ids.", file=sys.stderr)
        return 1
    try:
        client = _client()
    except Exception as exc:  # noqa: BLE001
        # Mark anyway. The human's decision is the durable part; reaching the
        # server is not, and a source left ACTIVE would keep uploading a folder
        # they have already said should be gone.
        sources.remove_mark(args.source_id)
        print(f"{_me(args)}: {_unreachable(exc)}\n"
              f"  the source is marked for removal and will be deleted on the "
              f"next sync.", file=sys.stderr)
        return 1

    summary = sync.remove_source(args.source_id, client=client)
    if summary["status"] == "removed":
        print(f"{_me(args)}: removed {args.source_id} — "
              f"{_plural(summary['deleted'], 'corpus source')} deleted. "
              f"The folder on disk is untouched.")
        return 0
    for warning in summary["warnings"]:
        print(f"{_me(args)}: {warning}", file=sys.stderr)
    return 1


# --- the entrypoint ---------------------------------------------------------


def _build_parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Folders of documents, indexed into the Keep's corpus.",
    )
    sub = parser.add_subparsers(dest="action")

    add = sub.add_parser("add", help="register a folder to index")
    add.add_argument("path", help="folder to index")
    add.add_argument("--shared", action="store_true",
                     help="share with your workspace (default: private to you)")
    add.set_defaults(func=cmd_add)

    listing = sub.add_parser("list", help="registered folders, counts and staleness")
    listing.set_defaults(func=cmd_list)

    syncing = sub.add_parser("sync", help="scan registered folders and sync them now")
    syncing.add_argument("--source", help="sync one source by id")
    # Accepted and ignored: a bare `sync` already means every source, and the
    # detached background spawn passes --all explicitly. Rejecting the flag
    # would make the documented background command fail here.
    syncing.add_argument("--all", action="store_true", dest="all_sources",
                         help="sync every registered source (the default)")
    syncing.add_argument("--quiet", action="store_true", help="print nothing")
    syncing.set_defaults(func=cmd_sync)

    removing = sub.add_parser(
        "remove", help="delete a source and its corpus replicas (the folder is kept)")
    removing.add_argument("source_id", metavar="id", help="source id from `list`")
    removing.set_defaults(func=cmd_remove)
    return parser


def main(argv: list[str] | None = None, *, prog: str = "firekeep-docdex") -> int:
    """Every path returns an exit code. `prog` is what the user typed: the main
    CLI delegates here as `firekeep docdex`, and a usage line naming the console
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

        hooklog.log_failure("docdex", f"{args.action} failed: {exc}")
        print(f"{prog}: {args.action} failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised via `main`
    sys.exit(main())
