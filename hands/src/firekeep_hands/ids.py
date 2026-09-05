"""The three identifiers Hands hashes or persists: a stable per-machine id,
a content hash of a proposed action, and the challenge id the approval
broker and the requesting session both derive independently to agree they
are talking about the same pending step.
"""
from __future__ import annotations

import hashlib
import json
import secrets

from firekeep_client import state

from . import paths


def machine_id() -> str:
    """32 hex chars, generated once and persisted 0600. Stable across
    processes (and across Hands restarts) because everything that needs to
    prove "this machine" — challenge ids, evidence provenance — needs the
    same value every time, not a fresh one per run."""
    path = paths.machine_id_path()
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    value = secrets.token_hex(16)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    state._private(path)
    return value


def action_hash(action: dict) -> str:
    """A short, order-independent fingerprint of a proposed action.
    sort_keys makes {"a": 1, "b": 2} and {"b": 2, "a": 1} hash identically;
    the compact separators keep the encoding canonical (no incidental
    whitespace differences)."""
    encoded = json.dumps(action, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def challenge_id_for(machine: str, session: str, task: str, step_index: int, ahash: str) -> str:
    """Deterministic id for one pending approval: both sides (the session
    proposing the action and the broker granting or denying it) compute this
    from the same five fields and compare, rather than one side minting an id
    and handing it to the other — so a mismatched machine/session/task/step
    cannot be papered over by trusting whichever side spoke first."""
    raw = f"hands|{machine}|{session}|{task}|{step_index}|{ahash}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
