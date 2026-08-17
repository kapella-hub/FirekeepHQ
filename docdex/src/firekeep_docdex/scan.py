"""Walking a source root and hashing what is in it.

Two properties are load-bearing and everything else here serves them.

**Containment (review #6).** The resolved path of every entry must stay under
the resolved root. A symlink or a Windows junction pointing out of the folder
is skipped, not followed: a human who said "index ~/Notes" said nothing about
`~/.ssh`, and one symlink is all it takes for a folder to mean something very
different from what it looks like. The resolve-under-root predicate catches
symlinks and junctions alike, which is why it is used instead of `is_symlink()`
(which answers False for a junction on older Pythons).

**A walk either COMPLETED or it did not (I4a).** Deletions are inferred by the
caller from `files`, and absence of evidence is not deletion — a missing root,
an unmounted volume, a permission-denied root, or a source over the file cap
all produce `completed=False`, and the caller must delete nothing. A subtree
that errored mid-walk is named in `errors` so the caller can exclude exactly
that subtree instead of throwing the whole walk away.
"""
from __future__ import annotations

import hashlib
import os
import unicodedata
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from . import env_int
from .extract import is_supported

DEFAULT_MAX_FILES = 5000
DEFAULT_MAX_FILE_MB = 25

# A mistake net, NOT a security boundary — the docs say "do not add folders
# containing secrets". Dot-entries cover `.git`, `.venv` and friends in one
# rule; the rest are the policy deny list's secret patterns plus the two
# directories that are always build output.
DEFAULT_EXCLUDES: tuple[str, ...] = (
    ".*",
    "node_modules",
    "__pycache__",
    ".env*",
    "*.key",
    "*.pem",
    "*id_rsa*",
)

_HASH_CHUNK = 1 << 20


@dataclass(frozen=True)
class WalkResult:
    """What one walk saw. `files` maps a normalized relpath to the sha256 of the
    file's RAW bytes — hashing the bytes, not the extracted text, means a
    re-save that changes nothing visible still re-ingests, and an extractor
    upgrade never has to invalidate anything."""

    completed: bool
    files: dict[str, str] = field(default_factory=dict)
    # Relpaths of subtrees (and individual files) that could not be read. The
    # caller excludes these from deletion inference — an unreadable folder is
    # not an empty one.
    errors: list[str] = field(default_factory=list)
    # Oversize files are SKIPPED, which is not the same as gone: keeping them
    # here lets the caller exclude them from deletion inference so a file that
    # grew past the cap keeps its existing replica instead of losing it.
    oversize: dict[str, int] = field(default_factory=dict)
    unsupported: int = 0
    too_many: bool = False
    root_error: str | None = None


def max_files() -> int:
    return env_int("FIREKEEP_DOCDEX_MAX_FILES", DEFAULT_MAX_FILES)


def max_file_bytes() -> int:
    return env_int("FIREKEEP_DOCDEX_MAX_FILE_MB", DEFAULT_MAX_FILE_MB) * 1024 * 1024


def normalize_relpath(rel: str) -> str:
    """NFC + forward slashes, case preserved (spec §3).

    This string is what gets hashed into the corpus source name, so it must be
    identical for the same file seen from macOS (which hands out NFD) and from
    Windows (backslashes). Without this, one folder synced from two machines
    indexes itself twice."""
    return unicodedata.normalize("NFC", rel.replace("\\", "/"))


def is_contained(candidate: Path, root: Path) -> bool:
    """True when `candidate` resolves to `root` or something beneath it.

    `Path.resolve()` on both sides, then a PARENT-CHAIN comparison rather than
    a string prefix: `/notes-private` starts with `/notes` and is not under it.
    Any resolve failure answers False — a path we cannot pin down is a path we
    do not walk.
    """
    try:
        resolved_root = root.resolve()
        resolved = candidate.resolve()
    except OSError:
        return False
    if resolved == resolved_root:
        return True
    try:
        resolved.relative_to(resolved_root)
    except ValueError:
        return False
    return True


def _excluded(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch(name, pattern) for pattern in patterns)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def walk(
    root: str | Path,
    *,
    excludes: tuple[str, ...] = DEFAULT_EXCLUDES,
    file_cap: int | None = None,
    size_cap: int | None = None,
) -> WalkResult:
    """Walk `root`, hashing every supported file that is contained within it."""
    cap = max_files() if file_cap is None else file_cap
    size_limit = max_file_bytes() if size_cap is None else size_cap

    root_path = Path(root)
    try:
        resolved_root = root_path.resolve()
        if not resolved_root.is_dir():
            return WalkResult(completed=False, root_error=f"not a folder: {root_path}")
        os.scandir(resolved_root).close()
    except OSError as e:
        # Missing, unmounted, or permission-denied AT THE ROOT. The caller sees
        # completed=False and deletes nothing (I4a) — an unplugged USB drive
        # must never wipe a member's index.
        return WalkResult(completed=False, root_error=f"{type(e).__name__}: {e}")

    files: dict[str, str] = {}
    errors: list[str] = []
    oversize: dict[str, int] = {}
    unsupported = 0
    # Resolved directories already walked. A symlink pointing at an ancestor
    # INSIDE the root passes containment and would otherwise recurse forever.
    visited: set[Path] = {resolved_root}
    pending: list[Path] = [resolved_root]

    while pending:
        current = pending.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda e: e.name)
        except OSError:
            errors.append(_rel(current, resolved_root))
            continue
        for entry in entries:
            name = entry.name
            if _excluded(name, excludes):
                continue
            path = Path(entry.path)
            if not is_contained(path, resolved_root):
                continue  # symlink/junction out of the root — never followed
            try:
                is_dir = entry.is_dir()
            except OSError:
                errors.append(_rel(path, resolved_root))
                continue
            if is_dir:
                try:
                    resolved = path.resolve()
                except OSError:
                    errors.append(_rel(path, resolved_root))
                    continue
                if resolved not in visited:
                    visited.add(resolved)
                    pending.append(resolved)
                continue
            if not is_supported(path):
                unsupported += 1
                continue
            rel = _rel(path, resolved_root)
            try:
                size = entry.stat().st_size
            except OSError:
                errors.append(rel)
                continue
            if size > size_limit:
                oversize[rel] = size
                continue
            if len(files) >= cap:
                # Refuse the source rather than index a silent subset. Reported
                # via too_many; completed stays False so the partial view here
                # can never be mistaken for evidence that anything was deleted.
                return WalkResult(
                    completed=False, files=files, errors=errors, oversize=oversize,
                    unsupported=unsupported, too_many=True,
                )
            try:
                files[rel] = _sha256(path)
            except OSError:
                errors.append(rel)

    return WalkResult(
        completed=True, files=files, errors=sorted(errors), oversize=oversize,
        unsupported=unsupported,
    )


def _rel(path: Path, root: Path) -> str:
    try:
        return normalize_relpath(str(path.relative_to(root)))
    except ValueError:  # pragma: no cover - containment already guarantees this
        return normalize_relpath(str(path))


def excluded_by_errors(relpath: str, errors: list[str]) -> bool:
    """True when `relpath` lies inside a subtree the walk could not read.

    The caller uses this to keep an unreadable folder's files OUT of deletion
    inference: they were not seen, but they were not observed to be gone either
    (I4a, the partial-subtree half).
    """
    for err in errors:
        if relpath == err or relpath.startswith(err + "/"):
            return True
    return False
