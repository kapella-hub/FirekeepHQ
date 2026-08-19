"""The dex registry — which domain indexes this Keep understands, and which of
them this machine has turned on.

A *dex* is a domain index: symdex indexes code, docdex indexes documents. Before
this module the gateway carried a hardcoded `LOCAL_SERVERS = ("symdex",
"decision")` tuple, which meant adding a second dex meant editing the gateway,
and turning one OFF meant editing nothing — there was no off. Two things live
here instead:

  * `KNOWN_DEXES` — the manifests this client ships knowledge of, written **as
    if public** (SDK ladder rung 1, ROADMAP §5): every field a third-party
    `dex.json` would need, and nothing client-internal. `kind` is the field the
    gateway reads: `mcp-stdio` mounts a backend, `ingest-client` mounts nothing
    (docdex spec §2 — it has no MCP server; its registry entry drives lifecycle,
    doctor and the sync trigger instead).
  * `~/.firekeep/dexes.json` — the INSTALLED registry: which of them this machine
    has registered. Registration gates ACTIVITY, not installation. The wheels
    stay always-installed and checksum-verified by the bootstrap, so the signed
    supply chain is untouched by anything in this file; `firekeep dex add` only
    decides whether the already-present code gets mounted.

Stdlib-only, like every module on the client spine (tests/test_import_boundary.py).
Reads never raise: a corrupt registry costs you your dexes for that session, and
must never cost you the session.
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import os
from dataclasses import dataclass
from pathlib import Path

from firekeep_client import hooklog, resolver, state

REGISTRY_NAME = "dexes.json"
_LOG = "dexes"


@dataclass(frozen=True)
class DexManifest:
    """One dex, described the way a third party would have to describe theirs.

    `name` is the registry key — the same string in `dexes.json`, in `firekeep
    dex add <name>`, and as the gateway backend's name. `import_probe` is the
    module `firekeep dex add` imports to prove the wheel is actually present
    before registering something that would then fail to mount.
    """

    id: str
    name: str
    title: str
    indexes: str
    kind: str
    console_script: str
    import_probe: str
    description: str


KNOWN_DEXES: dict[str, DexManifest] = {
    "symdex": DexManifest(
        id="firekeep.symdex",
        name="symdex",
        title="Symdex",
        indexes="code",
        kind="mcp-stdio",
        console_script="firekeep-symdex",
        import_probe="firekeep_symdex",
        description=(
            "Code intelligence — tree-sitter symbol index over your repos "
            "(symbols, callers, impact, architecture map)."
        ),
    ),
    "docdex": DexManifest(
        id="firekeep.docdex",
        name="docdex",
        title="Docdex",
        indexes="documents",
        kind="ingest-client",
        console_script="firekeep-docdex",
        import_probe="firekeep_docdex",
        description=(
            "Documents — folders you choose, extracted into recall. "
            "Private to you by default, even on a shared Keep."
        ),
    ),
    "maildex": DexManifest(
        id="firekeep.maildex",
        name="maildex",
        title="Maildex",
        indexes="email",
        kind="ingest-client",
        console_script="firekeep-maildex",
        import_probe="firekeep_maildex",
        description=(
            "Email — a mailbox you connect read-only, recent mail extracted into "
            "recall. Always private to you: never shared, and it cannot send."
        ),
    ),
}


def registry_path() -> Path:
    """`~/.firekeep/dexes.json`, beside the config it belongs to.

    Derived from `resolver._config_path()` rather than `Path.home()` so
    `FIREKEEP_CONFIG` isolates the registry exactly as it isolates the config and
    the personal marker (`resolver.personal_marker_path` does the same)."""
    return resolver._config_path().parent / REGISTRY_NAME


def read_registry() -> dict[str, dict]:
    """The installed registry, or `{}` when it is missing or unreadable.

    Never raises. A corrupt file is logged and treated as empty — the gateway
    calls this at startup, and a JSON typo in a hand-edited registry must cost a
    user their dexes for that session, never the session itself. Anything that
    is not a JSON object is 'unreadable': a list would otherwise iterate as a
    sequence of names and silently mean something else."""
    path = registry_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        hooklog.log_failure(_LOG, f"cannot read {path}: {exc}")
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        hooklog.log_failure(_LOG, f"{path} is not valid JSON ({exc}) — no dexes this session")
        return {}
    if not isinstance(data, dict):
        hooklog.log_failure(
            _LOG, f"{path} is JSON but not an object — no dexes this session"
        )
        return {}
    return data


def write_registry(entries: dict) -> None:
    """Persist the registry atomically and privately.

    Temp file in the SAME directory + `os.replace`, mirroring
    `state._write_atomic`: a gateway starting mid-write reads the old complete
    file or the new one, never a truncated object it would then log as corrupt.
    `sort_keys` keeps the file diffable — it is a user-editable dotfile.
    Raises on a write failure: unlike reads, a failed `dex add` must be loud."""
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(dict(entries), indent=2, sort_keys=True) + "\n"
    tmp = path.parent / f"{path.name}.tmp-{os.getpid()}"
    try:
        tmp.write_text(text, encoding="utf-8")
        state._private(tmp)  # 0600 (POSIX) / owner-only ACL (Windows), best-effort
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def registered() -> list[DexManifest]:
    """Manifests for the registered dexes this client knows, in KNOWN_DEXES order.

    A name in the file with no manifest here (a hand-edited entry, or a dex from
    a newer client after a rollback) is skipped rather than raised on: there is
    nothing to mount for a dex whose shape we do not know."""
    entries = read_registry()
    return [m for name, m in KNOWN_DEXES.items() if name in entries]


def is_installed(manifest: DexManifest) -> bool:
    """True when this dex's wheel is importable in THIS venv.

    `find_spec` rather than an import: the point is to prove the code is present
    without paying for (or being broken by) importing it — symdex drags
    tree-sitter, docdex drags pypdf. Never raises; a probe that cannot answer
    means 'absent', which is the safe direction: `dex add` refuses rather than
    registering a dex whose backend would fail to start next session."""
    try:
        return importlib.util.find_spec(manifest.import_probe) is not None
    except (ImportError, ValueError, TypeError):
        return False


def _manifest(name: str) -> DexManifest:
    manifest = KNOWN_DEXES.get(name)
    if manifest is None:
        known = ", ".join(KNOWN_DEXES)
        raise ValueError(f"unknown dex '{name}' (known dexes: {known})")
    return manifest


def _stamp() -> dict:
    return {
        "added_at": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        # `source` is where the CODE came from, not who asked for it. Everything
        # milestone 1 registers is a bundled, checksum-verified wheel; the
        # dev-mode side-loading rung (ROADMAP §5, SDK ladder rung 3) is what
        # will write anything else here, and doctor will mark it loudly.
        "source": "bundled",
    }


def add(name: str) -> DexManifest:
    """Register a dex. Idempotent — re-adding keeps the original `added_at`
    rather than churning the stamp (and the file's mtime) on every re-run."""
    manifest = _manifest(name)
    entries = read_registry()
    if name not in entries:
        entries[name] = _stamp()
        write_registry(entries)
    return manifest


def remove(name: str) -> DexManifest:
    """Deregister a dex. Idempotent; the wheel stays installed either way."""
    manifest = _manifest(name)
    entries = read_registry()
    if name in entries:
        del entries[name]
        write_registry(entries)
    return manifest


def ensure_migrated(*, installing: bool = False) -> None:
    """Seed the registry once, deterministically, asking the user nothing.

    The rule, in full — two lines now, where there used to be three:

      * `dexes.json` exists -> do nothing, ever. The user's choices are theirs:
        someone who ran `firekeep dex remove symdex` keeps it removed, across
        every update, forever.
      * absent -> write BOTH `{"symdex": ..., "docdex": ...}`. Fresh machine or
        long-configured one, the answer is the same.

    The second line used to fork on whether the config had a `[server]` section:
    an existing install grandfathered symdex (an update must never remove a
    capability an install already has), and a fresh one got `{}` — dexes were "a
    suggestion, not a default", and the two-question install must not grow a
    third question. The owner reversed that half (ROADMAP §5, amendment of
    2026-08-19 evening): the original reviewer recommendation of DEFAULT-ON
    stands vindicated, so Firekeep understands your code and your documents out
    of the box, and `firekeep dex` survives as the OFF-switch rather than as
    ceremony. Note what that makes of the first line: default-on is only
    defensible while removal STICKS, so "exists -> untouched" is now the rule
    carrying the weight, not the footnote.

    maildex is deliberately not in the default set. A connector with no account
    indexes nothing, so registering it here would buy a doctor row and no mail;
    `firekeep maildex add` registers it at the moment it becomes real.

    Called from `cmd_install` (installing=True) and from gateway startup, which
    is what covers an update that never re-ran install. `installing` does not
    change the rule — it names the caller in the failure log, so a support log
    distinguishes "install could not seed the registry" (the user was watching)
    from "the gateway could not" (nobody was, and the symptom is dexes that
    quietly never mount). Never raises: a registry that cannot be seeded leaves
    `read_registry()` returning `{}`, which is a degraded session, not a dead one.
    """
    caller = "install" if installing else "gateway"
    try:
        if registry_path().exists():
            return
        write_registry({"symdex": _stamp(), "docdex": _stamp()})
    except Exception as exc:  # noqa: BLE001 — seeding must not break start-up
        hooklog.log_failure(_LOG, f"registry migration failed during {caller}: {exc}")
