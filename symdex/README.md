# firekeep-symdex

Token-efficient MCP server for source code exploration via tree-sitter AST parsing.

Instead of dumping entire files into context, firekeep-symdex parses your codebase into symbols (functions, classes, methods, types, constants, routes, variables) and serves only what you need.

**On a single targeted lookup** — fetch one function instead of reading its file — that
saves 80-96% of tokens (see the table below). **Across a whole task**, where an agent needs
several symbols and their callers, the measured figure is 12% on a mixed workload and 46% on
comprehension questions, at equal answer quality — and modification tasks deliberately cost
more. `benchmarks/benchmark_runner.py` produces those numbers; do not quote the per-lookup
figure as a per-task one.

> **Inside Firekeep**, firekeep-symdex is installed and registered automatically by `firekeep install` as one of six always-on client MCP servers (`firekeep-cortex`, `firekeep-bridge`, `firekeep-sentinel`, `firekeep-relay`, `firekeep-decision`, `firekeep-symdex`). It runs as a **local stdio server** against your working tree — there is no server-side container and no `--with-symdex` opt-in flag. The standalone install steps below are for running firekeep-symdex on its own, outside Firekeep.

## Features

- **12 languages**: Python, JavaScript, TypeScript, Go, Rust, Java, PHP, C, C#, Ruby, Kotlin, Swift
- **Smart symbol extraction**: Captures functions, classes, methods, constants, types, variables, routes, and module preambles -- plus JS/TS assigned functions, arrow functions, CommonJS exports, and prototype assignments
- **Framework-aware route extraction**: Detects `app.get('/path', handler)`, `router.post(...)`, `app.use(middleware)` patterns as first-class route symbols
- **Fuzzy and semantic search**: Subsequence matching ("auth" finds "authenticate") plus a built-in programming thesaurus ("auth" also finds "login", "token", "session"). Inverted name-token index for O(1) candidate narrowing with full-scan fallback for docstring/signature matches
- **PR review context**: Auto-assemble the minimal context for understanding a code change -- changed symbols, affected callers, dependencies, and related tests
- **Smart context budgeting**: Fill a token budget with the most relevant symbols + dependencies, with bidirectional byte-range deduplication to prevent overlap (evicts child symbols when parent is added, and vice versa)
- **Scope-aware references**: Callers/callees resolved with file-scope priority (same file > imported file > dotted name > fallback), with `from_symbol` enrichment tracking which function makes each call
- **Byte-offset retrieval**: O(1) source lookup via stored offsets, no re-parsing
- **Auto-reindex**: Stale files are automatically re-parsed on access (hash-based detection), no manual reindex needed
- **Incremental indexing**: Only re-parse changed files (hash-based or git-diff)
- **Architecture intelligence**: Dead code detection, import graphs, impact analysis, architecture maps
- **Code evolution (FirekeepTime)**: Git-powered timeline, complexity metrics, contributor mapping, churn analysis
- **Pattern-aware scaffolding (FirekeepForge)**: Convention extraction, pattern detection
- **Declarative tool registry**: Each tool module exports `TOOL_DEF`; the compact registry-driven server auto-discovers and registers them
- **38 tools** for comprehensive code exploration (30 shown by default; 8 analytics tools hidden unless `SYMDEX_ANALYTICS_ENABLED=true`)

## Installation

### Inside Firekeep (recommended)

`firekeep install` installs and registers firekeep-symdex automatically — there are no manual steps. In a developer checkout it is installed from the local `symdex/` path; in a release install it comes from the checksum-verified client wheel. Either way the client adapter renders the MCP registration for you, so there is nothing to hand-edit.

### Standalone use

To run firekeep-symdex on its own, install it from a local checkout. **Do not `pip install firekeep-symdex` by the bare PyPI name** — that name may belong to an unrelated third party (the same supply-chain hazard the Firekeep client installer deliberately avoids). Install from the repository directory instead:

```bash
git clone https://github.com/morganbarrett/firekeep-symdex.git
cd firekeep-symdex
uv sync                 # or: pip install .
```

Add `uv sync --extra all` (or `pip install .[all]`) for AI summary support.

#### Claude Desktop / MCP Client Configuration (standalone only)

Firekeep renders this registration for you. When registering manually for standalone use, add to your MCP client config:

```json
{
  "mcpServers": {
    "firekeep-symdex": {
      "command": "firekeep-symdex"
    }
  }
}
```

## Tools (38)

**30 tools are shown by default.** The 8 analytics tools marked † require indexed repos and are hidden unless `SYMDEX_ANALYTICS_ENABLED=true`: `get_hotspots`, `get_change_summary`, `compare_repos`, `get_evolution_timeline`, `get_complexity_metrics`, `get_contributors`, `get_code_churn`, `detect_patterns`.

### Indexing (2)

| Tool | Description |
|------|-------------|
| `index_repo` | Index a GitHub repository by URL (fetches via API) |
| `index_folder` | Index a local folder on disk |

### Exploration (7)

| Tool | Description |
|------|-------------|
| `get_file_tree` | Get file tree with per-file summaries |
| `get_file_outline` | Get all symbols in a file (signatures only, no source) |
| `get_symbol` | Get a single symbol by ID with full source; `include_imports` for file context |
| `get_symbols` | Batch-get multiple symbols by ID; `include_imports` for file context |
| `search_symbols` | Fuzzy + semantic search by name, kind, file pattern |
| `suggest_symbols` | Natural language task description to relevant symbols |
| `get_similar_symbols` | Find symbols with similar signatures or structure |

### Architecture Intelligence (8)

| Tool | Description |
|------|-------------|
| `get_callers` | Find all callers of a symbol |
| `get_dependencies` | Find all dependencies of a symbol |
| `get_impact` | Transitive impact analysis -- BFS through caller graph |
| `get_import_graph` | File-to-file dependency graph (adjacency, DOT, or summary) |
| `get_architecture_map` | Auto-classify files into layers (API, core, utility, etc.) |
| `find_dead_code` | Find unreferenced symbols (potential dead code) |
| `get_hotspots` † | Rank symbols by caller count |
| `get_type_hierarchy` | Inheritance chain -- parent classes and subclasses |

### Change Detection (4)

| Tool | Description |
|------|-------------|
| `get_change_summary` † | Compare current files against stored index |
| `diff_since_index` | Show what changed on disk since last indexing |
| `get_symbol_history` | Change history for a specific symbol across re-indexes |
| `compare_repos` † | Diff the symbol surface between two repositories |

### Smart Context (5)

| Tool | Description |
|------|-------------|
| `get_context` | Fill a token budget with the most relevant symbols; optionally include dependencies; auto-deduplicates overlapping byte ranges |
| `get_review_context` | Assemble minimal context for a PR review: changed symbols + callers + deps + related tests |
| `learn_from_changes` | Detect code changes and record them to FirekeepCortex memory |
| `recall_with_code` | Recall past experiences AND cross-reference with current code symbols |
| `review_with_history` | PR review context enriched with historical memory |

### Watch & Index Management (5)

| Tool | Description |
|------|-------------|
| `list_repos` | List all indexed repositories |
| `export_index` | Export the index as structured Markdown or JSON for direct context inclusion |
| `watch_folder` | Watch a local folder and auto-trigger incremental reindex on file changes (folder must be indexed first) |
| `unwatch_folder` | Stop watching a folder for changes |
| `list_watches` | List all actively watched folders |

### Code Evolution -- FirekeepTime (4)

All four FirekeepTime tools are analytics-gated (†) -- hidden unless `SYMDEX_ANALYTICS_ENABLED=true`.

| Tool | Description |
|------|-------------|
| `get_evolution_timeline` † | Git-powered change timeline for a symbol or file |
| `get_complexity_metrics` † | Complexity scoring: line count, nesting depth, cyclomatic complexity, risk level |
| `get_contributors` † | Contributor mapping via `git blame` -- ownership percentages per symbol/file |
| `get_code_churn` † | Change frequency analysis -- commits, lines added/removed, churn score |

### Pattern Analysis -- FirekeepForge (3)

| Tool | Description |
|------|-------------|
| `extract_conventions` | Analyze naming conventions, structure patterns, code patterns, framework detection |
| `detect_patterns` † | Find recurring structural patterns -- groups of symbols following the same template |
| `scaffold_symbol` | Generate a code scaffold for a new symbol that matches existing codebase conventions (AI when available, template fallback) |

## Configuration

| Env var | Default | Purpose |
|---------|---------|---------|
| `SYMDEX_ANALYTICS_ENABLED` | `false` | Register the 8 analytics tools (git history, churn, complexity, contributors, hotspots, cross-repo diff). Hidden by default because they require indexed repos. |
| `FIREKEEP_SYMDEX_MAX_FILES` | `1500` | Per-index ceiling on the number of source files parsed. |
| `FIREKEEP_CORTEX_URL` | (unset) | Enables the optional FirekeepCortex integration tools (see below). |

## Benchmarks

### LLM Answer Quality: FirekeepSymdex vs Raw File Context

Benchmark comparing LLM answer quality when given FirekeepSymdex-assembled context vs raw file dumps. Tested on the [Click](https://github.com/pallets/click) library (Python CLI framework): 20 questions across comprehension, navigation, and modification categories, scored by an LLM judge on accuracy, completeness, and relevance (1-5 scale). Each question run 3 times (60 total evaluations).

**FirekeepSymdex uses a hybrid strategy**: selective symbol extraction for comprehension/navigation questions (extracting only relevant symbols with relationships), and raw files + structural analysis for modification questions (where full implementation context is needed).

| Metric | FirekeepSymdex | Raw Files | Delta |
|--------|:-----------:|:---------:|:-----:|
| Accuracy | 4.33 | 4.32 | **+0.01** |
| Completeness | 4.77 | 4.62 | **+0.15** |
| Relevance | 4.97 | 4.95 | **+0.02** |
| Avg context tokens | 13,112 | 16,850 | **22% fewer** |
| Win/Loss/Tie | 14 | 13 | 33 ties |

**By category:**

| Category | FirekeepSymdex Accuracy | Raw Accuracy | Token Savings |
|----------|:--------------------:|:------------:|:-------------:|
| Comprehension | 4.67 | 4.62 | **47%** |
| Navigation | 4.19 | 4.19 | — |
| Modification | 4.11 | 4.11 | — |

**Key finding**: FirekeepSymdex matches or beats raw file context on all quality metrics while using 22% fewer tokens on average. For comprehension questions, selective symbol extraction saves 47% of tokens with no accuracy loss — the model focuses on relevant symbols instead of scanning thousands of lines of irrelevant code.

---

Self-indexed on firekeep-symdex itself: 58 Python files, 12,545 lines, 437 symbols, 3,176 references.

### Search Accuracy

| Query | Top-1 Correct | Total Matches |
|-------|:---:|---:|
| `IndexStore` | Yes | 3 |
| `parse_file` | Yes | 2 |
| `extract_references` | Yes | 3 |
| `get_context` | Yes | 4 |
| `refresh_file` | Yes | 2 |
| `score_symbol` | Yes | 2 |

**Top-1 accuracy: 100%** (6/6 queries return the correct symbol as the first result)

### Token Savings (get_symbol)

| Symbol | Tokens Saved | Timing |
|--------|----------:|------:|
| `CodeIndex` (class) | 7,842 | 3.6ms |
| `parse_file` | 6,707 | 10.5ms |
| `IndexStore` (class) | 3,569 | 12.4ms |
| `extract_references` | 3,120 | 10.2ms |
| `get_context` | 1,025 | 9.9ms |
| `search_symbols` | 636 | 9.7ms |
| **Average** | **3,817** | **9.4ms** |

### Whole-Repo Token Savings

| Approach | Tokens | vs Raw File Reading |
|----------|-------:|--------------------:|
| Raw file reading (all 58 files) | ~117,600 | baseline |
| Signatures only (`get_file_outline`) | ~23,261 | **80.2% saved** |
| Smart context (per query, 4K budget) | ~4,000 | **96.6% saved** |
| Single symbol retrieval (`get_symbol`) | ~50-280 | **96-99.8% saved** |

### Context Budget Efficiency (get_context)

| Budget | Used | Utilization | Symbols |
|-------:|-----:|------:|----:|
| 1,000 | 994 | 99.4% | 6 |
| 4,000 | 3,994 | 99.9% | 23 |
| 10,000 | 9,999 | 100.0% | 34 |

### Performance

| Operation | Timing |
|-----------|-------:|
| Symbol retrieval (`get_symbol`) | **9.4ms** avg |
| File outline (`get_file_outline`) | **< 0.5ms** |
| Architecture map (`get_architecture_map`) | **7.7ms** |
| Search (`search_symbols`) | **< 5ms** |

### Task-by-Task: FirekeepSymdex vs Grep + File Reading

| Task | Base (grep/read) | FirekeepSymdex | Savings |
|------|------------------:|------------:|--------:|
| Find a specific function | ~500 tokens | ~49 tokens | **90%** |
| Who calls `get_context`? | ~800 tokens | ~180 tokens | **78%** |
| Read `load_index` implementation | ~6,755 tokens | ~280 tokens | **96%** |
| Find all summarizer classes | ~16 tokens | ~120 tokens | -- |
| Impact of changing `extract_references` | ~500 tokens | ~450 tokens | -- |

**Key takeaways:**
- **Targeted lookups** (read a specific function): **90-96% token savings** by avoiding full file reads
- **Dependency analysis** (callers, impact): comparable tokens but **vastly richer structured data** -- transitive impact analysis is impossible with grep
- **Signatures-only mode**: ideal for codebase overviews at **80% savings**
- **Smart context budgeting**: serves the most relevant code for any query at a fixed token cost, with **99.4-100% budget utilization**

### Quality

| Metric | Value |
|--------|-------|
| Tests | **507 passed**, 4 skipped, 0 failed |
| Coverage | **69%** (65% threshold) |
| Search top-1 accuracy | **100%** |
| `from_symbol` accuracy | **99%** (call refs attributed to correct enclosing function) |
| Auto-reindex | Hash-based, transparent to callers |

## Usage Examples

### Index and explore a repo

```
index_folder path="/home/user/my-project"
get_file_tree repo="my-project"
search_symbols repo="my-project" query="auth" kind="function"
get_symbol repo="my-project" symbol_id="auth.py::authenticate#function" include_imports=true
```

### PR review context

```
get_review_context repo="my-project" changed_files=["lib/auth.js", "lib/session.js"] budget_tokens=8000
```

### Architecture analysis

```
get_architecture_map repo="my-project"
get_import_graph repo="my-project" format="summary"
find_dead_code repo="my-project"
```

### Impact analysis

```
get_impact repo="my-project" symbol_id="utils.py::parse_config#function" max_depth=3
get_callers repo="my-project" symbol_id="db.py::connect#function"
```

### Smart context budgeting

```
get_context repo="my-project" focus="authentication" budget_tokens=4000 include_deps=true
```

### Code evolution (FirekeepTime)

```
get_evolution_timeline repo="my-project" symbol_id="auth.py::login#function"
get_complexity_metrics repo="my-project" sort_by="complexity" max_results=10
get_contributors repo="my-project" file_path="src/auth.py"
get_code_churn repo="my-project" since="3 months ago"
```

### Pattern analysis (FirekeepForge)

```
extract_conventions repo="my-project"
detect_patterns repo="my-project" kind="function" min_group_size=3
```

## Architecture

```
src/firekeep_symdex/
|-- server.py              # MCP server -- auto-discovers tools via registry
|-- parser/
|   |-- extractor.py       # tree-sitter AST walking + symbol extraction
|   |-- languages.py       # Per-language specs (node types, patterns)
|   |-- references.py      # Import/call reference extraction with from_symbol tracking
|   |-- symbols.py         # Symbol dataclass
|   \-- hierarchy.py       # Parent-child symbol tree
|-- storage/
|   |-- index_store.py     # Index save/load, byte-offset retrieval, search scoring, auto-reindex
|   \-- token_tracker.py   # Token savings tracking
|-- tools/
|   |-- __init__.py        # discover_tools() -- declarative tool registry
|   |-- index_repo.py      # GitHub repo indexing
|   |-- index_folder.py    # Local folder indexing
|   |-- get_context.py     # Smart context budgeting with bidirectional deduplication
|   |-- get_review_context.py  # PR review context assembly
|   |-- find_dead_code.py  # Unreferenced symbol detection
|   |-- get_import_graph.py # File dependency graph
|   |-- get_impact.py      # Transitive impact analysis
|   |-- get_change_summary.py # Index-vs-current diffing
|   |-- get_architecture_map.py # Auto layer classification
|   \-- _utils.py          # Shared helpers (resolve_repo, file summaries, scope-aware resolution)
|-- cortex/                # FirekeepCortex integration (optional)
|-- security/              # Path validation, secret detection
\-- summarizer/            # AI-powered symbol summaries (optional)
```

## Claude Code Plugin (Automatic Coding Intelligence)

FirekeepSymdex ships with a Claude Code plugin that automatically makes Claude a better coder — no manual tool calls needed. It primes every session with architecture context and reminds Claude to check callers before edits.

> **In Firekeep**, `firekeep install` already registers firekeep-symdex — skip steps 1–2 below (they are for standalone use). The hand-written `.mcp.json` and the hardcoded `.venv/Scripts/firekeep-symdex.exe` path are only needed when running symdex outside the client kit.

### Quick Start (standalone)

```bash
# 1. Install FirekeepSymdex
git clone https://github.com/morganbarrett/firekeep-symdex.git
cd firekeep-symdex
uv sync

# 2. Register as MCP server (add to ~/.claude/.mcp.json)
{
  "mcpServers": {
    "firekeep-symdex": {
      "command": "<path-to-repo>/.venv/Scripts/firekeep-symdex.exe",
      "args": []
    }
  }
}

# 3. Install the plugin (inside Claude Code, run):
/plugin marketplace add <path-to-repo>/claude-plugin
```

Then, for each project you want intelligence on:

```
# In Claude Code, just say:
"Index this project with index_folder"
```

That's it. Every Claude Code session now gets:
- **Architecture priming** — Claude calls `get_architecture_map` and `extract_conventions` at session start
- **Caller awareness** — advisory reminder to check `get_callers`/`get_impact` before modifying public APIs
- **Self-review** — run `/symdex:self-review` after changes to validate callers and conventions

### What the Plugin Does

| Hook | When | What |
|------|------|------|
| SessionStart | Every conversation | Checks if project is indexed, primes Claude with architecture + conventions + coding guidelines |
| PreToolUse | Every Edit/Write | Injects advisory reminder to check callers before structural changes |

| Skill | Invocation | What |
|-------|------------|------|
| Self-review | `/symdex:self-review` | Validates recent changes against callers, dependencies, and conventions |

### Overhead

~3-5k tokens for session priming + ~80 tokens per edit reminder. For a typical 10-edit session, roughly 5% of context window — in exchange for cross-file awareness, convention adherence, and fewer broken references.

## FirekeepCortex Integration

When [FirekeepCortex](https://github.com/morganbarrett/firekeep-cortex) is running, firekeep-symdex gains persistent code memory:

### Setup

Set the environment variable to enable integration:

```bash
export FIREKEEP_CORTEX_URL=http://localhost:8100
```

In Firekeep the Cortex REST API is published on `:8100` (container-internal `8000` is not reachable from the client-side stdio symdex). Under `AUTH_ENABLED=true`, symdex also threads `FIREKEEP_INTERNAL_KEY` as an `X-API-Key` header on its outbound Cortex calls.

### Integration Tools

| Tool | Description |
|------|-------------|
| `learn_from_changes` | Detect code changes and record them to FirekeepCortex memory for future recall |
| `recall_with_code` | Recall past experiences AND cross-reference with current code symbols |
| `review_with_history` | PR review context enriched with historical memory about changed files |

### How It Works

- **learn_from_changes**: After editing code, this tool detects what changed (symbols added/modified/removed) and stores the action/outcome in FirekeepCortex. Future agents working on the same files get historical context.
- **recall_with_code**: Queries FirekeepCortex for relevant memories, extracts keywords, then uses those keywords to focus the code context search. Returns both memories and relevant code symbols, plus cross-references showing which symbols appear in past memories.
- **review_with_history**: Wraps the standard `get_review_context` with per-file historical lookups. Generates warnings when past changes to the same files caused regressions.

All integration tools gracefully degrade when FirekeepCortex is unavailable -- they fall back to code-only results.

## Development

```bash
git clone https://github.com/morganbarrett/firekeep-symdex.git
cd firekeep-symdex
uv sync --extra test
uv run pytest
```

## License

Proprietary — see [`LICENSE`](../LICENSE). Not open source.

`firekeep-symdex` is a component of Firekeep and is covered by the same licence as the
rest of the product; it is not separately licensed. This section previously read `MIT`,
inherited from the standalone tool this package grew out of. Because `pyproject.toml`
sets `readme = "README.md"`, that line was the wheel's long description, so the built
`firekeep-symdex` METADATA carried both `License-Expression:
LicenseRef-Firekeep-Proprietary` and a contradictory `MIT` — on every developer machine,
since the bootstrap always installs this package.

Third-party dependency licences (including the tree-sitter grammar bundle) are
reproduced in [`NOTICE`](./NOTICE).
