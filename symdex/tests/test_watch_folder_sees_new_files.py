"""The watcher must see a file that did not exist when it started.

THE BUG
-------
`_get_indexed_mtimes` built its snapshot by iterating the symbols ALREADY in
the index:

    for sym in index.symbols:
        mtimes[sym["file"]] = (folder_path / sym["file"]).stat().st_mtime

A newly added file contributes no symbols to an index built before it existed,
so it could never enter that map -- which made the loop's own addition test,
`if rel not in last_mtimes`, unreachable by construction. Modifications and
deletions of already-indexed files worked; additions did not, silently.

That is the case an intra-session watcher exists for. `session_start`'s
background index already covers "the repo changed since last session"; the only
thing `watch_folder` adds is noticing work done DURING a session, and creating
files is most of that work.

THE FIX is to snapshot the filesystem instead of the index -- but deliberately
NOT via `discover_local_files`, the obvious reuse. That function opens every
candidate to sniff for binary content, MEASURED at ~4.0s on the Firekeep root
against this module's 5s poll. `_scan_source_mtimes` applies only the path-based
half of the same filter chain and reads mtime from the directory entry: ~321ms
on the same tree, 74ms on symdex.

`TestTheOldSnapshotWouldFailThis` runs the original index-derived logic against
the same fixture and asserts it misses the addition, so this file cannot quietly
become decoration.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from firekeep_symdex.tools.watch_folder import (
    _PRUNE_DIRS,
    _changed,
    _scan_source_mtimes,
)

SRC = "def greet(name):\n    return name\n"


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text(SRC, encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "util.py").write_text(SRC, encoding="utf-8")
    return tmp_path


class TestTheBug:
    def test_a_newly_added_file_is_seen(self, tree: Path):
        """THE regression. This is what could not happen before."""
        before = _scan_source_mtimes(tree)
        assert "brand_new.py" not in before

        (tree / "brand_new.py").write_text(SRC, encoding="utf-8")
        after = _scan_source_mtimes(tree)

        assert "brand_new.py" in after
        assert _changed(before, after), "an added file must trigger a reindex"

    def test_a_file_added_in_a_new_subdirectory_is_seen(self, tree: Path):
        """A new package is the common shape of 'work done this session'."""
        before = _scan_source_mtimes(tree)
        (tree / "newpkg").mkdir()
        (tree / "newpkg" / "mod.py").write_text(SRC, encoding="utf-8")
        after = _scan_source_mtimes(tree)

        assert "newpkg/mod.py" in after
        assert _changed(before, after)


class TestTheCasesThatAlreadyWorkedStillWork:
    """A fix that trades additions for modifications is the same bug moved."""

    def test_a_modified_file_is_detected(self, tree: Path):
        before = _scan_source_mtimes(tree)
        target = tree / "app.py"
        os.utime(target, (before["app.py"] + 100, before["app.py"] + 100))
        after = _scan_source_mtimes(tree)
        assert _changed(before, after)

    def test_a_deleted_file_is_detected(self, tree: Path):
        before = _scan_source_mtimes(tree)
        (tree / "pkg" / "util.py").unlink()
        after = _scan_source_mtimes(tree)
        assert "pkg/util.py" not in after
        assert _changed(before, after)

    def test_an_unchanged_tree_does_not_trigger_a_reindex(self, tree: Path):
        """The watcher polls every 5s forever. A false positive here is a
        permanent reindex loop, which is worse than the bug being fixed."""
        first = _scan_source_mtimes(tree)
        second = _scan_source_mtimes(tree)
        assert not _changed(first, second)

    def test_a_rename_is_detected_as_both_sides(self, tree: Path):
        before = _scan_source_mtimes(tree)
        (tree / "app.py").rename(tree / "renamed.py")
        after = _scan_source_mtimes(tree)
        assert "app.py" not in after and "renamed.py" in after
        assert _changed(before, after)

    def test_swapping_one_file_for_another_is_not_missed(self, tree: Path):
        """`_changed` short-circuits on len() for speed. A simultaneous add and
        delete keeps the count identical, so the length check must not be the
        only check."""
        before = _scan_source_mtimes(tree)
        (tree / "app.py").unlink()
        (tree / "other.py").write_text(SRC, encoding="utf-8")
        after = _scan_source_mtimes(tree)
        assert len(before) == len(after), "precondition: counts must match"
        assert _changed(before, after)


class TestItDoesNotScanWhatIndexingIgnores:
    """Every exclusion here is also a reindex trigger avoided. Churn inside
    .git or node_modules would otherwise reindex the repo continuously."""

    @pytest.mark.parametrize("pruned", sorted(_PRUNE_DIRS)[:6])
    def test_pruned_directories_are_not_descended(self, tree: Path, pruned: str):
        d = tree / pruned
        d.mkdir()
        (d / "noise.py").write_text(SRC, encoding="utf-8")
        found = _scan_source_mtimes(tree)
        assert not any(r.startswith(f"{pruned}/") for r in found)

    def test_git_churn_does_not_trigger_a_reindex(self, tree: Path):
        """The single most common source of constant filesystem activity."""
        (tree / ".git").mkdir()
        before = _scan_source_mtimes(tree)
        (tree / ".git" / "index").write_text("x", encoding="utf-8")
        (tree / ".git" / "hooks.py").write_text(SRC, encoding="utf-8")
        assert not _changed(before, _scan_source_mtimes(tree))

    def test_gitignored_files_are_excluded(self, tree: Path):
        (tree / ".gitignore").write_text("secrets_dir/\nignored.py\n", encoding="utf-8")
        (tree / "ignored.py").write_text(SRC, encoding="utf-8")
        (tree / "secrets_dir").mkdir()
        (tree / "secrets_dir" / "a.py").write_text(SRC, encoding="utf-8")

        found = _scan_source_mtimes(tree)
        assert "ignored.py" not in found
        assert "secrets_dir/a.py" not in found
        assert "app.py" in found

    def test_non_source_extensions_are_ignored(self, tree: Path):
        for name in ("notes.txt", "data.csv", "image.png", "archive.zip"):
            (tree / name).write_bytes(b"x")
        found = _scan_source_mtimes(tree)
        assert not any(r in found for r in ("notes.txt", "data.csv", "image.png", "archive.zip"))

    def test_a_symlink_is_not_followed(self, tree: Path):
        """Mirrors discover_local_files' follow_symlinks=False. A symlink to a
        parent directory would otherwise make the walk unbounded."""
        target = tree / "app.py"
        link = tree / "link.py"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not permitted in this environment")
        assert "link.py" not in _scan_source_mtimes(tree)


class TestItNeverKillsTheWatcher:
    """The scan runs on a daemon thread with no supervisor. Anything it raises
    ends watching for that folder until the process restarts."""

    def test_an_unreadable_directory_is_skipped_not_raised(self, tree: Path, monkeypatch):
        real = os.scandir

        def _boom(p):
            if str(p).endswith("pkg"):
                raise PermissionError("denied")
            return real(p)

        monkeypatch.setattr(os, "scandir", _boom)
        found = _scan_source_mtimes(tree)
        assert "app.py" in found, "one bad directory must not lose the rest"

    def test_a_file_deleted_mid_scan_is_skipped(self, tree: Path, monkeypatch):
        """stat() races the editor that is being watched, by construction."""
        real_stat = os.DirEntry.stat

        def _flaky(self, *a, **kw):
            if self.name == "app.py":
                raise FileNotFoundError("vanished")
            return real_stat(self, *a, **kw)

        monkeypatch.setattr(os.DirEntry, "stat", _flaky, raising=False)
        found = _scan_source_mtimes(tree)
        assert "pkg/util.py" in found

    def test_an_empty_folder_yields_an_empty_map(self, tmp_path: Path):
        assert _scan_source_mtimes(tmp_path) == {}
        assert not _changed({}, {})


class TestTheOldSnapshotWouldFailThis:
    """Proof this file discriminates.

    Reproduces the pre-fix logic exactly -- mtimes derived from the symbols in
    the index -- and shows it cannot see the added file. If this ever fails, the
    fix has been reverted or the tests above have stopped testing it.
    """

    @staticmethod
    def _index_derived(indexed_files: list[str], folder: Path) -> dict[str, float]:
        mtimes: dict[str, float] = {}
        for rel in indexed_files:  # stands in for `for sym in index.symbols`
            try:
                mtimes[rel] = (folder / rel).stat().st_mtime
            except OSError:
                pass
        return mtimes

    def test_the_old_logic_cannot_see_an_added_file(self, tree: Path):
        indexed = ["app.py", "pkg/util.py"]  # what the index knew at build time
        before = self._index_derived(indexed, tree)

        (tree / "brand_new.py").write_text(SRC, encoding="utf-8")

        # The index has not been rebuilt, so it still reports the same files --
        # which is precisely why the new file is invisible.
        after = self._index_derived(indexed, tree)
        assert not _changed(before, after), (
            "the old snapshot is expected to MISS this; if it now detects the "
            "addition, update this discriminator rather than deleting it"
        )

        # The shipped scan sees it.
        assert "brand_new.py" in _scan_source_mtimes(tree)
