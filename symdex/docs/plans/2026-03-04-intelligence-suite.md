# Intelligence Suite Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add 6 new tools + 1 upgrade that give firekeep-symdex architecture intelligence, impact analysis, and smart context capabilities — making it unlike any other code intelligence MCP server.

**Architecture:** Each tool is a standalone Python module in `src/firekeep_symdex/tools/`. Tools build on existing `CodeIndex` data (symbols, references) plus new graph traversal logic. New tools are registered in `server.py`. All tools follow the existing pattern: function that takes repo + params, returns dict with `_meta` envelope.

**Tech Stack:** Python 3.10+, tree-sitter (existing), no new dependencies.

---

## Task 1: `find_dead_code`

**Files:**
- Create: `src/firekeep_symdex/tools/find_dead_code.py`
- Modify: `src/firekeep_symdex/server.py` (register tool)
- Test: `tests/test_intelligence.py`

**Implementation:** Iterate all symbols, check if any reference calls them. Symbols with zero callers (excluding entry points like `main`, `__init__`, exports) are dead.

---

## Task 2: `get_import_graph`

**Files:**
- Create: `src/firekeep_symdex/tools/get_import_graph.py`
- Modify: `src/firekeep_symdex/server.py` (register tool)
- Test: `tests/test_intelligence.py`

**Implementation:** Build file-to-file dependency graph from stored import references. For each file, find its imports, resolve them to files in the index. Output adjacency list + optional DOT format for visualization.

---

## Task 3: `get_impact`

**Files:**
- Create: `src/firekeep_symdex/tools/get_impact.py`
- Modify: `src/firekeep_symdex/server.py` (register tool)
- Test: `tests/test_intelligence.py`

**Implementation:** BFS/DFS from a symbol through the caller graph. For each caller, find ITS callers, recursively. Return the transitive dependency tree with depth info. Cap at configurable max_depth to prevent explosion.

---

## Task 4: `get_change_summary`

**Files:**
- Create: `src/firekeep_symdex/tools/get_change_summary.py`
- Modify: `src/firekeep_symdex/server.py` (register tool)
- Test: `tests/test_intelligence.py`

**Implementation:** Compare current files against stored index. For changed files, diff the symbol lists (added/modified/removed symbols). Uses existing `detect_changes` + re-parse of changed files.

---

## Task 5: `get_architecture_map`

**Files:**
- Create: `src/firekeep_symdex/tools/get_architecture_map.py`
- Modify: `src/firekeep_symdex/server.py` (register tool)
- Test: `tests/test_intelligence.py`

**Implementation:** Classify files into layers based on path patterns and import graph position. Entry points (few importers, many exports) are "API/routes". Leaves (many imports, few importers) are "utilities". Middle nodes are "services/business logic". Output layered architecture summary.

---

## Task 6: `get_context` upgrade + file summaries

**Files:**
- Modify: `src/firekeep_symdex/tools/get_context.py` (add dependency inclusion)
- Modify: `src/firekeep_symdex/tools/index_repo.py` (generate file summaries)
- Modify: `src/firekeep_symdex/tools/index_folder.py` (generate file summaries)
- Test: `tests/test_intelligence.py`

**Implementation:** When retrieving context for a focused symbol, also include its imports and direct callees within budget. File summaries: at index time, generate a one-line summary per file from its symbols (no LLM needed — just "N functions, M classes: name1, name2, ...").

---

## Task 7: README.md + Final validation

**Files:**
- Create: `README.md`
- Run: Full test suite

**Implementation:** Comprehensive README covering installation, all tools with examples, architecture overview.

---

## Execution Strategy

Tasks 1-5 are independent and can be parallelized. Task 6 depends on reference data patterns from 1-2. Task 7 is final.

**Parallel batch 1:** Tasks 1, 2, 3, 4, 5 (all independent tool implementations)
**Sequential:** Task 6 (context upgrade)
**Sequential:** Task 7 (README + validation + commit + push)
