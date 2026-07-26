"""Tests for compare_repos and export_index tools."""

import json

import pytest

from firekeep_symdex.storage import IndexStore
from firekeep_symdex.parser.symbols import Symbol


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_symbol(
    name: str,
    kind: str = "function",
    file: str = "src/main.py",
    parent: str = "",
    signature: str = "",
    summary: str = "",
    content_hash: str = "",
    line: int = 1,
    end_line: int = 10,
    byte_offset: int = 0,
    byte_length: int = 100,
) -> Symbol:
    """Create a Symbol with sensible defaults for testing."""
    qname = f"{parent}.{name}" if parent else name
    return Symbol(
        id=f"{file}::{qname}",
        file=file,
        name=name,
        qualified_name=qname,
        kind=kind,
        language="python",
        signature=signature or f"def {name}()",
        docstring="",
        summary=summary or f"Summary of {name}",
        decorators=[],
        keywords=[],
        parent=parent,
        line=line,
        end_line=end_line,
        byte_offset=byte_offset,
        byte_length=byte_length,
        content_hash=content_hash or f"hash_{name}",
    )


def _save_test_repo(store, owner, name, symbols, raw_files=None):
    """Save a test repo index with the given symbols."""
    source_files = sorted({s.file for s in symbols})
    if raw_files is None:
        raw_files = {f: f"# content of {f}\n" for f in source_files}
    languages = {"python": len(source_files)}
    return store.save_index(
        owner=owner,
        name=name,
        source_files=source_files,
        symbols=symbols,
        raw_files=raw_files,
        languages=languages,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_storage(tmp_path):
    """Provide a temporary storage directory path as a string."""
    return str(tmp_path)


@pytest.fixture
def two_repos(tmp_storage):
    """Create two repos sharing some symbols, differing in others."""
    store = IndexStore(base_path=tmp_storage)

    # Shared symbols (same qualified_name+kind)
    shared_unchanged = _make_symbol("parse", content_hash="aaa", file="src/parser.py", signature="def parse(data)")
    shared_modified_a = _make_symbol("render", content_hash="bbb", file="src/view.py", signature="def render(ctx)")
    shared_modified_b = _make_symbol("render", content_hash="ccc", file="src/view.py", signature="def render(ctx, opts)")

    # Only in A
    only_a = _make_symbol("login", file="src/auth.py", signature="def login(user, pwd)")

    # Only in B
    only_b = _make_symbol("signup", file="src/auth.py", signature="def signup(email)")

    _save_test_repo(store, "acme", "alpha", [shared_unchanged, shared_modified_a, only_a])
    _save_test_repo(store, "acme", "beta", [shared_unchanged, shared_modified_b, only_b])

    return tmp_storage


@pytest.fixture
def single_repo(tmp_storage):
    """Create a single repo with a class hierarchy for export tests."""
    store = IndexStore(base_path=tmp_storage)

    cls = _make_symbol(
        "AuthHandler", kind="class", file="src/auth.py",
        signature="class AuthHandler", summary="Handles authentication",
        line=1, end_line=30, byte_length=500,
    )
    method_login = _make_symbol(
        "login", kind="method", file="src/auth.py",
        parent="src/auth.py::AuthHandler",
        signature="def login(username, password)", summary="Authenticate user",
        line=5, end_line=15, byte_length=200,
    )
    method_logout = _make_symbol(
        "logout", kind="method", file="src/auth.py",
        parent="src/auth.py::AuthHandler",
        signature="def logout(session)", summary="End session",
        line=17, end_line=25, byte_length=150,
    )
    util_func = _make_symbol(
        "hash_password", kind="function", file="src/utils.py",
        signature="def hash_password(pwd)", summary="Hash with bcrypt",
        line=1, end_line=8, byte_length=120,
    )

    _save_test_repo(store, "acme", "webapp", [cls, method_login, method_logout, util_func])
    return tmp_storage


# ---------------------------------------------------------------------------
# compare_repos tests
# ---------------------------------------------------------------------------

class TestCompareRepos:

    def test_basic_comparison(self, two_repos):
        from firekeep_symdex.tools.compare_repos import compare_repos
        result = compare_repos("acme/alpha", "acme/beta", storage_path=two_repos)

        assert "error" not in result
        assert result["repo_a"] == "acme/alpha"
        assert result["repo_b"] == "acme/beta"

        # only_in_a should contain "login"
        only_a_names = [e["name"] for e in result["only_in_a"]]
        assert "login" in only_a_names

        # only_in_b should contain "signup"
        only_b_names = [e["name"] for e in result["only_in_b"]]
        assert "signup" in only_b_names

        # modified should contain "render" (different content_hash)
        assert len(result["modified"]) == 1
        assert result["modified"][0]["symbol_a"]["name"] == "render"
        assert result["modified"][0]["symbol_b"]["name"] == "render"

        # unchanged should be 1 ("parse")
        assert result["unchanged_count"] == 1

    def test_summary_counts(self, two_repos):
        from firekeep_symdex.tools.compare_repos import compare_repos
        result = compare_repos("acme/alpha", "acme/beta", storage_path=two_repos)
        summary = result["summary"]

        assert summary["only_in_a"] == 1
        assert summary["only_in_b"] == 1
        assert summary["modified"] == 1
        assert summary["unchanged"] == 1

    def test_meta_present(self, two_repos):
        from firekeep_symdex.tools.compare_repos import compare_repos
        result = compare_repos("acme/alpha", "acme/beta", storage_path=two_repos)

        assert "_meta" in result
        assert "timing_ms" in result["_meta"]
        assert "tokens_saved" in result["_meta"]
        assert "cost_avoided" in result["_meta"]

    def test_symbol_entry_fields(self, two_repos):
        from firekeep_symdex.tools.compare_repos import compare_repos
        result = compare_repos("acme/alpha", "acme/beta", storage_path=two_repos)

        for entry in result["only_in_a"]:
            assert "symbol_id" in entry
            assert "name" in entry
            assert "kind" in entry
            assert "file" in entry
            assert "signature" in entry

    def test_missing_repo_a(self, tmp_storage):
        from firekeep_symdex.tools.compare_repos import compare_repos
        result = compare_repos("nonexistent/repo", "acme/beta", storage_path=tmp_storage)
        assert "error" in result

    def test_missing_repo_b(self, two_repos):
        from firekeep_symdex.tools.compare_repos import compare_repos
        result = compare_repos("acme/alpha", "nonexistent/repo", storage_path=two_repos)
        assert "error" in result

    def test_identical_repos(self, tmp_storage):
        """Comparing a repo with itself should yield no diffs."""
        from firekeep_symdex.tools.compare_repos import compare_repos

        store = IndexStore(base_path=tmp_storage)
        syms = [_make_symbol("foo", content_hash="xyz")]
        _save_test_repo(store, "acme", "same", syms)

        result = compare_repos("acme/same", "acme/same", storage_path=tmp_storage)
        assert result["only_in_a"] == []
        assert result["only_in_b"] == []
        assert result["modified"] == []
        assert result["unchanged_count"] == 1


# ---------------------------------------------------------------------------
# export_index tests
# ---------------------------------------------------------------------------

class TestExportIndex:

    def test_markdown_format(self, single_repo):
        from firekeep_symdex.tools.export_index import export_index
        result = export_index("acme/webapp", format="markdown", storage_path=single_repo)

        assert "error" not in result
        assert result["format"] == "markdown"
        content = result["content"]

        # File headers present
        assert "## src/auth.py" in content
        assert "## src/utils.py" in content

        # Top-level class
        assert "class AuthHandler" in content
        assert "Handles authentication" in content

        # Nested methods (indented)
        assert "def login(username, password)" in content
        assert "def logout(session)" in content

        # Utility function
        assert "def hash_password(pwd)" in content

    def test_json_format(self, single_repo):
        from firekeep_symdex.tools.export_index import export_index
        result = export_index("acme/webapp", format="json", storage_path=single_repo)

        assert result["format"] == "json"
        data = json.loads(result["content"])

        assert isinstance(data, list)
        assert len(data) == 2  # two files

        # Find the auth file entry
        auth_file = next(f for f in data if f["file"] == "src/auth.py")
        assert len(auth_file["symbols"]) == 1  # AuthHandler is top-level
        auth_handler = auth_file["symbols"][0]
        assert auth_handler["name"] == "AuthHandler"
        assert "children" in auth_handler
        assert len(auth_handler["children"]) == 2  # login, logout

        child_names = {c["name"] for c in auth_handler["children"]}
        assert child_names == {"login", "logout"}

    def test_json_includes_signatures_and_summaries(self, single_repo):
        from firekeep_symdex.tools.export_index import export_index
        result = export_index("acme/webapp", format="json", storage_path=single_repo)
        data = json.loads(result["content"])

        utils_file = next(f for f in data if f["file"] == "src/utils.py")
        sym = utils_file["symbols"][0]
        assert "signature" in sym
        assert "summary" in sym

    def test_without_signatures(self, single_repo):
        from firekeep_symdex.tools.export_index import export_index
        result = export_index(
            "acme/webapp", format="json",
            include_signatures=False, storage_path=single_repo,
        )
        data = json.loads(result["content"])
        for file_entry in data:
            for sym in file_entry["symbols"]:
                assert "signature" not in sym

    def test_without_summaries(self, single_repo):
        from firekeep_symdex.tools.export_index import export_index
        result = export_index(
            "acme/webapp", format="markdown",
            include_summaries=False, storage_path=single_repo,
        )
        # Summaries should not appear after "--"
        for line in result["content"].splitlines():
            if line.startswith("- ") or line.startswith("  - "):
                assert " -- " not in line

    def test_path_prefix_filter(self, single_repo):
        from firekeep_symdex.tools.export_index import export_index
        result = export_index(
            "acme/webapp", format="markdown",
            path_prefix="src/auth", storage_path=single_repo,
        )
        content = result["content"]
        assert "## src/auth.py" in content
        assert "## src/utils.py" not in content
        assert result["file_count"] == 1

    def test_invalid_format(self, single_repo):
        from firekeep_symdex.tools.export_index import export_index
        result = export_index("acme/webapp", format="xml", storage_path=single_repo)
        assert "error" in result

    def test_missing_repo(self, tmp_storage):
        from firekeep_symdex.tools.export_index import export_index
        result = export_index("nonexistent/repo", storage_path=tmp_storage)
        assert "error" in result

    def test_meta_includes_token_savings(self, single_repo):
        from firekeep_symdex.tools.export_index import export_index
        result = export_index("acme/webapp", format="markdown", storage_path=single_repo)

        meta = result["_meta"]
        assert "timing_ms" in meta
        assert "export_bytes" in meta
        assert "raw_bytes" in meta
        assert "tokens_saved" in meta
        assert "cost_avoided" in meta

    def test_symbol_and_file_counts(self, single_repo):
        from firekeep_symdex.tools.export_index import export_index
        result = export_index("acme/webapp", format="json", storage_path=single_repo)

        assert result["file_count"] == 2
        assert result["symbol_count"] == 4  # AuthHandler, login, logout, hash_password
