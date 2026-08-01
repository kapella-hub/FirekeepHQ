"""Unit tests for the model-free polyglot benchmark protocol."""

from benchmarks.polyglot_symbol_benchmark import (
    is_test_path,
    load_manifest,
    measured_source_changes,
    parse_porcelain_z,
    select_symbols,
    stable_tool_response,
    summarize_rows,
)


def test_selection_is_stable_across_index_order():
    spec = {"language": "python", "commit": "abc"}
    symbols = [
        {
            "id": f"file_{i % 5}.py::symbol_{i}#function",
            "file": f"file_{i % 5}.py",
        }
        for i in range(20)
    ]

    forward = select_symbols(spec, symbols, 8)
    reverse = select_symbols(spec, list(reversed(symbols)), 8)

    assert [item["id"] for item in forward] == [item["id"] for item in reverse]


def test_selection_balances_across_files_before_reusing_them():
    spec = {"language": "python", "commit": "abc"}
    symbols = [
        {"id": f"file_{file}.py::symbol_{symbol}#function", "file": f"file_{file}.py"}
        for file in range(4)
        for symbol in range(5)
    ]

    selected = select_symbols(spec, symbols, 8)
    file_counts = {
        file_path: sum(item["file"] == file_path for item in selected)
        for file_path in {item["file"] for item in selected}
    }

    assert file_counts == {f"file_{file}.py": 2 for file in range(4)}


def test_test_and_fixture_paths_are_excluded():
    assert is_test_path("pkg/command_test.go")
    assert is_test_path("src/__tests__/queue.ts")
    assert is_test_path("lib/widget.spec.rb")
    assert is_test_path("fixtures/sample.py")
    assert not is_test_path("src/testing.py")
    assert not is_test_path("lib/specification.rb")


def test_cleanliness_check_ignores_tests_but_not_measured_source():
    spec = {
        "source_prefix": "lib/",
        "extensions": [".rb"],
    }
    status = [
        {"status": " D", "path": "test/spec_multipart.rb"},
        {"status": " M", "path": "lib/rack/request.rb"},
        {"status": " M", "path": "README.md"},
        {"status": " M", "path": "lib/path with spaces.rb"},
    ]

    assert measured_source_changes(spec, status) == [
        {"status": " M", "path": "lib/rack/request.rb"},
        {"status": " M", "path": "lib/path with spaces.rb"},
    ]


def test_porcelain_parser_preserves_spaces_and_rename_paths():
    output = (
        b" M Sources/Path With Spaces.swift\0"
        b"R  Sources/New Name.swift\0Sources/Old Name.swift\0"
    )

    assert parse_porcelain_z(output) == [
        {"status": " M", "path": "Sources/Path With Spaces.swift"},
        {
            "status": "R ",
            "path": "Sources/New Name.swift",
            "original_path": "Sources/Old Name.swift",
        },
    ]


def test_manifest_covers_every_built_in_language_with_pinned_commits():
    specs = load_manifest()

    assert [spec["language"] for spec in specs] == [
        "python",
        "javascript",
        "typescript",
        "go",
        "rust",
        "java",
        "php",
        "c",
        "csharp",
        "ruby",
        "kotlin",
        "swift",
    ]
    assert len({spec["id"] for spec in specs}) == 12
    assert all(len(spec["commit"]) == 40 for spec in specs)


def test_stable_response_keeps_payload_but_removes_runtime_counters():
    result = {
        "id": "a.py::run#function",
        "source": "def run(): pass",
        "_meta": {
            "timing_ms": 8.2,
            "tokens_saved": 100,
            "total_tokens_saved": 999,
            "cost_avoided": {"model": 0.01},
            "total_cost_avoided": {"model": 10.0},
            "content_verified": True,
        },
    }

    stable = stable_tool_response(result)

    assert stable["id"] == result["id"]
    assert stable["_meta"]["tokens_saved"] == 100
    assert stable["_meta"]["content_verified"] is True
    assert "timing_ms" not in stable["_meta"]
    assert "total_tokens_saved" not in stable["_meta"]
    assert "total_cost_avoided" not in stable["_meta"]


def test_summary_distinguishes_mean_from_total_consumption():
    rows = [
        {
            "language": "python",
            "repo": "repo",
            "file": "large.py",
            "source_fidelity": True,
            "indexed_file_matches_git": True,
            "tokens": {
                "cl100k_base": {"symdex": 10, "raw": 100, "reduction_pct": 90},
                "o200k_base": {"symdex": 10, "raw": 100, "reduction_pct": 90},
            },
        },
        {
            "language": "python",
            "repo": "repo",
            "file": "small.py",
            "source_fidelity": True,
            "indexed_file_matches_git": True,
            "tokens": {
                "cl100k_base": {"symdex": 20, "raw": 10, "reduction_pct": -100},
                "o200k_base": {"symdex": 20, "raw": 10, "reduction_pct": -100},
            },
        },
    ]

    summary = summarize_rows(rows)
    metrics = summary["encodings"]["cl100k_base"]

    assert metrics["mean_reduction_pct"] == -5
    assert metrics["total_reduction_pct"] == 72.73
    assert metrics["lookups_using_more_tokens"] == 1
    assert summary["unique_file_count"] == 2
    assert summary["source_fidelity_passes"] == 2
    assert summary["git_file_matches"] == 2
