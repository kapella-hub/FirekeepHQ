"""`firekeep hands status|allow|chord|config|evidence` — the wheel side of
the client kit's `firekeep hands` command.

`firekeep_client.cli.cmd_hands` handles `enable`/`disable` itself (they touch
the kit's venv and dex registry) and translates every other action onto
`main(argv)` here: `argv[0]` is the action name, the rest are the words that
followed it on the command line. Deliberately absent: any way to approve or
deny a pending permit from here. A CLI a script could drive is exactly the
"human at a terminal, not a human at the keyboard" hole the broker's OS input
listener and phone bridge exist to close — see `broker/__init__.py`.

Nothing here touches an OS API at import time; `load_backend()` is only
called inside `_cmd_status`, and only ever wrapped in a broad `except`, so a
platform backend that cannot construct (a missing accessibility permission,
an absent optional dependency) is reported the same way `status` reports
anything else, not a stack trace.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
from pathlib import Path

from . import backends, paths
from .broker import parse_chord
from .broker.client import BrokerClient
from .config import HandsConfig, load_config, load_policy, save_config, save_policy

_RESTART_NOTE = "restart the broker to apply: `firekeep-hands-broker run`, or log out and in"
_TRUE_WORDS = {"true", "1", "yes"}
_FALSE_WORDS = {"false", "0", "no"}
_INT_RE = re.compile(r"-?\d+")


# -- argument parsing -------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="firekeep hands", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="backend, broker, policy and last-task summary")

    p_allow = sub.add_parser("allow", help="manage the app/domain allowlist")
    allow_sub = p_allow.add_subparsers(dest="allow_command")
    p_allow_app = allow_sub.add_parser("app")
    p_allow_app.add_argument("name")
    p_allow_domain = allow_sub.add_parser("domain")
    p_allow_domain.add_argument("host")
    allow_sub.add_parser("list")
    p_allow_forget = allow_sub.add_parser("forget")
    p_allow_forget.add_argument("cls")
    p_allow_forget.add_argument("app")
    p_allow_forget.add_argument("match")

    p_chord = sub.add_parser("chord", help="print or set the approve/deny chords")
    chord_sub = p_chord.add_subparsers(dest="chord_command")
    p_chord_set = chord_sub.add_parser("set")
    p_chord_set.add_argument("value")
    p_chord_set_deny = chord_sub.add_parser("set-deny")
    p_chord_set_deny.add_argument("value")

    p_config = sub.add_parser("config", help="print or edit config.json")
    config_sub = p_config.add_subparsers(dest="config_command")
    p_config_set = config_sub.add_parser("set")
    p_config_set.add_argument("key")
    p_config_set.add_argument("value")

    p_evidence = sub.add_parser("evidence", help="list tasks, or show one task's steps")
    p_evidence.add_argument("task_id", nargs="?", default=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse's own usage errors (bad subcommand, missing argument, -h)
        # already print their own message; just surface the exit code as a
        # return value so this function never raises.
        code = exc.code
        return 0 if code is None else (code if isinstance(code, int) else 1)

    handler = {
        "status": _cmd_status,
        "allow": _cmd_allow,
        "chord": _cmd_chord,
        "config": _cmd_config,
        "evidence": _cmd_evidence,
    }[args.command]
    try:
        return handler(args)
    except Exception as exc:  # noqa: BLE001 - a CLI command failing must report, not crash
        print(f"firekeep hands {args.command}: {exc}", file=sys.stderr)
        return 1


# -- status -----------------------------------------------------------------

def _cmd_status(args) -> int:
    print(f"platform: {sys.platform}")

    try:
        backend = backends.load_backend()
        perms = backend.permissions()
    except Exception as exc:  # noqa: BLE001 - a missing OS dependency is a status line, not a crash
        print(f"backend: unavailable ({exc})")
    else:
        print(f"backend: {backend.name}")
        for key in sorted(perms):
            print(f"  {key}: {perms[key]}")

    client = BrokerClient.from_disk()
    health = client.health() if client is not None else None
    if health is None:
        print("broker: not running — start it with `firekeep-hands-broker run` "
              "or re-run `firekeep hands enable`")
    else:
        listeners = health.get("listeners") or {}
        print(
            f"broker: chord {health.get('chord', '?')} ({listeners.get('chord', '?')}) · "
            f"phone {listeners.get('phone', '?')} · pending {health.get('pending', 0)}"
        )
        if listeners.get("phone") == "off":
            print("phone approvals are off — `firekeep hands config set phone_approvals true` "
                  "turns them on; docs/guides/hands.md explains what that trusts")

    policy = load_policy()
    print(f"policy: {len(policy.apps)} apps, {len(policy.domains)} domains, "
          f"{len(policy.remembered)} remembered")

    root = paths.evidence_root()
    tasks = _sorted_tasks(root)
    if not tasks:
        print("last task: none")
    else:
        started, task_id, data = tasks[0]
        outcome = data.get("outcome") or "running"
        steps = _step_count(root / task_id, data)
        print(f"last task: {task_id}  {started or '?'}  {outcome}  {steps} steps")
    return 0


# -- allow --------------------------------------------------------------

def _cmd_allow(args) -> int:
    command = getattr(args, "allow_command", None) or "list"
    policy = load_policy()

    if command == "app":
        if args.name not in policy.apps:
            policy.apps.append(args.name)
            save_policy(policy)
        print(f"allow: added app {args.name!r}")
        return 0

    if command == "domain":
        if args.host not in policy.domains:
            policy.domains.append(args.host)
            save_policy(policy)
        print(f"allow: added domain {args.host!r}")
        return 0

    if command == "list":
        print("apps:")
        for name in policy.apps:
            print(f"  {name}")
        print("domains:")
        for host in policy.domains:
            print(f"  {host}")
        print("remembered:")
        for entry in policy.remembered:
            print(f"  {entry.cls} · {entry.app} · {entry.match} · until {entry.until}")
        return 0

    if command == "forget":
        before = len(policy.remembered)
        policy.remembered = [
            entry for entry in policy.remembered
            if not (entry.cls == args.cls and entry.app == args.app and entry.match == args.match)
        ]
        removed = before - len(policy.remembered)
        if removed == 0:
            print(f"allow forget: no remembered entry matched {args.cls} {args.app} {args.match!r}",
                  file=sys.stderr)
            return 1
        save_policy(policy)
        print(f"allow forget: removed {removed} entr{'y' if removed == 1 else 'ies'}")
        return 0

    return 2  # unreachable: argparse's subparser choices already reject anything else


# -- chord --------------------------------------------------------------

def _cmd_chord(args) -> int:
    command = getattr(args, "chord_command", None)
    cfg = load_config()

    if command is None:
        print(f"chord (approve): {cfg.chord}")
        print(f"chord (deny):    {cfg.deny_chord}")
        return 0

    if command == "set":
        try:
            parse_chord(args.value)
        except ValueError as exc:
            print(f"chord set: {exc}", file=sys.stderr)
            return 2
        cfg.chord = args.value
        save_config(cfg)
        print(f"chord (approve) set to {args.value}")
        print(_RESTART_NOTE)
        return 0

    if command == "set-deny":
        try:
            parse_chord(args.value)
        except ValueError as exc:
            print(f"chord set-deny: {exc}", file=sys.stderr)
            return 2
        cfg.deny_chord = args.value
        save_config(cfg)
        print(f"chord (deny) set to {args.value}")
        print(_RESTART_NOTE)
        return 0

    return 2  # unreachable: argparse's subparser choices already reject anything else


# -- config -------------------------------------------------------------

def _coerce(field_type: type, raw: str):
    """Coerce `raw` to `field_type`, raising ValueError with a message fit to
    print directly — `_cmd_config` never needs to reword it. `bool` is
    checked ahead of `int` deliberately: `bool` is an `int` subclass, but a
    field whose current value is a bool must accept "true"/"false", not "0"
    silently meaning False and anything else meaning True."""
    if field_type is bool:
        lowered = raw.lower()
        if lowered in _TRUE_WORDS:
            return True
        if lowered in _FALSE_WORDS:
            return False
        raise ValueError(f"not a bool: {raw!r} (use true/false, 1/0, or yes/no)")
    if field_type is int:
        if not _INT_RE.fullmatch(raw):
            raise ValueError(f"not an int: {raw!r}")
        return int(raw)
    return raw


def _cmd_config(args) -> int:
    command = getattr(args, "config_command", None)
    cfg = load_config()

    if command is None:
        print(json.dumps(dataclasses.asdict(cfg), indent=2, sort_keys=True))
        return 0

    if command == "set":
        field_names = sorted(f.name for f in dataclasses.fields(HandsConfig))
        if args.key not in field_names:
            print(f"config set: unknown key {args.key!r} — fields: {', '.join(field_names)}",
                  file=sys.stderr)
            return 2
        current_type = type(getattr(cfg, args.key))
        try:
            value = _coerce(current_type, args.value)
        except ValueError as exc:
            print(f"config set: {exc}", file=sys.stderr)
            return 2
        setattr(cfg, args.key, value)
        save_config(cfg)
        print(f"set {args.key} = {value}")
        return 0

    return 2  # unreachable: argparse's subparser choices already reject anything else


# -- evidence -------------------------------------------------------------

def _read_task_json(task_dir: Path) -> dict:
    try:
        data = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a missing/corrupt task.json degrades to "unknown"
        return {}
    return data if isinstance(data, dict) else {}


def _sorted_tasks(root: Path) -> list[tuple[str, str, dict]]:
    """`(started, task_id, task.json)` for every task dir under `root`,
    newest `started` first. A task whose `started` is missing or unparsable
    sorts last, not first — an unknown time is never treated as "now"."""
    if not root.exists():
        return []
    rows = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        data = _read_task_json(child)
        started = data.get("started") if isinstance(data.get("started"), str) else ""
        rows.append((started, child.name, data))
    rows.sort(key=lambda row: row[0], reverse=True)
    return rows


def _step_count(task_dir: Path, data: dict) -> int:
    if isinstance(data.get("steps"), int):
        return data["steps"]
    steps_path = task_dir / "steps.jsonl"
    if not steps_path.exists():
        return 0
    return sum(1 for line in steps_path.read_text(encoding="utf-8").splitlines() if line.strip())


def _format_step(line: str) -> str:
    try:
        step = json.loads(line)
    except ValueError:
        return f"#? {line}"
    if not isinstance(step, dict):
        return f"#? {line}"
    action = step.get("action") if isinstance(step.get("action"), dict) else {}
    permit = step.get("permit") if isinstance(step.get("permit"), dict) else None
    before = step.get("before")
    after = step.get("after")
    return (
        f"#{step.get('step_index', '?')} {action.get('kind', '?')} [{step.get('route', '?')}] "
        f"{step.get('outcome', '?')} classes={','.join(step.get('classes') or [])} "
        f"permit={(permit.get('via') or '?') if permit else 'none'} "
        f"before={before[:8] if before else '-'} after={after[:8] if after else '-'}"
    )


def _cmd_evidence(args) -> int:
    root = paths.evidence_root()
    task_id = getattr(args, "task_id", None)

    if task_id is None:
        for started, tid, data in _sorted_tasks(root):
            outcome = data.get("outcome") or "running"
            steps = _step_count(root / tid, data)
            print(f"{tid}  {started or '?'}  {outcome}  {steps} steps")
        return 0

    task_dir = root / task_id
    task_json = task_dir / "task.json"
    if not task_dir.is_dir() or not task_json.exists():
        print(f"evidence: no such task {task_id!r}", file=sys.stderr)
        return 1

    data = _read_task_json(task_dir)
    outcome = data.get("outcome") or "running"
    print(f"{task_id}  {data.get('started', '?')}  {outcome}")
    if data.get("goal"):
        print(f"  goal: {data['goal']}")
    if data.get("apps"):
        print(f"  apps: {', '.join(data['apps'])}")
    if data.get("summary"):
        print(f"  summary: {data['summary']}")

    steps_path = task_dir / "steps.jsonl"
    if steps_path.exists():
        for line in steps_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                print(_format_step(line))
    return 0


if __name__ == "__main__":
    sys.exit(main())
