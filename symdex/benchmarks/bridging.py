"""Reference bridging — find cross-file connections between relevant files."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from firekeep_symdex.storage import IndexStore

STORAGE_PATH = str(Path(__file__).parent / "repos" / ".click-index")


def find_cross_references(
    repo: str,
    file_basenames: list[str],
    storage_path: str = STORAGE_PATH,
) -> list[dict]:
    """Find references that cross between the given files.

    Scans the index's references list for import/call refs where:
    - The reference is IN one of the relevant files
    - The reference TARGET is in a different relevant file

    Returns list of:
        {"from_file": str, "from_symbol": str, "from_line": int,
         "to_file": str, "to_symbol": str, "type": "import"|"call"}
    """
    owner, name = repo.split("/", 1)
    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)

    if not index:
        return []

    # Resolve basenames to actual indexed file paths
    file_set = set()
    for sf in index.source_files:
        if Path(sf).name in file_basenames:
            file_set.add(sf)

    if len(file_set) < 2:
        return []  # Need at least 2 files for cross-references

    # Build symbol lookup: name -> list of {name, file} for symbols in relevant files
    # A symbol name can appear in multiple files, so we map name -> list of files
    sym_by_name: dict[str, list[dict]] = {}
    for sym in index.symbols:
        sym_file = sym["file"]
        if sym_file in file_set:
            sym_name = sym["name"]
            sym_by_name.setdefault(sym_name, []).append(
                {"name": sym_name, "file": sym_file}
            )

    bridges = []
    for ref in index.references:
        ref_file = ref.get("file", "")

        if ref_file not in file_set:
            continue

        ref_name = ref.get("name", "")
        # Extract the simple name (last component of dotted names like "module.Class")
        simple_name = ref_name.rsplit(".", 1)[-1] if "." in ref_name else ref_name

        # Look up targets by simple name
        targets = sym_by_name.get(simple_name, [])
        for target in targets:
            target_file = target["file"]
            # Cross-file reference: source in one file, target in another
            if target_file != ref_file:
                bridges.append({
                    "from_file": Path(ref_file).name,
                    "from_symbol": ref.get("from_symbol", "") or "",
                    "from_line": ref.get("line", 0),
                    "to_file": Path(target_file).name,
                    "to_symbol": target["name"],
                    "type": ref.get("type", "call"),
                })

    # Deduplicate and sort
    seen = set()
    unique = []
    for b in bridges:
        key = (b["from_file"], b["to_file"], b["to_symbol"], b["type"])
        if key not in seen:
            seen.add(key)
            unique.append(b)

    return sorted(unique, key=lambda b: (b["from_file"], b["from_line"]))


def format_bridges(bridges: list[dict]) -> str:
    """Format cross-references into a readable string.

    Returns something like:
        ## Cross-File References
        - core.py:Parameter.type_cast_value() -> types.py:ParamType.convert() [call]
        - types.py:ParamType.fail() -> exceptions.py:BadParameter [call]
    """
    if not bridges:
        return ""

    lines = ["## Cross-File References"]
    for b in bridges:
        # Clean up from_symbol: "core.py::Context.forward#method" -> "Context.forward"
        from_sym = b.get("from_symbol", "")
        if "::" in from_sym:
            from_sym = from_sym.split("::")[-1]
        if "#" in from_sym:
            from_sym = from_sym.split("#")[0]
        from_ctx = f"{b['from_file']}:{from_sym}" if from_sym else b["from_file"]
        lines.append(f"- {from_ctx} -> {b['to_file']}:{b['to_symbol']} [{b['type']}]")

    return "\n".join(lines)
