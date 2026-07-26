"""Shared helpers for tool modules."""

import os
from collections import Counter
from typing import Optional

from ..parser.symbols import Symbol
from ..storage import IndexStore, CodeIndex


def _read_max_files_env(default: int = 1500) -> int:
    raw = os.environ.get("FIREKEEP_SYMDEX_MAX_FILES", "")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


DEFAULT_MAX_FILES = _read_max_files_env()


def resolve_repo(repo: str, storage_path: Optional[str] = None) -> tuple[str, str]:
    """Parse 'owner/repo' or look up single name. Returns (owner, name).

    Raises ValueError if repo not found.
    """
    if "/" in repo:
        return repo.split("/", 1)
    store = IndexStore(base_path=storage_path)
    repos = store.list_repos()
    matching = [r for r in repos if r["repo"].endswith(f"/{repo}")]
    if not matching:
        raise ValueError(f"Repository not found: {repo}")
    if len(matching) > 1:
        options = ", ".join(r["repo"] for r in matching)
        raise ValueError(f"Ambiguous repo name '{repo}' matches multiple repositories: {options}")
    return matching[0]["repo"].split("/", 1)


def maybe_refresh_files(
    store: IndexStore,
    owner: str,
    name: str,
    file_paths: list[str],
    index: Optional[CodeIndex] = None,
) -> int:
    """Refresh stale files in the index. Returns count of refreshed files.

    Uses the repo_root stored in the index to locate files on disk.
    If repo_root is not set or the directory doesn't exist, returns 0.
    """
    if index is None:
        index = store.load_index(owner, name)
    if not index or not index.repo_root:
        return 0
    from pathlib import Path
    root = Path(index.repo_root)
    if not root.is_dir():
        return 0
    refreshed = 0
    for fp in file_paths:
        if store.refresh_file(owner, name, fp, index.repo_root):
            refreshed += 1
    return refreshed


def resolve_call_targets(index, call_name: str, caller_file: str) -> list[str]:
    """Resolve a call reference name to the most likely symbol IDs.

    Uses scope-aware matching with priority:
    1. Same file, exact name match
    2. Imported file, exact name match
    3. Dotted name match (e.g., call "obj.method" matches symbol qualified_name "Obj.method")
    4. Any file, exact name match (fallback)

    Returns list of symbol IDs, best matches first.
    """
    # Get imports for the caller's file
    imported_files: set[str] = set()
    for ref in index.get_refs(caller_file, "import"):
        imp_name = ref.get("name", "")
        # Try to resolve import to a file
        base = imp_name.split("/")[-1].split(".")[-1]
        for sf in index.source_files:
            if (
                sf.endswith(f"/{base}.py")
                or sf.endswith(f"/{base}.js")
                or sf.endswith(f"/{base}.ts")
                or sf == f"{base}.py"
                or sf == f"{base}.js"
                or sf == f"{base}.ts"
            ):
                imported_files.add(sf)

    # Strip common prefixes from call name for matching
    # e.g., "self.parse" -> "parse", "this.render" -> "render"
    bare_name = call_name
    if "." in call_name:
        parts = call_name.split(".")
        bare_name = parts[-1]

    same_file: list[str] = []
    imported: list[str] = []
    dotted: list[str] = []
    fallback: list[str] = []

    # Use name-based index for O(1) lookup instead of scanning all symbols
    seen_ids: set[str] = set()
    candidates: list[dict] = []
    for sym in index.get_symbols_by_name(bare_name):
        sid = sym.get("id", "")
        if sid and sid not in seen_ids:
            seen_ids.add(sid)
            candidates.append(sym)
    for sym in candidates:
        sym_name = sym.get("name", "")
        sym_qname = sym.get("qualified_name", "")
        sym_file = sym.get("file", "")

        name_match = sym_name == bare_name or sym_name == call_name
        qname_match = sym_qname == call_name or sym_qname.endswith(f".{bare_name}")

        if not name_match and not qname_match:
            continue

        sid = sym["id"]

        if sym_file == caller_file:
            same_file.append(sid)
        elif sym_file in imported_files:
            imported.append(sid)
        elif qname_match and "." in call_name:
            dotted.append(sid)
        else:
            fallback.append(sid)

    return same_file + imported + dotted + fallback


def get_file_imports(index, file_path: str) -> list[dict]:
    """Get import references for a file."""
    if hasattr(index, "get_refs"):
        return [
            {"name": ref["name"], "line": ref["line"]}
            for ref in index.get_refs(file_path, "import")
        ]
    return [
        {"name": ref["name"], "line": ref["line"]}
        for ref in index.references
        if ref.get("type") == "import" and ref.get("file") == file_path
    ]


def generate_file_summaries(symbols: list[Symbol]) -> dict[str, str]:
    """Generate one-line summaries per file from symbol data.

    Example output: "3 functions, 2 classes: MyClass, helper, process_data"
    """
    # Group symbols by file
    by_file: dict[str, list[Symbol]] = {}
    for sym in symbols:
        by_file.setdefault(sym.file, []).append(sym)

    summaries = {}
    for file_path, file_syms in by_file.items():
        # Count kinds
        kind_counts: Counter[str] = Counter()
        names = []
        for sym in file_syms:
            kind_counts[sym.kind] += 1
            # Only include top-level symbol names (no parent)
            if not sym.parent:
                names.append(sym.name)

        # Build kind string: "3 functions, 2 classes"
        kind_parts = []
        for kind in ["class", "function", "method", "type", "constant"]:
            count = kind_counts.get(kind, 0)
            if count:
                label = kind + ("es" if kind == "class" else "s") if count > 1 else kind
                kind_parts.append(f"{count} {label}")

        kind_str = ", ".join(kind_parts)
        # Append top-level names (limit to 5)
        if names:
            name_str = ", ".join(names[:5])
            if len(names) > 5:
                name_str += f" (+{len(names) - 5} more)"
            summaries[file_path] = f"{kind_str}: {name_str}" if kind_str else name_str
        elif kind_str:
            summaries[file_path] = kind_str

    return summaries
