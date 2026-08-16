"""The release-coupling guard: does a bundle change since the last server tag warn?

The failure it prevents: install.sh (or another bundle-allowlist file) changes but
no ``vX.Y.Z`` tag is cut to PUBLISH the new bundle, so ``firekeep init`` keeps
serving the stale one. The guard is advisory (always exits 0) and git-only.

These tests drive the pure core with a fake "changed since tag" state — no git, no
network, no clock — so they are deterministic regardless of the repo's real tags.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_server_release_pending as guard
from check_server_release_pending import (
    evaluate,
    format_warning,
    latest_release_tag,
)

# The exact phrase the CI annotation must carry, verbatim.
FIX_PHRASE = "cut a server release (git tag vX.Y.Z) or firekeep init serves a stale bundle"


def _reader(head: dict[str, bytes], tagged: dict[str, bytes]):
    """A fake ``read_at_ref``: HEAD reads from ``head``, anything else from ``tagged``."""

    def read_at_ref(ref: str, path: str) -> bytes | None:
        table = head if ref == "HEAD" else tagged
        return table.get(path)

    return read_at_ref


def test_changed_since_tag_produces_warning() -> None:
    read_at_ref = _reader(
        head={"install.sh": b"new"},
        tagged={"install.sh": b"old"},
    )
    warning = evaluate(
        tags=["v1.0.0"],
        head_sha="headsha",
        commit_of=lambda tag: "tagsha",
        read_at_ref=read_at_ref,
        bundle_files=["install.sh"],
    )
    assert warning is not None
    assert warning.startswith("::warning::")
    assert "install.sh" in warning
    assert "v1.0.0" in warning
    assert FIX_PHRASE in warning


def test_unchanged_is_silent() -> None:
    read_at_ref = _reader(
        head={"install.sh": b"same", "update.sh": b"same"},
        tagged={"install.sh": b"same", "update.sh": b"same"},
    )
    warning = evaluate(
        tags=["v1.0.0"],
        head_sha="headsha",
        commit_of=lambda tag: "tagsha",
        read_at_ref=read_at_ref,
        bundle_files=["install.sh", "update.sh"],
    )
    assert warning is None


def test_head_is_the_release_tag_is_silent_even_if_content_would_differ() -> None:
    # commit_of(tag) == head_sha means HEAD *is* the release commit: the bundle was
    # just published, so no warning regardless of what the reader would report.
    read_at_ref = _reader(head={"install.sh": b"new"}, tagged={"install.sh": b"old"})
    warning = evaluate(
        tags=["v1.0.0"],
        head_sha="sameref",
        commit_of=lambda tag: "sameref",
        read_at_ref=read_at_ref,
        bundle_files=["install.sh"],
    )
    assert warning is None


def test_no_release_tag_is_silent() -> None:
    read_at_ref = _reader(head={"install.sh": b"new"}, tagged={})
    warning = evaluate(
        tags=["client-v0.1.45"],  # no final vX.Y.Z tag to compare against
        head_sha="headsha",
        commit_of=lambda tag: "tagsha",
        read_at_ref=read_at_ref,
        bundle_files=["install.sh"],
    )
    assert warning is None


def test_latest_release_tag_is_semver_max_ignoring_client_and_prerelease() -> None:
    tags = [
        "v1.9.9",
        "v1.10.0",
        "v1.10.0-rc.1",
        "client-v1.11.0",
        "not-a-tag",
        "v1.2.3",
    ]
    assert latest_release_tag(tags) == "v1.10.0"
    assert latest_release_tag(["client-v0.1.45", "garbage"]) is None


def test_warning_names_every_changed_file_and_the_fix() -> None:
    warning = format_warning("v2.0.0", ["install.sh", "deploy/lib.sh", "update.sh"])
    assert warning.startswith("::warning::")
    for name in ("install.sh", "deploy/lib.sh", "update.sh"):
        assert name in warning
    assert FIX_PHRASE in warning
    # Single-line: a raw newline would truncate the annotation in GitHub's parser.
    assert "\n" not in warning


def test_added_or_removed_bundle_file_counts_as_changed() -> None:
    # Present at HEAD, absent at the tag (a newly bundled file) → stale bundle.
    read_at_ref = _reader(head={"docs/DEPLOYMENT.md": b"content"}, tagged={})
    warning = evaluate(
        tags=["v1.0.0"],
        head_sha="headsha",
        commit_of=lambda tag: "tagsha",
        read_at_ref=read_at_ref,
        bundle_files=["docs/DEPLOYMENT.md"],
    )
    assert warning is not None
    assert "docs/DEPLOYMENT.md" in warning


def test_uses_the_builder_allowlist_by_default() -> None:
    # The guard must diff the SAME files the builder ships, imported not copied.
    from deploy.build_server_bundle import BUNDLE_FILES

    assert guard.BUNDLE_FILES is BUNDLE_FILES
    assert "install.sh" in guard.BUNDLE_FILES
    assert "deploy/lib.sh" in guard.BUNDLE_FILES
