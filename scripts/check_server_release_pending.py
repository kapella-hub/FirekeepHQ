#!/usr/bin/env python3
"""Warn when the server deployment bundle is ahead of the latest server release.

``firekeep init`` installs the SOURCE-FREE bundle published by a ``vX.Y.Z`` server
release, not the working tree. So a merge that edits ``install.sh`` (or any other
file in the bundle allowlist) without a following server tag leaves the published
bundle stale — ``firekeep init`` keeps serving the old one. This exact gap once
shipped a dead-end.

Git-only, no network: compare every allowlisted bundle file at HEAD against its
content at the latest ``v[0-9]+.[0-9]+.[0-9]+`` tag. If any differ and HEAD is not
itself that tag, the bundle is behind and we emit a GitHub ``::warning::``
annotation naming the changed files.

Advisory by construction — ``main`` always exits 0. Cutting a server release is a
deliberate act (client first, then the server tag); this reminds, it never blocks.

The allowlist is imported from ``deploy.build_server_bundle`` so the guard and the
builder can never drift about which files ship in the bundle.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from re import compile as _re_compile
from typing import Callable, Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
# Run-as-a-script puts scripts/ on sys.path, not the repo root, so the import
# below would miss the deploy namespace package. Under pytest the repo root is
# already sys.path[0]; this insert is idempotent either way.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from deploy.build_server_bundle import BUNDLE_FILES, is_newer_release  # noqa: E402

# Strict FINAL release tag only. A prerelease (``v1.0.0-rc.1``) does not publish
# the customer-facing bundle, so it does not clear the warning.
FINAL_RELEASE_TAG = _re_compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")

ReadAtRef = Callable[[str, str], "bytes | None"]
CommitOf = Callable[[str], str]


def latest_release_tag(tags: Iterable[str]) -> str | None:
    """The semver-highest ``vX.Y.Z`` tag, or None when none match.

    SemVer ordering, not lexical — reuses the builder's comparator so ``v1.10.0``
    beats ``v1.9.9``. ``client-v*`` and any non-final tag are ignored.
    """
    finals = [t for t in tags if FINAL_RELEASE_TAG.fullmatch(t)]
    if not finals:
        return None
    latest = finals[0]
    for candidate in finals[1:]:
        if is_newer_release(candidate, latest):
            latest = candidate
    return latest


def changed_bundle_files(
    read_at_ref: ReadAtRef,
    tag: str,
    bundle_files: Sequence[str] = BUNDLE_FILES,
    head: str = "HEAD",
) -> list[str]:
    """Bundle files whose content at ``head`` differs from their content at ``tag``.

    A file missing at one ref (added or removed) reads as ``None`` and so counts as
    changed — a newly bundled or dropped file also makes the published bundle stale.
    Allowlist order is preserved.
    """
    return [
        rel
        for rel in bundle_files
        if read_at_ref(head, rel) != read_at_ref(tag, rel)
    ]


def format_warning(tag: str, changed: Sequence[str]) -> str:
    """The single-line GitHub annotation. Carries the changed files and the fix."""
    files = ", ".join(changed)
    return (
        f"::warning::Server bundle files changed since {tag} but no matching server "
        f"release was tagged ({files}); cut a server release (git tag vX.Y.Z) or "
        f"firekeep init serves a stale bundle"
    )


def evaluate(
    *,
    tags: Iterable[str],
    head_sha: str,
    commit_of: CommitOf,
    read_at_ref: ReadAtRef,
    bundle_files: Sequence[str] = BUNDLE_FILES,
) -> str | None:
    """Return the warning text, or None when the bundle is in sync.

    None (silent) in three cases: no ``vX.Y.Z`` tag exists to compare against;
    HEAD is itself the latest release commit (the bundle was just published); or
    no bundle file differs from the release.
    """
    tag = latest_release_tag(tags)
    if tag is None:
        return None
    if commit_of(tag) == head_sha:
        return None
    changed = changed_bundle_files(read_at_ref, tag, bundle_files)
    if not changed:
        return None
    return format_warning(tag, changed)


def _git(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        check=False,
    )


def _git_tags() -> list[str]:
    out = _git("tag", "--list").stdout.decode("utf-8", "replace")
    return out.split()


def _git_commit_of(ref: str) -> str:
    # rev-list -n1 peels an annotated tag to the commit it points at.
    return _git("rev-list", "-n", "1", ref).stdout.decode("utf-8", "replace").strip()


def _git_read_at_ref(ref: str, path: str) -> bytes | None:
    # Bytes, no decode: the allowlist carries SVGs and other non-text files.
    result = _git("show", f"{ref}:{path}")
    if result.returncode != 0:
        return None
    return result.stdout


def main(argv: Sequence[str] | None = None) -> int:
    warning = evaluate(
        tags=_git_tags(),
        head_sha=_git_commit_of("HEAD"),
        commit_of=_git_commit_of,
        read_at_ref=_git_read_at_ref,
    )
    if warning:
        print(warning)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
