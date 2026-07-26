"""Build a file-to-file import dependency graph from indexed references."""

import os
import time
from collections import defaultdict
from typing import Optional

from ..storage import IndexStore
from ._utils import resolve_repo


def get_import_graph(
    repo: str,
    format: str = "adjacency",
    file_path: Optional[str] = None,
    storage_path: Optional[str] = None,
) -> dict:
    """Build a file-to-file import dependency graph.

    Resolves import references to actual files in the index, producing
    an adjacency list, DOT graph, or summary statistics.

    Args:
        repo: Repository identifier (owner/repo or just repo name).
        format: Output format - "adjacency", "dot", or "summary".
        file_path: Optional file to restrict the graph to.
        storage_path: Custom storage path.

    Returns:
        Dict with graph data in the requested format.
    """
    start = time.perf_counter()

    try:
        owner, name = resolve_repo(repo, storage_path)
    except ValueError as e:
        return {"error": str(e)}

    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)

    if not index:
        return {"error": f"Repository not indexed: {owner}/{name}"}

    source_files = set(index.source_files)

    # Collect import references grouped by file
    imports_by_file: dict[str, list[str]] = defaultdict(list)
    external_imports: set[str] = set()

    for ref in index.references:
        if ref.get("type") != "import":
            continue

        importing_file = ref.get("file", "")
        module_name = ref.get("name", "")

        if not importing_file or not module_name:
            continue

        resolved = _resolve_import(importing_file, module_name, source_files)

        if resolved:
            if resolved not in imports_by_file[importing_file]:
                imports_by_file[importing_file].append(resolved)
        else:
            external_imports.add(module_name)

    # Build the full bidirectional graph
    imported_by: dict[str, list[str]] = defaultdict(list)
    for src_file, targets in imports_by_file.items():
        for target in targets:
            if src_file not in imported_by[target]:
                imported_by[target].append(src_file)

    # If file_path filter is given, restrict the graph
    if file_path:
        filtered_files = set()
        filtered_files.add(file_path)
        # Include direct imports and importers
        for target in imports_by_file.get(file_path, []):
            filtered_files.add(target)
        for importer in imported_by.get(file_path, []):
            filtered_files.add(importer)
    else:
        # All files that participate in any import relationship
        filtered_files = set(imports_by_file.keys()) | set(imported_by.keys())

    elapsed = (time.perf_counter() - start) * 1000
    meta = {"timing_ms": round(elapsed, 1)}
    repo_id = f"{owner}/{name}"

    if format == "dot":
        return _format_dot(repo_id, imports_by_file, filtered_files, file_path, meta)
    elif format == "summary":
        return _format_summary(
            repo_id, imports_by_file, imported_by, external_imports,
            source_files, filtered_files, meta,
        )
    else:
        return _format_adjacency(
            repo_id, imports_by_file, imported_by, external_imports,
            filtered_files, meta,
        )


def _resolve_import(
    importing_file: str,
    module_name: str,
    source_files: set[str],
) -> Optional[str]:
    """Try to resolve an import module name to a file in the index.

    Returns the resolved file path or None if it's an external import.
    """
    importing_dir = os.path.dirname(importing_file).replace("\\", "/")
    ext = os.path.splitext(importing_file)[1].lower()

    # JS/TS relative imports
    if ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
        return _resolve_js_import(importing_dir, module_name, source_files)

    # Python imports
    if ext == ".py":
        return _resolve_python_import(module_name, source_files)

    # Go imports
    if ext == ".go":
        return _resolve_go_import(module_name, source_files)

    # Fallback: try direct path match
    return _resolve_generic_import(importing_dir, module_name, source_files)


def _resolve_js_import(
    importing_dir: str,
    module_name: str,
    source_files: set[str],
) -> Optional[str]:
    """Resolve JS/TS import to a file path."""
    if not module_name.startswith("."):
        return None  # External package

    # Normalize the relative path
    if importing_dir:
        base = os.path.normpath(os.path.join(importing_dir, module_name)).replace("\\", "/")
    else:
        base = os.path.normpath(module_name).replace("\\", "/")

    # Try exact match first (already has extension)
    if base in source_files:
        return base

    # Try adding extensions
    for suffix in (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"):
        candidate = base + suffix
        if candidate in source_files:
            return candidate

    # Try index files
    for suffix in ("/index.js", "/index.ts", "/index.jsx", "/index.tsx"):
        candidate = base + suffix
        if candidate in source_files:
            return candidate

    return None


def _resolve_python_import(
    module_name: str,
    source_files: set[str],
) -> Optional[str]:
    """Resolve Python import to a file path."""
    # Convert dots to path separators
    parts = module_name.split(".")

    # Try as module file: mypackage.utils -> mypackage/utils.py
    candidate = "/".join(parts) + ".py"
    if candidate in source_files:
        return candidate

    # Try as package: mypackage.utils -> mypackage/utils/__init__.py
    candidate = "/".join(parts) + "/__init__.py"
    if candidate in source_files:
        return candidate

    # Try partial matches (the import might use absolute names but
    # files in index are relative). Search for suffix match.
    suffix_file = "/".join(parts) + ".py"
    suffix_pkg = "/".join(parts) + "/__init__.py"
    for f in source_files:
        normalized = f.replace("\\", "/")
        if normalized.endswith("/" + suffix_file) or normalized == suffix_file:
            return f
        if normalized.endswith("/" + suffix_pkg) or normalized == suffix_pkg:
            return f

    return None


def _resolve_go_import(
    module_name: str,
    source_files: set[str],
) -> Optional[str]:
    """Resolve Go import by matching package path suffix."""
    # Go imports are full paths like "github.com/user/repo/pkg/util"
    # Try to match by suffix against indexed files
    for f in source_files:
        normalized = f.replace("\\", "/")
        # Match if the file's directory path ends with the import path
        file_dir = os.path.dirname(normalized)
        if file_dir == module_name or file_dir.endswith("/" + module_name):
            return f
        # Also try matching just the last component
        parts = module_name.rsplit("/", 1)
        if len(parts) == 2 and os.path.basename(file_dir) == parts[-1]:
            return f

    return None


def _resolve_generic_import(
    importing_dir: str,
    module_name: str,
    source_files: set[str],
) -> Optional[str]:
    """Fallback resolution: try the module name as a relative path."""
    if importing_dir:
        candidate = os.path.normpath(
            os.path.join(importing_dir, module_name)
        ).replace("\\", "/")
    else:
        candidate = module_name

    if candidate in source_files:
        return candidate

    return None


def _format_adjacency(
    repo: str,
    imports_by_file: dict[str, list[str]],
    imported_by: dict[str, list[str]],
    external_imports: set[str],
    filtered_files: set[str],
    meta: dict,
) -> dict:
    """Format output as adjacency list."""
    graph: dict[str, dict] = {}
    edge_count = 0

    for f in sorted(filtered_files):
        file_imports = [t for t in imports_by_file.get(f, []) if t in filtered_files]
        file_imported_by = [s for s in imported_by.get(f, []) if s in filtered_files]
        graph[f] = {
            "imports": file_imports,
            "imported_by": file_imported_by,
        }
        edge_count += len(file_imports)

    # Hubs: most imported files
    hubs = sorted(
        [
            {"file": f, "imported_by_count": len(entry["imported_by"])}
            for f, entry in graph.items()
            if entry["imported_by"]
        ],
        key=lambda x: x["imported_by_count"],
        reverse=True,
    )[:10]

    # Fans: files that import the most
    fans = sorted(
        [
            {"file": f, "imports_count": len(entry["imports"])}
            for f, entry in graph.items()
            if entry["imports"]
        ],
        key=lambda x: x["imports_count"],
        reverse=True,
    )[:10]

    return {
        "repo": repo,
        "node_count": len(graph),
        "edge_count": edge_count,
        "graph": graph,
        "external_imports": sorted(external_imports),
        "hubs": hubs,
        "fans": fans,
        "_meta": meta,
    }


def _format_dot(
    repo: str,
    imports_by_file: dict[str, list[str]],
    filtered_files: set[str],
    file_path: Optional[str],
    meta: dict,
) -> dict:
    """Format output as DOT graph."""
    lines = ["digraph imports {"]
    lines.append('  rankdir=LR;')
    lines.append(f'  label="{repo}";')

    for src in sorted(filtered_files):
        for target in imports_by_file.get(src, []):
            if target in filtered_files:
                lines.append(f'  "{src}" -> "{target}";')

    lines.append("}")

    return {
        "repo": repo,
        "dot": "\n".join(lines),
        "_meta": meta,
    }


def _format_summary(
    repo: str,
    imports_by_file: dict[str, list[str]],
    imported_by: dict[str, list[str]],
    external_imports: set[str],
    source_files: set[str],
    filtered_files: set[str],
    meta: dict,
) -> dict:
    """Format output as summary statistics."""
    total_edges = sum(len(targets) for targets in imports_by_file.values())

    # Most imported
    most_imported = sorted(
        [
            {"file": f, "count": len(importers)}
            for f, importers in imported_by.items()
        ],
        key=lambda x: x["count"],
        reverse=True,
    )[:10]

    # Most importing
    most_importing = sorted(
        [
            {"file": f, "count": len(targets)}
            for f, targets in imports_by_file.items()
        ],
        key=lambda x: x["count"],
        reverse=True,
    )[:10]

    # Isolated files: in source_files but not in any import relationship
    connected = set(imports_by_file.keys()) | set(imported_by.keys())
    isolated = sorted(source_files - connected)

    return {
        "repo": repo,
        "total_files": len(source_files),
        "total_internal_edges": total_edges,
        "total_external_imports": len(external_imports),
        "most_imported": most_imported,
        "most_importing": most_importing,
        "isolated_files": isolated,
        "_meta": meta,
    }


TOOL_DEF = {
    "name": "get_import_graph",
    "description": "Build a file-to-file import dependency graph. Shows which files import which, identifies hubs (most-imported files) and fans (files importing the most). Supports adjacency list, DOT, and summary formats.",
    "inputSchema": {
            "type": "object",
            "properties": {
                    "repo": {
                            "type": "string",
                            "description": "Repository identifier (owner/repo or just repo name)"
                    },
                    "format": {
                            "type": "string",
                            "description": "Output format",
                            "enum": [
                                    "adjacency",
                                    "dot",
                                    "summary"
                            ],
                            "default": "adjacency"
                    },
                    "file_path": {
                            "type": "string",
                            "description": "Optional: only show graph for this file and its direct neighbors"
                    }
            },
            "required": [
                    "repo"
            ]
    },
    "handler": get_import_graph,
}
