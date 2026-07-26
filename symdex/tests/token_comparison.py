"""Token savings and accuracy comparison: raw files vs firekeep-symdex.

Compares three approaches for understanding a codebase:
1. Raw file reading (dump entire files)
2. firekeep-symdex symbols only (functions, classes, methods, etc.)
3. firekeep-symdex smart context (focused query with dependency inclusion)

Measures: token count, symbol coverage, bytes transferred.
"""

import sys
from pathlib import Path

# Add project to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from firekeep_symdex.storage import IndexStore
from firekeep_symdex.tools.get_context import get_context


def estimate_tokens(text: str) -> int:
    """Estimate token count (1 token ≈ 4 bytes for code)."""
    return len(text.encode("utf-8")) // 4 or 1


def run_comparison(owner: str, name: str, storage_path: str = None):
    """Run full comparison for an indexed repo."""
    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)

    if not index:
        print(f"  ERROR: {owner}/{name} not indexed")
        return

    print(f"\n{'='*70}")
    print(f"  REPO: {owner}/{name}")
    print(f"  Files: {len(index.source_files)}  |  Symbols: {len(index.symbols)}")
    print(f"  Languages: {index.languages}")
    print(f"{'='*70}")

    # --- Approach 1: Raw file reading ---
    content_dir = store._content_dir(owner, name)
    total_raw_bytes = 0
    total_raw_tokens = 0
    files_read = 0

    for file_path in index.source_files:
        safe_path = store._safe_content_path(content_dir, file_path)
        if safe_path and safe_path.exists():
            content = safe_path.read_text(encoding="utf-8", errors="replace")
            total_raw_bytes += len(content.encode("utf-8"))
            total_raw_tokens += estimate_tokens(content)
            files_read += 1

    print(f"\n  1. RAW FILE READING (all {files_read} files)")
    print(f"     Bytes: {total_raw_bytes:,}")
    print(f"     Tokens: {total_raw_tokens:,}")

    # --- Approach 2: Symbols only (signatures + source) ---
    total_sym_bytes = 0
    total_sig_bytes = 0
    symbols_with_source = 0
    kind_counts = {}

    for sym in index.symbols:
        # Signature-only cost
        sig_text = f"{sym['kind']} {sym['name']}: {sym['signature']}"
        if sym.get("summary"):
            sig_text += f" — {sym['summary']}"
        total_sig_bytes += len(sig_text.encode("utf-8"))

        # Full source cost
        source = store.get_symbol_content(owner, name, sym["id"])
        if source:
            total_sym_bytes += len(source.encode("utf-8"))
            symbols_with_source += 1

        kind = sym.get("kind", "unknown")
        kind_counts[kind] = kind_counts.get(kind, 0) + 1

    total_sym_tokens = total_sym_bytes // 4 or 1
    total_sig_tokens = total_sig_bytes // 4 or 1

    print("\n  2. FIREKEEP-SYMDEX: ALL SYMBOLS (source)")
    print(f"     Symbols: {symbols_with_source} ({', '.join(f'{v} {k}s' for k, v in sorted(kind_counts.items(), key=lambda x: -x[1]))})")
    print(f"     Bytes: {total_sym_bytes:,}")
    print(f"     Tokens: {total_sym_tokens:,}")
    savings_pct = (1 - total_sym_tokens / total_raw_tokens) * 100 if total_raw_tokens else 0
    print(f"     Savings vs raw: {savings_pct:.1f}%")

    print("\n  2b. FIREKEEP-SYMDEX: SIGNATURES ONLY (no source)")
    print(f"     Bytes: {total_sig_bytes:,}")
    print(f"     Tokens: {total_sig_tokens:,}")
    sig_savings = (1 - total_sig_tokens / total_raw_tokens) * 100 if total_raw_tokens else 0
    print(f"     Savings vs raw: {sig_savings:.1f}%")

    # --- Approach 3: Smart context (focused query) ---
    queries = ["request handling", "middleware", "routing", "config"]
    budget = 4000

    print(f"\n  3. SMART CONTEXT (budget={budget} tokens, include_deps=True)")
    for query in queries:
        result = get_context(
            repo=f"{owner}/{name}",
            budget_tokens=budget,
            focus=query,
            include_deps=True,
            storage_path=storage_path,
        )
        if "error" in result:
            continue

        meta = result.get("_meta", {})
        syms = result.get("symbols", [])
        tokens_used = meta.get("tokens_used", 0)
        deps = meta.get("deps_included", 0)

        sym_names = [s["name"] for s in syms[:5]]
        name_str = ", ".join(sym_names)
        if len(syms) > 5:
            name_str += f" (+{len(syms)-5} more)"

        print(f"     Query '{query}': {len(syms)} symbols ({deps} deps), {tokens_used} tokens")
        print(f"       -> {name_str}")

    # --- Accuracy analysis ---
    print("\n  ACCURACY ANALYSIS")

    # Check how many lines of the raw files are covered by symbols
    total_lines = 0
    covered_lines = 0
    for file_path in index.source_files:
        safe_path = store._safe_content_path(content_dir, file_path)
        if not safe_path or not safe_path.exists():
            continue
        content = safe_path.read_text(encoding="utf-8", errors="replace")
        file_lines = content.count("\n") + 1
        total_lines += file_lines

        # Find symbols in this file
        file_syms = [s for s in index.symbols if s["file"] == file_path]
        for sym in file_syms:
            start = sym.get("line", 0)
            end = sym.get("end_line", start)
            covered_lines += (end - start + 1)

    coverage_pct = (covered_lines / total_lines * 100) if total_lines else 0
    print(f"     Total source lines: {total_lines:,}")
    print(f"     Lines covered by symbols: {covered_lines:,}")
    print(f"     Line coverage: {coverage_pct:.1f}%")

    # Check reference quality
    total_refs = len(index.references)
    import_refs = sum(1 for r in index.references if r.get("type") == "import")
    call_refs = sum(1 for r in index.references if r.get("type") == "call")
    print(f"     References: {total_refs} total ({import_refs} imports, {call_refs} calls)")

    # File summaries
    summaries = getattr(index, "file_summaries", {})
    print(f"     File summaries: {len(summaries)}/{len(index.source_files)} files")

    # Preambles
    preambles = [s for s in index.symbols if s.get("kind") == "module"]
    print(f"     Preambles captured: {len(preambles)}/{len(index.source_files)} files")

    # Final summary
    print(f"\n  {'-'*50}")
    print("  SUMMARY")
    print(f"  {'-'*50}")
    print(f"  Raw files:       {total_raw_tokens:>8,} tokens")
    print(f"  All symbols:     {total_sym_tokens:>8,} tokens  ({savings_pct:+.1f}%)")
    print(f"  Signatures only: {total_sig_tokens:>8,} tokens  ({sig_savings:+.1f}%)")
    print(f"  Smart context:   {budget:>8,} tokens  (per query, focused)")
    print(f"  Line coverage:   {coverage_pct:.1f}%")
    print()


def main():
    store = IndexStore()
    repos = store.list_repos()

    if not repos:
        print("No indexed repos found. Run index_folder or index_repo first.")
        return

    print("\n" + "=" * 70)
    print("  FIREKEEP-SYMDEX TOKEN SAVINGS & ACCURACY REPORT")
    print("=" * 70)

    for repo_info in repos:
        parts = repo_info["repo"].split("/")
        run_comparison(parts[0], parts[1])

    print("=" * 70)
    print("  END OF REPORT")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()
