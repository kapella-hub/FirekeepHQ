"""Export a repository index as structured markdown or JSON."""

import json
import time
from collections import defaultdict
from typing import Optional

from ..storage import IndexStore, record_savings, estimate_savings, cost_avoided
from ._utils import resolve_repo


def export_index(
    repo: str,
    format: str = "markdown",
    include_signatures: bool = True,
    include_summaries: bool = True,
    path_prefix: Optional[str] = None,
    storage_path: Optional[str] = None,
) -> dict:
    """Export a repo index as structured markdown or JSON for context inclusion.

    Produces a compact, readable representation of the full symbol hierarchy
    organized by file. Ideal for injecting into LLM context windows.

    Args:
        repo: Repository identifier (owner/repo or just repo name).
        format: Output format, either "markdown" or "json".
        include_signatures: Whether to include function/method signatures.
        include_summaries: Whether to include symbol summaries.
        path_prefix: Optional filter to only include files matching this prefix.
        storage_path: Custom storage path.

    Returns:
        Dict with the exported content and _meta envelope.
    """
    start = time.perf_counter()

    if format not in ("markdown", "json"):
        return {"error": f"Invalid format: {format!r}. Must be 'markdown' or 'json'."}

    try:
        owner, name = resolve_repo(repo, storage_path)
    except ValueError as e:
        return {"error": str(e)}

    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)

    if not index:
        return {"error": f"Repository not indexed: {owner}/{name}"}

    # Group symbols by file, preserving hierarchy (parent relationship)
    symbols_by_file: dict[str, list[dict]] = defaultdict(list)
    for sym in index.symbols:
        file_path = sym.get("file", "")
        if path_prefix and not file_path.startswith(path_prefix):
            continue
        symbols_by_file[file_path].append(sym)

    # Sort files for deterministic output
    sorted_files = sorted(symbols_by_file.keys())

    if format == "markdown":
        content = _render_markdown(
            sorted_files, symbols_by_file,
            include_signatures, include_summaries,
        )
    else:
        content = _render_json(
            sorted_files, symbols_by_file,
            include_signatures, include_summaries,
        )

    # Token savings: compare export size vs estimated raw file sizes
    export_bytes = len(content.encode("utf-8")) if isinstance(content, str) else len(content)
    raw_bytes = sum(s.get("byte_length", 0) for s in index.symbols)
    tokens_saved = estimate_savings(raw_bytes, export_bytes)
    total_saved = record_savings(tokens_saved)

    elapsed = (time.perf_counter() - start) * 1000

    return {
        "repo": f"{owner}/{name}",
        "format": format,
        "file_count": len(sorted_files),
        "symbol_count": sum(len(syms) for syms in symbols_by_file.values()),
        "content": content,
        "_meta": {
            "timing_ms": round(elapsed, 1),
            "export_bytes": export_bytes,
            "raw_bytes": raw_bytes,
            "tokens_saved": tokens_saved,
            "total_tokens_saved": total_saved,
            **cost_avoided(tokens_saved, total_saved),
        },
    }


def _render_markdown(
    sorted_files: list[str],
    symbols_by_file: dict[str, list[dict]],
    include_signatures: bool,
    include_summaries: bool,
) -> str:
    """Render symbols as structured markdown organized by file."""
    lines: list[str] = []

    for file_path in sorted_files:
        syms = symbols_by_file[file_path]
        lines.append(f"## {file_path}")

        # Separate top-level symbols from children
        top_level = [s for s in syms if not s.get("parent")]
        children_by_parent: dict[str, list[dict]] = defaultdict(list)
        for s in syms:
            parent = s.get("parent")
            if parent:
                children_by_parent[parent].append(s)

        for sym in top_level:
            lines.append(_format_symbol_md(sym, include_signatures, include_summaries, indent=0))
            # Render children (methods of a class, etc.)
            sym_id = sym.get("id", "")
            for child in children_by_parent.get(sym_id, []):
                lines.append(_format_symbol_md(child, include_signatures, include_summaries, indent=1))

        lines.append("")  # Blank line between files

    return "\n".join(lines)


def _format_symbol_md(
    sym: dict,
    include_signatures: bool,
    include_summaries: bool,
    indent: int,
) -> str:
    """Format a single symbol as a markdown list item."""
    prefix = "  " * indent + "- "
    kind = sym.get("kind", "")
    name = sym.get("name", "")

    if include_signatures and sym.get("signature"):
        label = f"`{sym['signature']}`"
    else:
        label = f"`{kind} {name}`" if kind else f"`{name}`"

    summary = sym.get("summary", "")
    if include_summaries and summary:
        return f"{prefix}{label} -- {summary}"
    return f"{prefix}{label}"


def _render_json(
    sorted_files: list[str],
    symbols_by_file: dict[str, list[dict]],
    include_signatures: bool,
    include_summaries: bool,
) -> str:
    """Render symbols as structured JSON."""
    files_out = []

    for file_path in sorted_files:
        syms = symbols_by_file[file_path]

        # Separate top-level from children
        top_level = [s for s in syms if not s.get("parent")]
        children_by_parent: dict[str, list[dict]] = defaultdict(list)
        for s in syms:
            parent = s.get("parent")
            if parent:
                children_by_parent[parent].append(s)

        symbols_out = []
        for sym in top_level:
            entry = _symbol_to_json_entry(sym, include_signatures, include_summaries)
            # Nest children
            sym_id = sym.get("id", "")
            kids = children_by_parent.get(sym_id, [])
            if kids:
                entry["children"] = [
                    _symbol_to_json_entry(c, include_signatures, include_summaries)
                    for c in kids
                ]
            symbols_out.append(entry)

        files_out.append({
            "file": file_path,
            "symbols": symbols_out,
        })

    return json.dumps(files_out, indent=2)


def _symbol_to_json_entry(
    sym: dict,
    include_signatures: bool,
    include_summaries: bool,
) -> dict:
    """Convert a symbol dict to a compact JSON-exportable entry."""
    entry: dict = {
        "name": sym.get("name", ""),
        "kind": sym.get("kind", ""),
        "line": sym.get("line", 0),
    }
    if include_signatures and sym.get("signature"):
        entry["signature"] = sym["signature"]
    if include_summaries and sym.get("summary"):
        entry["summary"] = sym["summary"]
    return entry


TOOL_DEF = {
    "name": "export_index",
    "description": "Export the index as structured markdown or JSON for direct context inclusion. Organized by file with symbol hierarchy, signatures, and summaries.",
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
                                    "markdown",
                                    "json"
                            ],
                            "default": "markdown"
                    },
                    "include_signatures": {
                            "type": "boolean",
                            "description": "Include signatures (default true)",
                            "default": True
                    },
                    "include_summaries": {
                            "type": "boolean",
                            "description": "Include summaries (default true)",
                            "default": True
                    },
                    "path_prefix": {
                            "type": "string",
                            "description": "Optional path prefix filter"
                    }
            },
            "required": [
                    "repo"
            ]
    },
    "handler": export_index,
}
