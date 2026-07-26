"""Tests for get_review_context tool."""
from firekeep_symdex.parser import parse_file
from firekeep_symdex.parser.references import extract_references
from firekeep_symdex.storage import IndexStore
from firekeep_symdex.tools.get_review_context import get_review_context


def _build_test_index(files_dict, storage_path):
    """Helper: parse files, extract refs, save index."""
    all_symbols = []
    all_refs = []
    raw_files = {}
    for path, content in files_dict.items():
        lang = "javascript" if path.endswith(".js") else "python"
        symbols = parse_file(content, path, lang)
        refs = extract_references(content, path, lang)
        for r in refs:
            r["file"] = path
        all_symbols.extend(symbols)
        all_refs.extend(refs)
        raw_files[path] = content

    store = IndexStore(base_path=str(storage_path))
    store.save_index(
        owner="test", name="test-repo",
        source_files=list(files_dict.keys()),
        symbols=all_symbols,
        raw_files=raw_files,
        languages={"javascript": len(files_dict)},
        references=all_refs,
    )
    return store


def test_review_context_finds_changed_symbols(tmp_path):
    """Changed symbols from the specified file are included."""
    files = {
        "lib/auth.js": 'function login(user) { return validate(user); }\nfunction validate(user) { return true; }',
        "lib/utils.js": 'function helper() { return login("admin"); }',
        "tests/test_auth.js": 'function test_login() { login("test"); }',
    }
    _build_test_index(files, tmp_path)

    result = get_review_context(
        repo="test/test-repo",
        changed_files=["lib/auth.js"],
        storage_path=str(tmp_path),
    )

    assert "error" not in result
    changed = result["sections"]["changed"]
    assert len(changed) > 0
    changed_names = {s["name"] for s in changed}
    assert "login" in changed_names


def test_review_context_finds_callers(tmp_path):
    """Callers of changed symbols in other files are identified."""
    files = {
        "lib/auth.js": 'function login(user) { return true; }',
        "lib/app.js": 'function main() { login("admin"); }',
    }
    _build_test_index(files, tmp_path)

    result = get_review_context(
        repo="test/test-repo",
        changed_files=["lib/auth.js"],
        storage_path=str(tmp_path),
    )

    callers = result["sections"]["callers"]
    caller_names = {s["name"] for s in callers}
    assert "main" in caller_names


def test_review_context_finds_test_files(tmp_path):
    """Related test files are detected by name matching."""
    files = {
        "lib/auth.js": 'function login() { return true; }',
        "tests/test_auth.js": 'function test_login() { login(); }',
    }
    _build_test_index(files, tmp_path)

    result = get_review_context(
        repo="test/test-repo",
        changed_files=["lib/auth.js"],
        storage_path=str(tmp_path),
    )

    assert "tests/test_auth.js" in result["related_test_files"]


def test_review_context_respects_budget(tmp_path):
    """Token budget is respected even with large symbols."""
    files = {
        "lib/big.js": 'function big() { return "x".repeat(10000); }',
    }
    _build_test_index(files, tmp_path)

    result = get_review_context(
        repo="test/test-repo",
        changed_files=["lib/big.js"],
        budget_tokens=500,
        storage_path=str(tmp_path),
    )

    assert result["_meta"]["tokens_used"] <= 500


def test_review_context_returns_error_for_missing_repo(tmp_path):
    """Returns error dict when repo is not indexed."""
    result = get_review_context(
        repo="nonexistent/repo",
        changed_files=["foo.js"],
        storage_path=str(tmp_path),
    )

    assert "error" in result


def test_review_context_meta_fields(tmp_path):
    """Result contains expected metadata fields."""
    files = {
        "lib/auth.js": 'function login() { return true; }',
    }
    _build_test_index(files, tmp_path)

    result = get_review_context(
        repo="test/test-repo",
        changed_files=["lib/auth.js"],
        storage_path=str(tmp_path),
    )

    meta = result["_meta"]
    assert "timing_ms" in meta
    assert "tokens_used" in meta
    assert "tokens_budget" in meta
    assert "changed_symbols" in meta
    assert "affected_callers" in meta
    assert "dependencies" in meta
    assert "test_symbols" in meta
    assert result["repo"] == "test/test-repo"


def test_review_context_finds_dependencies(tmp_path):
    """Dependencies called by changed symbols are identified."""
    files = {
        "lib/auth.js": 'function login() { return validate(); }',
        "lib/validate.js": 'function validate() { return true; }',
    }
    _build_test_index(files, tmp_path)

    result = get_review_context(
        repo="test/test-repo",
        changed_files=["lib/auth.js"],
        storage_path=str(tmp_path),
    )

    deps = result["sections"]["dependencies"]
    dep_names = {s["name"] for s in deps}
    assert "validate" in dep_names


def test_review_context_no_changed_files(tmp_path):
    """Empty changed_files list returns empty sections."""
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


def test_review_context_source_included(tmp_path):
    """Each symbol entry includes its source code."""
    files = {
        "lib/auth.js": 'function login() { return true; }',
    }
    _build_test_index(files, tmp_path)

    result = get_review_context(
        repo="test/test-repo",
        changed_files=["lib/auth.js"],
        storage_path=str(tmp_path),
    )

    changed = result["sections"]["changed"]
    assert len(changed) > 0
    for sym in changed:
        assert "source" in sym
        assert len(sym["source"]) > 0
        assert "context_type" in sym
        assert sym["context_type"] == "changed"
