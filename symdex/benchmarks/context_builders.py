"""Context builders for FirekeepSymdex vs raw file benchmark comparison.

V16: Hybrid — selective extraction for comprehension/navigation, raw+intel for modification
- Comprehension/navigation: V15 selective extraction (47% token savings, equal/better accuracy)
- Modification: raw files + pattern examples + type hierarchy (needs full context for extension)
- Best of both worlds: save tokens where possible, preserve accuracy where it matters
"""

import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import tiktoken

from firekeep_symdex.storage import IndexStore
from firekeep_symdex.tools.search_symbols import search_symbols
from firekeep_symdex.tools.get_symbol import get_symbol
from firekeep_symdex.tools.get_file_outline import get_file_outline
from firekeep_symdex.tools.get_dependencies import get_dependencies
from firekeep_symdex.tools.get_callers import get_callers
from firekeep_symdex.tools.get_type_hierarchy import get_type_hierarchy
from benchmarks.bridging import find_cross_references, format_bridges

STORAGE_PATH = str(Path(__file__).parent / "repos" / ".click-index")
REPO = "local/click"

_enc = tiktoken.encoding_for_model("gpt-4")


def count_tokens(text: str) -> int:
    return len(_enc.encode(text))


def _read_raw_file(basename: str, index, store) -> Optional[str]:
    content_dir = store._content_dir("local", "click")
    for sf in index.source_files:
        if Path(sf).name == basename:
            fpath = content_dir / sf
            if fpath.exists():
                return fpath.read_text(encoding="utf-8", errors="replace")
    return None


def _resolve_file_path(basename: str, index) -> Optional[str]:
    for sf in index.source_files:
        if Path(sf).name == basename:
            return sf
    return None


def _format_outline(outline: dict) -> str:
    lines = []
    for sym in outline.get("symbols", []):
        sig = sym.get("signature", sym["name"])
        lines.append(f"  {sym['kind']} {sig} (line {sym['line']})")
        for child in sym.get("children", []):
            csig = child.get("signature", child["name"])
            lines.append(f"    {child['kind']} {csig} (line {child['line']})")
    return "\n".join(lines)


def _clean_name(name: str) -> str:
    if "::" in name:
        name = name.split("::")[-1]
    if "#" in name:
        name = name.split("#")[0]
    return name


def _get_search_terms(question: dict) -> list[str]:
    """Extract search terms, prioritizing relevant_symbols method names."""
    terms = []
    seen = set()
    for sym_name in question.get("relevant_symbols", []):
        parts = sym_name.split(".")
        for part in reversed(parts):
            if part not in seen:
                seen.add(part)
                terms.append(part)
    for hint in question.get("search_hints", []):
        if hint not in seen:
            seen.add(hint)
            terms.append(hint)
    return terms


def _find_pattern_examples(repo: str, terms: list[str],
                            relevant_basenames: set) -> str:
    """For modification questions: find existing implementations as patterns."""
    examples = []
    seen = set()

    for term in terms[:4]:
        result = search_symbols(repo, term, kind="class", max_results=4,
                                storage_path=STORAGE_PATH)
        if "error" in result:
            continue
        for match in result.get("results", []):
            if match["id"] in seen:
                continue
            if Path(match["file"]).name not in relevant_basenames:
                continue
            seen.add(match["id"])

            hier = get_type_hierarchy(repo, match["id"],
                                      storage_path=STORAGE_PATH)
            if "error" in hier:
                continue

            parents = hier.get("parents", [])
            children = hier.get("children", [])

            if parents:  # This is a subclass = pattern example
                sym = get_symbol(repo, match["id"], storage_path=STORAGE_PATH)
                if "error" in sym:
                    continue
                parent_names = [p.get("name", "?") for p in parents]
                child_names = [c.get("name", "?") for c in children]

                lines = [f"### {match['name']} (extends {', '.join(parent_names)}) — {match['file']} line {match['line']}"]
                if child_names:
                    lines.append(f"Sibling subclasses: {', '.join(child_names)}")
                if sym.get("docstring"):
                    doc_first = sym["docstring"].strip().split("\n")[0]
                    lines.append(f"Purpose: {doc_first}")
                if sym.get("source"):
                    source = sym["source"]
                    if len(source) > 1500:
                        source = source[:1500] + "\n    # ... (see full source above)"
                    lines.append(f"```python\n{source}\n```")
                examples.append("\n".join(lines))

    if not examples:
        return ""
    return "Pattern examples (existing implementations to follow):\n\n" + "\n\n".join(examples[:3])


def _build_rich_symbol_intel(repo: str, terms: list[str],
                              relevant_basenames: set) -> str:
    """Build rich symbol intelligence with relationships, filtered to relevant files."""
    parts = []
    seen_ids = set()

    for term in terms[:8]:
        result = search_symbols(repo, term, max_results=3, storage_path=STORAGE_PATH)
        if "error" in result:
            continue
        for match in result.get("results", []):
            if match["id"] in seen_ids:
                continue
            if Path(match["file"]).name not in relevant_basenames:
                continue
            seen_ids.add(match["id"])

            sym = get_symbol(repo, match["id"], storage_path=STORAGE_PATH)
            if "error" in sym:
                continue

            entry = [f"### {sym['name']} ({sym['kind']}) — {sym['file']} line {sym['line']}-{sym.get('end_line', '?')}"]

            if sym.get("signature"):
                entry.append(f"Signature: `{sym['signature']}`")
            if sym.get("docstring"):
                entry.append(f"Purpose: {sym['docstring'].strip()}")

            # Relationships
            rel_parts = []
            deps = get_dependencies(repo, match["id"], storage_path=STORAGE_PATH)
            if "error" not in deps and deps.get("calls"):
                calls = [_clean_name(c.get("name", "?")) for c in deps["calls"][:4]]
                rel_parts.append(f"Calls: {', '.join(calls)}")
            callers = get_callers(repo, match["id"], storage_path=STORAGE_PATH)
            if "error" not in callers and callers.get("callers"):
                cnames = [_clean_name(c.get("name", "?")) for c in callers["callers"][:4]]
                rel_parts.append(f"Called by: {', '.join(cnames)}")
            if rel_parts:
                entry.append(" | ".join(rel_parts))

            # Source excerpt
            if sym.get("source"):
                source = sym["source"]
                if len(source) > 2000:
                    source = source[:2000] + "\n    # ... (see full source above)"
                entry.append(f"```python\n{source}\n```")

            parts.append("\n".join(entry))

    if not parts:
        return ""
    return "Key symbols:\n\n" + "\n\n".join(parts[:12])


def _find_key_symbols(repo: str, terms: list[str],
                       relevant_basenames: set) -> list[dict]:
    """Find key symbols matching search terms, with dependency expansion."""
    key_syms = []
    seen_ids = set()

    # First: find direct matches
    for term in terms[:10]:
        result = search_symbols(repo, term, max_results=5,
                                storage_path=STORAGE_PATH)
        if "error" in result:
            continue
        for match in result.get("results", []):
            if match["id"] in seen_ids:
                continue
            if Path(match["file"]).name not in relevant_basenames:
                continue
            seen_ids.add(match["id"])
            key_syms.append(match)

    # Second: expand with dependencies of key symbols (1 level deep)
    dep_syms = []
    for sym in key_syms[:15]:
        deps = get_dependencies(repo, sym["id"], storage_path=STORAGE_PATH)
        if "error" in deps:
            continue
        for dep in deps.get("calls", [])[:4]:
            dep_id = dep.get("id", "")
            if dep_id and dep_id not in seen_ids:
                if Path(dep.get("file", "")).name in relevant_basenames:
                    seen_ids.add(dep_id)
                    dep_syms.append(dep)

    return key_syms + dep_syms


def _build_selective_context(repo: str, terms: list[str],
                              relevant_files: list,
                              relevant_basenames: set) -> str:
    """V15 selective extraction for comprehension/navigation questions."""
    sections = []

    # Key symbols with full source
    key_matches = _find_key_symbols(repo, terms, relevant_basenames)
    sym_parts = []
    for match in key_matches[:20]:
        sym = get_symbol(repo, match["id"], storage_path=STORAGE_PATH)
        if "error" in sym:
            continue

        header = f"### {sym['name']} ({sym['kind']}) — {sym['file']} line {sym['line']}"
        if sym.get("end_line"):
            header += f"-{sym['end_line']}"
        entry = [header]

        if sym.get("signature"):
            entry.append(f"Signature: `{sym['signature']}`")
        if sym.get("docstring"):
            entry.append(f"Docstring: {sym['docstring'].strip()}")

        rel_parts = []
        deps = get_dependencies(repo, match["id"], storage_path=STORAGE_PATH)
        if "error" not in deps and deps.get("calls"):
            calls = [_clean_name(c.get("name", "?")) for c in deps["calls"][:6]]
            rel_parts.append(f"Calls: {', '.join(calls)}")
        callers = get_callers(repo, match["id"], storage_path=STORAGE_PATH)
        if "error" not in callers and callers.get("callers"):
            cnames = [_clean_name(c.get("name", "?")) for c in callers["callers"][:6]]
            rel_parts.append(f"Called by: {', '.join(cnames)}")
        if rel_parts:
            entry.append(" | ".join(rel_parts))

        if sym.get("source"):
            entry.append(f"```python\n{sym['source']}\n```")

        sym_parts.append("\n".join(entry))

    if sym_parts:
        sections.append("# Key Symbols\n\n" + "\n\n".join(sym_parts))

    # File outlines
    outline_parts = []
    for basename in relevant_files:
        outline = get_file_outline(repo, basename, storage_path=STORAGE_PATH)
        if "error" not in outline and outline.get("symbols"):
            outline_parts.append(f"{basename}:\n{_format_outline(outline)}")
    if outline_parts:
        sections.append("# File Structure\n" + "\n\n".join(outline_parts))

    # Type hierarchy
    hierarchy_parts = _build_type_hierarchy(repo, terms, relevant_basenames)
    if hierarchy_parts:
        sections.append("# Type Hierarchy\n" + "\n".join(hierarchy_parts))

    # Cross-file bridges
    if len(relevant_files) >= 2:
        bridges = find_cross_references(repo, relevant_files,
                                        storage_path=STORAGE_PATH)
        bridge_text = format_bridges(bridges)
        if bridge_text:
            sections.append(bridge_text)

    return "\n\n".join(sections) if sections else "(no symbols found)"


def _build_modification_context(repo: str, terms: list[str],
                                 relevant_files: list, relevant_basenames: set,
                                 store, index) -> str:
    """Raw files + intelligence for modification questions."""
    sections = []

    # Raw files first (full context needed for modification)
    for basename in relevant_files:
        full_path = _resolve_file_path(basename, index)
        if not full_path:
            continue
        raw = _read_raw_file(basename, index, store)
        if raw:
            sections.append(f"## {full_path}\n```python\n{raw}\n```")

    intel_sections = []

    # Key symbols with relationships (highlights what matters)
    symbol_intel = _build_rich_symbol_intel(repo, terms, relevant_basenames)
    if symbol_intel:
        intel_sections.append(symbol_intel)

    # Pattern examples (critical for modification)
    pattern_text = _find_pattern_examples(repo, terms, relevant_basenames)
    if pattern_text:
        intel_sections.append(pattern_text)

    # Type hierarchy
    hierarchy_parts = _build_type_hierarchy(repo, terms, relevant_basenames)
    if hierarchy_parts:
        intel_sections.append("Type hierarchy:\n" + "\n".join(hierarchy_parts))

    # Cross-file bridges
    if len(relevant_files) >= 2:
        bridges = find_cross_references(repo, relevant_files,
                                        storage_path=STORAGE_PATH)
        bridge_text = format_bridges(bridges)
        if bridge_text:
            intel_sections.append(bridge_text)

    if intel_sections:
        sections.append(
            "---\n# Structural Analysis\n" + "\n\n".join(intel_sections)
        )

    return "\n\n".join(sections) if sections else "(no files found)"


def _build_type_hierarchy(repo: str, terms: list[str],
                           relevant_basenames: set) -> list[str]:
    """Build type hierarchy entries for relevant classes."""
    hierarchy_parts = []
    seen_hier = set()
    for term in terms[:5]:
        result = search_symbols(repo, term, kind="class", max_results=3,
                                storage_path=STORAGE_PATH)
        if "error" in result or not result.get("results"):
            continue
        for match in result["results"]:
            if match["name"] in seen_hier:
                continue
            if Path(match["file"]).name not in relevant_basenames:
                continue
            seen_hier.add(match["name"])
            hier = get_type_hierarchy(repo, match["id"],
                                      storage_path=STORAGE_PATH)
            if "error" in hier:
                continue
            parents = hier.get("parents", [])
            children = hier.get("children", [])
            if parents or children:
                parts = [f"- {match['name']}"]
                if parents:
                    parent_names = [p.get("name", "?") for p in parents]
                    parts.append(f"  inherits from: {', '.join(parent_names)}")
                if children:
                    child_names = [c.get("name", "?") for c in children[:10]]
                    parts.append(f"  subclasses: {', '.join(child_names)}")
                hierarchy_parts.append("\n".join(parts))
    return hierarchy_parts


def build_symdex_context(question: dict, repo: str = REPO) -> tuple[str, int]:
    """V16: Hybrid context — routes by question category.

    - Comprehension/navigation: selective extraction (fewer tokens, equal quality)
    - Modification: raw files + intelligence (needs full context for extension)

    Returns:
        (context_string, token_count)
    """
    relevant_files = question.get("relevant_files", [])
    relevant_basenames = set(relevant_files)
    category = question.get("category", "comprehension")
    terms = _get_search_terms(question)

    store = IndexStore(base_path=STORAGE_PATH)
    index = store.load_index("local", "click")

    if category == "modification":
        context = _build_modification_context(
            repo, terms, relevant_files, relevant_basenames, store, index)
    else:
        context = _build_selective_context(
            repo, terms, relevant_files, relevant_basenames)

    tokens = count_tokens(context)
    return context, tokens


def build_raw_context(question: dict, repo: str = REPO) -> tuple[str, int]:
    """Build context by reading full files listed in the question."""
    owner, name = repo.split("/", 1)
    store = IndexStore(base_path=STORAGE_PATH)
    index = store.load_index(owner, name)

    if not index:
        return "(index not found)", 0

    relevant_basenames = question.get("relevant_files", [])
    content_dir = store._content_dir(owner, name)

    sections: list[str] = []
    for basename in relevant_basenames:
        matched: Optional[str] = None
        for sf in index.source_files:
            if Path(sf).name == basename:
                matched = sf
                break

        if not matched:
            continue

        file_path = content_dir / matched
        if not file_path.exists():
            continue

        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        sections.append(f"## {matched}\n```python\n{content}\n```")

    context_string = "\n\n".join(sections) if sections else "(no files found)"
    tokens = count_tokens(context_string)
    return context_string, tokens
