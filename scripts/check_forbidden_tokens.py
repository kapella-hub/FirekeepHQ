#!/usr/bin/env python3
"""Fail the build if material that must never ship reappears in the tree.

Firekeep was seeded from a private predecessor codebase (NexusStack) that was
entangled with an employer's build estate and the author's personal
infrastructure. The seed was scrubbed once and verified clean. This guard exists
because the *initial* scrub is the easy part: code gets copied back from the
archive for months afterwards, and reintroduction is the likely failure mode,
not the original miss.

Design notes, both learned by getting them wrong during the seed:

- A guard that cannot fail is worse than no guard, because it manufactures
  confidence. The first version of this check was a shell loop that set its
  failure flag inside a `$(...)` subshell, so the flag never propagated and it
  printed PASS while a residual token was sitting in the tree. There is a test
  (tests/test_forbidden_tokens.py) that plants each token in a temp file and
  asserts this scanner actually reports it.

- Allowlists must be narrow and justified. `.example` hostnames are RFC 2606
  documentation placeholders and are legitimate; the old product name is not.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Substring, case-insensitive. Keep the reason with the token — a future reader
# deleting an entry should have to argue with the reason, not guess at it.
FORBIDDEN: dict[str, str] = {
    "nexusstack": "predecessor product name; this is Firekeep",
    "nexuscortex": "predecessor component name",
    "charterlab": "employer-internal domain",
    "artifactory.": "employer-internal registry host",
    "apodid": "employer-internal group path",
    "31.97.212.55": "author's personal VPS address",
    "srv1143982": "author's personal VPS hostname",
}

# Paths never scanned. Deliberately short.
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules",
             ".venv", "venv", ".superpowers", ".mypy_cache"}


def _is_skipped_path(path: Path) -> bool:
    """Ignore caches and nested agent worktrees, not tracked `.claude` guidance."""
    parts = path.parts
    return (
        any(part in SKIP_DIRS for part in parts)
        or any(parts[i:i + 2] == (".claude", "worktrees") for i in range(len(parts) - 1))
    )

# This file names every forbidden token by definition, as does its test.
SKIP_FILES = {
    "scripts/check_forbidden_tokens.py",
    "tests/test_forbidden_tokens.py",
}

MAX_BYTES = 2_000_000  # skip anything larger; nothing textual here is bigger


def iter_files(root: Path):
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if _is_skipped_path(p.relative_to(root)):
            continue
        rel = p.relative_to(root).as_posix()
        if rel in SKIP_FILES:
            continue
        try:
            if p.stat().st_size > MAX_BYTES:
                continue
        except OSError:
            continue
        yield p, rel


def scan(root: Path) -> list[tuple[str, int, str, str]]:
    """Return [(relpath, lineno, token, reason)] for every hit."""
    hits: list[tuple[str, int, str, str]] = []
    for path, rel in iter_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, ValueError, OSError):
            continue  # binary or unreadable — nothing to match
        lowered = text.lower()
        if not any(tok in lowered for tok in FORBIDDEN):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            low = line.lower()
            for tok, reason in FORBIDDEN.items():
                if tok in low:
                    hits.append((rel, lineno, tok, reason))
    return hits


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    hits = scan(root)

    if not hits:
        print(f"forbidden-token scan: clean ({len(FORBIDDEN)} tokens checked)")
        return 0

    print(f"forbidden-token scan: {len(hits)} occurrence(s) FOUND\n", file=sys.stderr)
    for rel, lineno, tok, reason in hits:
        print(f"  {rel}:{lineno}  '{tok}'  — {reason}", file=sys.stderr)
    print(
        "\nThis material must not ship. Remove it, or if the match is a genuine "
        "false positive, narrow the token in scripts/check_forbidden_tokens.py "
        "and say why in the same commit.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
