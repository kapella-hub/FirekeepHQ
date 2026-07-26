"""MCP tools package."""

import importlib
import os
import pkgutil

# Analytics tools require indexed repos (git history analysis, churn, hotspots, etc.).
# Hidden by default because no repos are indexed. Enable when repos are actively indexed.
_ANALYTICS_TOOL_NAMES = frozenset({
    "get_evolution_timeline",
    "get_code_churn",
    "get_contributors",
    "get_change_summary",
    "detect_patterns",
    "get_complexity_metrics",
    "get_hotspots",
    "compare_repos",
})


def discover_tools() -> dict[str, dict]:
    """Auto-discover TOOL_DEF(s) from all tool modules.

    Scans every non-private module in this package for a ``TOOL_DEF`` dict
    (single tool) or ``TOOL_DEFS`` list (multiple tools) and returns a
    mapping of tool name to its definition dict.

    Analytics tools (requiring indexed repos) are hidden unless
    SYMDEX_ANALYTICS_ENABLED=true.
    """
    analytics_enabled = os.getenv("SYMDEX_ANALYTICS_ENABLED", "false").lower() == "true"

    tools: dict[str, dict] = {}
    package = importlib.import_module("firekeep_symdex.tools")
    for _, module_name, _ in pkgutil.iter_modules(package.__path__):
        if module_name.startswith("_"):
            continue
        module = importlib.import_module(f".{module_name}", package="firekeep_symdex.tools")
        if hasattr(module, "TOOL_DEF"):
            defn = module.TOOL_DEF
            if not analytics_enabled and defn["name"] in _ANALYTICS_TOOL_NAMES:
                continue
            tools[defn["name"]] = defn
        elif hasattr(module, "TOOL_DEFS"):
            for defn in module.TOOL_DEFS:
                if not analytics_enabled and defn["name"] in _ANALYTICS_TOOL_NAMES:
                    continue
                tools[defn["name"]] = defn
    return tools
