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
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from firekeep_client import hooklog, state

from . import paths

_EXTRA_ATTR = "_extra"


@dataclass
class HandsConfig:
    chord: str = "ctrl+alt+y"
    deny_chord: str = "ctrl+alt+n"
    permit_ttl_s: int = 60
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
    settings never land world-readable even momentarily."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f"{path.name}.tmp-{os.getpid()}"
    try:
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        state._private(tmp)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    state._private(path)


def load_config() -> HandsConfig:
    raw = _read_json(paths.config_path()) or {}
    known, extra = _split_known_extra(HandsConfig, raw)
    cfg = HandsConfig(**known)
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
