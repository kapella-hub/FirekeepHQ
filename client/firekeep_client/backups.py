"""`firekeep backup` — the workstation's view of the Keep's server-side backups.

The server takes a cold snapshot every night, keeps 7 nightly + 4 weekly, and
writes a `manifest.json` describing every artifact. None of that helps if the
one disk holding the Keep also holds the only copy, so this module is the
off-box half: it reads status with the member key, and — once a human has
linked a deployment ADMIN key — pulls whole backups down and verifies them.

Four things here are decisions rather than mechanics:

* **`link` verifies before it stores.** The failure this command exists to
  prevent is discovering at disaster time that the stored key never worked. So
  the candidate key is spent on a real admin-gated fetch first, and a key that
  fails is never written anywhere.
* **`pull` verifies EVERY sha256 and there is no resume.** A truncated archive
  that sits on disk looking complete is worse than no archive, because it is
  the one you reach for. A mismatch deletes the file and fails loudly.
* **`status` never claims freshness it cannot know.** The server can say when
  it last backed up; only this machine knows when a copy last left the server.
  Both numbers are printed, and the second one is the honest one.
* **`restore` prints steps and runs nothing.** Restore happens on the host with
  the stack down. This module deliberately imports no process-spawning
  machinery at all (a test pins that), so it cannot quietly grow the habit of
  reaching onto someone's server.

Stdlib-only, like the rest of the client spine. `get_json` / `get_file` are
module-level names so tests can substitute a transport without a live server.
"""
from __future__ import annotations

import configparser
import getpass
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

from firekeep_client import resolver
from firekeep_client.transport import DEFAULT_FILE_TIMEOUT, TransportError, get_file, get_json

SECTION = "backup"
KEY_OPTION = "admin_key"
PULL_STATE_NAME = "backups-pull.json"
STATUS_PATH = "/ops/backups"
MANIFEST_NAME = "manifest.json"
PULL_PREFIX = "firekeep-backup-"
KEEP_LOCAL_PULLS = 3
DEFAULT_DEST_NAME = "FirekeepBackups"
# The doctor row's budget. Doctor is what people run when the server is the
# broken thing, so no row of it may hang on a server that is not answering.
DOCTOR_TIMEOUT = 5.0
STALE_AFTER_SECONDS = 36 * 3600


class BackupError(Exception):
    """Anything the user needs told in one line, with no traceback."""


# --- where things live -------------------------------------------------------

def firekeep_home() -> Path:
    """`~/.firekeep`, derived from the resolver's config path rather than
    `Path.home()` so `FIREKEEP_CONFIG` relocates the whole kit coherently."""
    return resolver._config_path().parent


def pull_state_path() -> Path:
    return firekeep_home() / PULL_STATE_NAME


def default_dest() -> Path:
    return Path.home() / DEFAULT_DEST_NAME


def pull_dir_name(stamp: str) -> str:
    return f"{PULL_PREFIX}{stamp}"


# --- the stored admin key ----------------------------------------------------

def admin_key() -> str | None:
    """The linked deployment admin key, or None. Read RAW (never `load_config`)
    so merely asking whether this machine is linked cannot migrate or rewrite
    somebody's config."""
    value = resolver._raw_config().get(SECTION, KEY_OPTION, fallback="").strip()
    return value or None


def store_admin_key(key: str) -> Path:
    """Persist `[backup] admin_key`, 0600, without disturbing any other section.

    The same whole-file round-trip `resolver.set_generic_agents_md` uses:
    [server], [identity] and [pins] are re-serialized alongside, because
    truncating a connection while storing a credential would break the machine
    in the act of protecting it."""
    path = resolver._config_path()
    cfg = resolver._raw_config(path)
    if not cfg.has_section(SECTION):
        cfg.add_section(SECTION)
    cfg.set(SECTION, KEY_OPTION, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        cfg.write(fh)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def _read_key() -> str:
    """Prompt for the admin key. Hidden, because it is a credential and the
    alternative is leaving it in shell scrollback.

    The no-TTY refusal lives HERE rather than in the caller so that the whole
    "where does the key come from" question is one function: a script, a cron
    entry or a CI step gets told which flag to use instead of blocking on a
    prompt nobody will ever see."""
    if not sys.stdin.isatty():
        raise BackupError("no TTY to prompt on — pass the key with "
                          "`firekeep backup link --key <ADMIN KEY>`")
    return getpass.getpass("Firekeep deployment ADMIN key: ").strip()


# --- the last-pull record ----------------------------------------------------

def read_pull_state() -> dict:
    """What this machine last pulled, or `{}`. Never raises: a corrupt record
    means "unknown", which `status` already knows how to say."""
    try:
        data = json.loads(pull_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_pull_state(stamp: str, path: Path) -> None:
    record = {
        "stamp": stamp,
        "path": str(Path(path)),
        "at": datetime.now(timezone.utc).isoformat(),
    }
    target = pull_state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target.parent,
            prefix=f".{target.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temp_name = handle.name
            json.dump(record, handle, indent=2)
        try:
            os.chmod(temp_name, 0o600)
        except OSError:
            pass
        os.replace(temp_name, target)
        temp_name = ""
    finally:
        if temp_name:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass


# --- formatting --------------------------------------------------------------

def humanize_age(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    for size, unit in ((86400.0, "d"), (3600.0, "h"), (60.0, "m")):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return "just now"


def humanize_bytes(count) -> str:
    try:
        value = float(count)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0
    return f"{value:.1f} TiB"  # pragma: no cover - unreachable, loop returns


def _last_pull_phrase() -> str:
    state = read_pull_state()
    stamp = str(state.get("stamp") or "")
    at = str(state.get("at") or "")
    if not stamp:
        return "never"
    when = _age_of(at)
    return f"{stamp} ({humanize_age(when)})" if when is not None else stamp


def _age_of(iso: str) -> float | None:
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - when).total_seconds())


# --- talking to the server ---------------------------------------------------

def _endpoint(cfg=None):
    return resolver.resolve("cortex", cfg=cfg)


def fetch_status(cfg=None, *, timeout: float | None = None) -> dict:
    """The member-readable status payload. Raises BackupError with the server's
    own words when it cannot be had."""
    try:
        ep = _endpoint(cfg)
    except resolver.ConfigError as exc:
        raise BackupError(str(exc)) from exc
    url = f"{ep.rest_base}{STATUS_PATH}"
    kwargs = {"headers": ep.headers, "verify": ep.verify}
    if timeout is not None:
        kwargs["timeout"] = timeout
    try:
        payload = get_json(url, **kwargs)
    except (TransportError, OSError) as exc:
        raise BackupError(str(exc)) from exc
    if not isinstance(payload, dict):
        raise BackupError(f"GET {url} returned {type(payload).__name__}, expected an object")
    return payload


def entries(payload: dict) -> list[dict]:
    listed = payload.get("backups")
    return [b for b in listed if isinstance(b, dict)] if isinstance(listed, list) else []


def indexed_newest_first(payload: dict) -> list[dict]:
    """Backups with a manifest, newest first.

    The endpoint already sorts, and this re-sorts by `age_seconds` anyway when
    every entry carries one: `pull` takes the head of this list, so a listing
    that arrived in an unexpected order would silently fetch the WRONG backup —
    the one class of bug here with no visible symptom."""
    listed = [b for b in entries(payload) if b.get("indexed") and b.get("stamp")]
    if all(isinstance(b.get("age_seconds"), (int, float)) for b in listed):
        listed.sort(key=lambda b: b["age_seconds"])
    return listed


def newest_indexed(payload: dict) -> dict | None:
    """The newest backup with a manifest. Unindexed dirs are real and rotation
    protects them, but they have no manifest, so nothing can be verified about
    them — they are listed, never downloaded."""
    listed = indexed_newest_first(payload)
    return listed[0] if listed else None


def _admin_headers(ep, key: str) -> dict[str, str]:
    """The member headers with the admin key substituted. Everything else —
    X-Agent-Id, the X-Firekeep-* attribution — is unchanged, so an admin fetch
    is still attributable to the machine that made it."""
    headers = dict(ep.headers)
    headers["X-API-Key"] = key
    return headers


def _artifact_url(ep, stamp: str, name: str) -> str:
    return f"{ep.rest_base}{STATUS_PATH}/{quote(stamp, safe='')}/{quote(name, safe='')}"


def _safe_name(name) -> str:
    """Refuse a manifest filename that is anything but a plain filename.

    The names come off the wire, and every one of them is about to become a
    path under the user's `--dest`. A server (or anything that can answer as
    one) offering `../../.ssh/authorized_keys` must get an error, not a write."""
    text = str(name or "")
    if not text or text in (".", "..") or text != Path(text).name or os.sep in text or (
            os.altsep and os.altsep in text):
        raise BackupError(f"refusing manifest entry with an unsafe filename: {name!r}")
    return text


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def server_host(cfg) -> str:
    """The host a person would scp to. Best-effort: an unreadable config yields
    a visible placeholder rather than a wrong hostname in a runbook."""
    try:
        kind = cfg.get("server", "kind", fallback="").strip().lower()
        if kind == "paths":
            return urlparse(cfg.get("server", "base_url", fallback="")).hostname or "YOUR-SERVER"
        return cfg.get("server", "host", fallback="").strip() or "YOUR-SERVER"
    except (configparser.Error, ValueError):
        return "YOUR-SERVER"


# --- actions -----------------------------------------------------------------

def action_status(args) -> int:
    try:
        payload = fetch_status()
    except BackupError as exc:
        print(f"firekeep backup: {exc}", file=sys.stderr)
        return 1
    listed = entries(payload)
    indexed = [b for b in listed if b.get("indexed")]
    policy = str(payload.get("policy") or "unknown")

    print("firekeep backup — snapshots of the Keep, on the server")
    newest = newest_indexed(payload)
    if newest is None and not listed:
        print("  newest   none — nothing has been backed up yet"
              + ("" if payload.get("enabled") else " (no backups directory on the server)"))
    elif newest is None:
        print(f"  newest   none indexed — {len(listed)} unindexed backup(s) only; "
              "the nightly wrapper has not run yet")
    else:
        print(f"  newest   {newest['stamp']} · {humanize_age(newest.get('age_seconds'))} · "
              f"{newest.get('mode') or 'unknown'} · {humanize_bytes(newest.get('total_bytes'))}")
    print(f"  kept     {len(listed)} on the server "
          f"({len(indexed)} indexed, {len(listed) - len(indexed)} unindexed)")
    print(f"  policy   {policy}")
    print("  off-box  freshness = your last pull — "
          f"last pull on this machine: {_last_pull_phrase()}")
    if admin_key():
        print("  link     admin key stored on this machine — `firekeep backup pull` is ready")
    else:
        print("  link     no admin key stored — run `firekeep backup link` to enable pull")
    return 0


def action_list(args) -> int:
    try:
        payload = fetch_status()
    except BackupError as exc:
        print(f"firekeep backup: {exc}", file=sys.stderr)
        return 1
    listed = entries(payload)
    if not listed:
        print("firekeep backup: no backups on the server yet — "
              f"policy is {payload.get('policy') or 'unknown'}")
        return 0
    print(f"firekeep backup — {len(listed)} on the server (newest first)")
    for entry in listed:
        indexed = bool(entry.get("indexed"))
        print(f"  {str(entry.get('stamp') or '?'):<18} "
              f"{humanize_age(entry.get('age_seconds')):<10} "
              f"{str(entry.get('mode') or '-'):<8} "
              f"{humanize_bytes(entry.get('total_bytes')) if indexed else '-':>12}  "
              + ("indexed" if indexed
                 else "unindexed (no manifest — rotation never deletes it)"))
    print(f"firekeep backup: policy is {payload.get('policy') or 'unknown'}")
    return 0


def action_link(args) -> int:
    """Store a deployment admin key — but only after proving it opens the gate."""
    key = (getattr(args, "key", None) or "").strip()
    if not key:
        try:
            key = _read_key()
        except BackupError as exc:
            print(f"firekeep backup: {exc}", file=sys.stderr)
            return 2
    if not key:
        print("firekeep backup: no key given — nothing stored.", file=sys.stderr)
        return 2

    try:
        cfg = resolver.load_config()
        ep = _endpoint(cfg)
        payload = fetch_status(cfg)
    except (BackupError, resolver.ConfigError) as exc:
        print(f"firekeep backup: {exc}", file=sys.stderr)
        return 1

    newest = newest_indexed(payload)
    if newest is None:
        print("firekeep backup: the server has no indexed backup to verify a key "
              "against, so this key cannot be proven to work.\n"
              "firekeep backup: nothing stored — an unverified key is exactly the "
              "failure `link` exists to prevent. Run it again after the first "
              "nightly backup (04:30 server time).", file=sys.stderr)
        return 1

    url = _artifact_url(ep, str(newest["stamp"]), MANIFEST_NAME)
    print(f"firekeep backup: verifying the key against {url}")
    try:
        get_json(url, headers=_admin_headers(ep, key), verify=ep.verify)
    except TransportError as exc:
        if exc.status in (401, 403):
            print("firekeep backup: that key lacks admin scope — the download endpoint "
                  "is admin-only, and no member key will ever open it.\n"
                  "firekeep backup: nothing stored. Mint a deployment ADMIN key from the "
                  "dashboard, or with `deploy/firekeep-admin` on the server.",
                  file=sys.stderr)
        else:
            print(f"firekeep backup: could not verify the key — {exc}\n"
                  "firekeep backup: nothing stored.", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"firekeep backup: could not verify the key — {exc}\n"
              "firekeep backup: nothing stored.", file=sys.stderr)
        return 1

    path = store_admin_key(key)
    print(f"firekeep backup: linked — admin key verified and stored in {path} (0600).")
    print("firekeep backup: `firekeep backup pull` can now fetch archives to this machine.")
    return 0


def action_pull(args) -> int:
    key = admin_key()
    if not key:
        print("firekeep backup: this machine is not linked — run `firekeep backup link` "
              "with a deployment ADMIN key first (the download endpoint is admin-only).",
              file=sys.stderr)
        return 1

    dest_root = Path(getattr(args, "dest", None) or default_dest()).expanduser()
    try:
        cfg = resolver.load_config()
        ep = _endpoint(cfg)
        payload = fetch_status(cfg)
    except (BackupError, resolver.ConfigError) as exc:
        print(f"firekeep backup pull: {exc}", file=sys.stderr)
        return 1

    newest = newest_indexed(payload)
    if newest is None:
        print("firekeep backup pull: no indexed backup on the server — there is nothing "
              "to pull yet. `firekeep backup list` shows what is there.", file=sys.stderr)
        return 1

    stamp = str(newest["stamp"])
    target = dest_root / pull_dir_name(stamp)
    print(f"firekeep backup pull: newest indexed backup is {stamp} "
          f"({humanize_age(newest.get('age_seconds'))}) -> {target}")
    headers = _admin_headers(ep, key)

    try:
        target.mkdir(parents=True, exist_ok=True)
        manifest = _fetch_manifest(ep, stamp, target, headers)
        _pull_files(ep, stamp, target, headers, manifest)
    except BackupError as exc:
        print(f"firekeep backup pull: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"firekeep backup pull: {exc}", file=sys.stderr)
        return 1

    write_pull_state(stamp, target)
    print(f"firekeep backup pull: verified into {target}")
    if manifest.get("sensitive", True):
        print("firekeep backup pull: this archive contains the server's .env "
              "(VAULT_KEY included) — treat it as a secret, not a file share.")
    pruned = prune_local_pulls(dest_root)
    if pruned:
        print(f"firekeep backup pull: pruned {len(pruned)} older local pull(s), "
              f"keeping {KEEP_LOCAL_PULLS} — {', '.join(pruned)}")
    return 0


def _fetch_manifest(ep, stamp: str, target: Path, headers: dict) -> dict:
    """Download the manifest as a FILE (not just parsed JSON) so the local copy
    is byte-identical to the server's — it is the document every later check of
    this pull is made against."""
    path = target / MANIFEST_NAME
    try:
        get_file(_artifact_url(ep, stamp, MANIFEST_NAME), path,
                 headers=headers, verify=ep.verify, timeout=DEFAULT_FILE_TIMEOUT)
    except TransportError as exc:
        raise BackupError(f"could not fetch {MANIFEST_NAME} — {exc}") from exc
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackupError(f"{MANIFEST_NAME} for {stamp} is not readable JSON: {exc}") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        raise BackupError(f"{MANIFEST_NAME} for {stamp} has no file list — refusing to "
                          "call an unverifiable download a backup")
    return manifest


def _pull_files(ep, stamp: str, target: Path, headers: dict, manifest: dict) -> None:
    for entry in manifest["files"]:
        if not isinstance(entry, dict):
            raise BackupError(f"{MANIFEST_NAME} for {stamp} has a malformed file entry")
        name = _safe_name(entry.get("name"))
        if name == MANIFEST_NAME:
            continue  # already fetched; a manifest cannot carry its own digest
        expected = str(entry.get("sha256") or "").lower()
        if not expected:
            raise BackupError(f"{name} has no sha256 in the manifest — refusing to "
                              "download what cannot be verified")
        path = target / name
        try:
            written = get_file(_artifact_url(ep, stamp, name), path,
                               headers=headers, verify=ep.verify,
                               timeout=DEFAULT_FILE_TIMEOUT)
        except TransportError as exc:
            raise BackupError(f"could not fetch {name} — {exc}") from exc
        actual = _sha256(path)
        if actual != expected:
            # Delete it. There is no resume, so the only correct next step is a
            # fresh pull — and a file left here would be indistinguishable from
            # a good one at exactly the moment that matters.
            try:
                path.unlink()
            except OSError:
                pass
            raise BackupError(
                f"{name} failed sha256 verification (expected {expected[:16]}…, got "
                f"{actual[:16]}…) — the download was corrupt or truncated. It has been "
                "deleted; re-run `firekeep backup pull` (there is no resume).")
        print(f"  {name:<24} ok  {humanize_bytes(written)}")


def prune_local_pulls(dest_root: Path) -> list[str]:
    """Keep the newest `KEEP_LOCAL_PULLS` pulled directories, delete the rest.

    Only directories this command itself names (`firekeep-backup-<stamp>`) are
    ever considered — a `--dest` pointed at a folder holding anything else must
    not turn into a delete radius. Stamps sort lexicographically because they
    are `YYYYMMDD-HHMMSS`."""
    try:
        pulls = sorted(
            (p for p in Path(dest_root).iterdir()
             if p.is_dir() and p.name.startswith(PULL_PREFIX)),
            key=lambda p: p.name,
        )
    except OSError:
        return []
    removed: list[str] = []
    for path in pulls[:max(0, len(pulls) - KEEP_LOCAL_PULLS)]:
        try:
            shutil.rmtree(path)
            removed.append(path.name)
        except OSError:
            pass
    return removed


def action_restore(args) -> int:
    """Print the real steps. Restore runs ON the host, with the stack down —
    that is physics, and pretending otherwise would be the dishonest kind of
    convenience. Deliberately does NOT call the server: the day you need this
    is the day the server is not answering."""
    try:
        cfg = resolver.load_config()
        host = server_host(cfg)
    except resolver.ConfigError:
        host = "YOUR-SERVER"

    state = read_pull_state()
    local = Path(state["path"]) if state.get("path") else None
    stamp = str(state.get("stamp") or "<stamp>")
    remote = f"/tmp/{pull_dir_name(stamp)}"

    print("firekeep backup restore — the guided path. Nothing here runs automatically:")
    print("restore happens ON the server, with the stack down.\n")

    if local is not None and local.exists():
        print(f"You have a verified local pull: {local}\n")
        print("  1. Copy it to the server")
        print(f'       scp -r "{local}" root@{host}:{remote}')
    else:
        print("This machine has no verified local pull yet.\n")
        print("  0. Pull one first")
        print("       firekeep backup pull")
        print("  1. Copy it to the server")
        print(f'       scp -r "<the pulled directory>" root@{host}:{remote}')
    print("  2. On the server, stop the stack and restore")
    print(f"       ssh root@{host}")
    print("       cd /opt/Firekeep")
    print("       docker compose down")
    print(f"       bash deploy/restore.sh {remote}")
    print("  3. Bring it back up")
    print("       docker compose up -d")
    print("       (models re-pull on first up — memory writes report "
          "status=\"partial\" until that finishes)\n")

    print("Fresh VPS — nothing installed yet:")
    print("  1. Install the server first, then restore over it")
    print("       git clone <your Firekeep checkout> /opt/Firekeep && cd /opt/Firekeep")
    print("       bash install.sh")
    print("       docker compose down")
    print(f"       bash deploy/restore.sh {remote}")
    print("       docker compose up -d")
    print("  2. The archive carries the server's .env (VAULT_KEY) — restore.sh asks "
          "before installing it, and refuses to overwrite an existing one silently.\n")
    print("firekeep backup restore: `firekeep doctor` on this machine confirms the "
          "Keep is answering again.")
    return 0


ACTIONS = {
    "status": action_status,
    "list": action_list,
    "link": action_link,
    "pull": action_pull,
    "restore": action_restore,
}


def run(args) -> int:
    action = getattr(args, "action", None) or "status"
    handler = ACTIONS.get(action)
    if handler is None:  # pragma: no cover - argparse chokes on this first
        print(f"firekeep: unknown backup action '{action}' "
              f"({', '.join(ACTIONS)})", file=sys.stderr)
        return 2
    return handler(args)


# --- doctor ------------------------------------------------------------------

def doctor_row(cfg=None, *, reachable: bool = True) -> tuple[str, str, str]:
    """The `backup` row for `firekeep doctor`.

    fail when there has never been a backup — one disk holding everything is
    the state this whole feature exists to end, and it is the only backup fact
    worth a red row. warn when the newest is stale (>36h, i.e. a nightly was
    missed) or when only unindexed dirs exist. An unreachable server degrades
    THIS row and nothing else: doctor's job on a dead server is to report, not
    to hang.

    `reachable=False` is doctor telling us NOTHING is answering. The row is
    still printed — its absence would read as "backups are fine" — but it
    spends no budget re-proving a diagnosis the row above already made."""
    linked = " · admin key stored on this machine" if admin_key() else ""
    if not reachable:
        return ("backup", "warn",
                f"unknown — no Firekeep server is reachable from this machine{linked}")
    try:
        payload = fetch_status(cfg, timeout=DOCTOR_TIMEOUT)
    except BackupError as exc:
        return ("backup", "warn",
                f"cannot ask the server about backups ({exc}){linked}")
    listed = entries(payload)
    indexed = indexed_newest_first(payload)
    policy = str(payload.get("policy") or "")
    suffix = f" ({policy})" if policy else ""

    if not listed:
        return ("backup", "fail",
                "never — one disk holds everything. The nightly wrapper installs with "
                f"`bash update.sh` on the server{suffix}{linked}")
    if not indexed:
        return ("backup", "warn",
                f"{len(listed)} backup(s) on the server but none indexed — the nightly "
                f"wrapper has not run yet{suffix}{linked}")
    newest = indexed[0]
    age = newest.get("age_seconds")
    detail = (f"last backup {humanize_age(age)} · {len(listed)} kept{suffix} · "
              f"last pull here: {_last_pull_phrase()}{linked}")
    if not isinstance(age, (int, float)) or age > STALE_AFTER_SECONDS:
        return ("backup", "warn", f"stale — {detail}")
    return ("backup", "ok", detail)
