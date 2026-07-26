"""End-to-end server tests."""

import pytest

from firekeep_symdex.server import list_tools
from firekeep_symdex.tools._utils import get_file_imports as _get_file_imports


@pytest.mark.asyncio
async def test_server_lists_all_tools():
    """Test that server lists expected tools.

    Analytics tools (get_evolution_timeline, get_code_churn, etc.) are only
    included when SYMDEX_ANALYTICS_ENABLED=true. This test validates the
    default (analytics disabled) set.
    """
    import os
    analytics_enabled = os.environ.get("SYMDEX_ANALYTICS_ENABLED", "false").lower() == "true"

    tools = await list_tools()
    names = {t.name for t in tools}

    # Core tools always present
    core_tools = {
        "index_repo", "index_folder", "get_file_tree",
        "get_file_outline", "get_symbol", "get_symbols", "search_symbols",
        "get_context",
        "get_callers", "get_dependencies",
        "find_dead_code", "get_import_graph", "get_impact",
        "get_architecture_map",
        "get_review_context",
        "learn_from_changes", "recall_with_code", "review_with_history",
        "diff_since_index", "get_symbol_history", "suggest_symbols",
        "get_type_hierarchy", "get_similar_symbols",
        "extract_conventions",
        "watch_folder", "unwatch_folder", "list_watches",
        "scaffold_symbol", "export_index",
        "list_repos",
    }
    analytics_tools = {
        "get_change_summary",
        "get_hotspots", "compare_repos",
        "get_evolution_timeline", "get_complexity_metrics",
        "get_contributors", "get_code_churn",
        "detect_patterns",
    }

    assert core_tools.issubset(names), f"Missing core tools: {core_tools - names}"

    if analytics_enabled:
        assert analytics_tools.issubset(names), f"Missing analytics tools: {analytics_tools - names}"
        assert len(tools) == len(core_tools) + len(analytics_tools)
    else:
        assert not (names & analytics_tools), f"Analytics tools unexpectedly present: {names & analytics_tools}"
        assert len(tools) == len(core_tools)


@pytest.mark.asyncio
async def test_index_repo_tool_schema():
    """Test index_repo tool has correct schema."""
    tools = await list_tools()

    index_repo = next(t for t in tools if t.name == "index_repo")

    assert "url" in index_repo.inputSchema["properties"]
    assert "use_ai_summaries" in index_repo.inputSchema["properties"]
    assert "url" in index_repo.inputSchema["required"]


@pytest.mark.asyncio
async def test_search_symbols_tool_schema():
    """Test search_symbols tool has correct schema."""
    tools = await list_tools()

    search = next(t for t in tools if t.name == "search_symbols")

    props = search.inputSchema["properties"]
    assert "repo" in props
    assert "query" in props
    assert "kind" in props
    assert "file_pattern" in props
    assert "max_results" in props

    # kind should have enum
    assert "enum" in props["kind"]
    assert set(props["kind"]["enum"]) == {"function", "class", "method", "constant", "type"}


@pytest.mark.asyncio
async def test_get_symbol_has_include_imports_param():
    """Test get_symbol tool schema includes include_imports parameter."""
    tools = await list_tools()
    tool = next(t for t in tools if t.name == "get_symbol")
    props = tool.inputSchema["properties"]
    assert "include_imports" in props
    assert props["include_imports"]["type"] == "boolean"
    assert props["include_imports"]["default"] is False


@pytest.mark.asyncio
async def test_get_symbols_has_include_imports_param():
    """Test get_symbols tool schema includes include_imports parameter."""
    tools = await list_tools()
    tool = next(t for t in tools if t.name == "get_symbols")
    props = tool.inputSchema["properties"]
    assert "include_imports" in props
    assert props["include_imports"]["type"] == "boolean"
    assert props["include_imports"]["default"] is False


def test_get_file_imports_filters_correctly():
    """Test _get_file_imports returns only import refs for the given file."""

    class FakeIndex:
        references = [
            {"type": "import", "name": "os", "line": 1, "file": "main.py"},
            {"type": "import", "name": "sys", "line": 2, "file": "main.py"},
            {"type": "call", "name": "print", "line": 5, "file": "main.py"},
            {"type": "import", "name": "json", "line": 1, "file": "other.py"},
        ]

    result = _get_file_imports(FakeIndex(), "main.py")
    assert result == [
        {"name": "os", "line": 1},
        {"name": "sys", "line": 2},
    ]


def test_get_file_imports_empty_for_unknown_file():
    """Test _get_file_imports returns empty list for a file with no imports."""

    class FakeIndex:
        references = [
            {"type": "import", "name": "os", "line": 1, "file": "main.py"},
        ]

    result = _get_file_imports(FakeIndex(), "nonexistent.py")
    assert result == []
