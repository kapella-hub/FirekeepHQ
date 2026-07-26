"""Inline annotation engine — enrich raw code with FirekeepSymdex intelligence."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from firekeep_symdex.storage import IndexStore
from firekeep_symdex.tools.get_callers import get_callers

STORAGE_PATH = str(Path(__file__).parent / "repos" / ".click-index")


def annotate_file(
    repo: str,
    file_basename: str,
    raw_content: str,
    storage_path: str = STORAGE_PATH,
    max_annotations: int = 15,
) -> str:
    """Add inline annotations to raw file content.

    For each top-level symbol definition in the file, adds a comment showing:
    - Who calls this symbol (top callers)
    - What interface it implements (if it overrides a parent)

    Args:
        repo: Repository identifier
        file_basename: Basename of the file (e.g., "types.py")
        raw_content: The raw file content to annotate
        storage_path: Path to FirekeepSymdex index
        max_annotations: Maximum annotations to add (to avoid bloat)

    Returns:
        Annotated file content with inline comments
    """
    owner, name = repo.split("/", 1)
    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)

    if not index:
        return raw_content

    # Find symbols in this file
    file_symbols = []
    for sym in index.symbols:
        if Path(sym["file"]).name == file_basename:
            # Only annotate top-level symbols (classes, functions) and important methods
            if sym.get("kind") in ("class", "function", "method"):
                file_symbols.append(sym)

    if not file_symbols:
        return raw_content

    # Sort by line number descending (so we insert from bottom up without shifting lines)
    file_symbols.sort(key=lambda s: s.get("line", 0), reverse=True)

    lines = raw_content.split("\n")
    annotations_added = 0

    for sym in file_symbols:
        if annotations_added >= max_annotations:
            break

        line_num = sym.get("line", 0)
        if line_num < 1 or line_num > len(lines):
            continue

        # Get callers for this symbol
        callers_result = get_callers(repo, sym["id"], storage_path=storage_path)

        annotations = []

        if "error" not in callers_result:
            caller_list = callers_result.get("callers", [])
            if caller_list:
                caller_names = []
                for c in caller_list[:3]:
                    # from_symbol is a full symbol ID like "core.py::Command.method#method"
                    raw_caller = c.get("from_symbol") or c.get("call_expression", "?")
                    # Extract the readable name: strip "file::" prefix and "#kind" suffix
                    caller_name = raw_caller
                    if "::" in caller_name:
                        caller_name = caller_name.split("::", 1)[1]
                    if "#" in caller_name:
                        caller_name = caller_name.rsplit("#", 1)[0]

                    caller_file = Path(c.get("file", "")).name
                    if caller_file and caller_file != file_basename:
                        caller_names.append(f"{caller_file}:{caller_name}")
                    else:
                        caller_names.append(caller_name)

                if caller_names:
                    annotations.append(f"# <- called by: {', '.join(caller_names)}")

        # Check if this is an override (has parent with same name)
        if sym.get("kind") == "method" and sym.get("parent"):
            parent_sym = next(
                (s for s in index.symbols
                 if s["id"] == sym.get("parent") and s.get("kind") == "class"),
                None
            )
            if parent_sym:
                annotations.append(f"# (method of {parent_sym['name']})")

        if annotations:
            # Insert annotation comment above the symbol definition line
            idx = line_num - 1  # 0-indexed
            indent = len(lines[idx]) - len(lines[idx].lstrip()) if idx < len(lines) else 0
            indent_str = " " * indent
            annotation_lines = [f"{indent_str}{a}" for a in annotations]
            for a in reversed(annotation_lines):
                lines.insert(idx, a)
            annotations_added += 1

    return "\n".join(lines)
