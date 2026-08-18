"""Backup visibility (member) and backup retrieval (admin).

Reads the `./backups` directory that `deploy/backup-cron.sh` writes, mounted
read-only into cortex-api by docker-compose.yml. Nothing here writes, deletes or
schedules anything: the nightly run is a cron line on the host, and rotation is
the one operation in the feature that deletes, so it stays where a human can
read it in shell rather than behind an HTTP surface.

The two routes are deliberately asymmetric (spec §3):

- ``GET /ops/backups`` is member-readable and reveals EXISTENCE AND AGE ONLY.
  It powers `firekeep doctor`'s backup row and the dashboard card, and a
  staleness nag only the owner can see nags nobody.
- ``GET /ops/backups/{stamp}/{filename}`` is ADMIN-ONLY, permanently. A raw
  volume tar is every member's private corpus, and `env` carries VAULT_KEY.
  No `backup:*` scope may ever be introduced to make this "granular": since
  v1.0.0 an enrolled member is stamped ENROLLABLE_SCOPES = SCOPES − {admin, *},
  so a new scope here would be handed to every member automatically. Guarded by
  test_no_backup_scope_exists.

Filenames are resolved through the scanned directory listing and the backup's
own manifest.json — never by joining a caller-supplied string onto a path.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from auth.middleware import require_scope

logger = logging.getLogger(__name__)

DEFAULT_BACKUPS_DIR = "/backups"
DIR_PREFIX = "firekeep-backup-"
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"

# Stated once, read by the doctor row and the dashboard card so the two cannot
# describe different policies. Must stay in step with backup_retention_plan in
# deploy/lib.sh, which is what actually enforces it.
RETENTION_POLICY = "nightly 04:30 · keep 7 nightly + 4 weekly"


def _backups_dir() -> Path:
    """Resolved per call, not at import: tests point it at a tmp dir, and a
    module-level constant would freeze whatever the first import saw."""
    return Path(os.environ.get("FIREKEEP_BACKUPS_DIR", DEFAULT_BACKUPS_DIR))


def _age_seconds(stamp: str, fallback_path: Path) -> int:
    """Seconds since the backup was taken.

    From the stamp when it parses — that is when the snapshot STARTED, which is
    the number an operator means by "how old is my backup". Directory mtime is
    the fallback, and it drifts (rotation, an rsync, a manifest rewrite), which
    is why it is not the primary source.
    """
    try:
        taken = datetime.strptime(stamp, STAMP_FORMAT).replace(tzinfo=timezone.utc)
        return max(0, int(datetime.now(timezone.utc).timestamp() - taken.timestamp()))
    except (ValueError, TypeError):
        try:
            return max(0, int(time.time() - fallback_path.stat().st_mtime))
        except OSError:
            return 0


def _read_manifest(path: Path) -> dict[str, Any] | None:
    """The manifest, or None if it is absent or unreadable.

    A half-written manifest degrades the backup to `unindexed` rather than
    raising: this endpoint backs the doctor row and the dashboard card for every
    member, and one corrupt directory must not take both out.
    """
    try:
        data = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _scan(root: Path) -> list[dict[str, Any]]:
    """Every backup directory under `root`, newest first. Blocking; call in a thread."""
    entries: list[dict[str, Any]] = []
    try:
        names = sorted(os.listdir(root), reverse=True)
    except OSError:
        return entries

    for name in names:
        if not name.startswith(DIR_PREFIX):
            continue
        path = root / name
        if not path.is_dir():
            continue
        stamp = name[len(DIR_PREFIX):]
        manifest = _read_manifest(path)
        entry: dict[str, Any] = {
            "stamp": stamp,
            "age_seconds": _age_seconds(stamp, path),
            "indexed": manifest is not None,
            "mode": None,
            "total_bytes": None,
        }
        if manifest is not None:
            entry["mode"] = manifest.get("mode")
            total = manifest.get("total_bytes")
            entry["total_bytes"] = total if isinstance(total, int) else None
        entries.append(entry)
    return entries


def _resolve_file(root: Path, stamp: str, filename: str) -> Path | None:
    """The path to serve for (stamp, filename), or None to 404.

    Resolution is by MEMBERSHIP, not by construction: the stamp must be one the
    scan actually produced, and the filename must be one the backup's own
    manifest names (plus manifest.json itself, which the client fetches first to
    learn what to download and to verify an admin key against). A caller-supplied
    string is therefore never a path component that was trusted — it is only ever
    compared against strings this process read off disk.
    """
    if filename != "manifest.json":
        # Cheap pre-check so an obvious traversal never even reaches the scan.
        if "/" in filename or "\\" in filename or filename in ("", ".", ".."):
            return None

    for entry in _scan(root):
        if entry["stamp"] != stamp:
            continue
        if not entry["indexed"]:
            # No manifest means no checksums, so nothing a pull could verify.
            return None
        path = root / f"{DIR_PREFIX}{stamp}"
        allowed = {"manifest.json"}
        manifest = _read_manifest(path) or {}
        for item in manifest.get("files") or []:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                allowed.add(item["name"])
        if filename not in allowed:
            return None
        candidate = path / filename
        # Belt and braces: the name came from the manifest, but the manifest is
        # a file on disk, and a manifest that named "../.." would otherwise
        # escape. Confirm the resolved path is still inside the backup dir.
        try:
            resolved = candidate.resolve()
            if resolved.parent != path.resolve():
                return None
        except OSError:
            return None
        return candidate if candidate.is_file() else None
    return None


def create_ops_backups_router() -> APIRouter:
    router = APIRouter(prefix="/ops/backups", tags=["ops"])

    @router.get("")
    async def list_backups(identity: dict = Depends(require_scope("memory:read"))):
        """Existence, age and size of every backup on the host.

        `enabled: false` means the directory is not there at all — no mount, or
        a deployment whose first nightly has not run. That is "no backups yet",
        not an error, and the dashboard renders it as such.
        """
        root = _backups_dir()
        if not root.is_dir():
            return {"enabled": False, "backups": [], "count": 0, "policy": RETENTION_POLICY}
        backups = await run_in_threadpool(_scan, root)
        return {
            "enabled": True,
            "backups": backups,
            "count": len(backups),
            "policy": RETENTION_POLICY,
        }

    @router.get("/{stamp}/{filename}")
    async def download_backup_file(
        stamp: str,
        filename: str,
        identity: dict = Depends(require_scope("admin")),
    ):
        """Stream one file out of one backup. Admin only — see the module docstring."""
        root = _backups_dir()
        path = await run_in_threadpool(_resolve_file, root, stamp, filename)
        if path is None:
            raise HTTPException(status_code=404, detail="No such backup file")
        return FileResponse(
            path,
            media_type="application/octet-stream",
            filename=filename,
        )

    return router
