"""Deterministic symbol-context benchmark across every built-in language.

This benchmark measures one bounded workflow: retrieving a known symbol instead
of reading its entire containing file. It does not claim to measure whole-agent
token use or answer quality. Source fidelity is verified byte-for-byte through
the index to each pinned Git blob, so the quality check does not require an LLM
judge.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import platform
import random
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from unittest.mock import patch

import tiktoken

from firekeep_symdex.storage import IndexStore
from firekeep_symdex.tools.index_folder import index_folder


HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent
MANIFEST_PATH = HERE / "polyglot_repos.json"
ENVIRONMENT_PATH = HERE / "benchmark-requirements.txt"
REPOS_DIR = HERE / "repos" / "polyglot"
INDEX_DIR = HERE / "repos" / ".polyglot-index"
RESULTS_DIR = HERE / "results"
BENCHMARK_SOURCE_PATHS = (
    "src/firekeep_symdex/parser",
    "src/firekeep_symdex/security.py",
    "src/firekeep_symdex/storage",
    "src/firekeep_symdex/summarizer",
    "src/firekeep_symdex/tools/__init__.py",
    "src/firekeep_symdex/tools/_utils.py",
    "src/firekeep_symdex/tools/get_symbol.py",
    "src/firekeep_symdex/tools/index_folder.py",
)

DEFAULT_SAMPLE_SIZE = 30
MIN_SYMBOL_BYTES = 80
MIN_SYMBOL_LINES = 3
ELIGIBLE_KINDS = {"function", "method", "class", "type"}
ENCODINGS = ("cl100k_base", "o200k_base")
BOOTSTRAP_SAMPLES = 5_000
BOOTSTRAP_SEED = 20260801
EXCLUDED_PATH_PARTS = {
    "test",
    "tests",
    "spec",
    "specs",
    "__tests__",
    "fixture",
    "fixtures",
    "example",
    "examples",
    "benchmark",
    "benchmarks",
}


def load_manifest(path: Path = MANIFEST_PATH) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_paths_sha256(root: Path, relative_paths: tuple[str, ...]) -> str:
    """Fingerprint the exact implementation files exercised by the benchmark."""
    digest = hashlib.sha256()
    files: list[Path] = []
    for relative_path in relative_paths:
        path = root / relative_path
        files.extend(path.rglob("*.py") if path.is_dir() else [path])
    for path in sorted(set(files)):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def package_version(distribution: str) -> str | None:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


def provenance() -> dict[str, Any]:
    source_pathspecs = [f"symdex/{path}" for path in BENCHMARK_SOURCE_PATHS]
    relevant_status = git_status_entries(
        PROJECT_ROOT,
        pathspecs=[
            "symdex/benchmarks",
            *source_pathspecs,
        ],
    )
    relevant_status = [
        entry
        for entry in relevant_status
        if not entry["path"].replace("\\", "/").startswith(
            "symdex/benchmarks/results/"
        )
    ]
    return {
        "firekeep_git_commit": _run_git(["rev-parse", "HEAD"], PROJECT_ROOT),
        "relevant_worktree_clean": not bool(relevant_status),
        "relevant_worktree_status": relevant_status,
        "benchmark_script_sha256": sha256_file(Path(__file__).resolve()),
        "manifest_sha256": sha256_file(MANIFEST_PATH),
        "benchmark_environment_sha256": sha256_file(ENVIRONMENT_PATH),
        "symdex_source_paths": list(BENCHMARK_SOURCE_PATHS),
        "symdex_source_paths_sha256": source_paths_sha256(
            HERE.parent,
            BENCHMARK_SOURCE_PATHS,
        ),
        "python": sys.version,
        "platform": platform.platform(),
        "dependencies": {
            distribution: package_version(distribution)
            for distribution in (
                "firekeep-symdex",
                "tiktoken",
                "tree-sitter-language-pack",
            )
        },
    }


def _run_git(args: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def git_status_entries(
    repo_path: Path, pathspecs: list[str] | None = None
) -> list[dict[str, str]]:
    """Parse NUL-delimited porcelain output without quoted-path ambiguity."""
    args = ["git", "status", "--porcelain=v1", "-z"]
    if pathspecs:
        args.extend(["--", *pathspecs])
    result = subprocess.run(
        args,
        cwd=str(repo_path),
        check=True,
        capture_output=True,
    )
    return parse_porcelain_z(result.stdout)


def parse_porcelain_z(output: bytes) -> list[dict[str, str]]:
    """Parse `git status --porcelain=v1 -z` bytes, including rename pairs."""
    records = output.split(b"\0")
    entries: list[dict[str, str]] = []
    index = 0
    while index < len(records) and records[index]:
        record = records[index].decode("utf-8", errors="surrogateescape")
        status = record[:2]
        entry = {"status": status, "path": record[3:]}
        if "R" in status or "C" in status:
            index += 1
            entry["original_path"] = records[index].decode(
                "utf-8", errors="surrogateescape"
            )
        entries.append(entry)
        index += 1
    return entries


def read_git_blob(repo_path: Path, commit: str, file_path: str) -> bytes:
    """Read canonical bytes directly from the pinned Git object database."""
    result = subprocess.run(
        ["git", "show", f"{commit}:{file_path}"],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
    )
    return result.stdout


def ensure_repo(spec: dict[str, Any], clone_missing: bool = True) -> Path:
    """Clone a pinned repository if allowed, then verify its measured sources."""
    target = REPOS_DIR / spec["id"]
    expected = spec["commit"]

    if not target.exists():
        if not clone_missing:
            raise RuntimeError(f"Benchmark repo is missing: {target}")
        target.mkdir(parents=True)
        _run_git(["init"], target)
        _run_git(["remote", "add", "origin", spec["url"]], target)
        _run_git(["fetch", "--depth", "1", "origin", expected], target)
        _run_git(["checkout", "--detach", "FETCH_HEAD"], target)

    actual = _run_git(["rev-parse", "HEAD"], target)
    if actual != expected:
        raise RuntimeError(
            f"{spec['id']} is at {actual}; expected pinned commit {expected}. "
            "Remove the ignored benchmark clone and run again."
        )
    status = git_status_entries(target)
    relevant_changes = measured_source_changes(spec, status)
    if relevant_changes:
        raise RuntimeError(
            f"{spec['id']} has local changes in measured source. Restore or remove "
            f"the ignored benchmark clone before running:\n{relevant_changes}"
        )
    return target


def index_repo(spec: dict[str, Any], repo_path: Path) -> dict[str, Any]:
    result = index_folder(
        path=str(repo_path),
        use_ai_summaries=False,
        storage_path=str(INDEX_DIR),
    )
    if not result.get("success"):
        raise RuntimeError(f"Could not index {spec['id']}: {result.get('error')}")
    return result


def is_test_path(file_path: str) -> bool:
    """Exclude obvious tests, fixtures, examples, and benchmark sources."""
    path = PurePosixPath(file_path)
    parts = [part.lower() for part in path.parts]
    if any(part in EXCLUDED_PATH_PARTS for part in parts[:-1]):
        return True
    name = parts[-1] if parts else ""
    return (
        name.startswith("test_")
        or "_test." in name
        or ".test." in name
        or ".spec." in name
        or name.endswith("_spec.rb")
    )


def measured_source_changes(
    spec: dict[str, Any], status: list[dict[str, str]]
) -> list[dict[str, str]]:
    """Return dirty tracked paths that could enter this repository's sample."""
    prefix = spec["source_prefix"]
    extensions = set(spec["extensions"])
    changes: list[dict[str, str]] = []
    for entry in status:
        paths = [entry["path"], entry.get("original_path", "")]
        for path in paths:
            path = path.replace("\\", "/")
            if not path:
                continue
            if prefix and not path.startswith(prefix):
                continue
            if Path(path).suffix.lower() not in extensions:
                continue
            if is_test_path(path):
                continue
            changes.append(entry)
            break
    return changes


def eligible_symbols(
    spec: dict[str, Any], store: IndexStore, index: Any
) -> list[dict[str, Any]]:
    prefix = spec["source_prefix"]
    extensions = set(spec["extensions"])
    eligible: list[dict[str, Any]] = []

    for symbol in index.symbols:
        if symbol.get("language") != spec["language"]:
            continue
        if symbol.get("kind") not in ELIGIBLE_KINDS:
            continue
        file_path = symbol.get("file", "")
        if prefix and not file_path.startswith(prefix):
            continue
        if Path(file_path).suffix.lower() not in extensions:
            continue
        if is_test_path(file_path):
            continue
        line_count = symbol.get("end_line", 0) - symbol.get("line", 0) + 1
        if line_count < MIN_SYMBOL_LINES:
            continue
        if symbol.get("byte_length", 0) < MIN_SYMBOL_BYTES:
            continue
        source = store.get_symbol_content("local", spec["id"], symbol["id"])
        if not source or not source.strip():
            continue
        eligible.append(symbol)

    return eligible


def select_symbols(
    spec: dict[str, Any], symbols: list[dict[str, Any]], sample_size: int
) -> list[dict[str, Any]]:
    """Select a stable, file-balanced sample independent of index order."""
    if len(symbols) < sample_size:
        raise RuntimeError(
            f"{spec['language']} has only {len(symbols)} eligible symbols; "
            f"the protocol requires {sample_size}."
        )

    by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for symbol in symbols:
        by_file[symbol["file"]].append(symbol)

    def stable_hash(value: str) -> str:
        payload = f"{spec['language']}:{spec['commit']}:{value}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    ranked_files = sorted(by_file, key=lambda file_path: stable_hash(file_path))
    for file_path in ranked_files:
        by_file[file_path].sort(key=lambda symbol: stable_hash(symbol["id"]))

    selected: list[dict[str, Any]] = []
    depth = 0
    while len(selected) < sample_size:
        added = False
        for file_path in ranked_files:
            file_symbols = by_file[file_path]
            if depth >= len(file_symbols):
                continue
            selected.append(file_symbols[depth])
            added = True
            if len(selected) == sample_size:
                return selected
        if not added:
            break
        depth += 1

    raise RuntimeError(
        f"Could not select {sample_size} file-balanced symbols for {spec['language']}"
    )


def stable_tool_response(result: dict[str, Any]) -> dict[str, Any]:
    """Remove only timing and cumulative counters from the measured response."""
    stable = dict(result)
    meta = dict(stable.get("_meta", {}))
    for key in ("timing_ms", "total_tokens_saved", "total_cost_avoided"):
        meta.pop(key, None)
    stable["_meta"] = meta
    return stable


def _token_count(encoding: Any, text: str) -> int:
    return len(encoding.encode(text))


def benchmark_symbol(
    spec: dict[str, Any],
    symbol: dict[str, Any],
    repo_path: Path,
    store: IndexStore,
    encodings: dict[str, Any],
) -> dict[str, Any]:
    module = importlib.import_module("firekeep_symdex.tools.get_symbol")
    with patch.object(module, "record_savings", lambda saved: saved):
        result = module.get_symbol(
            repo=f"local/{spec['id']}",
            symbol_id=symbol["id"],
            verify=True,
            storage_path=str(INDEX_DIR),
        )
    if "error" in result:
        raise RuntimeError(f"get_symbol failed for {symbol['id']}: {result['error']}")

    raw_path = store._safe_content_path(
        store._content_dir("local", spec["id"]), symbol["file"]
    )
    if not raw_path or not raw_path.exists():
        raise RuntimeError(f"Raw indexed file is missing: {symbol['file']}")

    raw_bytes = raw_path.read_bytes()
    repo_root = repo_path.resolve()
    worktree_path = (repo_root / symbol["file"]).resolve()
    try:
        worktree_path.relative_to(repo_root)
    except ValueError as exc:
        raise RuntimeError(f"Source path escapes pinned repository: {symbol['file']}") from exc
    if not worktree_path.is_file():
        raise RuntimeError(f"Pinned source file is missing: {symbol['file']}")
    git_blob = read_git_blob(repo_root, spec["commit"], symbol["file"])
    indexed_file_matches_git = raw_bytes == git_blob

    start = symbol["byte_offset"]
    expected_source = raw_bytes[start : start + symbol["byte_length"]].decode(
        "utf-8", errors="replace"
    )
    source_fidelity = (
        result.get("source") == expected_source
        and result.get("_meta", {}).get("content_verified") is True
        and indexed_file_matches_git
    )

    tool_text = json.dumps(
        stable_tool_response(result),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    raw_text = (
        f"## {symbol['file']}\n"
        f"```{spec['language']}\n"
        f"{raw_bytes.decode('utf-8', errors='replace')}\n"
        "```"
    )

    token_metrics: dict[str, Any] = {}
    for name, encoding in encodings.items():
        symdex_tokens = _token_count(encoding, tool_text)
        raw_tokens = _token_count(encoding, raw_text)
        token_metrics[name] = {
            "symdex": symdex_tokens,
            "raw": raw_tokens,
            "reduction_pct": round(100 * (1 - symdex_tokens / raw_tokens), 4),
        }

    return {
        "language": spec["language"],
        "repo": spec["id"],
        "symbol_id": symbol["id"],
        "kind": symbol["kind"],
        "file": symbol["file"],
        "line": symbol["line"],
        "end_line": symbol["end_line"],
        "source_fidelity": source_fidelity,
        "indexed_file_matches_git": indexed_file_matches_git,
        "tokens": token_metrics,
    }


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def stratified_file_bootstrap_ci(
    rows: list[dict[str, Any]], encoding: str
) -> tuple[float, float]:
    by_language: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in rows:
        by_language[row["language"]][row["file"]].append(
            row["tokens"][encoding]["reduction_pct"]
        )

    rng = random.Random(BOOTSTRAP_SEED)
    samples: list[float] = []
    for _ in range(BOOTSTRAP_SAMPLES):
        language_means = []
        for file_values in by_language.values():
            files = list(file_values)
            draw = []
            for _ in files:
                draw.extend(file_values[rng.choice(files)])
            language_means.append(sum(draw) / len(draw))
        samples.append(sum(language_means) / len(language_means))
    return _percentile(samples, 0.025), _percentile(samples, 0.975)


def summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_language: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_language[row["language"]].append(row)

    summary: dict[str, Any] = {
        "sample_count": len(rows),
        "language_count": len(by_language),
        "unique_file_count": len({(row["repo"], row["file"]) for row in rows}),
        "source_fidelity_passes": sum(row["source_fidelity"] for row in rows),
        "git_file_matches": sum(
            row["indexed_file_matches_git"] for row in rows
        ),
        "encodings": {},
        "by_language": {},
    }

    for encoding in ENCODINGS:
        reductions = [row["tokens"][encoding]["reduction_pct"] for row in rows]
        symdex_total = sum(row["tokens"][encoding]["symdex"] for row in rows)
        raw_total = sum(row["tokens"][encoding]["raw"] for row in rows)
        ci_low, ci_high = stratified_file_bootstrap_ci(rows, encoding)
        summary["encodings"][encoding] = {
            "mean_reduction_pct": round(sum(reductions) / len(reductions), 2),
            "mean_reduction_file_cluster_ci_95": [
                round(ci_low, 2),
                round(ci_high, 2),
            ],
            "median_reduction_pct": round(_percentile(reductions, 0.5), 2),
            "total_symdex_tokens": symdex_total,
            "total_raw_tokens": raw_total,
            "total_reduction_pct": round(100 * (1 - symdex_total / raw_total), 2),
            "min_reduction_pct": round(min(reductions), 2),
            "max_reduction_pct": round(max(reductions), 2),
            "lookups_using_more_tokens": sum(value < 0 for value in reductions),
        }

    for language, language_rows in sorted(by_language.items()):
        item: dict[str, Any] = {
            "sample_count": len(language_rows),
            "unique_file_count": len({row["file"] for row in language_rows}),
            "source_fidelity_passes": sum(
                row["source_fidelity"] for row in language_rows
            ),
            "encodings": {},
        }
        for encoding in ENCODINGS:
            reductions = [
                row["tokens"][encoding]["reduction_pct"] for row in language_rows
            ]
            symdex_total = sum(
                row["tokens"][encoding]["symdex"] for row in language_rows
            )
            raw_total = sum(row["tokens"][encoding]["raw"] for row in language_rows)
            item["encodings"][encoding] = {
                "mean_reduction_pct": round(sum(reductions) / len(reductions), 2),
                "median_reduction_pct": round(_percentile(reductions, 0.5), 2),
                "total_reduction_pct": round(100 * (1 - symdex_total / raw_total), 2),
                "lookups_using_more_tokens": sum(value < 0 for value in reductions),
            }
        summary["by_language"][language] = item

    return summary


def print_report(summary: dict[str, Any]) -> None:
    primary = summary["encodings"]["cl100k_base"]
    print("\nPolyglot targeted-symbol benchmark")
    print("=" * 71)
    print(
        f"{summary['sample_count']} lookups across "
        f"{summary['language_count']} languages and {summary['unique_file_count']} files"
    )
    print(
        f"Mean token reduction: {primary['mean_reduction_pct']:.2f}% "
        f"(95% file-cluster bootstrap interval "
        f"{primary['mean_reduction_file_cluster_ci_95'][0]:.2f}% to "
        f"{primary['mean_reduction_file_cluster_ci_95'][1]:.2f}%)"
    )
    print(f"Median token reduction: {primary['median_reduction_pct']:.2f}%")
    print(f"Total token reduction: {primary['total_reduction_pct']:.2f}%")
    print(
        f"Exact source fidelity: {summary['source_fidelity_passes']}/"
        f"{summary['sample_count']}"
    )
    print(f"Lookups using more tokens: {primary['lookups_using_more_tokens']}")
    print("\nLanguage       Mean       Median      Total       Files   Fidelity")
    print("-" * 79)
    for language, item in summary["by_language"].items():
        metrics = item["encodings"]["cl100k_base"]
        print(
            f"{language:<14} {metrics['mean_reduction_pct']:>7.2f}% "
            f"{metrics['median_reduction_pct']:>10.2f}% "
            f"{metrics['total_reduction_pct']:>9.2f}% "
            f"{item['unique_file_count']:>7} "
            f"{item['source_fidelity_passes']:>5}/{item['sample_count']:<5}"
        )


def run_benchmark(sample_size: int, setup: bool, reindex: bool) -> dict[str, Any]:
    specs = load_manifest()
    encodings = {name: tiktoken.get_encoding(name) for name in ENCODINGS}
    index_stats: dict[str, Any] = {}
    repository_verification: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []

    for spec in specs:
        repo_path = ensure_repo(spec, clone_missing=setup)
        status = git_status_entries(repo_path)
        repository_verification[spec["language"]] = {
            "head": _run_git(["rev-parse", "HEAD"], repo_path),
            "measured_source_clean": not bool(measured_source_changes(spec, status)),
            "worktree_status": status,
        }
        if reindex:
            result = index_repo(spec, repo_path)
            index_stats[spec["language"]] = {
                "repo": result.get("repo"),
                "file_count": result.get("file_count"),
                "symbol_count": result.get("symbol_count"),
            }

        store = IndexStore(base_path=INDEX_DIR)
        index = store.load_index("local", spec["id"])
        if not index:
            raise RuntimeError(f"Index is missing for local/{spec['id']}")
        candidates = eligible_symbols(spec, store, index)
        selected = select_symbols(spec, candidates, sample_size)
        for symbol in selected:
            rows.append(benchmark_symbol(spec, symbol, repo_path, store, encodings))

    summary = summarize_rows(rows)
    return {
        "protocol": {
            "name": "polyglot-targeted-symbol-v2",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "sample_size_per_language": sample_size,
            "min_symbol_bytes": MIN_SYMBOL_BYTES,
            "min_symbol_lines": MIN_SYMBOL_LINES,
            "eligible_kinds": sorted(ELIGIBLE_KINDS),
            "selection": (
                "file-balanced round-robin; SHA-256 order of "
                "language:commit:file and language:commit:symbol_id"
            ),
            "excluded_path_parts": sorted(EXCLUDED_PATH_PARTS),
            "excluded_file_forms": [
                "test_*",
                "*_test.*",
                "*.test.*",
                "*.spec.*",
                "*_spec.rb",
            ],
            "baseline": "entire containing file",
            "symdex_context": "stable get_symbol MCP payload",
            "fidelity": (
                "response source equals indexed byte slice, get_symbol content hash "
                "passes, and indexed file equals the pinned Git blob"
            ),
            "encodings": list(ENCODINGS),
        },
        "provenance": provenance(),
        "repositories": specs,
        "repository_verification": repository_verification,
        "index_stats": index_stats,
        "summary": summary,
        "results": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE)
    parser.add_argument("--skip-setup", action="store_true")
    parser.add_argument("--skip-index", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    result = run_benchmark(
        sample_size=args.sample_size,
        setup=not args.skip_setup,
        reindex=not args.skip_index,
    )
    print_report(result["summary"])

    output = args.output
    if output is None:
        RESULTS_DIR.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output = RESULTS_DIR / f"polyglot_symbol_{timestamp}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"\nResults saved to {output}")


if __name__ == "__main__":
    main()
