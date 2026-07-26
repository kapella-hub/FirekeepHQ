"""Tests for auto-reindex: refresh_file, maybe_refresh_files, and tool integration."""


import pytest

from firekeep_symdex.storage.index_store import IndexStore, _file_hash
from firekeep_symdex.tools._utils import maybe_refresh_files
from firekeep_symdex.tools.get_file_outline import get_file_outline
from firekeep_symdex.tools.get_symbol import get_symbol, get_symbols
from firekeep_symdex.parser.extractor import parse_file
from firekeep_symdex.parser.references import extract_references


INITIAL_PYTHON = '''\
def greet(name):
    """Say hello."""
    return f"Hello, {name}!"
'''

MODIFIED_PYTHON = '''\
def greet(name):
    """Say hello."""
    return f"Hello, {name}!"


def farewell(name):
    """Say goodbye."""
    return f"Goodbye, {name}!"
'''


@pytest.fixture
def indexed_folder(tmp_path):
    """Create a temp folder, index it, and return (store, owner, name, folder_path)."""
    # Create a Python file
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    py_file = src_dir / "greet.py"
    py_file.write_text(INITIAL_PYTHON, encoding="utf-8")

    # Index the folder
    storage_path = str(tmp_path / "index_store")
    owner = "local"
    name = tmp_path.name

    store = IndexStore(base_path=storage_path)

    # Parse and save
    content = py_file.read_text(encoding="utf-8")
    rel_path = "src/greet.py"
    symbols = parse_file(content, rel_path, "python")
    refs = extract_references(content, rel_path, "python")
    for ref in refs:
        ref["file"] = rel_path

    store.save_index(
        owner=owner,
        name=name,
        source_files=[rel_path],
        symbols=symbols,
        raw_files={rel_path: content},
        languages={"python": 1},
        file_hashes={rel_path: _file_hash(content)},
        references=refs,
        repo_root=str(tmp_path),
    )

    return store, owner, name, tmp_path, storage_path


class TestRefreshFile:
    """Tests for IndexStore.refresh_file()."""

    def test_unchanged_file_returns_false(self, indexed_folder):
        store, owner, name, folder, _ = indexed_folder
        result = store.refresh_file(owner, name, "src/greet.py", str(folder))
        assert result is False

    def test_modified_file_returns_true(self, indexed_folder):
        store, owner, name, folder, _ = indexed_folder
        # Modify the file
        (folder / "src" / "greet.py").write_text(MODIFIED_PYTHON, encoding="utf-8")
        result = store.refresh_file(owner, name, "src/greet.py", str(folder))
        assert result is True

    def test_modified_file_updates_symbols(self, indexed_folder):
        store, owner, name, folder, _ = indexed_folder

        # Before modification: only greet + preamble (if any)
        index = store.load_index(owner, name)
        old_names = {s["name"] for s in index.symbols}
        assert "greet" in old_names
        assert "farewell" not in old_names

        # Modify and refresh
        (folder / "src" / "greet.py").write_text(MODIFIED_PYTHON, encoding="utf-8")
        store.refresh_file(owner, name, "src/greet.py", str(folder))

        # After refresh: both functions present
        index = store.load_index(owner, name)
        new_names = {s["name"] for s in index.symbols}
        assert "greet" in new_names
        assert "farewell" in new_names

    def test_modified_file_updates_hash(self, indexed_folder):
        store, owner, name, folder, _ = indexed_folder
        (folder / "src" / "greet.py").write_text(MODIFIED_PYTHON, encoding="utf-8")
        store.refresh_file(owner, name, "src/greet.py", str(folder))

        index = store.load_index(owner, name)
        assert index.file_hashes["src/greet.py"] == _file_hash(MODIFIED_PYTHON)

    def test_second_refresh_is_noop(self, indexed_folder):
        store, owner, name, folder, _ = indexed_folder
        (folder / "src" / "greet.py").write_text(MODIFIED_PYTHON, encoding="utf-8")

        assert store.refresh_file(owner, name, "src/greet.py", str(folder)) is True
        assert store.refresh_file(owner, name, "src/greet.py", str(folder)) is False

    def test_nonexistent_file_returns_false(self, indexed_folder):
        store, owner, name, folder, _ = indexed_folder
        result = store.refresh_file(owner, name, "src/nope.py", str(folder))
        assert result is False

    def test_unsupported_extension_returns_false(self, indexed_folder):
        store, owner, name, folder, _ = indexed_folder
        # Create a .txt file
        (folder / "readme.txt").write_text("hello", encoding="utf-8")
        result = store.refresh_file(owner, name, "readme.txt", str(folder))
        assert result is False

    def test_raw_content_file_updated(self, indexed_folder):
        store, owner, name, folder, _ = indexed_folder
        (folder / "src" / "greet.py").write_text(MODIFIED_PYTHON, encoding="utf-8")
        store.refresh_file(owner, name, "src/greet.py", str(folder))

        # Read the raw content from the store's content dir
        content_file = store._content_dir(owner, name) / "src" / "greet.py"
        assert content_file.exists()
        stored_content = content_file.read_text(encoding="utf-8")
        assert stored_content == MODIFIED_PYTHON

    def test_caches_invalidated(self, indexed_folder):
        store, owner, name, folder, _ = indexed_folder

        # Prime caches
        index = store.load_index(owner, name)
        _ = index.get_symbols_in_file("src/greet.py")
        assert index._symbols_by_file is not None

        # Modify and refresh
        (folder / "src" / "greet.py").write_text(MODIFIED_PYTHON, encoding="utf-8")
        store.refresh_file(owner, name, "src/greet.py", str(folder))

        # Caches should be cleared
        assert index._symbol_index is None
        assert index._symbols_by_file is None
        assert index._refs_by_file_type is None


class TestMaybeRefreshFiles:
    """Tests for maybe_refresh_files helper."""

    def test_no_repo_root_returns_zero(self, indexed_folder):
        store, owner, name, folder, _ = indexed_folder
        # Clear repo_root
        index = store.load_index(owner, name)
        index.repo_root = ""
        count = maybe_refresh_files(store, owner, name, ["src/greet.py"], index=index)
        assert count == 0

    def test_refreshes_modified_files(self, indexed_folder):
        store, owner, name, folder, _ = indexed_folder
        (folder / "src" / "greet.py").write_text(MODIFIED_PYTHON, encoding="utf-8")
        count = maybe_refresh_files(store, owner, name, ["src/greet.py"])
        assert count == 1

    def test_skips_unchanged_files(self, indexed_folder):
        store, owner, name, folder, _ = indexed_folder
        count = maybe_refresh_files(store, owner, name, ["src/greet.py"])
        assert count == 0


class TestToolIntegration:
    """Tests that hot-path tools return fresh data after file modification."""

    def test_get_file_outline_returns_new_symbols(self, indexed_folder):
        store, owner, name, folder, storage_path = indexed_folder
        # Modify the file
        (folder / "src" / "greet.py").write_text(MODIFIED_PYTHON, encoding="utf-8")

        result = get_file_outline(
            repo=f"{owner}/{name}",
            file_path="src/greet.py",
            storage_path=storage_path,
        )

        assert "error" not in result
        symbol_names = {s["name"] for s in result["symbols"]}
        assert "farewell" in symbol_names
        assert "greet" in symbol_names

    def test_get_symbol_returns_new_source(self, indexed_folder):
        store, owner, name, folder, storage_path = indexed_folder

        # Get the initial symbol ID for greet
        index = store.load_index(owner, name)
        greet_sym = None
        for s in index.symbols:
            if s["name"] == "greet":
                greet_sym = s
                break
        assert greet_sym is not None
        symbol_id = greet_sym["id"]

        # Modify file (greet still exists but content unchanged - source stays same)
        (folder / "src" / "greet.py").write_text(MODIFIED_PYTHON, encoding="utf-8")

        result = get_symbol(
            repo=f"{owner}/{name}",
            symbol_id=symbol_id,
            storage_path=storage_path,
        )

        assert "error" not in result
        assert result["name"] == "greet"
        assert "source" in result

    def test_get_symbols_refreshes_files(self, indexed_folder):
        store, owner, name, folder, storage_path = indexed_folder

        index = store.load_index(owner, name)
        symbol_ids = [s["id"] for s in index.symbols if s["name"] == "greet"]
        assert len(symbol_ids) > 0

        # Modify file
        (folder / "src" / "greet.py").write_text(MODIFIED_PYTHON, encoding="utf-8")

        result = get_symbols(
            repo=f"{owner}/{name}",
            symbol_ids=symbol_ids,
            storage_path=storage_path,
        )

        assert "error" not in result
        assert len(result["symbols"]) > 0


class TestRepoRootPersistence:
    """Tests that repo_root is persisted and loaded correctly."""

    def test_repo_root_stored_in_index(self, indexed_folder):
        store, owner, name, folder, _ = indexed_folder
        index = store.load_index(owner, name)
        assert index.repo_root == str(folder)

    def test_repo_root_survives_reload(self, indexed_folder):
        store, owner, name, folder, storage_path = indexed_folder
        # Create a fresh store to force disk reload
        from firekeep_symdex.storage.index_store import _cache_invalidate
        index_path = store._index_path(owner, name)
        _cache_invalidate(str(index_path))

        store2 = IndexStore(base_path=storage_path)
        index = store2.load_index(owner, name)
        assert index is not None
        assert index.repo_root == str(folder)
