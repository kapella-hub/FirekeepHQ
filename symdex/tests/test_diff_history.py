"""Tests for diff_since_index and get_symbol_history tools."""

import json
from pathlib import Path


from firekeep_symdex.storage.index_store import CodeIndex, IndexStore
from firekeep_symdex.tools.diff_since_index import diff_since_index
from firekeep_symdex.tools.get_symbol_history import get_symbol_history


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_symbol(sym_id: str, name: str, file: str, content_hash: str = "abc123",
                 signature: str = "def foo()") -> dict:
    """Create a minimal symbol dict for testing."""
    return {
        "id": sym_id,
        "file": file,
        "name": name,
        "qualified_name": name,
        "kind": "function",
        "language": "python",
        "signature": signature,
        "docstring": "",
        "summary": "",
        "decorators": [],
        "keywords": [],
        "parent": None,
        "line": 1,
        "end_line": 5,
        "byte_offset": 0,
        "byte_length": 50,
        "content_hash": content_hash,
    }


def _create_index_on_disk(storage_dir: str, owner: str, name: str,
                          file_hashes: dict, symbols: list[dict],
                          indexed_at: str = "2025-01-01T00:00:00") -> None:
    """Write a CodeIndex JSON file directly to the storage directory."""
    slug = f"{owner}-{name}"
    index_data = {
        "repo": f"{owner}/{name}",
        "owner": owner,
        "name": name,
        "indexed_at": indexed_at,
        "source_files": list(file_hashes.keys()),
        "languages": {"python": len(file_hashes)},
        "symbols": symbols,
        "index_version": 2,
        "file_hashes": file_hashes,
        "git_head": "",
        "file_summaries": {},
        "references": [],
    }
    index_path = Path(storage_dir) / f"{slug}.json"
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index_data, f)


def _hash_content(content: str) -> str:
    """Match the hashing used by IndexStore."""
    import hashlib
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# diff_since_index tests
# ---------------------------------------------------------------------------

class TestDiffSinceIndex:
    """Tests for the diff_since_index tool."""

    def test_repo_not_indexed(self, tmp_path):
        """Returns error when repo has no index."""
        result = diff_since_index("nonexistent/repo", storage_path=str(tmp_path))
        assert "error" in result

    def test_no_changes(self, tmp_path):
        """Returns all unchanged when disk matches index."""
        # Create a source file on disk
        repo_dir = tmp_path / "myrepo"
        repo_dir.mkdir()
        src = repo_dir / "main.py"
        src.write_text("print('hello')", encoding="utf-8")

        content_hash = _hash_content("print('hello')")
        storage_dir = tmp_path / "storage"
        storage_dir.mkdir()

        _create_index_on_disk(
            str(storage_dir), "local", "myrepo",
            file_hashes={"main.py": content_hash},
            symbols=[_make_symbol("s1", "main", "main.py")],
        )

        result = diff_since_index(str(repo_dir), storage_path=str(storage_dir))

        assert "error" not in result
        assert result["new_files"] == []
        assert result["modified_files"] == []
        assert result["deleted_files"] == []
        assert result["unchanged_count"] == 1

    def test_modified_file(self, tmp_path):
        """Detects a modified file when content hash differs."""
        repo_dir = tmp_path / "myrepo"
        repo_dir.mkdir()
        src = repo_dir / "main.py"
        src.write_text("print('updated')", encoding="utf-8")

        old_hash = _hash_content("print('original')")
        storage_dir = tmp_path / "storage"
        storage_dir.mkdir()

        _create_index_on_disk(
            str(storage_dir), "local", "myrepo",
            file_hashes={"main.py": old_hash},
            symbols=[_make_symbol("s1", "main", "main.py")],
        )

        result = diff_since_index(str(repo_dir), storage_path=str(storage_dir))

        assert result["modified_files"] == ["main.py"]
        assert result["new_files"] == []
        assert result["deleted_files"] == []
        assert result["unchanged_count"] == 0

    def test_new_file(self, tmp_path):
        """Detects a new file not present in the index."""
        repo_dir = tmp_path / "myrepo"
        repo_dir.mkdir()
        (repo_dir / "main.py").write_text("x = 1", encoding="utf-8")
        (repo_dir / "new_file.py").write_text("y = 2", encoding="utf-8")

        storage_dir = tmp_path / "storage"
        storage_dir.mkdir()

        _create_index_on_disk(
            str(storage_dir), "local", "myrepo",
            file_hashes={"main.py": _hash_content("x = 1")},
            symbols=[_make_symbol("s1", "main", "main.py")],
        )

        result = diff_since_index(str(repo_dir), storage_path=str(storage_dir))

        assert "new_file.py" in result["new_files"]
        assert result["unchanged_count"] == 1

    def test_deleted_file(self, tmp_path):
        """Detects a deleted file that was in the index but not on disk."""
        repo_dir = tmp_path / "myrepo"
        repo_dir.mkdir()
        # Only main.py exists; old_file.py was indexed but is gone
        (repo_dir / "main.py").write_text("x = 1", encoding="utf-8")

        storage_dir = tmp_path / "storage"
        storage_dir.mkdir()

        _create_index_on_disk(
            str(storage_dir), "local", "myrepo",
            file_hashes={
                "main.py": _hash_content("x = 1"),
                "old_file.py": _hash_content("old stuff"),
            },
            symbols=[_make_symbol("s1", "main", "main.py")],
        )

        result = diff_since_index(str(repo_dir), storage_path=str(storage_dir))

        assert "old_file.py" in result["deleted_files"]
        assert result["unchanged_count"] == 1

    def test_mixed_changes(self, tmp_path):
        """Detects a combination of new, modified, and deleted files."""
        repo_dir = tmp_path / "myrepo"
        repo_dir.mkdir()
        (repo_dir / "unchanged.py").write_text("a = 1", encoding="utf-8")
        (repo_dir / "modified.py").write_text("b = 2 # changed", encoding="utf-8")
        (repo_dir / "brand_new.py").write_text("c = 3", encoding="utf-8")

        storage_dir = tmp_path / "storage"
        storage_dir.mkdir()

        _create_index_on_disk(
            str(storage_dir), "local", "myrepo",
            file_hashes={
                "unchanged.py": _hash_content("a = 1"),
                "modified.py": _hash_content("b = 2"),
                "deleted.py": _hash_content("gone"),
            },
            symbols=[],
        )

        result = diff_since_index(str(repo_dir), storage_path=str(storage_dir))

        assert result["new_files"] == ["brand_new.py"]
        assert result["modified_files"] == ["modified.py"]
        assert result["deleted_files"] == ["deleted.py"]
        assert result["unchanged_count"] == 1

    def test_skips_non_source_files(self, tmp_path):
        """Non-source files on disk are not included in new_files."""
        repo_dir = tmp_path / "myrepo"
        repo_dir.mkdir()
        (repo_dir / "main.py").write_text("x = 1", encoding="utf-8")
        (repo_dir / "readme.md").write_text("# Hello", encoding="utf-8")
        (repo_dir / "data.csv").write_text("a,b,c", encoding="utf-8")

        storage_dir = tmp_path / "storage"
        storage_dir.mkdir()

        _create_index_on_disk(
            str(storage_dir), "local", "myrepo",
            file_hashes={"main.py": _hash_content("x = 1")},
            symbols=[],
        )

        result = diff_since_index(str(repo_dir), storage_path=str(storage_dir))

        assert result["new_files"] == []
        assert result["unchanged_count"] == 1

    def test_meta_contains_timing(self, tmp_path):
        """Result _meta includes timing info."""
        repo_dir = tmp_path / "myrepo"
        repo_dir.mkdir()
        (repo_dir / "main.py").write_text("x = 1", encoding="utf-8")

        storage_dir = tmp_path / "storage"
        storage_dir.mkdir()

        _create_index_on_disk(
            str(storage_dir), "local", "myrepo",
            file_hashes={"main.py": _hash_content("x = 1")},
            symbols=[],
        )

        result = diff_since_index(str(repo_dir), storage_path=str(storage_dir))

        assert "_meta" in result
        assert "timing_ms" in result["_meta"]
        assert isinstance(result["_meta"]["timing_ms"], float)


# ---------------------------------------------------------------------------
# get_symbol_history tests
# ---------------------------------------------------------------------------

class TestGetSymbolHistory:
    """Tests for the get_symbol_history tool."""

    def test_repo_not_indexed(self, tmp_path):
        """Returns error when repo has no index."""
        result = get_symbol_history("nonexistent/repo", "sym1", storage_path=str(tmp_path))
        assert "error" in result

    def test_symbol_not_found(self, tmp_path):
        """Returns error when symbol ID doesn't exist in index."""
        storage_dir = tmp_path / "storage"
        storage_dir.mkdir()

        _create_index_on_disk(
            str(storage_dir), "local", "myrepo",
            file_hashes={"main.py": "abc"},
            symbols=[_make_symbol("s1", "main", "main.py")],
        )

        result = get_symbol_history("local/myrepo", "nonexistent_sym",
                                    storage_path=str(storage_dir))
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_no_history_file(self, tmp_path):
        """Returns current state with note when no history file exists."""
        storage_dir = tmp_path / "storage"
        storage_dir.mkdir()

        _create_index_on_disk(
            str(storage_dir), "local", "myrepo",
            file_hashes={"main.py": "abc"},
            symbols=[_make_symbol("s1", "main", "main.py",
                                  content_hash="hash1", signature="def foo()")],
        )

        result = get_symbol_history("local/myrepo", "s1",
                                    storage_path=str(storage_dir))

        assert result["symbol_id"] == "s1"
        assert result["change_count"] == 0
        assert result["history"] == []
        assert "note" in result
        assert "next re-index" in result["note"]
        assert result["current_state"]["content_hash"] == "hash1"

    def test_history_with_entries(self, tmp_path):
        """Returns history entries when history file exists."""
        storage_dir = tmp_path / "storage"
        storage_dir.mkdir()

        _create_index_on_disk(
            str(storage_dir), "local", "myrepo",
            file_hashes={"main.py": "abc"},
            symbols=[_make_symbol("s1", "main", "main.py",
                                  content_hash="hash3", signature="def foo(x, y)")],
        )

        # Write history file directly
        history = {
            "s1": [
                {
                    "timestamp": "2025-01-01T00:00:00",
                    "symbol_id": "s1",
                    "content_hash": "hash1",
                    "signature": "def foo()",
                },
                {
                    "timestamp": "2025-02-01T00:00:00",
                    "symbol_id": "s1",
                    "content_hash": "hash2",
                    "signature": "def foo(x)",
                },
                {
                    "timestamp": "2025-03-01T00:00:00",
                    "symbol_id": "s1",
                    "content_hash": "hash3",
                    "signature": "def foo(x, y)",
                },
            ],
        }
        history_path = storage_dir / "local-myrepo.history.json"
        with open(history_path, "w") as f:
            json.dump(history, f)

        result = get_symbol_history("local/myrepo", "s1",
                                    storage_path=str(storage_dir))

        assert result["symbol_id"] == "s1"
        assert result["change_count"] == 3
        assert len(result["history"]) == 3

        # First entry has no signature_changed_from
        assert "signature_changed_from" not in result["history"][0]

        # Second entry should show signature changed from first
        assert result["history"][1]["signature_changed_from"] == "def foo()"

        # Third entry should show signature changed from second
        assert result["history"][2]["signature_changed_from"] == "def foo(x)"

    def test_history_no_signature_change(self, tmp_path):
        """Entries with same signature don't get signature_changed_from."""
        storage_dir = tmp_path / "storage"
        storage_dir.mkdir()

        _create_index_on_disk(
            str(storage_dir), "local", "myrepo",
            file_hashes={"main.py": "abc"},
            symbols=[_make_symbol("s1", "main", "main.py",
                                  content_hash="hash2", signature="def foo()")],
        )

        history = {
            "s1": [
                {
                    "timestamp": "2025-01-01T00:00:00",
                    "symbol_id": "s1",
                    "content_hash": "hash1",
                    "signature": "def foo()",
                },
                {
                    "timestamp": "2025-02-01T00:00:00",
                    "symbol_id": "s1",
                    "content_hash": "hash2",
                    "signature": "def foo()",  # Same signature, different hash
                },
            ],
        }
        history_path = storage_dir / "local-myrepo.history.json"
        with open(history_path, "w") as f:
            json.dump(history, f)

        result = get_symbol_history("local/myrepo", "s1",
                                    storage_path=str(storage_dir))

        assert result["change_count"] == 2
        # No signature_changed_from on either entry
        assert "signature_changed_from" not in result["history"][0]
        assert "signature_changed_from" not in result["history"][1]

    def test_meta_contains_timing(self, tmp_path):
        """Result _meta includes timing info."""
        storage_dir = tmp_path / "storage"
        storage_dir.mkdir()

        _create_index_on_disk(
            str(storage_dir), "local", "myrepo",
            file_hashes={"main.py": "abc"},
            symbols=[_make_symbol("s1", "main", "main.py")],
        )

        result = get_symbol_history("local/myrepo", "s1",
                                    storage_path=str(storage_dir))

        assert "_meta" in result
        assert "timing_ms" in result["_meta"]


# ---------------------------------------------------------------------------
# IndexStore.save_history integration tests
# ---------------------------------------------------------------------------

class TestSaveHistory:
    """Tests for the save_history method on IndexStore."""

    def test_save_history_creates_file(self, tmp_path):
        """save_history creates a history JSON file."""
        store = IndexStore(base_path=str(tmp_path))
        index = CodeIndex(
            repo="local/test",
            owner="local",
            name="test",
            indexed_at="2025-01-01T00:00:00",
            source_files=["main.py"],
            languages={"python": 1},
            symbols=[_make_symbol("s1", "main", "main.py", "hash1", "def main()")],
        )

        store.save_history("local", "test", index)

        history_path = tmp_path / "local-test.history.json"
        assert history_path.exists()

        with open(history_path) as f:
            data = json.load(f)

        assert "s1" in data
        assert len(data["s1"]) == 1
        assert data["s1"][0]["content_hash"] == "hash1"
        assert data["s1"][0]["signature"] == "def main()"

    def test_save_history_appends_on_change(self, tmp_path):
        """save_history appends a new entry when content_hash changes."""
        store = IndexStore(base_path=str(tmp_path))

        # First save
        index1 = CodeIndex(
            repo="local/test", owner="local", name="test",
            indexed_at="2025-01-01T00:00:00",
            source_files=["main.py"], languages={"python": 1},
            symbols=[_make_symbol("s1", "main", "main.py", "hash1", "def main()")],
        )
        store.save_history("local", "test", index1)

        # Second save with changed hash
        index2 = CodeIndex(
            repo="local/test", owner="local", name="test",
            indexed_at="2025-02-01T00:00:00",
            source_files=["main.py"], languages={"python": 1},
            symbols=[_make_symbol("s1", "main", "main.py", "hash2", "def main(x)")],
        )
        store.save_history("local", "test", index2)

        data = store.load_history("local", "test")
        assert len(data["s1"]) == 2
        assert data["s1"][0]["content_hash"] == "hash1"
        assert data["s1"][1]["content_hash"] == "hash2"

    def test_save_history_skips_unchanged(self, tmp_path):
        """save_history does not append when content_hash is unchanged."""
        store = IndexStore(base_path=str(tmp_path))

        index1 = CodeIndex(
            repo="local/test", owner="local", name="test",
            indexed_at="2025-01-01T00:00:00",
            source_files=["main.py"], languages={"python": 1},
            symbols=[_make_symbol("s1", "main", "main.py", "hash1", "def main()")],
        )
        store.save_history("local", "test", index1)

        # Same hash, different timestamp
        index2 = CodeIndex(
            repo="local/test", owner="local", name="test",
            indexed_at="2025-02-01T00:00:00",
            source_files=["main.py"], languages={"python": 1},
            symbols=[_make_symbol("s1", "main", "main.py", "hash1", "def main()")],
        )
        store.save_history("local", "test", index2)

        data = store.load_history("local", "test")
        assert len(data["s1"]) == 1  # No duplicate entry

    def test_load_history_empty(self, tmp_path):
        """load_history returns empty dict when no history file."""
        store = IndexStore(base_path=str(tmp_path))
        data = store.load_history("local", "nonexistent")
        assert data == {}
