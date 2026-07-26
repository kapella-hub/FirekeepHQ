"""Show the inheritance hierarchy for a class/type symbol."""

import re
import time
from typing import Optional

from ..storage import IndexStore
from ._utils import resolve_repo


# Regex to extract base classes from a class definition line.
# Matches: class Foo(Bar, Baz):  or  class Foo(module.Bar, Baz):
_CLASS_BASES_RE = re.compile(
    r"^\s*class\s+\w+\s*\(([^)]*)\)\s*:", re.MULTILINE
)


def _parse_base_classes(source: str) -> list[str]:
    """Extract parent class names from the first class definition line in source.

    Returns simple names only (e.g., 'Bar' from 'module.Bar').
    """
    m = _CLASS_BASES_RE.search(source)
    if not m:
        return []
    raw = m.group(1)
    bases = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        # Strip generic parameters like Generic[T] or List[int]
        bracket = part.find("[")
        if bracket != -1:
            part = part[:bracket].strip()
        # Take last component of dotted name
        simple = part.rsplit(".", 1)[-1].strip()
        if simple:
            bases.append(simple)
    return bases


def _find_class_symbols(index, name: str) -> list[dict]:
    """Find all class/type symbols matching a simple name."""
    results = []
    for sym in index.symbols:
        if sym.get("kind") not in ("class", "type"):
            continue
        if sym.get("name") == name:
            results.append(sym)
    return results


def _get_class_definition_line(source: str) -> str:
    """Extract the class definition line from source."""
    for line in source.split("\n"):
        stripped = line.strip()
        if stripped.startswith("class "):
            return stripped
    return ""


def get_type_hierarchy(
    repo: str,
    symbol_id: str,
    storage_path: Optional[str] = None,
) -> dict:
    """Show the inheritance chain for a class or type symbol.

    For the given class, finds its parent classes (from source parsing)
    and child classes (subclasses that inherit from it).

    Args:
        repo: Repository identifier (owner/repo or just repo name).
        symbol_id: Symbol ID of a class/type to inspect.
        storage_path: Custom storage path.

    Returns:
        Dict with parents list, children list, and metadata.
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

    symbol = index.get_symbol(symbol_id)
    if not symbol:
        return {"error": f"Symbol not found: {symbol_id}"}

    if symbol["kind"] not in ("class", "type"):
        return {"error": f"Symbol is not a class or type: {symbol['kind']}"}

    target_name = symbol["name"]

    # --- Find parents ---
    parents = []
    source = store.get_symbol_content(owner, name, symbol_id)
    if source:
        base_names = _parse_base_classes(source)
        for base in base_names:
            matches = _find_class_symbols(index, base)
            if matches:
                parents.append({
                    "name": base,
                    "symbol_id": matches[0]["id"],
                })
            else:
                # Parent exists in code but not indexed (e.g., stdlib)
                parents.append({
                    "name": base,
                    "symbol_id": None,
                })

    # --- Find children (subclasses) ---
    children = []
    for sym in index.symbols:
        if sym.get("kind") not in ("class", "type"):
            continue
        if sym["id"] == symbol_id:
            continue
        # Check if this class inherits from our target
        child_source = store.get_symbol_content(owner, name, sym["id"])
        if not child_source:
            continue
        child_bases = _parse_base_classes(child_source)
        if target_name in child_bases:
            children.append({
                "name": sym["name"],
                "symbol_id": sym["id"],
            })

    elapsed = (time.perf_counter() - start) * 1000

    return {
        "symbol_id": symbol_id,
        "name": target_name,
        "parents": parents,
        "children": children,
        "_meta": {"timing_ms": round(elapsed, 1)},
    }


TOOL_DEF = {
    "name": "get_type_hierarchy",
    "description": "For a class or type symbol, show its inheritance chain \u2014 parent classes and subclasses found in the indexed codebase.",
    "inputSchema": {
            "type": "object",
            "properties": {
                    "repo": {
                            "type": "string",
                            "description": "Repository identifier (owner/repo or just repo name)"
                    },
                    "symbol_id": {
                            "type": "string",
                            "description": "Symbol ID of a class or type"
                    }
            },
            "required": [
                    "repo",
                    "symbol_id"
            ]
    },
    "handler": get_type_hierarchy,
}
