"""Tests for storage layer bugs: incremental_save file_summaries loss, git rename handling."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from firekeep_symdex.storage import IndexStore
from firekeep_symdex.parser import Symbol
from firekeep_symdex.storage.index_store import (
    score_symbol,
    _subsequence_match,
)


def _make_symbol(file: str, name: str, **kwargs) -> Symbol:
    """Helper to create a Symbol with minimal boilerplate."""
    defaults = dict(
        id=f"{file.replace('.', '-').replace('/', '-')}::{name}",
        file=file,
        name=name,
        qualified_name=name,
        kind="function",
        language="python",
        signature=f"def {name}():",
        byte_offset=0,
        byte_length=10,
    )
    defaults.update(kwargs)
    return Symbol(**defaults)


def _make_sym_dict(name, **kwargs):
    """Helper to build a minimal symbol dict for scoring tests."""
    d = {
        "name": name,
        "signature": kwargs.get("signature", ""),
        "summary": kwargs.get("summary", ""),
        "keywords": kwargs.get("keywords", []),
        "docstring": kwargs.get("docstring", ""),
    }
    d.update(kwargs)
    return d


# ---------------------------------------------------------------------------
# Bug #1: incremental_save drops file_summaries
# ---------------------------------------------------------------------------


class TestIncrementalSavePreservesFileSummaries:
    """incremental_save must preserve file_summaries from the old index."""

    def test_file_summaries_preserved_after_incremental_save(self, tmp_path):
        """File summaries for unchanged files survive an incremental update."""
        store = IndexStore(base_path=str(tmp_path))

        # Initial save with file_summaries
        sym_a = _make_symbol("a.py", "func_a")
        sym_b = _make_symbol("b.py", "func_b")
        store.save_index(
            owner="owner",
            name="repo",
            source_files=["a.py", "b.py"],
            symbols=[sym_a, sym_b],
            raw_files={"a.py": "def func_a(): pass", "b.py": "def func_b(): pass"},
            languages={"python": 2},
            file_summaries={"a.py": "Module A does stuff", "b.py": "Module B does other stuff"},
        )

        # Incremental save: change only b.py
        new_sym_b = _make_symbol("b.py", "func_b_v2")
        updated = store.incremental_save(
            owner="owner",
            name="repo",
            changed_files=["b.py"],
            new_files=[],
            deleted_files=[],
            new_symbols=[new_sym_b],
            raw_files={"b.py": "def func_b_v2(): pass"},
            languages={"python": 2},
        )

        assert updated is not None
        # a.py summary must survive
        assert "a.py" in updated.file_summaries
        assert updated.file_summaries["a.py"] == "Module A does stuff"

    def test_file_summaries_removed_for_deleted_files(self, tmp_path):
        """File summaries for deleted files should be removed."""
        store = IndexStore(base_path=str(tmp_path))

        sym_a = _make_symbol("a.py", "func_a")
        sym_b = _make_symbol("b.py", "func_b")
        store.save_index(
            owner="owner",
            name="repo",
            source_files=["a.py", "b.py"],
            symbols=[sym_a, sym_b],
            raw_files={"a.py": "def func_a(): pass", "b.py": "def func_b(): pass"},
            languages={"python": 2},
            file_summaries={"a.py": "Module A", "b.py": "Module B"},
        )

        # Delete b.py
        updated = store.incremental_save(
            owner="owner",
            name="repo",
            changed_files=[],
            new_files=[],
            deleted_files=["b.py"],
            new_symbols=[],
            raw_files={},
            languages={"python": 1},
        )

        assert updated is not None
        assert "a.py" in updated.file_summaries
        assert "b.py" not in updated.file_summaries

    def test_file_summaries_removed_for_changed_files_unless_new_provided(self, tmp_path):
        """Changed file summaries are dropped (stale) unless new ones are passed."""
        store = IndexStore(base_path=str(tmp_path))

        sym_a = _make_symbol("a.py", "func_a")
        store.save_index(
            owner="owner",
            name="repo",
            source_files=["a.py"],
            symbols=[sym_a],
            raw_files={"a.py": "def func_a(): pass"},
            languages={"python": 1},
            file_summaries={"a.py": "Old summary"},
        )

        # Change a.py without providing new summary
        new_sym_a = _make_symbol("a.py", "func_a_v2")
        updated = store.incremental_save(
            owner="owner",
            name="repo",
            changed_files=["a.py"],
            new_files=[],
            deleted_files=[],
            new_symbols=[new_sym_a],
            raw_files={"a.py": "def func_a_v2(): pass"},
            languages={"python": 1},
        )

        assert updated is not None
        # Old summary for changed file should be dropped (it's stale)
        assert "a.py" not in updated.file_summaries

    def test_new_file_summaries_merged_in(self, tmp_path):
        """New file summaries passed to incremental_save are merged in."""
        store = IndexStore(base_path=str(tmp_path))

        sym_a = _make_symbol("a.py", "func_a")
        store.save_index(
            owner="owner",
            name="repo",
            source_files=["a.py"],
            symbols=[sym_a],
            raw_files={"a.py": "def func_a(): pass"},
            languages={"python": 1},
            file_summaries={"a.py": "Module A"},
        )

        # Add a new file with summary
        new_sym_b = _make_symbol("b.py", "func_b")
        updated = store.incremental_save(
            owner="owner",
            name="repo",
            changed_files=[],
            new_files=["b.py"],
            deleted_files=[],
            new_symbols=[new_sym_b],
            raw_files={"b.py": "def func_b(): pass"},
            languages={"python": 2},
            new_file_summaries={"b.py": "Module B - new"},
        )

        assert updated is not None
        assert updated.file_summaries["a.py"] == "Module A"
        assert updated.file_summaries["b.py"] == "Module B - new"

    def test_file_summaries_round_trip_after_incremental(self, tmp_path):
        """file_summaries survive incremental_save -> load_index round-trip."""
        store = IndexStore(base_path=str(tmp_path))

        sym_a = _make_symbol("a.py", "func_a")
        sym_b = _make_symbol("b.py", "func_b")
        store.save_index(
            owner="owner",
            name="repo",
            source_files=["a.py", "b.py"],
            symbols=[sym_a, sym_b],
            raw_files={"a.py": "def func_a(): pass", "b.py": "def func_b(): pass"},
            languages={"python": 2},
            file_summaries={"a.py": "Summary A", "b.py": "Summary B"},
        )

        # Incremental save: modify b.py
        new_sym_b = _make_symbol("b.py", "func_b_new")
        store.incremental_save(
            owner="owner",
            name="repo",
            changed_files=["b.py"],
            new_files=[],
            deleted_files=[],
            new_symbols=[new_sym_b],
            raw_files={"b.py": "def func_b_new(): pass"},
            languages={"python": 2},
            new_file_summaries={"b.py": "Updated summary B"},
        )

        # Reload from disk
        loaded = store.load_index("owner", "repo")
        assert loaded is not None
        assert loaded.file_summaries["a.py"] == "Summary A"
        assert loaded.file_summaries["b.py"] == "Updated summary B"


# ---------------------------------------------------------------------------
# Bug #2: detect_changes_git skips renames when old path has unsupported ext
# ---------------------------------------------------------------------------


class TestDetectChangesGitRename:
    """detect_changes_git must handle renames where old/new exts differ."""

    def _setup_index(self, store, files, git_head="aaa111"):
        """Create an initial index with given files and git_head."""
        symbols = [_make_symbol(f, f"func_{f.replace('.', '_')}") for f in files]
        raw = {f: f"# content of {f}" for f in files}
        store.save_index(
            owner="owner",
            name="repo",
            source_files=files,
            symbols=symbols,
            raw_files=raw,
            languages={"python": len(files)},
            git_head=git_head,
        )

    @patch("firekeep_symdex.storage.index_store.subprocess.run")
    def test_rename_from_unsupported_ext_adds_new_file(self, mock_run, tmp_path):
        """Renaming foo.txt -> foo.py should add foo.py even though .txt is unsupported."""
        store = IndexStore(base_path=str(tmp_path))
        self._setup_index(store, ["existing.py"], git_head="aaa111")

        # Simulate git diff output for a rename from .txt to .py
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "R100\tfoo.txt\tfoo.py\n"
        mock_run.return_value = mock_result

        # Need to mock LANGUAGE_EXTENSIONS
        with patch("firekeep_symdex.storage.index_store.subprocess.run", return_value=mock_result):
            result = store.detect_changes_git(
                owner="owner",
                name="repo",
                repo_path=Path("/fake/repo"),
                current_head="bbb222",
            )

        assert result is not None
        changed, new, deleted = result
        # foo.py should appear as new (it has a supported ext)
        assert "foo.py" in new
        # foo.txt should NOT appear as deleted (unsupported ext)
        assert "foo.txt" not in deleted

    @patch("firekeep_symdex.storage.index_store.subprocess.run")
    def test_rename_between_supported_exts(self, mock_run, tmp_path):
        """Renaming old.py -> new.py should delete old and add new."""
        store = IndexStore(base_path=str(tmp_path))
        self._setup_index(store, ["old.py"], git_head="aaa111")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "R100\told.py\tnew.py\n"
        mock_run.return_value = mock_result

        result = store.detect_changes_git(
            owner="owner",
            name="repo",
            repo_path=Path("/fake/repo"),
            current_head="bbb222",
        )

        assert result is not None
        changed, new, deleted = result
        assert "new.py" in new
        assert "old.py" in deleted

    @patch("firekeep_symdex.storage.index_store.subprocess.run")
    def test_copy_status_handled(self, mock_run, tmp_path):
        """Git copy status (C) should add the destination file."""
        store = IndexStore(base_path=str(tmp_path))
        self._setup_index(store, ["source.py"], git_head="aaa111")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "C100\tsource.py\tcopy.py\n"
        mock_run.return_value = mock_result

        result = store.detect_changes_git(
            owner="owner",
            name="repo",
            repo_path=Path("/fake/repo"),
            current_head="bbb222",
        )

        assert result is not None
        changed, new, deleted = result
        assert "copy.py" in new
        # For copy, old file still exists, so it appears as deleted from index
        # (which is debatable but consistent with the code treating it like rename)
        assert "source.py" in deleted


# ---------------------------------------------------------------------------
# Edge case: score_symbol with empty query
# ---------------------------------------------------------------------------


class TestScoreSymbolEdgeCases:
    def test_empty_query_string(self):
        """Empty query should not crash and should match everything via substring."""
        sym = _make_sym_dict("anything", signature="def anything()")
        # Empty string is a substring of everything
        score = score_symbol(sym, "", set())
        # "" in "anything" is True -> +10, "" in "def anything()" is True -> +8
        assert score >= 10

    def test_empty_query_words_set(self):
        """Empty query_words set should not cause errors in iteration."""
        sym = _make_sym_dict("test_func")
        score = score_symbol(sym, "nomatch", set())
        # "nomatch" not in "test_func", no query_words to iterate
        assert score == 0

    def test_subsequence_both_empty(self):
        """Both query and target empty: empty is subsequence of empty."""
        assert _subsequence_match("", "") is True

    def test_subsequence_target_empty_query_nonempty(self):
        """Non-empty query cannot be subsequence of empty target."""
        assert _subsequence_match("a", "") is False


# ---------------------------------------------------------------------------
# Round-trip: file_summaries in save_index / load_index
# ---------------------------------------------------------------------------


class TestFileSummariesRoundTrip:
    def test_file_summaries_saved_and_loaded(self, tmp_path):
        """file_summaries must survive save -> load cycle."""
        store = IndexStore(base_path=str(tmp_path))

        sym = _make_symbol("main.py", "main")
        summaries = {"main.py": "Entry point for the application"}

        store.save_index(
            owner="owner",
            name="repo",
            source_files=["main.py"],
            symbols=[sym],
            raw_files={"main.py": "def main(): pass"},
            languages={"python": 1},
            file_summaries=summaries,
        )

        loaded = store.load_index("owner", "repo")
        assert loaded is not None
        assert loaded.file_summaries == summaries

    def test_file_summaries_in_json(self, tmp_path):
        """file_summaries must be present in the serialized JSON."""
        store = IndexStore(base_path=str(tmp_path))

        sym = _make_symbol("main.py", "main")
        store.save_index(
            owner="owner",
            name="repo",
            source_files=["main.py"],
            symbols=[sym],
            raw_files={"main.py": "def main(): pass"},
            languages={"python": 1},
            file_summaries={"main.py": "Entry point"},
        )

        index_path = tmp_path / "owner-repo.json"
        data = json.loads(index_path.read_text(encoding="utf-8"))
        assert "file_summaries" in data
        assert data["file_summaries"]["main.py"] == "Entry point"
