"""Tests for FirekeepTime tools: evolution timeline, complexity metrics, contributors, code churn."""

import subprocess

from firekeep_symdex.storage import IndexStore
from firekeep_symdex.parser import Symbol
from firekeep_symdex.tools.get_complexity_metrics import (
    get_complexity_metrics,
    _compute_nesting_depth,
    _compute_cyclomatic,
)
from firekeep_symdex.tools.get_evolution_timeline import get_evolution_timeline
from firekeep_symdex.tools.get_contributors import get_contributors
from firekeep_symdex.tools.get_code_churn import get_code_churn


# -- Helpers --

def _make_git_repo(tmp_path):
    """Create a temporary git repo with a sample file and commits."""
    repo_dir = tmp_path / "myrepo"
    repo_dir.mkdir()

    def git(*args):
        subprocess.run(
            ["git"] + list(args),
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    git("init")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test Author")

    # First commit
    sample = repo_dir / "sample.py"
    sample.write_text("def hello():\n    print('hello')\n", encoding="utf-8")
    git("add", "sample.py")
    git("commit", "-m", "Initial commit")

    # Second commit
    sample.write_text("def hello():\n    print('hello world')\n\ndef goodbye():\n    print('bye')\n", encoding="utf-8")
    git("add", "sample.py")
    git("commit", "-m", "Add goodbye function")

    return repo_dir


def _make_index(tmp_path, symbols_data, raw_files=None, references=None):
    """Helper: create a stored index with given symbols."""
    store = IndexStore(base_path=str(tmp_path / "store"))
    symbols = [
        Symbol(
            id=s["id"],
            file=s["file"],
            name=s["name"],
            qualified_name=s.get("qualified_name", s["name"]),
            kind=s["kind"],
            language=s.get("language", "python"),
            signature=s.get("signature", f"def {s['name']}():"),
            line=s.get("line", 1),
            end_line=s.get("end_line", 10),
            byte_offset=s.get("byte_offset", 0),
            byte_length=s.get("byte_length", 100),
        )
        for s in symbols_data
    ]

    if raw_files is None:
        raw_files = {}
        for s in symbols_data:
            if s["file"] not in raw_files:
                raw_files[s["file"]] = s.get("source", f"# placeholder for {s['file']}\n" * 10)

    store.save_index(
        owner="local",
        name="myrepo",
        source_files=list(raw_files.keys()),
        symbols=symbols,
        raw_files=raw_files,
        languages={"python": len(raw_files)},
        references=references or [],
    )

    return store


# -- Fixtures --

COMPLEX_SOURCE = """\
def process_data(items, config, logger):
    result = []
    for item in items:
        if item.is_valid():
            if item.type == "A":
                for sub in item.children:
                    if sub.active:
                        try:
                            val = sub.compute()
                        except ValueError:
                            logger.warn("bad value")
                            continue
                        result.append(val)
            elif item.type == "B":
                result.append(item.default)
            else:
                while item.has_next():
                    result.append(item.next())
    return result
"""

SIMPLE_SOURCE = """\
def greet(name):
    return f"Hello, {name}!"
"""


# ============================================================
# get_complexity_metrics tests
# ============================================================

class TestComplexityMetrics:
    def test_basic_metrics(self, tmp_path):
        _make_index(tmp_path, [
            {
                "id": "s1", "file": "main.py", "name": "process_data",
                "kind": "function",
                "signature": "def process_data(items, config, logger):",
                "source": COMPLEX_SOURCE,
                "byte_offset": 0,
                "byte_length": len(COMPLEX_SOURCE.encode("utf-8")),
            },
        ], raw_files={"main.py": COMPLEX_SOURCE})

        result = get_complexity_metrics(
            repo="local/myrepo",
            storage_path=str(tmp_path / "store"),
        )

        assert "error" not in result
        assert result["result_count"] == 1
        metrics = result["results"][0]
        assert metrics["name"] == "process_data"
        assert metrics["param_count"] == 3
        assert metrics["lines"] > 1
        assert metrics["nesting_depth"] > 0
        assert metrics["complexity_score"] > 1

    def test_single_symbol(self, tmp_path):
        _make_index(tmp_path, [
            {
                "id": "s1", "file": "main.py", "name": "greet",
                "kind": "function",
                "signature": "def greet(name):",
                "source": SIMPLE_SOURCE,
                "byte_offset": 0,
                "byte_length": len(SIMPLE_SOURCE.encode("utf-8")),
            },
        ], raw_files={"main.py": SIMPLE_SOURCE})

        result = get_complexity_metrics(
            repo="local/myrepo",
            symbol_id="s1",
            storage_path=str(tmp_path / "store"),
        )

        assert "error" not in result
        assert result["result_count"] == 1
        metrics = result["results"][0]
        assert metrics["param_count"] == 1
        assert metrics["complexity_score"] == 1  # no branches
        assert metrics["risk_level"] == "low"

    def test_sort_by_lines(self, tmp_path):
        _make_index(tmp_path, [
            {
                "id": "s1", "file": "main.py", "name": "short_fn",
                "kind": "function",
                "signature": "def short_fn():",
                "source": SIMPLE_SOURCE,
                "byte_offset": 0,
                "byte_length": len(SIMPLE_SOURCE.encode("utf-8")),
            },
            {
                "id": "s2", "file": "main.py", "name": "long_fn",
                "kind": "function",
                "signature": "def long_fn():",
                "source": COMPLEX_SOURCE,
                "byte_offset": len(SIMPLE_SOURCE.encode("utf-8")),
                "byte_length": len(COMPLEX_SOURCE.encode("utf-8")),
            },
        ], raw_files={"main.py": SIMPLE_SOURCE + COMPLEX_SOURCE})

        result = get_complexity_metrics(
            repo="local/myrepo",
            sort_by="lines",
            storage_path=str(tmp_path / "store"),
        )

        assert result["result_count"] == 2
        assert result["results"][0]["name"] == "long_fn"

    def test_filter_by_kind(self, tmp_path):
        _make_index(tmp_path, [
            {
                "id": "s1", "file": "main.py", "name": "MyClass",
                "kind": "class",
                "signature": "class MyClass:",
                "source": "class MyClass:\n    pass\n",
                "byte_offset": 0,
                "byte_length": 20,
            },
            {
                "id": "s2", "file": "main.py", "name": "my_func",
                "kind": "function",
                "signature": "def my_func():",
                "source": SIMPLE_SOURCE,
                "byte_offset": 20,
                "byte_length": len(SIMPLE_SOURCE.encode("utf-8")),
            },
        ], raw_files={"main.py": "class MyClass:\n    pass\n" + SIMPLE_SOURCE})

        result = get_complexity_metrics(
            repo="local/myrepo",
            kind="function",
            storage_path=str(tmp_path / "store"),
        )

        assert result["result_count"] == 1
        assert result["results"][0]["name"] == "my_func"

    def test_repo_not_indexed(self, tmp_path):
        IndexStore(base_path=str(tmp_path / "store"))
        result = get_complexity_metrics(
            repo="local/nonexistent",
            storage_path=str(tmp_path / "store"),
        )
        assert "error" in result

    def test_symbol_not_found(self, tmp_path):
        _make_index(tmp_path, [
            {
                "id": "s1", "file": "main.py", "name": "greet",
                "kind": "function", "source": SIMPLE_SOURCE,
                "byte_offset": 0, "byte_length": len(SIMPLE_SOURCE.encode("utf-8")),
            },
        ], raw_files={"main.py": SIMPLE_SOURCE})

        result = get_complexity_metrics(
            repo="local/myrepo",
            symbol_id="nonexistent",
            storage_path=str(tmp_path / "store"),
        )
        assert "error" in result


class TestComputeNestingDepth:
    def test_brace_based(self):
        source = "function foo() {\n  if (x) {\n    for (y) {\n      bar();\n    }\n  }\n}"
        depth = _compute_nesting_depth(source)
        assert depth == 3

    def test_indentation_based(self):
        source = "def foo():\n    if x:\n        for y in z:\n            bar()\n"
        depth = _compute_nesting_depth(source)
        assert depth >= 2

    def test_flat_code(self):
        source = "x = 1\ny = 2\nz = 3\n"
        depth = _compute_nesting_depth(source)
        assert depth == 0


class TestComputeCyclomatic:
    def test_simple_function(self):
        assert _compute_cyclomatic("def foo():\n    return 1\n") == 1

    def test_branches(self):
        source = "if x:\n    pass\nelif y:\n    pass\nelse:\n    pass\n"
        result = _compute_cyclomatic(source)
        assert result == 4  # if + elif + else + 1

    def test_loops_and_exceptions(self):
        source = "for x in y:\n    try:\n        pass\n    except:\n        pass\n"
        result = _compute_cyclomatic(source)
        assert result == 4  # for + try + except + 1

    def test_logical_operators(self):
        source = "if a && b || c:\n    pass\n"
        result = _compute_cyclomatic(source)
        assert result == 4  # if + && + || + 1


# ============================================================
# get_evolution_timeline tests
# ============================================================

class TestEvolutionTimeline:
    def test_file_timeline(self, tmp_path):
        repo_dir = _make_git_repo(tmp_path)
        _make_index(tmp_path, [
            {
                "id": "s1", "file": "sample.py", "name": "hello",
                "kind": "function",
                "source": "def hello():\n    print('hello world')\n",
                "byte_offset": 0, "byte_length": 40,
            },
        ], raw_files={"sample.py": "def hello():\n    print('hello world')\n"})

        result = get_evolution_timeline(
            repo=str(repo_dir),
            file_path="sample.py",
            storage_path=str(tmp_path / "store"),
        )

        assert "error" not in result
        assert result["total_changes"] >= 2
        assert result["timeline"][0]["author"] == "Test Author"
        assert result["timeline"][-1]["change_type"] == "created"

    def test_missing_params(self, tmp_path):
        result = get_evolution_timeline(
            repo="local/myrepo",
            storage_path=str(tmp_path / "store"),
        )
        assert "error" in result

    def test_non_git_repo(self, tmp_path):
        _make_index(tmp_path, [
            {
                "id": "s1", "file": "main.py", "name": "greet",
                "kind": "function", "source": SIMPLE_SOURCE,
                "byte_offset": 0, "byte_length": len(SIMPLE_SOURCE.encode("utf-8")),
            },
        ], raw_files={"main.py": SIMPLE_SOURCE})

        result = get_evolution_timeline(
            repo="local/myrepo",
            file_path="main.py",
            storage_path=str(tmp_path / "store"),
        )
        assert "error" in result

    def test_symbol_timeline(self, tmp_path):
        repo_dir = _make_git_repo(tmp_path)
        _make_index(tmp_path, [
            {
                "id": "s1", "file": "sample.py", "name": "hello",
                "kind": "function", "line": 1, "end_line": 2,
                "source": "def hello():\n    print('hello world')\n",
                "byte_offset": 0, "byte_length": 40,
            },
        ], raw_files={"sample.py": "def hello():\n    print('hello world')\n"})

        result = get_evolution_timeline(
            repo=str(repo_dir),
            symbol_id="s1",
            storage_path=str(tmp_path / "store"),
        )

        assert "error" not in result
        assert result["total_changes"] >= 1


# ============================================================
# get_contributors tests
# ============================================================

class TestContributors:
    def test_file_contributors(self, tmp_path):
        repo_dir = _make_git_repo(tmp_path)
        _make_index(tmp_path, [
            {
                "id": "s1", "file": "sample.py", "name": "hello",
                "kind": "function",
                "source": "def hello():\n    print('hello world')\n",
                "byte_offset": 0, "byte_length": 40,
            },
        ], raw_files={"sample.py": "def hello():\n    print('hello world')\n"})

        result = get_contributors(
            repo=str(repo_dir),
            file_path="sample.py",
            storage_path=str(tmp_path / "store"),
        )

        assert "error" not in result
        assert len(result["contributors"]) >= 1
        assert result["contributors"][0]["author"] == "Test Author"
        assert result["contributors"][0]["percentage"] > 0
        assert result["total_lines"] > 0

    def test_missing_file_and_symbol(self, tmp_path):
        _make_index(tmp_path, [
            {
                "id": "s1", "file": "main.py", "name": "greet",
                "kind": "function", "source": SIMPLE_SOURCE,
                "byte_offset": 0, "byte_length": len(SIMPLE_SOURCE.encode("utf-8")),
            },
        ], raw_files={"main.py": SIMPLE_SOURCE})

        result = get_contributors(
            repo="local/myrepo",
            storage_path=str(tmp_path / "store"),
        )
        assert "error" in result

    def test_non_git_repo(self, tmp_path):
        _make_index(tmp_path, [
            {
                "id": "s1", "file": "main.py", "name": "greet",
                "kind": "function", "source": SIMPLE_SOURCE,
                "byte_offset": 0, "byte_length": len(SIMPLE_SOURCE.encode("utf-8")),
            },
        ], raw_files={"main.py": SIMPLE_SOURCE})

        result = get_contributors(
            repo="local/myrepo",
            file_path="main.py",
            storage_path=str(tmp_path / "store"),
        )
        assert "error" in result


# ============================================================
# get_code_churn tests
# ============================================================

class TestCodeChurn:
    def test_basic_churn(self, tmp_path):
        repo_dir = _make_git_repo(tmp_path)
        _make_index(tmp_path, [
            {
                "id": "s1", "file": "sample.py", "name": "hello",
                "kind": "function",
                "source": "def hello():\n    print('hello world')\n",
                "byte_offset": 0, "byte_length": 40,
            },
        ], raw_files={"sample.py": "def hello():\n    print('hello world')\n"})

        result = get_code_churn(
            repo=str(repo_dir),
            storage_path=str(tmp_path / "store"),
        )

        assert "error" not in result
        assert result["result_count"] >= 1
        # sample.py should appear with commits > 0
        files = {r["file"] for r in result["results"]}
        assert "sample.py" in files
        churn_entry = [r for r in result["results"] if r["file"] == "sample.py"][0]
        assert churn_entry["commits"] >= 2
        assert churn_entry["lines_added"] > 0

    def test_with_since_filter(self, tmp_path):
        repo_dir = _make_git_repo(tmp_path)
        _make_index(tmp_path, [
            {
                "id": "s1", "file": "sample.py", "name": "hello",
                "kind": "function",
                "source": "def hello():\n    print('hello world')\n",
                "byte_offset": 0, "byte_length": 40,
            },
        ], raw_files={"sample.py": "def hello():\n    print('hello world')\n"})

        # Using a far-future date should return no results
        result = get_code_churn(
            repo=str(repo_dir),
            since="2099-01-01",
            storage_path=str(tmp_path / "store"),
        )

        assert "error" not in result
        assert result["result_count"] == 0

    def test_non_git_repo(self, tmp_path):
        _make_index(tmp_path, [
            {
                "id": "s1", "file": "main.py", "name": "greet",
                "kind": "function", "source": SIMPLE_SOURCE,
                "byte_offset": 0, "byte_length": len(SIMPLE_SOURCE.encode("utf-8")),
            },
        ], raw_files={"main.py": SIMPLE_SOURCE})

        result = get_code_churn(
            repo="local/myrepo",
            storage_path=str(tmp_path / "store"),
        )
        assert "error" in result

    def test_risk_levels(self, tmp_path):
        repo_dir = _make_git_repo(tmp_path)
        _make_index(tmp_path, [
            {
                "id": "s1", "file": "sample.py", "name": "hello",
                "kind": "function",
                "source": "def hello():\n    print('hello world')\n",
                "byte_offset": 0, "byte_length": 40,
            },
        ], raw_files={"sample.py": "def hello():\n    print('hello world')\n"})

        result = get_code_churn(
            repo=str(repo_dir),
            storage_path=str(tmp_path / "store"),
        )

        assert "error" not in result
        for entry in result["results"]:
            assert entry["risk_level"] in ("low", "medium", "high")


# ============================================================
# Meta / timing tests
# ============================================================

class TestMetaEnvelope:
    def test_complexity_has_meta(self, tmp_path):
        _make_index(tmp_path, [
            {
                "id": "s1", "file": "main.py", "name": "greet",
                "kind": "function",
                "signature": "def greet(name):",
                "source": SIMPLE_SOURCE,
                "byte_offset": 0, "byte_length": len(SIMPLE_SOURCE.encode("utf-8")),
            },
        ], raw_files={"main.py": SIMPLE_SOURCE})

        result = get_complexity_metrics(
            repo="local/myrepo",
            storage_path=str(tmp_path / "store"),
        )
        assert "_meta" in result
        assert "timing_ms" in result["_meta"]

    def test_timeline_has_meta(self, tmp_path):
        repo_dir = _make_git_repo(tmp_path)
        _make_index(tmp_path, [
            {
                "id": "s1", "file": "sample.py", "name": "hello",
                "kind": "function",
                "source": "def hello():\n    print('hello world')\n",
                "byte_offset": 0, "byte_length": 40,
            },
        ], raw_files={"sample.py": "def hello():\n    print('hello world')\n"})

        result = get_evolution_timeline(
            repo=str(repo_dir),
            file_path="sample.py",
            storage_path=str(tmp_path / "store"),
        )
        assert "_meta" in result
        assert "timing_ms" in result["_meta"]
