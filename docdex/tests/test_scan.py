"""Walking a source root. Two load-bearing properties live here: containment
(spec review #6) and completed-walk-only deletion inference (I4a)."""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

from firekeep_docdex import scan


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return root


def _symlink(link: Path, target: Path, *, directory: bool) -> None:
    """Create a symlink, falling back to a Windows JUNCTION for directories.

    Windows symlinks need SeCreateSymbolicLinkPrivilege (or Developer Mode),
    but a junction needs neither — and a junction is the harder case the spec
    names: `Path.is_symlink()` answers False for one while `resolve()` follows
    it straight out of the root. So the directory tests below really do run on
    Windows, exercising the predicate that catches both.

    A file symlink has no such fallback; that test skips, and
    `test_is_contained_*` covers the same predicate directly everywhere.
    """
    try:
        os.symlink(target, link, target_is_directory=directory)
        return
    except (OSError, NotImplementedError, AttributeError) as e:
        if not (directory and sys.platform == "win32"):
            pytest.skip(f"symlink creation unavailable: {e}")
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True,
    )
    if completed.returncode != 0:  # pragma: no cover - only on a locked-down box
        pytest.skip(f"junction creation unavailable: {completed.stderr.strip()}")


# --- the containment predicate, tested directly on every platform -----------


def test_is_contained_accepts_a_path_under_the_root(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    assert scan.is_contained(root / "a" / "b.md", root) is True


def test_is_contained_accepts_the_root_itself(tmp_path):
    assert scan.is_contained(tmp_path, tmp_path) is True


def test_is_contained_rejects_a_sibling(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    assert scan.is_contained(tmp_path / "elsewhere" / "secrets.md", root) is False


def test_is_contained_rejects_a_prefix_lookalike(tmp_path):
    """`/home/me/notes-private` is NOT under `/home/me/notes` — a string
    prefix test would say it is."""
    root = tmp_path / "notes"
    root.mkdir()
    assert scan.is_contained(tmp_path / "notes-private" / "x.md", root) is False


def test_is_contained_rejects_a_parent_escape(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    assert scan.is_contained(root / ".." / "outside.md", root) is False


# --- the walk ---------------------------------------------------------------


def test_walk_hashes_raw_bytes_of_supported_files(tmp_path):
    root = _tree(tmp_path / "src", {"a.md": "alpha", "sub/b.txt": "beta"})
    result = scan.walk(root)
    assert result.completed is True
    assert set(result.files) == {"a.md", "sub/b.txt"}
    assert result.files["a.md"] == hashlib.sha256(b"alpha").hexdigest()


def test_relpaths_use_forward_slashes_on_every_platform(tmp_path):
    root = _tree(tmp_path / "src", {"deep/nested/c.md": "c"})
    assert list(scan.walk(root).files) == ["deep/nested/c.md"]


def test_relpaths_are_nfc_normalized(tmp_path):
    """The same filename typed on macOS (NFD) and Windows (NFC) must hash to
    one corpus source name — otherwise a synced folder duplicates itself the
    first time it is opened on the other platform."""
    root = tmp_path / "src"
    root.mkdir()
    decomposed = unicodedata.normalize("NFD", "café.md")
    (root / decomposed).write_text("x", encoding="utf-8")
    assert list(scan.walk(root).files) == [unicodedata.normalize("NFC", "café.md")]


def test_unsupported_files_are_counted_not_indexed(tmp_path):
    root = _tree(tmp_path / "src", {"a.md": "a", "b.rtf": "b", "c.xlsx": "c"})
    result = scan.walk(root)
    assert set(result.files) == {"a.md"}
    assert result.unsupported == 2


def test_default_excludes(tmp_path):
    root = _tree(tmp_path / "src", {
        "keep.md": "keep",
        ".hidden.md": "no",
        ".git/config.md": "no",
        "node_modules/pkg/readme.md": "no",
        "__pycache__/x.md": "no",
        ".env.md": "no",
        "secrets/deploy.key": "no",
        "secrets/tls.pem": "no",
        "ssh/my_id_rsa_backup.md": "no",
    })
    assert set(scan.walk(root).files) == {"keep.md"}


def test_excludes_are_overridable(tmp_path):
    root = _tree(tmp_path / "src", {"keep.md": "k", "drafts/d.md": "d"})
    result = scan.walk(root, excludes=scan.DEFAULT_EXCLUDES + ("drafts",))
    assert set(result.files) == {"keep.md"}


# --- containment through the walk -------------------------------------------


def test_a_symlinked_file_pointing_outside_the_root_is_skipped(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("password", encoding="utf-8")
    root = _tree(tmp_path / "src", {"a.md": "a"})
    _symlink(root / "leak.md", outside / "secret.md", directory=False)
    assert set(scan.walk(root).files) == {"a.md"}


def test_a_symlinked_directory_pointing_outside_the_root_is_not_descended(tmp_path):
    outside = _tree(tmp_path / "outside", {"secret.md": "password"})
    root = _tree(tmp_path / "src", {"a.md": "a"})
    _symlink(root / "escape", outside, directory=True)
    assert set(scan.walk(root).files) == {"a.md"}


def test_a_symlink_cycle_inside_the_root_terminates(tmp_path):
    root = _tree(tmp_path / "src", {"a.md": "a", "sub/b.md": "b"})
    _symlink(root / "sub" / "loop", root, directory=True)
    result = scan.walk(root)
    assert set(result.files) == {"a.md", "sub/b.md"}


# --- I4a: a walk that did not complete emits nothing ------------------------


def test_missing_root_does_not_complete(tmp_path):
    result = scan.walk(tmp_path / "never-existed")
    assert result.completed is False
    assert result.files == {}
    assert result.root_error is not None


def test_a_file_as_root_does_not_complete(tmp_path):
    f = tmp_path / "notes.md"
    f.write_text("x", encoding="utf-8")
    result = scan.walk(f)
    assert result.completed is False
    assert result.root_error is not None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_unreadable_root_does_not_complete(tmp_path):
    root = _tree(tmp_path / "src", {"a.md": "a"})
    os.chmod(root, 0o000)
    try:
        result = scan.walk(root)
    finally:
        os.chmod(root, 0o700)
    assert result.completed is False
    assert result.root_error is not None


def test_an_unreadable_subtree_is_recorded_and_the_walk_still_completes(tmp_path, monkeypatch):
    root = _tree(tmp_path / "src", {"a.md": "a", "locked/b.md": "b"})
    real_scandir = os.scandir

    def boom(path):
        if str(path).endswith("locked"):
            raise PermissionError(13, "denied")
        return real_scandir(path)

    monkeypatch.setattr(scan.os, "scandir", boom)
    result = scan.walk(root)
    assert result.completed is True          # the root was readable
    assert result.errors == ["locked"]       # but this subtree is unknowable
    assert set(result.files) == {"a.md"}


def test_an_unreadable_file_is_recorded_not_fatal(tmp_path, monkeypatch):
    root = _tree(tmp_path / "src", {"a.md": "a", "b.md": "b"})
    real_open = Path.open

    def boom(self, *a, **kw):
        if self.name == "b.md":
            raise PermissionError(13, "denied")
        return real_open(self, *a, **kw)

    monkeypatch.setattr(Path, "open", boom)
    result = scan.walk(root)
    assert set(result.files) == {"a.md"}
    assert "b.md" in result.errors


# --- caps -------------------------------------------------------------------


def test_default_caps(firekeep_home):
    assert scan.max_files() == 5000
    assert scan.max_file_bytes() == 25 * 1024 * 1024


def test_caps_are_env_overridable(monkeypatch):
    monkeypatch.setenv("FIREKEEP_DOCDEX_MAX_FILES", "7")
    monkeypatch.setenv("FIREKEEP_DOCDEX_MAX_FILE_MB", "3")
    assert scan.max_files() == 7
    assert scan.max_file_bytes() == 3 * 1024 * 1024


def test_oversize_files_are_skipped_and_counted_separately(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_DOCDEX_MAX_FILE_MB", "1")
    root = tmp_path / "src"
    root.mkdir()
    (root / "small.md").write_text("small", encoding="utf-8")
    (root / "huge.md").write_bytes(b"x" * (2 * 1024 * 1024))
    result = scan.walk(root)
    assert set(result.files) == {"small.md"}
    # NOT merely absent: an oversize file must be distinguishable from a
    # deleted one, or the first sync after a file grows past the cap would
    # delete its replica (spec says "skipped", not "deleted").
    assert set(result.oversize) == {"huge.md"}


def test_too_many_files_refuses_the_walk_and_forbids_deletions(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_DOCDEX_MAX_FILES", "3")
    root = _tree(tmp_path / "src", {f"f{i}.md": str(i) for i in range(10)})
    result = scan.walk(root)
    assert result.too_many is True
    # completed=False is what makes the cap safe: a refused source must not be
    # able to emit deletions from the partial subset it happened to see.
    assert result.completed is False


def test_a_walk_at_exactly_the_cap_is_fine(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_DOCDEX_MAX_FILES", "3")
    root = _tree(tmp_path / "src", {f"f{i}.md": str(i) for i in range(3)})
    result = scan.walk(root)
    assert result.too_many is False and result.completed is True
    assert len(result.files) == 3


def test_normalize_relpath_is_pure_and_idempotent():
    assert scan.normalize_relpath("a\\b\\c.md") == "a/b/c.md"
    once = scan.normalize_relpath(unicodedata.normalize("NFD", "café/naïve.md"))
    assert scan.normalize_relpath(once) == once
    assert once == unicodedata.normalize("NFC", "café/naïve.md")
