"""Tests for storage module."""

import pytest
import json

from firekeep_symdex.storage import IndexStore, CodeIndex
from firekeep_symdex.parser import Symbol


def test_save_and_load_index(tmp_path):
    """Test saving and loading an index."""
    store = IndexStore(base_path=str(tmp_path))
    
    symbols = [
        Symbol(
            id="test-py::foo",
            file="test.py",
            name="foo",
            qualified_name="foo",
            kind="function",
            language="python",
            signature="def foo():",
            summary="Does foo",
            byte_offset=0,
            byte_length=100,
        )
    ]
    
    index = store.save_index(
        owner="testowner",
        name="testrepo",
        source_files=["test.py"],
        symbols=symbols,
        raw_files={"test.py": "def foo(): pass"},
        languages={"python": 1}
    )
    
    assert index.repo == "testowner/testrepo"
    assert len(index.symbols) == 1
    
    # Load and verify
    loaded = store.load_index("testowner", "testrepo")
    assert loaded is not None
    assert loaded.repo == "testowner/testrepo"
    assert len(loaded.symbols) == 1


def test_byte_offset_retrieval(tmp_path):
    """Test byte-offset content retrieval."""
    store = IndexStore(base_path=str(tmp_path))
    
    content = "line1\nline2\ndef foo():\n    pass\n"
    
    symbols = [
        Symbol(
            id="test-py::foo",
            file="test.py",
            name="foo",
            qualified_name="foo",
            kind="function",
            language="python",
            signature="def foo():",
            byte_offset=12,  # Start of "def foo():"
            byte_length=19,  # Length of "def foo():\n    pass"
        )
    ]
    
    store.save_index(
        owner="testowner",
        name="testrepo",
        source_files=["test.py"],
        symbols=symbols,
        raw_files={"test.py": content},
        languages={"python": 1}
    )
    
    # Retrieve symbol content
    source = store.get_symbol_content("testowner", "testrepo", "test-py::foo")
    assert source is not None
    assert "def foo():" in source


def test_list_repos(tmp_path):
    """Test listing indexed repositories."""
    store = IndexStore(base_path=str(tmp_path))
    
    # Create two indexes
    for owner, name in [("owner1", "repo1"), ("owner2", "repo2")]:
        store.save_index(
            owner=owner,
            name=name,
            source_files=["main.py"],
            symbols=[],
            raw_files={"main.py": ""},
            languages={"python": 1}
        )
    
    repos = store.list_repos()
    assert len(repos) == 2


def test_delete_index(tmp_path):
    """Test deleting an index."""
    store = IndexStore(base_path=str(tmp_path))
    
    store.save_index(
        owner="test",
        name="repo",
        source_files=["main.py"],
        symbols=[],
        raw_files={"main.py": ""},
        languages={"python": 1}
    )
    
    assert store.load_index("test", "repo") is not None
    
    store.delete_index("test", "repo")
    
    assert store.load_index("test", "repo") is None


def test_save_index_rejects_path_traversal_in_raw_files(tmp_path):
    """Raw file cache writes must not escape content dir."""
    store = IndexStore(base_path=str(tmp_path))

    with pytest.raises(ValueError, match="Unsafe file path"):
        store.save_index(
            owner="evil",
            name="repo",
            source_files=["../../escape.py"],
            symbols=[],
            raw_files={"../../escape.py": "print('x')"},
            languages={"python": 1},
        )


def test_get_symbol_content_rejects_traversal_symbol_file(tmp_path):
    """Traversal in stored symbol file path should be blocked on read."""
    store = IndexStore(base_path=str(tmp_path))
    content_dir = tmp_path / "owner-repo"
    content_dir.mkdir(parents=True)
    (content_dir / "safe.py").write_text("def ok():\n    return 1\n", encoding="utf-8")

    # Write index manually to simulate malicious/corrupt symbol metadata.
    index_path = tmp_path / "owner-repo.json"
    index_path.write_text(
        json.dumps({
            "repo": "owner/repo",
            "owner": "owner",
            "name": "repo",
            "indexed_at": "2025-01-01T00:00:00",
            "source_files": ["safe.py"],
            "languages": {"python": 1},
            "symbols": [{
                "id": "bad::sym",
                "file": "../../etc/passwd",
                "name": "bad",
                "qualified_name": "bad",
                "kind": "function",
                "language": "python",
                "signature": "def bad():",
                "docstring": "",
                "summary": "",
                "decorators": [],
                "keywords": [],
                "parent": "",
                "line": 1,
                "end_line": 1,
                "byte_offset": 0,
                "byte_length": 10,
                "content_hash": "",
            }],
            "index_version": 2,
            "file_hashes": {"safe.py": "abc"},
            "git_head": "",
        }),
        encoding="utf-8",
    )

    assert store.get_symbol_content("owner", "repo", "bad::sym") is None


def test_save_index_rejects_invalid_owner_component(tmp_path):
    """Owner/name inputs should be safe for filesystem paths."""
    store = IndexStore(base_path=str(tmp_path))

    with pytest.raises(ValueError, match="Invalid owner"):
        store.save_index(
            owner="../escape",
            name="repo",
            source_files=["main.py"],
            symbols=[],
            raw_files={"main.py": ""},
            languages={"python": 1},
        )


def test_save_index_rejects_invalid_name_component(tmp_path):
    """Reject names with path separators or unsafe characters."""
    store = IndexStore(base_path=str(tmp_path))

    with pytest.raises(ValueError, match="Invalid name"):
        store.save_index(
            owner="owner",
            name="repo/evil",
            source_files=["main.py"],
            symbols=[],
            raw_files={"main.py": ""},
            languages={"python": 1},
        )


def test_codeindex_get_symbol():
    """Test getting a symbol by ID from CodeIndex."""
    index = CodeIndex(
        repo="test/repo",
        owner="test",
        name="repo",
        indexed_at="2025-01-15T10:00:00",
        source_files=["main.py"],
        languages={"python": 1},
        symbols=[
            {"id": "main-py::foo", "name": "foo", "kind": "function"},
            {"id": "main-py::bar", "name": "bar", "kind": "function"},
        ]
    )
    
    sym = index.get_symbol("main-py::foo")
    assert sym is not None
    assert sym["name"] == "foo"
    
    assert index.get_symbol("nonexistent") is None


def test_codeindex_search():
    """Test searching symbols."""
    index = CodeIndex(
        repo="test/repo",
        owner="test",
        name="repo",
        indexed_at="2025-01-15T10:00:00",
        source_files=["main.py"],
        languages={"python": 1},
        symbols=[
            {"id": "main-py::authenticate", "name": "authenticate", "kind": "function", "signature": "def authenticate(user)", "summary": "Auth user", "keywords": ["auth"]},
            {"id": "main-py::login", "name": "login", "kind": "function", "signature": "def login()", "summary": "Login user", "keywords": []},
            {"id": "main-py::MyClass", "name": "MyClass", "kind": "class", "signature": "class MyClass", "summary": "A class", "keywords": []},
        ]
    )
    
    # Search by name
    results = index.search("authenticate")
    assert len(results) > 0
    assert results[0]["name"] == "authenticate"
    
    # Search by kind filter
    results = index.search("login", kind="class")
    assert len(results) == 0  # login is a function
    
    results = index.search("login", kind="function")
    assert len(results) > 0


def test_codeindex_get_symbols_in_file():
    """Test per-file symbol lookup."""
    index = CodeIndex(
        repo="test/repo",
        owner="test",
        name="repo",
        indexed_at="2025-01-15T10:00:00",
        source_files=["a.py", "b.py"],
        languages={"python": 2},
        symbols=[
            {"id": "a::foo", "name": "foo", "kind": "function", "file": "a.py", "line": 1, "end_line": 5},
            {"id": "a::bar", "name": "bar", "kind": "function", "file": "a.py", "line": 7, "end_line": 10},
            {"id": "b::baz", "name": "baz", "kind": "function", "file": "b.py", "line": 1, "end_line": 3},
        ],
    )

    a_syms = index.get_symbols_in_file("a.py")
    assert len(a_syms) == 2
    assert {s["name"] for s in a_syms} == {"foo", "bar"}

    b_syms = index.get_symbols_in_file("b.py")
    assert len(b_syms) == 1
    assert b_syms[0]["name"] == "baz"

    assert index.get_symbols_in_file("nonexistent.py") == []


def test_codeindex_find_containing_symbol():
    """Test finding the tightest symbol containing a line."""
    index = CodeIndex(
        repo="test/repo",
        owner="test",
        name="repo",
        indexed_at="2025-01-15T10:00:00",
        source_files=["main.py"],
        languages={"python": 1},
        symbols=[
            {"id": "main::MyClass", "name": "MyClass", "kind": "class", "file": "main.py", "line": 1, "end_line": 20},
            {"id": "main::MyClass.method", "name": "method", "kind": "method", "file": "main.py", "line": 5, "end_line": 10},
            {"id": "main::standalone", "name": "standalone", "kind": "function", "file": "main.py", "line": 22, "end_line": 25},
        ],
    )

    # Line inside method (should pick tightest: method, not class)
    assert index.find_containing_symbol("main.py", 7) == "main::MyClass.method"

    # Line inside class but outside method
    assert index.find_containing_symbol("main.py", 15) == "main::MyClass"

    # Line inside standalone function
    assert index.find_containing_symbol("main.py", 23) == "main::standalone"

    # Line not in any symbol
    assert index.find_containing_symbol("main.py", 30) is None

    # Non-existent file
    assert index.find_containing_symbol("other.py", 1) is None


def test_codeindex_get_refs():
    """Test per-file, per-type reference lookup."""
    index = CodeIndex(
        repo="test/repo",
        owner="test",
        name="repo",
        indexed_at="2025-01-15T10:00:00",
        source_files=["a.py", "b.py"],
        languages={"python": 2},
        symbols=[],
        references=[
            {"type": "import", "name": "os", "file": "a.py", "line": 1},
            {"type": "call", "name": "foo", "file": "a.py", "line": 5},
            {"type": "call", "name": "bar", "file": "a.py", "line": 8},
            {"type": "import", "name": "sys", "file": "b.py", "line": 1},
            {"type": "call", "name": "baz", "file": "b.py", "line": 3},
        ],
    )

    a_imports = index.get_refs("a.py", "import")
    assert len(a_imports) == 1
    assert a_imports[0]["name"] == "os"

    a_calls = index.get_refs("a.py", "call")
    assert len(a_calls) == 2

    b_calls = index.get_refs("b.py", "call")
    assert len(b_calls) == 1
    assert b_calls[0]["name"] == "baz"

    assert index.get_refs("nonexistent.py", "call") == []
    assert index.get_refs("a.py", "nonexistent") == []
