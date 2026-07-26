"""Index storage with save/load, byte-offset content retrieval, and incremental indexing."""

import hashlib
import json
import os
import shutil
import subprocess
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..parser.symbols import Symbol

# Bump this when the index schema changes in an incompatible way.
INDEX_VERSION = 2

try:
    import fcntl

    def _lock_file_exclusive(fd) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def _unlock_file(fd) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)

except ImportError:  # Windows has no fcntl; fall back to msvcrt region locking.
    import msvcrt

    def _lock_file_exclusive(fd) -> None:
        fd.seek(0)
        msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock_file(fd) -> None:
        fd.seek(0)
        msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)


def _file_hash(content: str) -> str:
    """SHA-256 hash of file content string."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _get_git_head(repo_path: Path) -> Optional[str]:
    """Get current HEAD commit hash for a git repo, or None."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_path),
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


# Semantic keyword mappings -- common programming concept synonyms
_SEMANTIC_MAP = {
    "auth": ["login", "logout", "authenticate", "authorize", "credential", "token", "session", "permission", "jwt", "oauth"],
    "login": ["auth", "authenticate", "signin", "sign_in", "logon"],
    "user": ["account", "profile", "member", "person"],
    "config": ["configuration", "settings", "options", "preferences", "env"],
    "error": ["exception", "fault", "failure", "issue", "bug"],
    "test": ["spec", "assert", "expect", "verify", "check", "mock"],
    "database": ["db", "sql", "query", "table", "model", "schema", "migration"],
    "db": ["database", "sql", "query", "connection", "pool"],
    "http": ["request", "response", "endpoint", "api", "rest", "fetch", "url"],
    "request": ["req", "http", "fetch", "call", "invoke"],
    "response": ["res", "reply", "result", "output"],
    "route": ["path", "endpoint", "url", "handler", "middleware"],
    "middleware": ["interceptor", "filter", "handler", "plugin", "hook"],
    "cache": ["memoize", "store", "buffer", "memo"],
    "parse": ["deserialize", "decode", "extract", "read", "load"],
    "serialize": ["encode", "stringify", "dump", "marshal", "format"],
    "render": ["display", "show", "draw", "paint", "template", "view"],
    "validate": ["check", "verify", "sanitize", "assert", "ensure"],
    "create": ["new", "make", "build", "init", "generate", "add"],
    "delete": ["remove", "destroy", "drop", "clear", "purge"],
    "update": ["modify", "change", "edit", "patch", "set"],
    "get": ["fetch", "find", "read", "retrieve", "load", "lookup"],
    "send": ["emit", "dispatch", "publish", "broadcast", "push", "notify"],
    "connect": ["link", "join", "attach", "bind", "mount"],
    "file": ["path", "directory", "folder", "fs", "io"],
    "log": ["logger", "debug", "trace", "print", "console"],
    "search": ["find", "query", "lookup", "filter", "match"],
}


def _expand_query_semantically(query_words: set) -> set:
    """Expand query words with semantic synonyms."""
    expanded = set(query_words)
    for word in query_words:
        related = _SEMANTIC_MAP.get(word, [])
        expanded.update(related)
    return expanded


def _subsequence_match(query: str, target: str) -> bool:
    """Check if query is a subsequence of target (fuzzy match).

    e.g., "auth" is a subsequence of "authenticate"
    """
    qi = 0
    for char in target:
        if qi < len(query) and char == query[qi]:
            qi += 1
    return qi == len(query)


def score_symbol(sym: dict, query_lower: str, query_words: set) -> int:
    """Calculate search score for a symbol.

    Shared scoring logic used by CodeIndex.search() and search_symbols tool.
    Supports exact, substring, subsequence (fuzzy), and semantic matching.
    """
    score = 0

    name_lower = sym.get("name", "").lower()

    # 1. Exact name match (highest weight)
    if query_lower == name_lower:
        score += 20
    elif query_lower in name_lower:
        score += 10
    elif _subsequence_match(query_lower, name_lower):
        score += 4  # Fuzzy match bonus

    # 2. Name word overlap
    for word in query_words:
        if word in name_lower:
            score += 5

    # 3. Signature match
    sig_lower = sym.get("signature", "").lower()
    if query_lower in sig_lower:
        score += 8
    for word in query_words:
        if word in sig_lower:
            score += 2

    # 4. Summary match
    summary_lower = sym.get("summary", "").lower()
    if query_lower in summary_lower:
        score += 5
    for word in query_words:
        if word in summary_lower:
            score += 1

    # 5. Keyword match
    keywords = set(sym.get("keywords", []))
    matching_keywords = query_words & keywords
    score += len(matching_keywords) * 3

    # 6. Docstring match
    doc_lower = sym.get("docstring", "").lower()
    for word in query_words:
        if word in doc_lower:
            score += 1

    # 7. Semantic expansion -- check related terms only when no direct matches found
    if score == 0:
        expanded = _expand_query_semantically(query_words)
        semantic_words = expanded - query_words  # Only check new words
        for word in semantic_words:
            if word in name_lower:
                score += 3  # Weaker than direct match
            elif word in sig_lower:
                score += 1
            elif word in doc_lower:
                score += 1

    return score


@dataclass
class CodeIndex:
    """Index for a repository's source code."""
    repo: str                    # "owner/repo"
    owner: str
    name: str
    indexed_at: str              # ISO timestamp
    source_files: list[str]      # All indexed file paths
    languages: dict[str, int]    # Language -> file count
    symbols: list[dict]          # Serialized Symbol dicts (without source content)
    index_version: int = INDEX_VERSION
    file_hashes: dict[str, str] = field(default_factory=dict)  # file_path -> sha256
    git_head: str = ""           # HEAD commit hash at index time (for git repos)
    file_summaries: dict[str, str] = field(default_factory=dict)  # file_path -> summary (populated by #9)
    references: list[dict] = field(default_factory=list)  # Cross-reference data from extract_references
    repo_root: str = ""  # Absolute path to the repository root on disk (for auto-refresh)

    def __post_init__(self):
        self._symbol_index: Optional[dict[str, dict]] = None
        self._symbols_by_file: Optional[dict[str, list[dict]]] = None
        self._refs_by_file_type: Optional[dict[tuple[str, str], list[dict]]] = None
        self._symbols_by_name: Optional[dict[str, list[dict]]] = None
        self._name_token_index: Optional[dict[str, list[int]]] = None

    def _build_symbol_index(self) -> dict[str, dict]:
        """Build id -> symbol dict for O(1) lookup."""
        return {sym["id"]: sym for sym in self.symbols if "id" in sym}

    def get_symbol(self, symbol_id: str) -> Optional[dict]:
        """Find a symbol by ID (O(1) via lazy index)."""
        if self._symbol_index is None:
            self._symbol_index = self._build_symbol_index()
        return self._symbol_index.get(symbol_id)

    def _build_symbols_by_file(self) -> dict[str, list[dict]]:
        """Build file -> symbols dict for per-file lookups."""
        by_file: dict[str, list[dict]] = {}
        for sym in self.symbols:
            f = sym.get("file", "")
            by_file.setdefault(f, []).append(sym)
        return by_file

    def get_symbols_in_file(self, file_path: str) -> list[dict]:
        """Get all symbols in a file (cached)."""
        if self._symbols_by_file is None:
            self._symbols_by_file = self._build_symbols_by_file()
        return self._symbols_by_file.get(file_path, [])

    def find_containing_symbol(self, file_path: str, line: int) -> Optional[str]:
        """Find the tightest (smallest-span) symbol that contains a given line.

        Returns the symbol ID, or None if no symbol contains the line.
        Uses per-file lookup for O(symbols_in_file) instead of O(all_symbols).
        """
        best_id = None
        best_span = float("inf")

        for sym in self.get_symbols_in_file(file_path):
            sym_start = sym.get("line", 0)
            sym_end = sym.get("end_line", 0)
            if sym_start <= line <= sym_end:
                span = sym_end - sym_start
                if span < best_span:
                    best_span = span
                    best_id = sym.get("id")

        return best_id

    def _build_refs_by_file_type(self) -> dict[tuple[str, str], list[dict]]:
        """Build (file, type) -> refs dict for efficient reference lookups."""
        by_ft: dict[tuple[str, str], list[dict]] = {}
        for ref in self.references:
            key = (ref.get("file", ""), ref.get("type", ""))
            by_ft.setdefault(key, []).append(ref)
        return by_ft

    def get_refs(self, file_path: str, ref_type: str) -> list[dict]:
        """Get references for a file and type (cached)."""
        if self._refs_by_file_type is None:
            self._refs_by_file_type = self._build_refs_by_file_type()
        return self._refs_by_file_type.get((file_path, ref_type), [])

    def _build_symbols_by_name(self) -> dict[str, list[dict]]:
        """Build name -> symbols dict for O(1) name lookup."""
        by_name: dict[str, list[dict]] = {}
        for sym in self.symbols:
            name = sym.get("name", "")
            by_name.setdefault(name, []).append(sym)
        return by_name

    def get_symbols_by_name(self, name: str) -> list[dict]:
        """Get all symbols with a given name (cached)."""
        if self._symbols_by_name is None:
            self._symbols_by_name = self._build_symbols_by_name()
        return self._symbols_by_name.get(name, [])

    def _build_name_token_index(self) -> dict[str, list[int]]:
        """Map lowercase tokens from symbol names to symbol indices."""
        import re as _re
        idx: dict[str, list[int]] = {}
        for i, sym in enumerate(self.symbols):
            name = sym.get("name", "").lower()
            tokens: set[str] = set()
            for part in name.replace(".", "_").split("_"):
                if part:
                    tokens.add(part)
                    for sub in _re.findall(r'[a-z]+|[A-Z][a-z]*', part):
                        tokens.add(sub.lower())
            for token in tokens:
                idx.setdefault(token, []).append(i)
        return idx

    def _get_candidate_indices(self, query_words: set[str]) -> Optional[set[int]]:
        """Get candidate symbol indices matching any query word. Returns None for full scan fallback."""
        if self._name_token_index is None:
            self._name_token_index = self._build_name_token_index()

        candidates: set[int] = set()
        for word in query_words:
            indices = self._name_token_index.get(word, [])
            candidates.update(indices)

        return candidates if candidates else None

    def search(self, query: str, kind: Optional[str] = None, file_pattern: Optional[str] = None) -> list[dict]:
        """Search symbols with weighted scoring.

        Uses an inverted name-token index to narrow candidates before scoring,
        falling back to a full scan when the index yields no results.
        """
        query_lower = query.lower()
        query_words = set(query_lower.split())

        # Try narrowing via inverted index first
        candidates = self._get_candidate_indices(query_words)

        scored = []
        symbols_to_check = (
            [(i, self.symbols[i]) for i in candidates] if candidates
            else enumerate(self.symbols)
        )

        for _, sym in symbols_to_check:
            if kind and sym.get("kind") != kind:
                continue
            if file_pattern and not self._match_pattern(sym.get("file", ""), file_pattern):
                continue
            score = self._score_symbol(sym, query_lower, query_words)
            if score > 0:
                scored.append((score, sym))

        # Also scan non-candidate symbols for docstring/signature/summary matches
        if candidates is not None:
            for i, sym in enumerate(self.symbols):
                if i in candidates:
                    continue  # already checked
                if kind and sym.get("kind") != kind:
                    continue
                if file_pattern and not self._match_pattern(sym.get("file", ""), file_pattern):
                    continue
                score = self._score_symbol(sym, query_lower, query_words)
                if score > 0:
                    scored.append((score, sym))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [sym for _, sym in scored]

    def _match_pattern(self, file_path: str, pattern: str) -> bool:
        """Match file path against glob pattern."""
        import fnmatch
        return fnmatch.fnmatch(file_path, pattern) or fnmatch.fnmatch(file_path, f"*/{pattern}")

    def _score_symbol(self, sym: dict, query_lower: str, query_words: set) -> int:
        """Calculate search score for a symbol."""
        return score_symbol(sym, query_lower, query_words)


_INDEX_CACHE_MAX = 20
_index_cache: OrderedDict[str, tuple[float, "CodeIndex"]] = OrderedDict()
_index_cache_lock = threading.Lock()


def _cache_get(path_key: str, current_mtime: float) -> Optional["CodeIndex"]:
    """Get a cached index if mtime matches."""
    with _index_cache_lock:
        entry = _index_cache.get(path_key)
        if entry and entry[0] == current_mtime:
            _index_cache.move_to_end(path_key)
            return entry[1]
    return None


def _cache_put(path_key: str, mtime: float, index: "CodeIndex") -> None:
    """Store an index in the cache, evicting oldest if full."""
    with _index_cache_lock:
        _index_cache[path_key] = (mtime, index)
        _index_cache.move_to_end(path_key)
        while len(_index_cache) > _INDEX_CACHE_MAX:
            _index_cache.popitem(last=False)


def _cache_invalidate(path_key: str) -> None:
    """Remove an entry from the cache."""
    with _index_cache_lock:
        _index_cache.pop(path_key, None)


class IndexStore:
    """Storage for code indexes with byte-offset content retrieval."""

    def __init__(self, base_path: Optional[str] = None):
        """Initialize store.

        Args:
            base_path: Base directory for storage. Defaults to ~/.code-index/
        """
        if base_path:
            self.base_path = Path(base_path)
        else:
            self.base_path = Path.home() / ".code-index"

        self.base_path.mkdir(parents=True, exist_ok=True)

    def _safe_repo_component(self, value: str, field_name: str) -> str:
        """Validate owner/name components used in on-disk cache paths."""
        import re

        if not value or value in {".", ".."}:
            raise ValueError(f"Invalid {field_name}: {value!r}")
        if "/" in value or "\\" in value:
            raise ValueError(f"Invalid {field_name}: {value!r}")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", value):
            raise ValueError(f"Invalid {field_name}: {value!r}")
        return value

    def _repo_slug(self, owner: str, name: str) -> str:
        """Stable and safe slug used for index/content file paths."""
        safe_owner = self._safe_repo_component(owner, "owner")
        safe_name = self._safe_repo_component(name, "name")
        return f"{safe_owner}-{safe_name}"

    def _index_path(self, owner: str, name: str) -> Path:
        """Path to index JSON file."""
        return self.base_path / f"{self._repo_slug(owner, name)}.json"

    def _content_dir(self, owner: str, name: str) -> Path:
        """Path to raw content directory."""
        return self.base_path / self._repo_slug(owner, name)

    def _safe_content_path(self, content_dir: Path, relative_path: str) -> Optional[Path]:
        """Resolve a content path and ensure it stays within content_dir.

        Prevents path traversal when writing/reading cached raw files from
        untrusted repository paths.
        """
        try:
            base = content_dir.resolve()
            candidate = (content_dir / relative_path).resolve()
            if os.path.commonpath([str(base), str(candidate)]) != str(base):
                return None
            return candidate
        except (OSError, ValueError):
            return None

    def save_index(
        self,
        owner: str,
        name: str,
        source_files: list[str],
        symbols: list[Symbol],
        raw_files: dict[str, str],
        languages: dict[str, int],
        file_hashes: Optional[dict[str, str]] = None,
        git_head: str = "",
        references: Optional[list[dict]] = None,
        file_summaries: Optional[dict[str, str]] = None,
        repo_root: str = "",
    ) -> "CodeIndex":
        """Save index and raw files to storage.

        Args:
            owner: Repository owner.
            name: Repository name.
            source_files: List of indexed file paths.
            symbols: List of Symbol objects.
            raw_files: Dict mapping file path to raw content.
            languages: Dict mapping language to file count.
            file_hashes: Optional precomputed {file_path: sha256} map.
            git_head: Optional HEAD commit hash at index time.
            references: Optional list of cross-reference dicts from extract_references.
            repo_root: Absolute path to repo root on disk (for auto-refresh).

        Returns:
            CodeIndex object.
        """
        # Compute file hashes if not provided
        if file_hashes is None:
            file_hashes = {fp: _file_hash(content) for fp, content in raw_files.items()}

        # Create index
        index = CodeIndex(
            repo=f"{owner}/{name}",
            owner=owner,
            name=name,
            indexed_at=datetime.now().isoformat(),
            source_files=source_files,
            languages=languages,
            symbols=[self._symbol_to_dict(s) for s in symbols],
            index_version=INDEX_VERSION,
            file_hashes=file_hashes,
            git_head=git_head,
            file_summaries=file_summaries or {},
            references=references or [],
            repo_root=repo_root,
        )

        # Save index JSON atomically: write to temp then rename
        index_path = self._index_path(owner, name)
        tmp_path = index_path.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self._index_to_dict(index), f, indent=2)
        # Atomic rename (on POSIX; best-effort on Windows)
        tmp_path.replace(index_path)
        _cache_invalidate(str(index_path))

        # Save symbol history snapshots
        self.save_history(owner, name, index)

        # Save raw files
        content_dir = self._content_dir(owner, name)
        content_dir.mkdir(parents=True, exist_ok=True)

        for file_path, content in raw_files.items():
            file_dest = self._safe_content_path(content_dir, file_path)
            if not file_dest:
                raise ValueError(f"Unsafe file path in raw_files: {file_path}")
            file_dest.parent.mkdir(parents=True, exist_ok=True)
            with open(file_dest, "wb") as f:
                f.write(content.encode("utf-8"))

        return index

    def load_index(self, owner: str, name: str) -> Optional[CodeIndex]:
        """Load index from storage. Rejects incompatible versions.

        Uses an in-memory cache keyed by file path and mtime to avoid
        re-reading and re-parsing the JSON on every tool call.
        """
        index_path = self._index_path(owner, name)

        if not index_path.exists():
            return None

        path_key = str(index_path)
        current_mtime = index_path.stat().st_mtime

        cached = _cache_get(path_key, current_mtime)
        if cached is not None:
            return cached

        try:
            with open(index_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return None  # Corrupted or unreadable index file

        # Version check
        stored_version = data.get("index_version", 1)
        if stored_version > INDEX_VERSION:
            return None  # Future version we can't read

        try:
            index = CodeIndex(
                repo=data["repo"],
                owner=data["owner"],
                name=data["name"],
                indexed_at=data["indexed_at"],
                source_files=data["source_files"],
                languages=data["languages"],
                symbols=data["symbols"],
                index_version=stored_version,
                file_hashes=data.get("file_hashes", {}),
                git_head=data.get("git_head", ""),
                file_summaries=data.get("file_summaries", {}),
                references=data.get("references", []),
                repo_root=data.get("repo_root", ""),
            )
        except KeyError:
            return None  # Malformed index data

        _cache_put(path_key, current_mtime, index)
        return index

    def get_symbol_content(self, owner: str, name: str, symbol_id: str) -> Optional[str]:
        """Read symbol source using stored byte offsets.

        This is O(1) - no re-parsing, just seek + read.
        """
        index = self.load_index(owner, name)
        if not index:
            return None

        symbol = index.get_symbol(symbol_id)
        if not symbol:
            return None

        file_path = self._safe_content_path(self._content_dir(owner, name), symbol["file"])
        if not file_path:
            return None

        if not file_path.exists():
            return None

        with open(file_path, "rb") as f:
            f.seek(symbol["byte_offset"])
            source_bytes = f.read(symbol["byte_length"])

        return source_bytes.decode("utf-8", errors="replace")

    _refresh_lock = threading.Lock()

    def refresh_file(self, owner: str, name: str, file_path: str, repo_root: str) -> bool:
        """Re-parse a single file if it has changed since indexing.

        Compares the file's current content hash against the stored hash.
        If different, re-parses the file, updates symbols and references
        in the in-memory index (and cache), and updates the raw content file.
        Does NOT write the full index JSON to disk (too expensive per-file).

        Args:
            owner: Repository owner.
            name: Repository name.
            file_path: Relative file path within the repo (e.g., 'src/main.py').
            repo_root: Absolute path to the repository root on disk.

        Returns:
            True if the file was refreshed, False if unchanged or not found.
        """
        from ..parser.extractor import parse_file
        from ..parser.references import extract_references
        from ..parser.languages import LANGUAGE_EXTENSIONS

        index = self.load_index(owner, name)
        if not index:
            return False

        # Store ref so callers sharing this cached object see mutations
        # (load_index returns cached objects, so mutations propagate)

        # Read the current file from disk
        abs_path = Path(repo_root) / file_path
        if not abs_path.is_file():
            return False

        try:
            content = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False

        # Compare hashes
        current_hash = _file_hash(content)
        stored_hash = index.file_hashes.get(file_path)
        if stored_hash == current_hash:
            return False

        # Detect language from extension
        ext = os.path.splitext(file_path)[1]
        language = LANGUAGE_EXTENSIONS.get(ext)
        if not language:
            return False

        # Parse new symbols and references
        try:
            new_symbols = parse_file(content, file_path, language)
            new_refs = extract_references(content, file_path, language)
        except Exception:
            return False

        # Tag each reference with its file
        for ref in new_refs:
            ref["file"] = file_path

        with self._refresh_lock:
            # Remove old symbols and refs for this file
            index.symbols = [s for s in index.symbols if s.get("file") != file_path]
            index.references = [r for r in index.references if r.get("file") != file_path]

            # Add new symbols (converted to dicts) and refs
            index.symbols.extend(self._symbol_to_dict(s) for s in new_symbols)
            index.references.extend(new_refs)

            # Update file hash
            index.file_hashes[file_path] = current_hash

            # Add file to source_files if not already present
            if file_path not in index.source_files:
                index.source_files.append(file_path)

            # Invalidate lazy caches
            index._symbol_index = None
            index._symbols_by_file = None
            index._refs_by_file_type = None
            index._symbols_by_name = None
            index._name_token_index = None

        # Update the raw content file on disk
        content_dir = self._content_dir(owner, name)
        dest = self._safe_content_path(content_dir, file_path)
        if dest:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as f:
                f.write(content.encode("utf-8"))

        # Update the in-memory cache (use current index_path mtime as key)
        index_path = self._index_path(owner, name)
        if index_path.exists():
            path_key = str(index_path)
            mtime = index_path.stat().st_mtime
            _cache_put(path_key, mtime, index)

        return True

    def detect_changes(
        self,
        owner: str,
        name: str,
        current_files: dict[str, str],
    ) -> tuple[list[str], list[str], list[str]]:
        """Detect changed, new, and deleted files by comparing hashes.

        Args:
            owner: Repository owner.
            name: Repository name.
            current_files: Dict mapping file_path -> content for current state.

        Returns:
            Tuple of (changed_files, new_files, deleted_files).
        """
        index = self.load_index(owner, name)
        if not index:
            # No existing index: all files are new
            return [], list(current_files.keys()), []

        old_hashes = index.file_hashes
        current_hashes = {fp: _file_hash(content) for fp, content in current_files.items()}

        old_set = set(old_hashes.keys())
        new_set = set(current_hashes.keys())

        new_files = list(new_set - old_set)
        deleted_files = list(old_set - new_set)
        changed_files = [
            fp for fp in (old_set & new_set)
            if old_hashes[fp] != current_hashes[fp]
        ]

        return changed_files, new_files, deleted_files

    def incremental_save(
        self,
        owner: str,
        name: str,
        changed_files: list[str],
        new_files: list[str],
        deleted_files: list[str],
        new_symbols: list[Symbol],
        raw_files: dict[str, str],
        languages: dict[str, int],
        git_head: str = "",
        new_references: Optional[list[dict]] = None,
        new_file_summaries: Optional[dict[str, str]] = None,
    ) -> Optional[CodeIndex]:
        """Incrementally update an existing index.

        Removes symbols for deleted/changed files, adds new symbols,
        updates raw content, and saves atomically.

        Args:
            owner: Repository owner.
            name: Repository name.
            changed_files: Files that changed (symbols will be replaced).
            new_files: New files (symbols will be added).
            deleted_files: Deleted files (symbols will be removed).
            new_symbols: Symbols extracted from changed + new files.
            raw_files: Raw content for changed + new files.
            languages: Updated language counts.
            git_head: Current HEAD commit hash.

        Returns:
            Updated CodeIndex, or None if no existing index.
        """
        index = self.load_index(owner, name)
        if not index:
            return None

        # Remove symbols for deleted and changed files
        files_to_remove = set(deleted_files) | set(changed_files)
        kept_symbols = [s for s in index.symbols if s.get("file") not in files_to_remove]

        # Add new symbols
        all_symbols_dicts = kept_symbols + [self._symbol_to_dict(s) for s in new_symbols]

        # Update source files list
        old_files = set(index.source_files)
        for f in deleted_files:
            old_files.discard(f)
        for f in new_files:
            old_files.add(f)
        for f in changed_files:
            old_files.add(f)

        # Update file hashes
        file_hashes = dict(index.file_hashes)
        for f in deleted_files:
            file_hashes.pop(f, None)
        for fp, content in raw_files.items():
            file_hashes[fp] = _file_hash(content)

        # Merge references: keep refs from unchanged files, add new refs
        kept_refs = [r for r in index.references if r.get("file") not in files_to_remove]
        all_refs = kept_refs + (new_references or [])

        # Preserve file_summaries: keep summaries for unchanged files, drop deleted ones
        kept_summaries = {
            fp: summary
            for fp, summary in index.file_summaries.items()
            if fp not in files_to_remove
        }
        # Merge in new summaries for changed/new files
        if new_file_summaries:
            kept_summaries.update(new_file_summaries)

        # Build updated index
        updated = CodeIndex(
            repo=f"{owner}/{name}",
            owner=owner,
            name=name,
            indexed_at=datetime.now().isoformat(),
            source_files=sorted(old_files),
            languages=languages,
            symbols=all_symbols_dicts,
            index_version=INDEX_VERSION,
            file_hashes=file_hashes,
            git_head=git_head,
            file_summaries=kept_summaries,
            references=all_refs,
            repo_root=index.repo_root,
        )

        # Save atomically
        index_path = self._index_path(owner, name)
        tmp_path = index_path.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self._index_to_dict(updated), f, indent=2)
        tmp_path.replace(index_path)
        _cache_invalidate(str(index_path))

        # Save symbol history snapshots
        self.save_history(owner, name, updated)

        # Update raw files
        content_dir = self._content_dir(owner, name)
        content_dir.mkdir(parents=True, exist_ok=True)

        # Remove deleted files from content dir
        for fp in deleted_files:
            dead = self._safe_content_path(content_dir, fp)
            if not dead:
                continue
            if dead.exists():
                dead.unlink()

        # Write changed + new files
        for fp, content in raw_files.items():
            dest = self._safe_content_path(content_dir, fp)
            if not dest:
                raise ValueError(f"Unsafe file path in raw_files: {fp}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as f:
                f.write(content.encode("utf-8"))

        return updated

    def detect_changes_git(
        self,
        owner: str,
        name: str,
        repo_path: Path,
        current_head: str,
    ) -> tuple[list[str], list[str], list[str]] | None:
        """Detect changes using git diff when possible.

        Returns (changed, new, deleted) file lists, or None if git diff isn't usable.
        """
        from ..parser.languages import LANGUAGE_EXTENSIONS

        index = self.load_index(owner, name)
        if not index:
            return None

        stored_head = index.git_head
        if not stored_head or not current_head:
            return None

        if stored_head == current_head:
            return [], [], []

        try:
            result = subprocess.run(
                ["git", "diff", "--name-status", f"{stored_head}..{current_head}"],
                cwd=str(repo_path),
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                return None
        except Exception:
            return None

        supported_exts = set(LANGUAGE_EXTENSIONS.keys())
        changed = []
        new = []
        deleted = []

        for line in result.stdout.strip().splitlines():
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            status = parts[0]
            file_path = parts[1]

            # Renames and copies have two paths -- handle before the extension filter
            if status.startswith("R") or status.startswith("C"):
                old_path = parts[1]
                new_path = parts[2] if len(parts) > 2 else parts[1]
                old_ext = os.path.splitext(old_path)[1]
                new_ext = os.path.splitext(new_path)[1]
                if old_ext in supported_exts:
                    deleted.append(old_path)
                if new_ext in supported_exts:
                    new.append(new_path)
                continue

            ext = os.path.splitext(file_path)[1]
            if ext not in supported_exts:
                continue

            if status == "A":
                new.append(file_path)
            elif status == "D":
                deleted.append(file_path)
            elif status == "M":
                changed.append(file_path)

        return changed, new, deleted

    def list_repos(self) -> list[dict]:
        """List all indexed repositories."""
        repos = []

        for index_file in self.base_path.glob("*.json"):
            # Skip non-index JSON files (history snapshots, token savings)
            if index_file.name.endswith(".history.json") or index_file.name == "_savings.json":
                continue
            try:
                with open(index_file, "r", encoding="utf-8") as f:
                    data = json.load(f)

                repos.append({
                    "repo": data["repo"],
                    "indexed_at": data["indexed_at"],
                    "symbol_count": len(data["symbols"]),
                    "file_count": len(data["source_files"]),
                    "languages": data["languages"],
                    "index_version": data.get("index_version", 1),
                })
            except Exception:
                continue

        return repos

    def delete_index(self, owner: str, name: str) -> bool:
        """Delete an index and its raw files."""
        index_path = self._index_path(owner, name)
        content_dir = self._content_dir(owner, name)
        _cache_invalidate(str(index_path))

        deleted = False

        if index_path.exists():
            index_path.unlink()
            deleted = True

        if content_dir.exists():
            shutil.rmtree(content_dir)
            deleted = True

        return deleted

    def _symbol_to_dict(self, symbol: Symbol) -> dict:
        """Convert Symbol to dict (without source content)."""
        return {
            "id": symbol.id,
            "file": symbol.file,
            "name": symbol.name,
            "qualified_name": symbol.qualified_name,
            "kind": symbol.kind,
            "language": symbol.language,
            "signature": symbol.signature,
            "docstring": symbol.docstring,
            "summary": symbol.summary,
            "decorators": symbol.decorators,
            "keywords": symbol.keywords,
            "parent": symbol.parent,
            "line": symbol.line,
            "end_line": symbol.end_line,
            "byte_offset": symbol.byte_offset,
            "byte_length": symbol.byte_length,
            "content_hash": symbol.content_hash,
        }

    def _history_path(self, owner: str, name: str) -> Path:
        """Path to symbol history JSON file."""
        return self.base_path / f"{self._repo_slug(owner, name)}.history.json"

    def save_history(self, owner: str, name: str, index: "CodeIndex") -> None:
        """Append symbol snapshots to the history file.

        Each symbol gets an entry with timestamp, content_hash, and signature.
        Only appends entries for symbols whose content_hash changed since the
        last recorded entry (or that are new to the history).
        """
        history_path = self._history_path(owner, name)

        # Use file locking around the read-modify-write cycle
        lock_path = history_path.with_suffix(".lock")
        lock_path.touch(exist_ok=True)
        # msvcrt.locking() requires at least one byte to lock; an empty file
        # (the common case right after touch()) fails with an OSError on Windows.
        if lock_path.stat().st_size == 0:
            lock_path.write_bytes(b"\0")
        with open(lock_path, "r+b") as lock_fd:
            _lock_file_exclusive(lock_fd)
            try:
                # Load existing history
                history: dict[str, list[dict]] = {}
                if history_path.exists():
                    try:
                        with open(history_path, "r", encoding="utf-8") as f:
                            history = json.load(f)
                    except (json.JSONDecodeError, OSError):
                        history = {}

                timestamp = index.indexed_at

                for sym in index.symbols:
                    sym_id = sym.get("id", "")
                    if not sym_id:
                        continue

                    entry = {
                        "timestamp": timestamp,
                        "symbol_id": sym_id,
                        "content_hash": sym.get("content_hash", ""),
                        "signature": sym.get("signature", ""),
                    }

                    entries = history.get(sym_id, [])
                    # Only append if content_hash changed from last entry (or first entry)
                    if not entries or entries[-1].get("content_hash") != entry["content_hash"]:
                        entries.append(entry)
                        history[sym_id] = entries

                # Write atomically
                tmp_path = history_path.with_suffix(".json.tmp")
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(history, f, indent=2)
                tmp_path.replace(history_path)
            finally:
                _unlock_file(lock_fd)

    def load_history(self, owner: str, name: str) -> dict[str, list[dict]]:
        """Load the symbol history file. Returns {symbol_id: [entries...]}."""
        history_path = self._history_path(owner, name)
        if not history_path.exists():
            return {}
        try:
            with open(history_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _index_to_dict(self, index: CodeIndex) -> dict:
        """Convert CodeIndex to dict."""
        return {
            "repo": index.repo,
            "owner": index.owner,
            "name": index.name,
            "indexed_at": index.indexed_at,
            "source_files": index.source_files,
            "languages": index.languages,
            "symbols": index.symbols,
            "index_version": index.index_version,
            "file_hashes": index.file_hashes,
            "git_head": index.git_head,
            "file_summaries": index.file_summaries,
            "references": index.references,
            "repo_root": index.repo_root,
        }
