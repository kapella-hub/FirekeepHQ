"""Tests for bug fixes in the tools and server layer."""
from firekeep_symdex.parser import parse_file
from firekeep_symdex.parser.references import extract_references
from firekeep_symdex.storage import IndexStore
from firekeep_symdex.tools._utils import resolve_call_targets


def _build_test_index(files_dict, storage_path, owner="test", repo_name="test-repo"):
    """Helper: parse files, extract refs, save index."""
    all_symbols = []
    all_refs = []
    raw_files = {}
    for path, content in files_dict.items():
        if path.endswith(".py"):
            lang = "python"
        elif path.endswith(".js"):
            lang = "javascript"
        elif path.endswith(".ts"):
            lang = "typescript"
        else:
            lang = "javascript"
        symbols = parse_file(content, path, lang)
        refs = extract_references(content, path, lang)
        for r in refs:
            r["file"] = path
        all_symbols.extend(symbols)
        all_refs.extend(refs)
        raw_files[path] = content

    languages = {}
    for path in files_dict:
        ext = path.rsplit(".", 1)[-1]
        languages[ext] = languages.get(ext, 0) + 1

    store = IndexStore(base_path=str(storage_path))
    store.save_index(
        owner=owner,
        name=repo_name,
        source_files=list(files_dict.keys()),
        symbols=all_symbols,
        raw_files=raw_files,
        languages=languages,
        references=all_refs,
    )
    return store


# ==============================================================================
# Bug #1: find_dead_code ignores import references
# ==============================================================================

class TestFindDeadCodeImportReferences:
    """Symbols referenced only via imports should NOT be flagged as dead code."""

    def test_imported_constant_not_dead(self, tmp_path):
        """A constant imported by another file should not be considered dead."""
        from firekeep_symdex.tools.find_dead_code import find_dead_code

        files = {
            "lib/config.py": '''
MAX_RETRIES = 3
DEFAULT_TIMEOUT = 30
''',
            "lib/app.py": '''
from lib.config import MAX_RETRIES

def connect():
    return MAX_RETRIES
''',
        }
        _build_test_index(files, tmp_path)
        result = find_dead_code("test/test-repo", storage_path=str(tmp_path))

        assert "error" not in result
        dead_names = {s["name"] for s in result["dead_symbols"]}
        # MAX_RETRIES is imported, so it should NOT be dead
        assert "MAX_RETRIES" not in dead_names

    def test_imported_class_not_dead(self, tmp_path):
        """A class imported but never called should not be considered dead."""
        from firekeep_symdex.tools.find_dead_code import find_dead_code

        files = {
            "lib/models.py": '''
class UserModel:
    pass

class OrphanModel:
    pass
''',
            "lib/app.py": '''
from lib.models import UserModel

def get_schema():
    return UserModel
''',
        }
        _build_test_index(files, tmp_path)
        result = find_dead_code("test/test-repo", storage_path=str(tmp_path))

        assert "error" not in result
        dead_names = {s["name"] for s in result["dead_symbols"]}
        # UserModel is imported, should not be dead
        assert "UserModel" not in dead_names
        # OrphanModel is never imported or called, should be dead
        assert "OrphanModel" in dead_names

    def test_js_import_not_dead(self, tmp_path):
        """A JS module imported via import statement should not be dead."""
        from firekeep_symdex.tools.find_dead_code import find_dead_code

        files = {
            "lib/utils.js": '''
function formatDate() { return "today"; }
function unusedHelper() { return "unused"; }
''',
            "lib/app.js": '''
import './utils';

function main() { formatDate(); }
''',
        }
        _build_test_index(files, tmp_path)
        result = find_dead_code("test/test-repo", storage_path=str(tmp_path))

        assert "error" not in result
        dead_names = {s["name"] for s in result["dead_symbols"]}
        # formatDate is called, should not be dead
        assert "formatDate" not in dead_names


# ==============================================================================
# Bug #2: get_review_context test file matching false positives
# ==============================================================================

class TestReviewContextTestFileMatching:
    """Test file matching should not produce false positives for short names."""

    def test_short_filename_no_false_positives(self, tmp_path):
        """Changing 'a.js' should not match test files just because 'a' appears in their name."""
        from firekeep_symdex.tools.get_review_context import get_review_context

        files = {
            "lib/a.js": 'function foo() { return 1; }',
            "tests/test_database.js": 'function test_db() { return 2; }',
            "tests/test_auth.js": 'function test_login() { return 3; }',
        }
        _build_test_index(files, tmp_path)

        result = get_review_context(
            repo="test/test-repo",
            changed_files=["lib/a.js"],
            storage_path=str(tmp_path),
        )

        assert "error" not in result
        # "a" is too short to match against test file names
        # Before fix: both test files would match because "a" is in "test_database" and "test_auth"
        assert len(result["related_test_files"]) == 0

    def test_normal_filename_still_matches(self, tmp_path):
        """Normal-length filenames should still find related test files."""
        from firekeep_symdex.tools.get_review_context import get_review_context

        files = {
            "lib/auth.js": 'function login() { return true; }',
            "tests/test_auth.js": 'function test_login() { return true; }',
            "tests/test_unrelated.js": 'function test_other() { return false; }',
        }
        _build_test_index(files, tmp_path)

        result = get_review_context(
            repo="test/test-repo",
            changed_files=["lib/auth.js"],
            storage_path=str(tmp_path),
        )

        assert "error" not in result
        assert "tests/test_auth.js" in result["related_test_files"]
        assert "tests/test_unrelated.js" not in result["related_test_files"]


# ==============================================================================
# Edge case tests
# ==============================================================================

class TestGetReviewContextEdgeCases:
    """Edge case tests for get_review_context."""

    def test_empty_changed_files(self, tmp_path):
        """Empty changed_files list returns empty sections."""
        from firekeep_symdex.tools.get_review_context import get_review_context

        files = {
            "lib/auth.js": 'function login() { return true; }',
        }
        _build_test_index(files, tmp_path)

        result = get_review_context(
            repo="test/test-repo",
            changed_files=[],
            storage_path=str(tmp_path),
        )

        assert "error" not in result
        assert len(result["sections"]["changed"]) == 0
        assert len(result["sections"]["callers"]) == 0
        assert len(result["sections"]["dependencies"]) == 0
        assert len(result["sections"]["tests"]) == 0
        assert result["_meta"]["tokens_used"] == 0

    def test_nonexistent_files(self, tmp_path):
        """Non-existent files in changed_files produce empty sections gracefully."""
        from firekeep_symdex.tools.get_review_context import get_review_context

        files = {
            "lib/auth.js": 'function login() { return true; }',
        }
        _build_test_index(files, tmp_path)

        result = get_review_context(
            repo="test/test-repo",
            changed_files=["nonexistent/file.js", "also/missing.py"],
            storage_path=str(tmp_path),
        )

        assert "error" not in result
        assert len(result["sections"]["changed"]) == 0
        assert result["_meta"]["tokens_used"] == 0


class TestGetContextEdgeCases:
    """Edge case tests for get_context."""

    def test_budget_tokens_zero_clamps_to_minimum(self, tmp_path):
        """budget_tokens=0 is clamped to minimum (100) and doesn't crash."""
        from firekeep_symdex.tools.get_context import get_context

        files = {
            "lib/small.js": 'function tiny() { return 1; }',
        }
        _build_test_index(files, tmp_path)

        result = get_context(
            repo="test/test-repo",
            budget_tokens=0,
            storage_path=str(tmp_path),
        )

        assert "error" not in result
        # Budget should have been clamped to 100
        assert result["_meta"]["tokens_budget"] == 100

    def test_budget_tokens_negative_clamps_to_minimum(self, tmp_path):
        """Negative budget_tokens is clamped to minimum."""
        from firekeep_symdex.tools.get_context import get_context

        files = {
            "lib/small.js": 'function tiny() { return 1; }',
        }
        _build_test_index(files, tmp_path)

        result = get_context(
            repo="test/test-repo",
            budget_tokens=-500,
            storage_path=str(tmp_path),
        )

        assert "error" not in result
        assert result["_meta"]["tokens_budget"] == 100


class TestGetImpactEdgeCases:
    """Edge case tests for get_impact."""

    def test_symbol_with_no_callers(self, tmp_path):
        """A symbol with no callers should return empty impact tree."""
        from firekeep_symdex.tools.get_impact import get_impact

        files = {
            "lib/utils.js": '''
function isolated() { return "nobody calls me"; }
function main() { return "entry point"; }
''',
        }
        _build_test_index(files, tmp_path)

        # Find the isolated symbol
        store = IndexStore(base_path=str(tmp_path))
        index = store.load_index("test", "test-repo")
        isolated_sym = None
        for sym in index.symbols:
            if sym["name"] == "isolated":
                isolated_sym = sym
                break
        assert isolated_sym is not None

        result = get_impact(
            repo="test/test-repo",
            symbol_id=isolated_sym["id"],
            storage_path=str(tmp_path),
        )

        assert "error" not in result
        assert result["total_impacted"] == 0
        assert result["impact_tree"] == []
        assert result["impacted_files"] == []
        assert result["max_depth_reached"] == 0

    def test_invalid_symbol_id(self, tmp_path):
        """Invalid symbol_id returns an error."""
        from firekeep_symdex.tools.get_impact import get_impact

        files = {"lib/a.js": 'function foo() { return 1; }'}
        _build_test_index(files, tmp_path)

        result = get_impact(
            repo="test/test-repo",
            symbol_id="nonexistent-symbol-id",
            storage_path=str(tmp_path),
        )

        assert "error" in result
        assert "Symbol not found" in result["error"]


class TestFindDeadCodeEdgeCases:
    """Edge case tests for find_dead_code."""

    def test_empty_repo(self, tmp_path):
        """find_dead_code on a repo with no symbols returns empty list."""
        from firekeep_symdex.tools.find_dead_code import find_dead_code

        # Create an index with no files/symbols
        store = IndexStore(base_path=str(tmp_path))
        store.save_index(
            owner="test",
            name="empty-repo",
            source_files=[],
            symbols=[],
            raw_files={},
            languages={},
            references=[],
        )

        result = find_dead_code("test/empty-repo", storage_path=str(tmp_path))

        assert "error" not in result
        assert result["dead_count"] == 0
        assert result["dead_symbols"] == []
        assert result["total_symbols"] == 0

    def test_not_indexed_returns_error(self, tmp_path):
        """Querying a non-existent repo returns an error."""
        from firekeep_symdex.tools.find_dead_code import find_dead_code

        result = find_dead_code("test/nonexistent", storage_path=str(tmp_path))
        assert "error" in result


class TestResolveCallTargetsEdgeCases:
    """Edge case tests for resolve_call_targets."""

    def test_empty_index(self, tmp_path):
        """resolve_call_targets with an index that has no symbols returns empty."""
        store = IndexStore(base_path=str(tmp_path))
        store.save_index(
            owner="test",
            name="empty-repo",
            source_files=[],
            symbols=[],
            raw_files={},
            languages={},
            references=[],
        )
        index = store.load_index("test", "empty-repo")
        assert index is not None

        targets = resolve_call_targets(index, "anything", "any_file.py")
        assert targets == []

    def test_no_matching_symbols(self, tmp_path):
        """resolve_call_targets returns empty when no symbols match the name."""
        files = {
            "lib/app.js": 'function foo() { return 1; }',
        }
        store = _build_test_index(files, tmp_path)
        index = store.load_index("test", "test-repo")

        targets = resolve_call_targets(index, "nonexistent_function", "lib/app.js")
        assert targets == []

    def test_no_references(self, tmp_path):
        """resolve_call_targets works even when index has no references."""
        store = IndexStore(base_path=str(tmp_path))
        # Create index with symbols but no references
        from firekeep_symdex.parser import parse_file
        content = 'function foo() { return 1; }'
        symbols = parse_file(content, "app.js", "javascript")
        store.save_index(
            owner="test",
            name="no-refs-repo",
            source_files=["app.js"],
            symbols=symbols,
            raw_files={"app.js": content},
            languages={"javascript": 1},
            references=[],
        )
        index = store.load_index("test", "no-refs-repo")

        targets = resolve_call_targets(index, "foo", "app.js")
        # Should still find the symbol by name even with no references
        assert len(targets) >= 1


class TestGetImportGraphEdgeCases:
    """Edge case tests for get_import_graph."""

    def test_empty_repo(self, tmp_path):
        """Import graph for empty repo returns empty graph."""
        from firekeep_symdex.tools.get_import_graph import get_import_graph

        store = IndexStore(base_path=str(tmp_path))
        store.save_index(
            owner="test",
            name="empty-repo",
            source_files=[],
            symbols=[],
            raw_files={},
            languages={},
            references=[],
        )

        result = get_import_graph("test/empty-repo", storage_path=str(tmp_path))

        assert "error" not in result
        assert result["node_count"] == 0
        assert result["edge_count"] == 0

    def test_file_path_filter_nonexistent(self, tmp_path):
        """Filtering by a non-existent file_path returns a graph with just that file node."""
        from firekeep_symdex.tools.get_import_graph import get_import_graph

        files = {
            "lib/a.js": 'import "./b";\nfunction foo() { return 1; }',
            "lib/b.js": 'function bar() { return 2; }',
        }
        _build_test_index(files, tmp_path)

        result = get_import_graph(
            "test/test-repo",
            file_path="nonexistent.js",
            storage_path=str(tmp_path),
        )

        assert "error" not in result


class TestGetArchitectureMapEdgeCases:
    """Edge case tests for get_architecture_map."""

    def test_empty_repo(self, tmp_path):
        """Architecture map for empty repo returns empty layers."""
        from firekeep_symdex.tools.get_architecture_map import get_architecture_map

        store = IndexStore(base_path=str(tmp_path))
        store.save_index(
            owner="test",
            name="empty-repo",
            source_files=[],
            symbols=[],
            raw_files={},
            languages={},
            references=[],
        )

        result = get_architecture_map("test/empty-repo", storage_path=str(tmp_path))

        assert "error" not in result
        assert result["file_count"] == 0
        assert result["spine"] == []
