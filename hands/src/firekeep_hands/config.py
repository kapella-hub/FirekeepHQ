"""Hands' two persisted JSON files: runtime settings (config.json) and the
allow-list policy (policy.json), both under `paths.hands_home()`.

Every load degrades to safe defaults on a missing or corrupt file rather than
raising — Hands starting up must never depend on these files being well
formed, only benefit from them when they are. Keys neither dataclass
declares (typically a newer client's field, on an older wheel after a
downgrade) are kept on the loaded instance and folded back in on the next
save, so round-tripping through an older build never silently drops them.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import threading
import uuid
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from firekeep_client import hooklog, state

from . import paths

_EXTRA_ATTR = "_extra"

# The one definition of what counts as a bool and what counts as an int, used
# by `_coerce_field` below when reading `config.json` and imported by
# `cli._coerce` when parsing `firekeep hands config set`. Two copies would be
# two answers to "is `"on"` true?", and the file a human edits by hand and the
# command they set a value with have to agree.
TRUE_WORDS = frozenset({"true", "1", "yes"})
FALSE_WORDS = frozenset({"false", "0", "no"})
INT_RE = re.compile(r"-?\d+")


@dataclass
class HandsConfig:
    chord: str = "ctrl+alt+y"
    deny_chord: str = "ctrl+alt+n"
    permit_ttl_s: int = 60
    # Off until relay records WHO completed a task. A relay task can be
    # completed by anyone holding the workspace key — the driving agent
    # included, through the same MCP surface it already has — so with this on,
    # the approval gate is only as strong as the key. See broker/phone.py.
    phone_approvals: bool = False
    max_steps: int = 400
    max_nodes: int = 200
    text_budget: int = 4000
    screenshot_max_width: int = 1280
    evidence_retention_days: int = 14
    browser: str = "auto"  # auto|chrome|edge


@dataclass
class Remembered:
    cls: str
    app: str
    match: str
    until: str  # ISO-8601 UTC


@dataclass
class Policy:
    apps: list[str]
    domains: list[str]
    remembered: list[Remembered]


def _known_field_names(cls) -> set[str]:
    return {f.name for f in fields(cls)}


def _split_known_extra(cls, raw: dict) -> tuple[dict, dict]:
    known_names = _known_field_names(cls)
    known = {k: v for k, v in raw.items() if k in known_names}
    extra = {k: v for k, v in raw.items() if k not in known_names}
    return known, extra


def _read_json(path: Path) -> dict | None:
    """The parsed object if `path` holds a JSON object, else None — for a
    missing file, unreadable file, invalid JSON, or JSON that isn't an
    object. Never raises; a read failure is logged, not propagated."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — any read/parse failure degrades to "absent"
        hooklog.log_failure("hands", f"could not read {path.name}: {exc}", exc)
        return None
    return data if isinstance(data, dict) else None


def _write_json_atomic(path: Path, payload: dict) -> None:
    """Temp file + os.replace in the same directory, then tighten
    permissions on both: a reader never sees a half-written file, and the
    settings never land world-readable even momentarily.

    The temp name carries a thread id and a random suffix, not just the pid.
    With the pid alone, two threads of one process raced for the same temp
    path and clobbered each other: measured on Windows on 2026-09-05, 48 of
    50 concurrent writes of `pending.json` failed with WinError 32 (the file
    is in use), each one swallowed by its caller and each one leaving the
    real file stale. Uniqueness per caller costs nothing and every writer
    here gets it, not just the one that was caught."""
    path.parent.mkdir(parents=True, exist_ok=True)
    unique = f"{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex[:8]}"
    tmp = path.parent / f"{path.name}.tmp-{unique}"
    try:
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        state._private(tmp)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    state._private(path)


def _coerce_field(default: object, value: object) -> object:
    """`value` as the type `default` is, or `_UNSET` when it cannot be.

    JSON has no schema and `config.json` is a file humans edit. A string where
    an int belongs used to reach `HandsConfig` untouched and raise far from
    its cause — `permit_ttl_s: "60"` inside `PermitStore.__init__`,
    `max_steps: "12"` as a TypeError in `_step_guard`, both a long way from
    the typo that caused them and both in flat contradiction of this module's
    own promise that every load degrades to safe defaults.

    The rules are `firekeep hands config set`'s rules, from the same
    `TRUE_WORDS`/`FALSE_WORDS`/`INT_RE` above, so the file a human edits and
    the command they set a value with agree. `bool` is tested before `int`
    because `bool` is an `int` subclass — and for the same reason a JSON
    `true` is NOT an integer: `max_steps: true` is a mistake, not a budget
    of 1."""
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in TRUE_WORDS:
                return True
            if lowered in FALSE_WORDS:
                return False
        return _UNSET
    if isinstance(default, int):
        if isinstance(value, bool):
            return _UNSET
        if isinstance(value, int):
            return value
        if isinstance(value, str) and INT_RE.fullmatch(value.strip()):
            return int(value.strip())
        return _UNSET
    if isinstance(default, str):
        return value if isinstance(value, str) else _UNSET
    return value


_UNSET = object()


def load_config() -> HandsConfig:
    """Never raises, and now that is true of the VALUES as well as the file.

    A value that cannot be coerced is dropped with a log line and the field
    keeps its default — which `save_config` then writes back over the bad one
    on the next save, healing the file rather than preserving the typo. That
    is the right direction: a setting Hands could not read is a setting that
    was not in force, and the file should say so."""
    raw = _read_json(paths.config_path()) or {}
    known, extra = _split_known_extra(HandsConfig, raw)
    defaults = HandsConfig()
    clean = {}
    for name, value in known.items():
        coerced = _coerce_field(getattr(defaults, name), value)
        if coerced is _UNSET:
            hooklog.log_failure(
                "hands",
                f"config.json: {name}={value!r} is not a "
                f"{type(getattr(defaults, name)).__name__} — using the default "
                f"{getattr(defaults, name)!r}",
            )
            continue
        clean[name] = coerced
    cfg = HandsConfig(**clean)
    setattr(cfg, _EXTRA_ATTR, extra)
    return cfg


def save_config(cfg: HandsConfig) -> None:
    payload = dataclasses.asdict(cfg)
    payload.update(getattr(cfg, _EXTRA_ATTR, {}))
    _write_json_atomic(paths.config_path(), payload)


def _remembered_list_from_raw(raw: Any) -> list[Remembered]:
    if not isinstance(raw, list):
        return []
    known_names = _known_field_names(Remembered)
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        known = {k: v for k, v in item.items() if k in known_names}
        try:
            out.append(Remembered(**known))
        except TypeError:
            continue  # missing a required field on this entry — drop it, not the whole list
    return out


def load_policy() -> Policy:
    raw = _read_json(paths.policy_path()) or {}
    known, extra = _split_known_extra(Policy, raw)
    apps = known.get("apps")
    domains = known.get("domains")
    pol = Policy(
        apps=list(apps) if isinstance(apps, list) else [],
        domains=list(domains) if isinstance(domains, list) else [],
        remembered=_remembered_list_from_raw(known.get("remembered")),
    )
    setattr(pol, _EXTRA_ATTR, extra)
    return pol


def save_policy(policy: Policy) -> None:
    payload = dataclasses.asdict(policy)
    payload.update(getattr(policy, _EXTRA_ATTR, {}))
    _write_json_atomic(paths.policy_path(), payload)
