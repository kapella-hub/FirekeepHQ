"""Tests for intelligence suite tools."""
from firekeep_symdex.parser import parse_file
from firekeep_symdex.parser.references import extract_references
from firekeep_symdex.storage import IndexStore

# Shared test fixtures
MULTI_FILE_JS = {
    "lib/main.js": '''
function main() {
    const result = helper();
    return result;
}

function helper() {
    return utils();
}

function unused() {
    return "never called";
}

function alsoUnused() {
    return "also never called";
}
''',
    "lib/utils.js": '''
function utils() {
    return "utility";
}

function deadUtil() {
    return "dead";
}
''',
}


def _build_test_index(files_dict, storage_path):
    """Helper: parse files, extract refs, save index."""
    all_symbols = []
    all_refs = []
    raw_files = {}
    for path, content in files_dict.items():
        lang = "javascript" if path.endswith(".js") else "python"
        symbols = parse_file(content, path, lang)
        refs = extract_references(content, path, lang)
        # Tag refs with file
        for r in refs:
            r["file"] = path
        all_symbols.extend(symbols)
        all_refs.extend(refs)
        raw_files[path] = content

    languages = {"javascript": len(files_dict)}
    store = IndexStore(base_path=storage_path)
    store.save_index(
        owner="test",
        name="test-repo",
        source_files=list(files_dict.keys()),
        symbols=all_symbols,
        raw_files=raw_files,
        languages=languages,
        references=all_refs,
    )
    return store


def test_find_dead_code_basic(tmp_path):
    """Test that unreferenced functions are detected as dead code."""
    from firekeep_symdex.tools.find_dead_code import find_dead_code

    _build_test_index(MULTI_FILE_JS, str(tmp_path))
    result = find_dead_code("test/test-repo", storage_path=str(tmp_path))

    assert "error" not in result
    dead_names = {s["name"] for s in result["dead_symbols"]}
    # These are never called
    assert "unused" in dead_names
    assert "alsoUnused" in dead_names
    assert "deadUtil" in dead_names
    # These ARE called (main is entry point, helper/utils are called)
    assert "main" not in dead_names
    assert "helper" not in dead_names
    assert "utils" not in dead_names


def test_find_dead_code_metadata(tmp_path):
    """Test that result contains expected metadata fields."""
    from firekeep_symdex.tools.find_dead_code import find_dead_code

    _build_test_index(MULTI_FILE_JS, str(tmp_path))
    result = find_dead_code("test/test-repo", storage_path=str(tmp_path))

    assert "dead_count" in result
    assert "total_symbols" in result
    assert "_meta" in result
    assert "timing_ms" in result["_meta"]
    assert result["dead_count"] == len(result["dead_symbols"])
    assert result["repo"] == "test/test-repo"


def test_find_dead_code_symbol_details(tmp_path):
    """Test that dead symbol entries contain required fields."""
    from firekeep_symdex.tools.find_dead_code import find_dead_code

    _build_test_index(MULTI_FILE_JS, str(tmp_path))
    result = find_dead_code("test/test-repo", storage_path=str(tmp_path))

    for sym in result["dead_symbols"]:
        assert "name" in sym
        assert "file" in sym
        assert "line" in sym
        assert "kind" in sym
        assert "qualified_name" in sym


def test_find_dead_code_not_indexed(tmp_path):
    """Test error when repo is not indexed."""
    from firekeep_symdex.tools.find_dead_code import find_dead_code

    result = find_dead_code("test/nonexistent", storage_path=str(tmp_path))
    assert "error" in result


def test_resolve_call_targets_prefers_same_file(tmp_path):
    """Test that scope-aware resolution prefers same-file symbols over other files."""
    from firekeep_symdex.tools._utils import resolve_call_targets

    # Two files each with a function named "helper"
    files = {
        "app.js": '''
function helper() { return 1; }
function main() { helper(); }
''',
        "other.js": '''
function helper() { return 2; }
''',
    }
    store = _build_test_index(files, str(tmp_path))
    index = store.load_index("test", "test-repo")

    targets = resolve_call_targets(index, "helper", "app.js")
    # First result should be from app.js (same file preferred)
    assert len(targets) >= 1
    first_sym = index.get_symbol(targets[0])
    assert first_sym is not None
    assert first_sym["file"] == "app.js"


def test_resolve_call_targets_dotted_name(tmp_path):
    """Test that dotted call names like self.method resolve correctly."""
    from firekeep_symdex.tools._utils import resolve_call_targets

    files = {
        "app.py": '''
class MyClass:
    def parse(self):
        pass

    def run(self):
        self.parse()
''',
    }
    store = _build_test_index(files, str(tmp_path))
    index = store.load_index("test", "test-repo")

    targets = resolve_call_targets(index, "self.parse", "app.py")
    assert len(targets) >= 1
    first_sym = index.get_symbol(targets[0])
    assert first_sym is not None
    assert first_sym["name"] == "parse"
    assert first_sym["file"] == "app.py"


def test_find_dependency_ids_scope_aware(tmp_path):
    """Test that _find_dependency_ids uses scope-aware resolution."""
    from firekeep_symdex.tools.get_context import _find_dependency_ids

    # Two files with same-named function; main calls "helper" and should
    # resolve to its own file's helper, not the other file's.
    files = {
        "app.js": '''
function helper() { return 1; }
function main() { helper(); }
''',
        "other.js": '''
function helper() { return 2; }
''',
    }
    store = _build_test_index(files, str(tmp_path))
    index = store.load_index("test", "test-repo")

    # Find the "main" symbol
    main_sym = None
    for sym in index.symbols:
        if sym["name"] == "main" and sym["file"] == "app.js":
            main_sym = sym
            break
    assert main_sym is not None

    dep_ids = _find_dependency_ids(index, main_sym["id"])
    assert len(dep_ids) == 1
    dep_sym = index.get_symbol(dep_ids[0])
    assert dep_sym["file"] == "app.js"
    assert dep_sym["name"] == "helper"


def test_find_dead_code_excludes_test_files(tmp_path):
    """Test that symbols from test files are excluded by default."""
    from firekeep_symdex.tools.find_dead_code import find_dead_code

    files = {
        **MULTI_FILE_JS,
        "tests/test_foo.js": '''
function test_something() {
    return unused();
}

function helperForTests() {
    return "test helper";
}
''',
    }
    _build_test_index(files, str(tmp_path))

    # Default: exclude test files
    result = find_dead_code("test/test-repo", storage_path=str(tmp_path))
    dead_names = {s["name"] for s in result["dead_symbols"]}
    assert "helperForTests" not in dead_names  # from test file, excluded

    # With include_tests=True
    result_with_tests = find_dead_code(
        "test/test-repo", include_tests=True, storage_path=str(tmp_path)
    )
    dead_names_with = {s["name"] for s in result_with_tests["dead_symbols"]}
    # test_something starts with test_ so still excluded by entry-point rule
    assert "test_something" not in dead_names_with
    # helperForTests should now appear since it's unreferenced and include_tests is on
    assert "helperForTests" in dead_names_with


def test_commonjs_require_detected_as_import():
    """Test that require() calls are captured as import references."""
    source = '''
var path = require('path');
var merge = require('utils-merge');
const debug = require('debug')('express');
var app = module.exports = {};

function handler(req, res) {
    res.send('hello');
}
'''
    refs = extract_references(source, "app.js", "javascript")
    imports = [r for r in refs if r["type"] == "import"]
    calls = [r for r in refs if r["type"] == "call"]

    import_names = {r["name"] for r in imports}
    assert "path" in import_names
    assert "utils-merge" in import_names
    assert "debug" in import_names

    # require itself should NOT appear as a call
    call_names = {r["name"] for r in calls}
    assert "require" not in call_names


def test_get_context_deduplicates_overlapping_symbols(tmp_path):
    """Test that contained symbols (methods inside classes) don't double-count tokens."""
    from firekeep_symdex.tools.get_context import get_context

    files = {
        "lib/service.py": '''
class UserService:
    def get_user(self, user_id):
        return {"id": user_id}

    def create_user(self, name):
        return {"name": name}

    def delete_user(self, user_id):
        return True
''',
    }
    # Build index with Python
    all_symbols = []
    raw_files = {}
    for path, content in files.items():
        symbols = parse_file(content, path, "python")
        all_symbols.extend(symbols)
        raw_files[path] = content

    store = IndexStore(base_path=str(tmp_path))
    store.save_index(
        owner="test", name="dedup-repo",
        source_files=list(files.keys()),
        symbols=all_symbols,
        raw_files=raw_files,
        languages={"python": 1},
        references=[],
    )

    result = get_context(
        repo="test/dedup-repo",
        budget_tokens=10000,
        focus="user",
        storage_path=str(tmp_path),
    )

    assert "error" not in result
    included = result["symbols"]
    included_names = {s["name"] for s in included}

    # If UserService class is included, its methods should be skipped
    # (their byte ranges are contained within the class)
    if "UserService" in included_names:
        # Methods should NOT also be included since they overlap
        assert "get_user" not in included_names
        assert "create_user" not in included_names
        assert "delete_user" not in included_names

    # Total tokens should be reasonable (no double-counting)
    total_tokens = result["_meta"]["tokens_used"]
    assert total_tokens < 200  # Class is ~200 bytes = ~50 tokens
