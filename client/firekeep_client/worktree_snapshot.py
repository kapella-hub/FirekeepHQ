"""Local snapshots of uncommitted work, so a destructive command is recoverable.

Design: docs/superpowers/specs/2026-08-02-uncommitted-work-preservation-design.md

WHY THIS EXISTS
On 2026-08-02 an agent ran `git checkout -- cortex/app/` to undo its own botched edit
script. That reverts every tracked file in the directory, and nine carried the user's
uncommitted feature work. It was unrecoverable. Bridge had faithfully recorded
"63 files changed, 2776 insertions(+)" -- `_git.workspace_snapshot()` runs
`git diff --stat`, so it knew the work existed and its exact size and retained none of
its content.

WHY IT STAYS ON THIS MACHINE
A raw `git diff` contains whatever was being edited: .env files, keys, customer data.
Sending that to a TEAM memory server inverts the guarantee personal mode provides.
Cortex's `secret_scan.py` cannot help -- it is server-side, and the hook cores are
deliberately stdlib-only, so reusing it would mean a second copy of security-critical
logic. Nothing here is transmitted, so nothing needs scanning: every byte written was
already on this disk.

Personal mode deliberately does NOT disable this. `is_bypassed()` means "nothing reaches
the server"; these are local files, and withdrawing recovery exactly when someone is
doing sensitive work by hand would be the wrong trade.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

_DEFAULT_MAX_BYTES = 8 * 1024 * 1024
_DEFAULT_KEEP = 20
_HOOK = "worktree_snapshot"


def _log(msg: str) -> None:
    try:
        from firekeep_client import hooklog
        hooklog.log_failure(_HOOK, msg)
    except Exception:  # noqa: BLE001 — logging must never be the failure
        pass


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def snapshots_root() -> Path:
    override = os.environ.get("FIREKEEP_SNAPSHOT_DIR")
    return Path(override) if override else Path.home() / ".firekeep" / "worktree-snapshots"


def _slug(repo_root: Path) -> str:
    """One directory per repo. Sanitised because the basename reaches a path."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", Path(repo_root).resolve().name) or "repo"


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess:
    # encoding= is load-bearing, not tidiness. `text=True` decodes with the LOCALE codec
    # (cp1252 on Windows), and git output containing e.g. UTF-8 U+0410 (D0 90) raises
    # UnicodeDecodeError inside subprocess's READER THREAD — so run() returns with
    # silently EMPTY stdout rather than raising. Files then vanish from a snapshot with
    # no signal whatsoever. Found by running the guard against the real repo.
    return subprocess.run(
        ["git", *args], cwd=str(repo_root), capture_output=True,
        encoding="utf-8", errors="replace", check=False,
    )


def _changed_paths(repo_root: Path) -> tuple[list[str], list[str]]:
    """(existing-but-differing paths, deleted paths) vs HEAD.

    One `git status --porcelain -z` covers modified, staged, added and untracked
    uniformly — the distinctions that make patch-based restore fragile do not matter
    when the answer is "copy this file".
    """
    out = _git(repo_root, "status", "--porcelain", "-z", "--untracked-files=all").stdout or ""
    fields = [f for f in out.split("\0") if f]
    changed: list[str] = []
    deleted: list[str] = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        if status.startswith("R") and i < len(fields):
            i += 1  # rename: the following field is the ORIGINAL path
        if "D" in status and not (repo_root / path).exists():
            deleted.append(path)
        elif (repo_root / path).is_file():
            changed.append(path)
    return changed, deleted


def repo_root(cwd: str | None = None) -> Path | None:
    """The git worktree root containing `cwd`, or None. One implementation, shared by
    the hook guard and the CLI — a second copy is how one bug comes to live in two files.
    """
    try:
        proc = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd,
                              capture_output=True, encoding="utf-8",
                              errors="replace", check=False)
        top = (proc.stdout or "").strip()
        return Path(top) if proc.returncode == 0 and top else None
    except Exception:  # noqa: BLE001
        return None


def snapshot_path(repo_root_: Path, snapshot_id: str) -> Path:
    """Where a given snapshot id lives for a given repo."""
    return snapshots_root() / _slug(Path(repo_root_)) / snapshot_id


def _is_repo(repo_root: Path) -> bool:
    try:
        return _git(repo_root, "rev-parse", "--git-dir").returncode == 0
    except Exception:  # noqa: BLE001
        return False


def capture(repo_root, *, reason: str = "") -> Path | None:
    """Snapshot uncommitted work. Returns the snapshot dir, or None.

    None means "nothing to do or could not" — never an exception. A snapshot failure
    must not be the reason a hook or a command stops.
    """
    try:
        repo_root = Path(repo_root)
        if not _is_repo(repo_root):
            return None

        # CONTENTS, not a patch. `git apply` is the obvious primitive and it is the
        # wrong one: `git diff HEAD` records post-image blob hashes that do not exist in
        # the object store for unstaged files, so --3way silently degrades, and `git
        # apply` is ATOMIC — one unappliable hunk discards the whole restore. A partially
        # staged tree (the normal case) hits both. Verified failing before this rewrite.
        #
        # Copying every differing file is larger on disk and cannot fail that way. For a
        # recovery tool, boring reliability beats elegance: it must work on the worst day.
        patch = _git(repo_root, "diff", "HEAD").stdout or ""   # kept for `--show` only
        changed, deleted = _changed_paths(repo_root)
        if not changed and not deleted:
            return None  # nothing uncommitted; writing an empty snapshot would let
            # rotation evict the ones that mattered

        max_bytes = _int_env("FIREKEEP_SNAPSHOT_MAX_BYTES", _DEFAULT_MAX_BYTES)
        truncated: list[str] = []

        out = snapshots_root() / _slug(repo_root) / (
            time.strftime("%Y%m%dT%H%M%S") + f"-{time.time_ns() % 1_000_000_000:09d}"
        )
        out.mkdir(parents=True, exist_ok=True)

        # Contents get the real budget; the patch is a human-readable extra, capped
        # separately so a huge diff can never starve the thing restore depends on.
        budget = max_bytes
        copied = 0
        for rel in changed:
            src = repo_root / rel
            try:
                size = src.stat().st_size
                if size > budget:
                    truncated.append(f"{rel} ({size}B, budget exhausted)")
                    continue
                dst = out / "files" / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                budget -= size
                copied += 1
            except OSError as e:
                truncated.append(f"{rel} (unreadable: {e})")

        data = patch.encode("utf-8", errors="replace")
        patch_cap = min(max_bytes, 1024 * 1024)
        if len(data) > patch_cap:
            truncated.append(f"tracked.patch display copy ({len(data)}B > {patch_cap}B)")
            data = data[:patch_cap]
        (out / "tracked.patch").write_bytes(data)

        meta = {
            "id": out.name,
            "reason": reason,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "repo": str(repo_root),
            "branch": (_git(repo_root, "branch", "--show-current").stdout or "").strip(),
            "head": (_git(repo_root, "rev-parse", "HEAD").stdout or "").strip()[:12],
            "files_copied": copied,
            "files_seen": len(changed),
            "deleted": deleted,
            "patch_bytes": len(data),
            "truncated": truncated,
        }
        (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        _rotate(repo_root)
        return out
    except Exception as e:  # noqa: BLE001 — never raise into a hook
        _log(f"capture failed: {e!r}")
        return None


def _rotate(repo_root: Path) -> None:
    keep = _int_env("FIREKEEP_SNAPSHOT_KEEP", _DEFAULT_KEEP)
    base = snapshots_root() / _slug(repo_root)
    try:
        dirs = sorted((d for d in base.iterdir() if d.is_dir()), key=lambda d: d.name)
    except OSError:
        return
    for old in dirs[:-keep] if keep > 0 else dirs:
        shutil.rmtree(old, ignore_errors=True)


def list_snapshots(repo_root) -> list[dict]:
    """Newest last. Returns [] rather than raising when the store is absent."""
    base = snapshots_root() / _slug(Path(repo_root))
    out: list[dict] = []
    try:
        dirs = sorted((d for d in base.iterdir() if d.is_dir()), key=lambda d: d.name)
    except OSError:
        return out
    for d in dirs:
        try:
            out.append(json.loads((d / "meta.json").read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001 — a corrupt meta must not hide the others
            out.append({"id": d.name, "reason": "", "truncated": ["meta.json unreadable"]})
    return out


def apply_snapshot(snapshot_dir, repo_root, *, backup: bool = True) -> dict:
    """Restore a snapshot's file contents into `repo_root`. Never raises.

    Snapshots the CURRENT state first, so a restore is itself undoable. That is what
    removes the need to refuse a dirty tree — and refusing would block the primary use
    case, since after a destructive command the tree is almost always still dirty
    somewhere else. An earlier draft refused, and the round-trip test caught it.

    Deletions are REPORTED, not re-applied: re-deleting files on a restore is a
    surprising second destructive act, and the caller can see the list and decide.
    """
    result: dict = {"restored": 0, "backup": None, "deleted_not_restored": [], "errors": []}
    try:
        snapshot_dir, repo_root = Path(snapshot_dir), Path(repo_root)
        if backup:
            b = capture(repo_root, reason="pre-restore safety")
            result["backup"] = str(b) if b else None

        src_root = snapshot_dir / "files"
        if src_root.is_dir():
            for src in src_root.rglob("*"):
                if not src.is_file():
                    continue
                dst = repo_root / src.relative_to(src_root)
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    result["restored"] += 1
                except OSError as e:
                    result["errors"].append(f"{src}: {e}")

        try:
            meta = json.loads((snapshot_dir / "meta.json").read_text(encoding="utf-8"))
            result["deleted_not_restored"] = meta.get("deleted") or []
        except Exception:  # noqa: BLE001 — a missing meta must not fail a restore
            pass
        return result
    except Exception as e:  # noqa: BLE001
        result["errors"].append(repr(e))
        return result
