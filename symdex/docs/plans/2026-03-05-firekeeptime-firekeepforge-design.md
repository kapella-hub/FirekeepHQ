# FirekeepTime + FirekeepForge Design

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add two feature suites to FirekeepSymdex — FirekeepTime (code evolution timeline, fully automated) and FirekeepForge (pattern-aware code scaffolding, AI-assisted).

**Architecture:** Each tool is a standalone Python module in `src/firekeep_symdex/tools/`. All tools follow the existing pattern: function that takes repo + params, returns dict with `_meta` envelope. New tools are registered in `server.py`.

**Tech Stack:** Python 3.10+, tree-sitter (existing), git CLI for history, no new dependencies.

---

## FirekeepTime — Code Evolution Timeline

Fully automated tools that analyze git history + AST data to reveal how code evolves over time.

### Tool 1: `get_evolution_timeline`

**Files:**
- Create: `src/firekeep_symdex/tools/get_evolution_timeline.py`
- Modify: `src/firekeep_symdex/server.py` (register tool)
- Test: `tests/test_firekeeptime.py`

**What it does:** For a given symbol or file, returns a timeline of changes from git history — when it was created, modified, and by whom. Combines `git log` data with symbol-level tracking.

**Parameters:**
- `repo` (str): Repository identifier
- `symbol_id` (str, optional): Specific symbol to track
- `file_path` (str, optional): File to track (if no symbol_id)
- `max_entries` (int, default 20): Max timeline entries

**Implementation:**
1. Resolve repo to local path (required — git history only works locally)
2. If `symbol_id`: get symbol's file and line range, run `git log -p` on that file, filter commits that touch the symbol's byte range
3. If `file_path`: run `git log --follow` on the file
4. Parse git log output: extract commit hash, author, date, message
5. Return timeline entries with commit metadata

**Returns:**
```python
{
    "target": symbol_id or file_path,
    "timeline": [
        {"commit": "abc123", "author": "name", "date": "ISO", "message": "...", "change_type": "modified"},
    ],
    "first_seen": "ISO date",
    "last_modified": "ISO date",
    "total_changes": N,
    "_meta": {"timing_ms": ...}
}
```

---

### Tool 2: `get_complexity_metrics`

**Files:**
- Create: `src/firekeep_symdex/tools/get_complexity_metrics.py`
- Modify: `src/firekeep_symdex/server.py` (register tool)
- Test: `tests/test_firekeeptime.py`

**What it does:** Computes complexity metrics for symbols using AST data — line count, nesting depth, parameter count, cyclomatic complexity approximation.

**Parameters:**
- `repo` (str): Repository identifier
- `symbol_id` (str, optional): Specific symbol (returns detailed metrics)
- `file_path` (str, optional): All symbols in a file
- `kind` (str, optional): Filter by symbol kind
- `sort_by` (str, default "complexity"): Sort field
- `max_results` (int, default 20): Max results

**Implementation:**
1. Load index, get symbol(s)
2. For each symbol, retrieve source content via byte-offset
3. Compute metrics:
   - `lines`: line count from source
   - `nesting_depth`: max indentation level / brace depth from source
   - `param_count`: from signature parsing (reuse `_extract_params` from get_similar_symbols)
   - `complexity_score`: approximation = branches (if/else/for/while/try/catch/switch/case/match/&&/||) + 1
   - `byte_length`: already stored
4. Return ranked list

**Returns:**
```python
{
    "results": [
        {"symbol_id": "...", "name": "...", "lines": 45, "nesting_depth": 4,
         "param_count": 3, "complexity_score": 12, "byte_length": 1200,
         "risk_level": "high"},  # high/medium/low based on score
    ],
    "_meta": {"timing_ms": ...}
}
```

---

### Tool 3: `get_contributors`

**Files:**
- Create: `src/firekeep_symdex/tools/get_contributors.py`
- Modify: `src/firekeep_symdex/server.py` (register tool)
- Test: `tests/test_firekeeptime.py`

**What it does:** Maps contributors to symbols/files using `git blame`. Shows who "owns" each area of the codebase.

**Parameters:**
- `repo` (str): Repository identifier
- `file_path` (str, optional): Specific file
- `symbol_id` (str, optional): Specific symbol
- `max_results` (int, default 20): Max results

**Implementation:**
1. Resolve repo to local path
2. Run `git blame --porcelain` on the file
3. Parse blame output: map line ranges → authors
4. If `symbol_id`: filter to symbol's line range
5. Aggregate: count lines per author, compute ownership percentages
6. If neither file nor symbol: aggregate across all indexed files (top contributors)

**Returns:**
```python
{
    "target": file_path or symbol_id,
    "contributors": [
        {"author": "name", "email": "...", "lines": 45, "percentage": 62.5,
         "last_commit": "ISO date"},
    ],
    "total_lines": 72,
    "_meta": {"timing_ms": ...}
}
```

---

### Tool 4: `get_code_churn`

**Files:**
- Create: `src/firekeep_symdex/tools/get_code_churn.py`
- Modify: `src/firekeep_symdex/server.py` (register tool)
- Test: `tests/test_firekeeptime.py`

**What it does:** Identifies files and symbols with the highest change frequency (churn). High churn + high complexity = technical debt hotspot.

**Parameters:**
- `repo` (str): Repository identifier
- `since` (str, optional): Date filter (e.g., "2025-01-01" or "3 months ago")
- `max_results` (int, default 20): Max results

**Implementation:**
1. Resolve repo to local path
2. Run `git log --numstat --since=<since>` to get per-file change counts
3. For each indexed file: count commits, total lines added/removed
4. Cross-reference with `get_hotspots` caller data for risk scoring
5. Churn score = commits × (lines_added + lines_removed)
6. Risk score = churn × complexity (if available from metrics)

**Returns:**
```python
{
    "results": [
        {"file": "...", "commits": 15, "lines_added": 200, "lines_removed": 150,
         "churn_score": 5250, "risk_level": "high"},
    ],
    "period": {"since": "...", "until": "now"},
    "_meta": {"timing_ms": ...}
}
```

---

## FirekeepForge — Pattern-Aware Code Scaffolding

AI-assisted tools that learn conventions from existing code and generate matching scaffolds.

### Tool 5: `extract_conventions`

**Files:**
- Create: `src/firekeep_symdex/tools/extract_conventions.py`
- Modify: `src/firekeep_symdex/server.py` (register tool)
- Test: `tests/test_firekeepforge.py`

**What it does:** Analyzes the codebase and extracts naming conventions, file organization patterns, common patterns (error handling, logging, decorators), and framework idioms. Fully automated — no AI needed.

**Parameters:**
- `repo` (str): Repository identifier
- `focus` (str, optional): Focus area — "naming", "structure", "patterns", "all" (default "all")

**Implementation:**
1. **Naming conventions:** Analyze symbol names
   - Functions: detect snake_case vs camelCase ratio
   - Classes: detect PascalCase vs other
   - Constants: detect UPPER_CASE ratio
   - Files: detect kebab-case vs snake_case vs camelCase
2. **Structure conventions:** Analyze file organization
   - Average symbols per file
   - Common directory patterns (src/, lib/, utils/)
   - Test file naming (test_*.py vs *.test.ts vs *_test.go)
   - Import grouping patterns
3. **Code patterns:** Analyze common patterns
   - Most-used decorators/attributes
   - Error handling style (try/catch frequency, custom exceptions)
   - Common parameter patterns (which params appear most)
   - Return type patterns
4. **Framework detection:** From route symbols + import patterns
   - Detect Express, Flask, FastAPI, Django, etc.
   - Middleware patterns
   - ORM usage

**Returns:**
```python
{
    "naming": {"functions": "snake_case (95%)", "classes": "PascalCase (100%)", ...},
    "structure": {"avg_symbols_per_file": 8.5, "test_pattern": "test_*.py", ...},
    "patterns": {"top_decorators": [...], "error_handling": "try/except with custom exceptions", ...},
    "framework": {"detected": "FastAPI", "patterns": [...]},
    "_meta": {"timing_ms": ...}
}
```

---

### Tool 6: `detect_patterns`

**Files:**
- Create: `src/firekeep_symdex/tools/detect_patterns.py`
- Modify: `src/firekeep_symdex/server.py` (register tool)
- Test: `tests/test_firekeepforge.py`

**What it does:** Finds recurring structural patterns in the codebase — groups of symbols that follow the same template (e.g., all API endpoints follow the same structure, all service classes have the same methods).

**Parameters:**
- `repo` (str): Repository identifier
- `kind` (str, optional): Filter by symbol kind
- `min_group_size` (int, default 3): Minimum symbols to form a pattern
- `max_results` (int, default 10): Max pattern groups

**Implementation:**
1. Group symbols by kind
2. For each group, extract structural fingerprints:
   - Parameter count + types
   - Return type
   - Decorator set
   - Body structure (sequence of statement types from AST)
3. Cluster symbols with similar fingerprints
4. For each cluster >= min_group_size, extract the common template
5. Return pattern groups with example symbols

**Returns:**
```python
{
    "patterns": [
        {
            "pattern_name": "API endpoint handler",
            "description": "Functions with (request) param, returning Response",
            "symbol_count": 12,
            "common_traits": {"param_pattern": "(request: Request)", "return_type": "Response", ...},
            "examples": [{"symbol_id": "...", "name": "get_users"}, ...],
        },
    ],
    "_meta": {"timing_ms": ...}
}
```

---

### Tool 7: `scaffold_symbol`

**Files:**
- Create: `src/firekeep_symdex/tools/scaffold_symbol.py`
- Modify: `src/firekeep_symdex/server.py` (register tool)
- Test: `tests/test_firekeepforge.py`

**What it does:** Generates a code scaffold for a new symbol that matches existing codebase conventions. Uses AI (same provider system as summarizer) for the generation step, with a template-based fallback when no AI is available.

**Parameters:**
- `repo` (str): Repository identifier
- `intent` (str): What the new symbol should do (e.g., "API endpoint for user deletion")
- `kind` (str, optional): Symbol kind to generate (function, class, method)
- `target_file` (str, optional): Where it should go (helps match conventions)
- `like` (str, optional): Symbol ID to use as template

**Implementation:**
1. If `like`: get that symbol's full source as template
2. Else: use `suggest_symbols` to find most similar existing symbol
3. Run `extract_conventions` for naming/pattern context
4. If AI available (reuse summarizer provider detection):
   - Build prompt: conventions + template source + intent
   - Generate scaffold via LLM (max ~500 tokens)
5. If no AI:
   - Template-based: copy structure from best-match symbol
   - Replace names based on intent keywords
   - Add TODO comments for body implementation
6. Return generated code + metadata about what conventions were applied

**Returns:**
```python
{
    "scaffold": "def delete_user(request: Request) -> Response:\n    ...",
    "target_file": "routes/users.py",
    "based_on": {"symbol_id": "...", "similarity": 85.2},
    "conventions_applied": ["snake_case naming", "Request/Response pattern", "@require_auth decorator"],
    "ai_generated": true,
    "_meta": {"timing_ms": ...}
}
```

---

## Implementation Strategy

**FirekeepTime tools (1-4)** are independent of each other and independent of FirekeepForge.
**FirekeepForge tools (5-6)** are independent. Tool 7 depends on tools 5 and 6.

**Parallel batch 1:** Tasks 1-6 (all independent)
**Sequential:** Task 7 (scaffold_symbol, depends on 5+6)
**Sequential:** Task 8 (server.py registration, tests, README update, commit+push)
