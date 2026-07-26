# FirekeepSymdex Coding Intelligence Plugin

**Date:** 2026-03-13
**Status:** Draft
**Goal:** Make Claude Code a better coder than stock by automatically providing deep code comprehension context via FirekeepSymdex's existing MCP tools.

## Problem

FirekeepSymdex exposes 36 MCP tools for code comprehension — callers, impact analysis, conventions, architecture maps, etc. But Claude Code never calls them proactively. It defaults to raw `Read`/`Grep`/`Glob` and codes blind, identical to stock Claude Code without FirekeepSymdex.

The tools exist. The integration doesn't.

## Solution

A Claude Code plugin that ships with FirekeepSymdex, providing:

1. **SessionStart hook** — primes Claude with architecture + conventions at every conversation start
2. **PreToolUse hook** on Edit/Write — injects caller/outline data before every code edit
3. **System instructions** — steers Claude toward FirekeepSymdex tools over raw file reads
4. **Self-review skill** — user-invocable `/self-review` for post-change validation

## Non-Goals

- No new FirekeepSymdex tools (the existing 36 are sufficient)
- No agent orchestration (hooks + instructions, not a new workflow)
- Token savings are secondary to code accuracy

## Architecture

```
firekeep-symdex/
└── claude-plugin/                    # New: Claude Code plugin
    ├── .claude-plugin/
    │   └── plugin.json               # Manifest (name: "symdex")
    ├── hooks/
    │   └── hooks.json                # SessionStart + PreToolUse hooks
    ├── skills/
    │   └── self-review/
    │       └── SKILL.md              # /symdex:self-review command
    └── scripts/
        ├── ensure-indexed.sh         # SessionStart: primes Claude with architecture context
        └── pre-edit-check.sh         # PreToolUse: advisory caller reminder
```

The plugin lives inside the FirekeepSymdex repo. Installing FirekeepSymdex and enabling the plugin gives Claude Code automatic coding intelligence.

## Component Details

### 1. SessionStart Hook (Architectural Priming)

**Trigger:** Every new Claude Code conversation.

**Mechanism:** Command hook that runs a script to check if the current working directory is indexed, then outputs plain text to stdout. Plain text stdout from a command hook (exit 0) is injected directly into Claude's context — this is the correct mechanism for SessionStart priming. (`systemMessage` only shows a UI warning to the user and does NOT enter Claude's context.)

**Script: `ensure-indexed.sh`**

```bash
#!/bin/bash
# Check if current project is indexed by FirekeepSymdex
# Plain text stdout on exit 0 is injected into Claude's context

# Read cwd from stdin JSON (hook input provides session context)
# Use Python with fallback chain — python3 (Linux/Mac), python (Windows), py -3 (Windows launcher)
INPUT=$(cat)
PYTHON=$(command -v python3 2>/dev/null || command -v python 2>/dev/null || echo "py -3")
PROJECT_DIR=$(echo "$INPUT" | $PYTHON -c "import sys,json; print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null)

# Fallback to PWD if cwd not in input
if [ -z "$PROJECT_DIR" ]; then
  PROJECT_DIR="$PWD"
fi

if [ -z "$PROJECT_DIR" ]; then
  echo "FirekeepSymdex: Could not determine project directory."
  exit 0
fi

FOLDER_NAME=$(basename "$PROJECT_DIR")

# Check if the project is indexed by looking for index file
# FirekeepSymdex stores indices at ~/.code-index/<owner>-<name>.json
# For local folders: ~/.code-index/local-<folder_name>.json
INDEX_FILE="$HOME/.code-index/local-${FOLDER_NAME}.json"
if [ ! -f "$INDEX_FILE" ]; then
  echo "FirekeepSymdex: Project '$FOLDER_NAME' is not indexed. Run index_folder(path='$PROJECT_DIR') to enable code intelligence."
  exit 0
fi

cat <<EOF
FirekeepSymdex code intelligence is active for repo 'local/$FOLDER_NAME'.

Before writing any code, call these tools to understand the codebase:
1. get_architecture_map(repo='local/$FOLDER_NAME') — understand layers and module roles
2. extract_conventions(repo='local/$FOLDER_NAME') — learn naming patterns and conventions

Coding guidelines:
- Prefer search_symbols/suggest_symbols over raw Grep/Glob when exploring code
- Use get_file_outline instead of reading entire files to understand structure
- Before modifying public APIs or widely-used symbols, call get_callers and get_impact
- After multi-file changes, call get_review_context to self-check for broken references
- If tool calls fail with "Repository not found", call list_repos() to discover the correct repo identifier
EOF
```

**Token cost:** ~200 tokens for the injected context. The `get_architecture_map` + `extract_conventions` calls that Claude makes in response cost ~2-4k tokens depending on repo size.

**Why command, not prompt:** The SessionStart hook needs to inject the repo name dynamically (derived from project directory). A prompt-based hook can't compute that. SessionStart only supports `type: "command"` hooks.

**Windows compatibility:** The script uses bash, which works via Git Bash on Windows (Claude Code's default shell on Windows). The script uses `python3` for JSON parsing instead of `jq` since Python is guaranteed to be available (FirekeepSymdex itself requires Python).

### 2. PreToolUse Hook on Edit/Write (Caller Awareness)

**Trigger:** Every `Edit` or `Write` tool call.

**Mechanism:** Command hook that injects an advisory reminder into Claude's context via stdout. This is intentionally a command hook (not a prompt hook) because prompt hooks can return `ok: false` and **block** the edit — which contradicts the advisory intent. A command hook that exits 0 with plain text stdout adds context without any blocking risk.

**Hook script: `scripts/pre-edit-check.sh`**

```bash
#!/bin/bash
# Advisory reminder injected before Edit/Write tool calls
# Plain text stdout on exit 0 is added to Claude's context (non-blocking)
cat <<'EOF'
FirekeepSymdex reminder: Before applying this edit, consider whether you've checked what other code depends on what you're changing. If you're modifying a function signature, return type, class interface, or public API, call get_callers() and get_impact() on the affected symbol first. If this is a simple internal change (string, comment, local variable), proceed without additional checks.
EOF
```

**Hook config:**
```json
{
  "matcher": "Edit|Write",
  "hooks": [
    {
      "type": "command",
      "command": "bash ${CLAUDE_PLUGIN_ROOT}/scripts/pre-edit-check.sh",
      "timeout": 5
    }
  ]
}
```

**Design choice: advisory, not blocking.** The command hook always exits 0, so it never blocks the edit. It injects a reminder into Claude's context. Claude decides whether the change is structural enough to warrant calling `get_callers`.

**Why not a prompt hook:** Prompt hooks invoke a separate LLM evaluator that can return `ok: false` and block the tool call. This would be expensive (Haiku call per edit) and could incorrectly block valid edits. A command hook is free, instant, and purely advisory.

**Token cost:** ~80 tokens for the injected reminder. Claude's subsequent `get_callers` call (when triggered) costs ~0.5-2k tokens depending on caller count.

### 3. System Instructions (via plugin.json)

The plugin manifest doesn't support a `systemPrompt` field directly — instructions are delivered via the SessionStart hook's system message (see Section 1).

Key instructions baked into the session-start message:
- Prefer `search_symbols`/`suggest_symbols` over raw `Grep` for code exploration
- Use `get_file_outline` instead of reading entire files
- Call `get_review_context` after multi-file changes
- Use `get_callers` + `get_impact` before modifying public APIs

### 4. Self-Review Skill

**Trigger:** User invokes `/symdex:self-review`.

**Purpose:** After completing a set of changes, validate them against the indexed codebase.

**SKILL.md content:**
```markdown
---
name: self-review
description: Review your recent code changes against the indexed codebase.
  Use after completing edits to verify callers are updated, conventions
  are followed, and no symbols are broken.
---

Run a self-review of recent changes:

1. Identify all files you modified in this session
2. Call get_review_context(repo, changed_files=[...]) to find:
   - Changed symbols and their callers (are callers updated?)
   - Dependencies of changed symbols (are they still valid?)
   - Related test files (do tests exist for your changes?)
3. Call extract_conventions(repo) and verify your changes follow
   the codebase's naming and structural patterns
4. Report findings: what looks good, what might need attention
```

**Token cost:** ~1-3k tokens depending on change scope.

## hooks.json (Complete)

```json
{
  "description": "FirekeepSymdex coding intelligence — automatic code comprehension for Claude Code",
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash ${CLAUDE_PLUGIN_ROOT}/scripts/ensure-indexed.sh",
            "timeout": 10
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [
          {
            "type": "command",
            "command": "bash ${CLAUDE_PLUGIN_ROOT}/scripts/pre-edit-check.sh",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

**Note:** The SessionStart entry omits `matcher` intentionally — this causes it to fire on all session types (startup, resume, clear, compact), which is desired for priming.

## plugin.json (Complete)

```json
{
  "name": "symdex",
  "version": "1.0.0",
  "description": "Automatic code comprehension for Claude Code via FirekeepSymdex — primes sessions with architecture context and injects caller awareness before edits",
  "author": {
    "name": "FirekeepSymdex"
  },
  "keywords": ["code-intelligence", "comprehension", "callers", "impact-analysis"]
}
```

**Note:** Plugin name is `symdex` (not `firekeep-symdex-intelligence`) so the self-review skill is invoked as `/symdex:self-review` — ergonomic for frequent use.

## Token Budget Analysis

| Component | When | Token Cost | API Cost | Frequency |
|---|---|---|---|---|
| SessionStart context injection | Once per conversation | ~200 tokens | Free (command hook) | 1x |
| Architecture map response | Once per conversation | ~1-3k tokens | FirekeepSymdex MCP call | 1x |
| Conventions response | Once per conversation | ~0.5-1k tokens | FirekeepSymdex MCP call | 1x |
| PreEdit advisory reminder | Every Edit/Write | ~80 tokens | Free (command hook) | N per session |
| get_callers response (when triggered) | Structural edits only | ~0.5-2k tokens | FirekeepSymdex MCP call | ~30% of edits |
| Self-review (optional) | User-invoked | ~1-3k tokens | FirekeepSymdex MCP call | 0-1x |

**Typical session overhead:** ~3-5k tokens for priming + ~80 tokens per edit (advisory reminder only — no LLM evaluator calls). For a 10-edit session with ~3 structural edits triggering `get_callers`, that's ~6-8k tokens total — roughly 5% of a typical 128k context window. Both hooks are command hooks, so there are no extra Haiku/LLM calls beyond the FirekeepSymdex MCP tool calls Claude chooses to make.

## Success Criteria

1. Claude Code calls `get_callers` before modifying public APIs without being asked
2. Claude Code follows codebase conventions (naming, error handling) detected by `extract_conventions`
3. Cross-file modifications include caller updates (fewer broken references)
4. Claude prefers `search_symbols`/`suggest_symbols` over raw `Grep` for code exploration

## Risks

1. **Hook ignored:** Claude may still skip the PreToolUse advisory. Mitigation: the SessionStart instructions reinforce the same behavior.
2. **Repo not indexed:** If the project hasn't been indexed via `index_folder`, all tool calls fail. Mitigation: `ensure-indexed.sh` can detect this and warn.
3. **Folder name collision:** `local/<folder_name>` may not be unique. Mitigation: `list_repos()` in the session-start message tells Claude to check.
4. **Overhead on simple tasks:** Even "fix this typo" gets the full session priming. Mitigation: 3-5k tokens is small relative to context window; architecture data is useful even for small changes.

## Future Enhancements (Out of Scope)

- Auto-index on SessionStart if project not yet indexed
- PostToolUse hook that validates edits against callers after the fact
- FirekeepCortex integration for cross-session learning about the codebase
- Benchmark comparing coding accuracy with/without the plugin
