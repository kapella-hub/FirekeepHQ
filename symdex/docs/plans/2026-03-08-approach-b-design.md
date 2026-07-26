# Approach B: Targeted Refactor + Fixes

**Date**: 2026-03-08
**Status**: Approved

## Overview

Four improvement tracks for FirekeepSymdex: declarative tool registry, performance fixes, call reference enrichment, and CI/coverage setup.

## 1. Declarative Tool Registry

**Problem**: server.py is 1,202 lines — ~600 lines of Tool schema + ~350 lines of if/elif dispatch. Every new tool requires editing two places.

**Solution**: Each tool module exports a `TOOL_DEF` dict with schema and handler. `server.py` auto-discovers them via `tools/__init__.py`.

- `TOOL_DEF` keys: `name`, `description`, `inputSchema`, `handler`, optional `is_async` flag
- Special cases (`get_symbol`/`get_symbols` with `include_imports`) handle post-processing internally
- `_get_file_imports` helper moves to `_utils.py`
- Migration: one tool at a time, each gets TOOL_DEF, corresponding elif deleted
- server.py shrinks to ~80 lines

## 2. Performance Fixes

### 2a. _find_spine() exponential memory
Replace `path + [neighbor]` copy pattern with mutable push/pop DFS. Memory: O(branches^depth) → O(depth).

### 2b. resolve_call_targets() linear scan
Add `CodeIndex.get_symbols_by_name(name)` lazy-cached lookup. `resolve_call_targets()` uses this instead of scanning all symbols.

### 2c. Search scoring inverted index
Add name-token inverted index on CodeIndex for fast candidate narrowing. Falls back to full scan for semantic/fuzzy with no token hits.

## 3. from_symbol on Call References

Populate `from_symbol` field on call refs during AST walk by tracking enclosing symbol context. Enables `get_callers`/`get_dependencies` to report caller function, not just file.

Changes to `references.py`:
- `_walk_for_references` gets `filename` and `enclosing_symbol` params
- When entering function/class nodes, compute symbol ID and pass down
- Call refs get `"from_symbol": enclosing_id`

## 4. Coverage + CI

- Add `[tool.coverage.run]` and `[tool.coverage.report]` to pyproject.toml
- Add `.github/workflows/ci.yml` with Python 3.10/3.11/3.12 matrix
- Steps: uv sync, pytest --cov, coverage report
- fail_under = 75
