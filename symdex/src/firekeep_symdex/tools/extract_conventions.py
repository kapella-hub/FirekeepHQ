"""Analyze codebase to extract naming conventions, structure patterns, and framework detection."""

import os
import re
import time
from collections import Counter, defaultdict
from typing import Optional

from ..storage import IndexStore
from ._utils import resolve_repo


_SNAKE_RE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)+$")
_CAMEL_RE = re.compile(r"^[a-z][a-zA-Z0-9]*$")
_PASCAL_RE = re.compile(r"^[A-Z][a-zA-Z0-9]*$")
_UPPER_RE = re.compile(r"^[A-Z][A-Z0-9]*(_[A-Z0-9]+)*$")
_KEBAB_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)+$")

_FRAMEWORK_INDICATORS = {
    "fastapi": ["fastapi", "FastAPI", "APIRouter"],
    "flask": ["flask", "Flask", "Blueprint"],
    "django": ["django", "models.Model", "views"],
    "express": ["express", "Router", "app.get", "app.post"],
    "gin": ["gin", "gin.Context"],
    "spring": ["springframework", "RestController", "RequestMapping"],
    "react": ["react", "useState", "useEffect", "jsx"],
    "nextjs": ["next", "getServerSideProps", "getStaticProps"],
}

_ERROR_PATTERNS = {
    "try/except": re.compile(r"\btry\b.*\bexcept\b", re.DOTALL),
    "try/catch": re.compile(r"\btry\b.*\bcatch\b", re.DOTALL),
    "if err": re.compile(r"\bif\s+err\s*!=\s*nil\b"),
    "Result/unwrap": re.compile(r"\b(unwrap|Result|Ok|Err)\b"),
}


def _classify_name(name: str) -> str:
    """Classify a name into a naming convention."""
    if _UPPER_RE.match(name) and len(name) > 1:
        return "UPPER_CASE"
    if _PASCAL_RE.match(name):
        return "PascalCase"
    if _SNAKE_RE.match(name):
        return "snake_case"
    if _CAMEL_RE.match(name) and not name.islower():
        return "camelCase"
    if name.islower() and "_" not in name:
        return "snake_case"  # single-word lowercase is snake_case compatible
    return "other"


def _classify_filename(filename: str) -> str:
    """Classify a filename's naming convention."""
    stem = os.path.splitext(os.path.basename(filename))[0]
    if _KEBAB_RE.match(stem):
        return "kebab-case"
    if _SNAKE_RE.match(stem) or (stem.islower() and "_" not in stem):
        return "snake_case"
    if _CAMEL_RE.match(stem) and not stem.islower():
        return "camelCase"
    if _PASCAL_RE.match(stem):
        return "PascalCase"
    return "other"


def _detect_test_pattern(source_files: list[str]) -> str:
    """Detect the test file naming pattern."""
    patterns = Counter()
    for f in source_files:
        basename = os.path.basename(f)
        if basename.startswith("test_"):
            patterns["test_*.py"] += 1
        elif basename.endswith("_test.go"):
            patterns["*_test.go"] += 1
        elif ".test." in basename:
            patterns["*.test.*"] += 1
        elif ".spec." in basename:
            patterns["*.spec.*"] += 1
        elif basename.startswith("Test"):
            patterns["Test*.java"] += 1
    if patterns:
        return patterns.most_common(1)[0][0]
    return "unknown"


def _analyze_naming(symbols: list[dict]) -> dict:
    """Analyze naming conventions across symbol kinds."""
    kind_conventions: dict[str, Counter] = defaultdict(Counter)

    for sym in symbols:
        name = sym.get("name", "")
        kind = sym.get("kind", "")
        if not name or not kind:
            continue
        convention = _classify_name(name)
        kind_conventions[kind][convention] += 1

    result = {}
    for kind in ["function", "method", "class", "constant", "type"]:
        counts = kind_conventions.get(kind)
        if not counts:
            continue
        total = sum(counts.values())
        dominant = counts.most_common(1)[0]
        pct = round(dominant[1] / total * 100) if total else 0
        result[kind + "s"] = f"{dominant[0]} ({pct}%)"

    return result


def _analyze_structure(symbols: list[dict], source_files: list[str]) -> dict:
    """Analyze file organization patterns."""
    symbols_per_file: Counter = Counter()
    for sym in symbols:
        symbols_per_file[sym.get("file", "")] += 1

    file_count = len(source_files)
    avg_symbols = round(sum(symbols_per_file.values()) / file_count, 1) if file_count else 0

    # Directory structure
    dirs: Counter = Counter()
    for f in source_files:
        parts = f.replace("\\", "/").split("/")
        if len(parts) > 1:
            dirs[parts[0]] += 1

    top_dirs = [d for d, _ in dirs.most_common(5)]

    # File naming
    file_conventions: Counter = Counter()
    for f in source_files:
        file_conventions[_classify_filename(f)] += 1

    dominant_file = file_conventions.most_common(1)[0] if file_conventions else ("unknown", 0)
    total_files = sum(file_conventions.values())
    file_pct = round(dominant_file[1] / total_files * 100) if total_files else 0

    return {
        "avg_symbols_per_file": avg_symbols,
        "total_files": file_count,
        "total_symbols": len(symbols),
        "test_pattern": _detect_test_pattern(source_files),
        "top_directories": top_dirs,
        "file_naming": f"{dominant_file[0]} ({file_pct}%)",
    }


def _analyze_patterns(symbols: list[dict], index) -> dict:
    """Analyze common code patterns."""
    # Decorators
    decorator_counts: Counter = Counter()
    for sym in symbols:
        for dec in sym.get("decorators", []):
            decorator_counts[dec] += 1

    top_decorators = [{"name": d, "count": c} for d, c in decorator_counts.most_common(10)]

    # Common parameter names
    param_counts: Counter = Counter()
    param_re = re.compile(r"\(([^)]*)\)")
    for sym in symbols:
        sig = sym.get("signature", "")
        m = param_re.search(sig)
        if m:
            for part in m.group(1).split(","):
                part = part.strip()
                if not part:
                    continue
                name = part.split(":")[0].split("=")[0].strip().split()[-1].strip("*&")
                if name and name not in ("self", "this", "cls"):
                    param_counts[name.lower()] += 1

    top_params = [{"name": p, "count": c} for p, c in param_counts.most_common(10)]

    # Error handling patterns (search references for try/except/catch keywords)
    error_styles: Counter = Counter()
    for ref in index.references:
        ref_name = ref.get("name", "")
        if "try" in ref_name.lower() or "catch" in ref_name.lower() or "except" in ref_name.lower():
            error_styles["try/catch or try/except"] += 1
        if "error" in ref_name.lower() or "err" in ref_name.lower():
            error_styles["error variable pattern"] += 1

    # Also check symbol names for error-related patterns
    error_symbols = [s for s in symbols if "error" in s.get("name", "").lower() or "exception" in s.get("name", "").lower()]

    error_handling = "unknown"
    if error_styles:
        error_handling = error_styles.most_common(1)[0][0]
    elif error_symbols:
        error_handling = "custom error/exception classes"

    return {
        "top_decorators": top_decorators,
        "top_parameters": top_params,
        "error_handling": error_handling,
        "error_related_symbols": len(error_symbols),
    }


def _detect_framework(symbols: list[dict], index) -> dict:
    """Detect frameworks from route symbols and import patterns."""
    # Check for route-kind symbols
    route_symbols = [s for s in symbols if s.get("kind") == "route"]

    # Check import references for framework indicators
    import_names: list[str] = []
    for ref in index.references:
        if ref.get("type") == "import":
            import_names.append(ref.get("name", ""))

    # Also check decorator names
    all_decorators: list[str] = []
    for sym in symbols:
        all_decorators.extend(sym.get("decorators", []))

    search_text = " ".join(import_names + all_decorators)

    detected = []
    for framework, indicators in _FRAMEWORK_INDICATORS.items():
        for indicator in indicators:
            if indicator.lower() in search_text.lower():
                detected.append(framework)
                break

    framework_name = detected[0] if detected else "none detected"
    patterns = []
    if route_symbols:
        patterns.append(f"{len(route_symbols)} route symbol(s)")
    if detected:
        patterns.append(f"imports: {', '.join(detected)}")

    return {
        "detected": framework_name,
        "all_detected": detected,
        "patterns": patterns,
        "route_count": len(route_symbols),
    }


def extract_conventions(
    repo: str,
    focus: str = "all",
    storage_path: Optional[str] = None,
) -> dict:
    """Analyze codebase and extract naming conventions, structure patterns, and framework idioms.

    Fully automated - no AI needed.

    Args:
        repo: Repository identifier (owner/repo or just repo name).
        focus: Focus area - "naming", "structure", "patterns", "framework", or "all".
        storage_path: Custom storage path.

    Returns:
        Dict with conventions analysis and _meta envelope.
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

    symbols = index.symbols
    source_files = index.source_files

    if not symbols:
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "naming": {},
            "structure": {"total_files": len(source_files), "total_symbols": 0},
            "patterns": {},
            "framework": {"detected": "none detected"},
            "_meta": {"timing_ms": round(elapsed, 1)},
        }

    result = {}

    if focus in ("all", "naming"):
        result["naming"] = _analyze_naming(symbols)

    if focus in ("all", "structure"):
        result["structure"] = _analyze_structure(symbols, source_files)

    if focus in ("all", "patterns"):
        result["patterns"] = _analyze_patterns(symbols, index)

    if focus in ("all", "framework"):
        result["framework"] = _detect_framework(symbols, index)

    elapsed = (time.perf_counter() - start) * 1000
    result["_meta"] = {"timing_ms": round(elapsed, 1)}

    return result


TOOL_DEF = {
    "name": "extract_conventions",
    "description": "Analyze codebase to extract naming conventions, file organization patterns, common code patterns (decorators, error handling), and framework detection. Fully automated, no AI needed.",
    "inputSchema": {
            "type": "object",
            "properties": {
                    "repo": {
                            "type": "string",
                            "description": "Repository identifier (owner/repo or just repo name)"
                    },
                    "focus": {
                            "type": "string",
                            "description": "Focus area",
                            "enum": [
                                    "naming",
                                    "structure",
                                    "patterns",
                                    "framework",
                                    "all"
                            ],
                            "default": "all"
                    }
            },
            "required": [
                    "repo"
            ]
    },
    "handler": extract_conventions,
}
