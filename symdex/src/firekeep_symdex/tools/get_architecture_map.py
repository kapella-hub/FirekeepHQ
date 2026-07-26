"""Auto-detect architectural layers and classify files by their role in the codebase."""

import os
import re
import time
from collections import defaultdict
from typing import Optional

from ..storage import IndexStore
from ._utils import resolve_repo
from .get_import_graph import _resolve_import


def get_architecture_map(
    repo: str,
    storage_path: Optional[str] = None,
) -> dict:
    """Classify every indexed file into an architectural layer and compute metrics.

    Builds a file-level import graph internally, then uses path heuristics and
    graph topology (importers / imports counts) to assign each file to a layer.

    Args:
        repo: Repository identifier (owner/repo or just repo name).
        storage_path: Custom storage path.

    Returns:
        Dict with layers, spine, file count, layer count, and metadata.
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

    # ------------------------------------------------------------------
    # 1. Build file-level import graph
    # ------------------------------------------------------------------
    imports: dict[str, set[str]] = defaultdict(set)   # file -> files it imports
    importers: dict[str, set[str]] = defaultdict(set)  # file -> files that import it

    for ref in index.references:
        if ref.get("type") != "import":
            continue
        importing_file = ref.get("file", "")
        module_name = ref.get("name", "")
        if not importing_file or not module_name:
            continue

        resolved = _resolve_import(importing_file, module_name, source_files)
        if resolved and resolved != importing_file:
            imports[importing_file].add(resolved)
            importers[resolved].add(importing_file)

    # ------------------------------------------------------------------
    # 2. Build symbol count per file
    # ------------------------------------------------------------------
    symbols_per_file: dict[str, int] = defaultdict(int)
    for sym in index.symbols:
        f = sym.get("file", "")
        if f:
            symbols_per_file[f] += 1

    # ------------------------------------------------------------------
    # 3. Classify each file into a layer
    # ------------------------------------------------------------------
    LAYER_DESCRIPTIONS = {
        "test": "Test files",
        "config": "Configuration files",
        "entry": "Application entry points",
        "api/routes": "API endpoints and request handlers",
        "core/service": "Business logic and service layer",
        "utility": "Shared utilities and helpers",
        "model/data": "Data models and schemas",
        "other": "Uncategorised files",
    }

    file_layers: dict[str, str] = {}

    for f in index.source_files:
        file_layers[f] = _classify_file(
            f, imports, importers, len(source_files),
        )

    # ------------------------------------------------------------------
    # 4. Aggregate into layer output
    # ------------------------------------------------------------------
    layers_data: dict[str, dict] = {}

    for layer_name, description in LAYER_DESCRIPTIONS.items():
        files_in_layer = sorted(
            f for f, layer in file_layers.items() if layer == layer_name
        )
        if not files_in_layer:
            continue

        sym_count = sum(symbols_per_file.get(f, 0) for f in files_in_layer)
        total_imports = sum(len(imports.get(f, set())) for f in files_in_layer)
        avg_imports = round(total_imports / len(files_in_layer), 1) if files_in_layer else 0

        layers_data[layer_name] = {
            "files": files_in_layer,
            "symbol_count": sym_count,
            "avg_imports": avg_imports,
            "description": description,
        }

    # ------------------------------------------------------------------
    # 5. Find the spine (longest import chain)
    # ------------------------------------------------------------------
    spine = _find_spine(imports, source_files)

    elapsed = (time.perf_counter() - start) * 1000

    return {
        "repo": f"{owner}/{name}",
        "layers": layers_data,
        "spine": spine,
        "file_count": len(index.source_files),
        "layer_count": len(layers_data),
        "_meta": {"timing_ms": round(elapsed, 1)},
    }


# ------------------------------------------------------------------
# Classification helpers
# ------------------------------------------------------------------

_TEST_PATTERN = re.compile(
    r"(^|/)(test|spec|__tests__|tests)(/|$)|/test_[^/]*$|^test_",
    re.IGNORECASE,
)

_CONFIG_PATTERN = re.compile(
    r"(^|/)config(/|$)"
    r"|/config\.[^/]*$"
    r"|/settings\.[^/]*$"
    r"|/[^/]*\.config\.[^/]*$",
    re.IGNORECASE,
)

_API_PATTERN = re.compile(
    r"(^|/)(route|handler|controller|endpoint|view|api)s?(/|$)",
    re.IGNORECASE,
)

_SERVICE_PATTERN = re.compile(
    r"(^|/)(service|core|domain)s?(/|$)",
    re.IGNORECASE,
)

_UTILITY_PATTERN = re.compile(
    r"(^|/)(util|helper|lib|common)s?(/|$)",
    re.IGNORECASE,
)

_MODEL_PATTERN = re.compile(
    r"(^|/)(model|schema|entity|type|interface|dto)s?(/|$)"
    r"|/models?\.[^/]*$"
    r"|/schemas?\.[^/]*$"
    r"|/types?\.[^/]*$",
    re.IGNORECASE,
)


def _classify_file(
    file_path: str,
    imports: dict[str, set[str]],
    importers: dict[str, set[str]],
    total_files: int,
) -> str:
    """Classify a single file into an architectural layer.

    Priority order: test > config > entry > api/routes > core/service >
    utility > model/data > other.
    """
    normalized = file_path.replace("\\", "/")
    basename = os.path.basename(normalized)

    # 1. Test
    if _TEST_PATTERN.search(normalized) or basename.startswith("test_"):
        return "test"

    # 2. Config
    if _CONFIG_PATTERN.search(normalized):
        return "config"

    has_imports = bool(imports.get(file_path))
    has_importers = bool(importers.get(file_path))

    # 3. Entry: nothing imports this file, but it imports others
    if not has_importers and has_imports:
        return "entry"

    # 4. API / routes
    if _API_PATTERN.search(normalized):
        return "api/routes"

    # 5. Core / service: both imported and imports (middle of graph),
    #    or lives in a service/core/domain directory
    if _SERVICE_PATTERN.search(normalized):
        return "core/service"
    if has_importers and has_imports:
        # Check if it's more of a utility (many importers, few imports)
        importer_count = len(importers.get(file_path, set()))
        import_count = len(imports.get(file_path, set()))
        # Utility threshold: imported by many (>= 3 or >= 10% of files) and imports few (<= 1)
        utility_threshold = max(3, total_files // 10)
        if importer_count >= utility_threshold and import_count <= 1:
            return "utility"
        return "core/service"

    # 6. Utility: imported by others but imports nothing itself,
    #    or lives in a utility directory
    if _UTILITY_PATTERN.search(normalized):
        return "utility"
    if has_importers and not has_imports:
        return "utility"

    # 7. Model / data
    if _MODEL_PATTERN.search(normalized):
        return "model/data"

    return "other"


# ------------------------------------------------------------------
# Spine detection (longest path via DFS)
# ------------------------------------------------------------------

def _find_spine(
    imports: dict[str, set[str]],
    source_files: set[str],
) -> list[str]:
    """Find the longest import chain (spine) through the codebase.

    Uses iterative DFS with mutable path (push/pop) to avoid O(branches^depth)
    memory from copying path lists on each step.
    """
    if not imports:
        return []

    # Build importers set for finding roots
    all_importers: dict[str, set[str]] = defaultdict(set)
    for src, targets in imports.items():
        for t in targets:
            all_importers[t].add(src)

    # Start from files that nothing imports (entry candidates)
    roots = [f for f in imports if f not in all_importers]
    if not roots:
        # If there are cycles, start from all files that have imports
        roots = list(imports.keys())

    best_path: list[str] = []

    for root in roots:
        path = [root]
        visited = {root}
        # Stack holds iterators over neighbors
        stack = [iter(imports.get(root, set()))]

        while stack:
            try:
                neighbor = next(stack[-1])
            except StopIteration:
                stack.pop()
                if path:
                    visited.discard(path.pop())
                continue

            if neighbor in visited:
                continue

            path.append(neighbor)
            visited.add(neighbor)

            if len(path) > len(best_path):
                best_path = list(path)

            # Cap search depth to avoid excessive work
            if len(path) >= 50:
                path.pop()
                visited.discard(neighbor)
                continue

            stack.append(iter(imports.get(neighbor, set())))

    return best_path


TOOL_DEF = {
    "name": "get_architecture_map",
    "description": "Auto-detect architectural layers in a codebase. Classifies every file into a layer (entry, api/routes, core/service, utility, model/data, test, config) using import-graph topology and path heuristics. Also finds the longest import chain (spine).",
    "inputSchema": {
            "type": "object",
            "properties": {
                    "repo": {
                            "type": "string",
                            "description": "Repository identifier (owner/repo or just repo name)"
                    }
            },
            "required": [
                    "repo"
            ]
    },
    "handler": get_architecture_map,
}
