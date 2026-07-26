# Coding Intelligence Plugin Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a Claude Code plugin that automatically primes Claude with FirekeepSymdex code comprehension context, making it a better coder than stock.

**Architecture:** A Claude Code plugin with two command hooks (SessionStart for architectural priming, PreToolUse for caller-awareness reminders), one skill (/symdex:self-review), and two bash scripts. All files live in `claude-plugin/` at the FirekeepSymdex repo root.

**Tech Stack:** Claude Code plugin system (plugin.json, hooks.json, SKILL.md), Bash scripts, Python (for JSON parsing in hooks)

**Spec:** `docs/superpowers/specs/2026-03-13-coding-intelligence-plugin-design.md`

---

## File Structure

```
claude-plugin/
├── .claude-plugin/
│   └── plugin.json               # Plugin manifest (name: "symdex")
├── hooks/
│   └── hooks.json                # SessionStart + PreToolUse hook config
├── skills/
│   └── self-review/
│       └── SKILL.md              # /symdex:self-review skill
└── scripts/
    ├── ensure-indexed.sh         # SessionStart: detect index, prime Claude
    └── pre-edit-check.sh         # PreToolUse: advisory caller reminder
```

---

## Chunk 1: Plugin Scaffold + Hooks

### Task 1: Create plugin manifest

**Files:**
- Create: `claude-plugin/.claude-plugin/plugin.json`

- [ ] **Step 1: Create directory structure**

Run:
```bash
mkdir -p claude-plugin/.claude-plugin claude-plugin/hooks claude-plugin/skills/self-review claude-plugin/scripts
```

- [ ] **Step 2: Create .gitattributes for LF line endings**

Create `claude-plugin/.gitattributes`:
```
# Bash scripts must have LF line endings, even on Windows
scripts/*.sh text eol=lf
```

This prevents Git from converting line endings to CRLF on Windows, which would break the bash scripts with `$'\r': command not found` errors.

- [ ] **Step 3: Write plugin.json**

Create `claude-plugin/.claude-plugin/plugin.json`:
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

- [ ] **Step 4: Commit**

```bash
git add claude-plugin/.claude-plugin/plugin.json claude-plugin/.gitattributes
git commit -m "feat(plugin): scaffold symdex Claude Code plugin"
```

---

### Task 2: Create hooks configuration

**Files:**
- Create: `claude-plugin/hooks/hooks.json`

- [ ] **Step 1: Write hooks.json**

Create `claude-plugin/hooks/hooks.json`:
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

Key points:
- SessionStart omits `matcher` → fires on all session types (startup, resume, clear, compact)
- PreToolUse matches `Edit|Write` only
- Both are `type: "command"` (not `type: "prompt"`) → advisory, never blocks

- [ ] **Step 2: Commit**

```bash
git add claude-plugin/hooks/hooks.json
git commit -m "feat(plugin): add SessionStart + PreToolUse hook config"
```

---

### Task 3: Create SessionStart script (ensure-indexed.sh)

**Files:**
- Create: `claude-plugin/scripts/ensure-indexed.sh`

- [ ] **Step 1: Write the script**

Create `claude-plugin/scripts/ensure-indexed.sh`:
```bash
#!/bin/bash
# FirekeepSymdex SessionStart hook
# Checks if current project is indexed and primes Claude with coding instructions.
# Plain text stdout on exit 0 is injected into Claude's context.

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

# Check if the project is indexed
# FirekeepSymdex stores indices at ~/.code-index/local-<folder_name>.json
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

- [ ] **Step 2: Make executable**

Run:
```bash
chmod +x claude-plugin/scripts/ensure-indexed.sh
```

- [ ] **Step 3: Test the script locally**

Test with a mock stdin (simulating Claude Code hook input):
```bash
echo '{"cwd": "'$(pwd)'"}' | bash claude-plugin/scripts/ensure-indexed.sh
```

Expected output (if FirekeepSymdex is indexed for this folder):
```
FirekeepSymdex code intelligence is active for repo 'local/FirekeepSymdex'.
...
```

Or (if not indexed):
```
FirekeepSymdex: Project 'FirekeepSymdex' is not indexed. Run index_folder(path='...') to enable code intelligence.
```

- [ ] **Step 4: Commit**

```bash
git add claude-plugin/scripts/ensure-indexed.sh
git commit -m "feat(plugin): add SessionStart script for architectural priming"
```

---

### Task 4: Create PreToolUse script (pre-edit-check.sh)

**Files:**
- Create: `claude-plugin/scripts/pre-edit-check.sh`

- [ ] **Step 1: Write the script**

Create `claude-plugin/scripts/pre-edit-check.sh`:
```bash
#!/bin/bash
# FirekeepSymdex PreToolUse hook (Edit|Write)
# Advisory reminder injected before edit/write tool calls.
# Plain text stdout on exit 0 is added to Claude's context (non-blocking).
cat <<'EOF'
FirekeepSymdex reminder: Before applying this edit, consider whether you've checked what other code depends on what you're changing. If you're modifying a function signature, return type, class interface, or public API, call get_callers() and get_impact() on the affected symbol first. If this is a simple internal change (string, comment, local variable), proceed without additional checks.
EOF
```

- [ ] **Step 2: Make executable**

Run:
```bash
chmod +x claude-plugin/scripts/pre-edit-check.sh
```

- [ ] **Step 3: Test the script locally**

Run:
```bash
bash claude-plugin/scripts/pre-edit-check.sh
echo "Exit code: $?"
```

Expected: The reminder text followed by `Exit code: 0`.

- [ ] **Step 4: Commit**

```bash
git add claude-plugin/scripts/pre-edit-check.sh
git commit -m "feat(plugin): add PreToolUse advisory caller reminder script"
```

---

## Chunk 2: Self-Review Skill + Integration Test

### Task 5: Create self-review skill

**Files:**
- Create: `claude-plugin/skills/self-review/SKILL.md`

- [ ] **Step 1: Write SKILL.md**

Create `claude-plugin/skills/self-review/SKILL.md`:
```markdown
---
name: self-review
description: Review your recent code changes against the indexed codebase. Use after completing edits to verify callers are updated, conventions are followed, and no symbols are broken.
---

Run a self-review of recent changes using FirekeepSymdex:

1. Identify all files you modified in this session (check git status or recall from conversation)
2. Call get_review_context(repo, changed_files=[<list of modified files>]) to find:
   - Changed symbols and their callers (are callers updated?)
   - Dependencies of changed symbols (are they still valid?)
   - Related test files (do tests exist for your changes?)
3. Call extract_conventions(repo) and verify your changes follow the codebase's naming and structural patterns
4. Report findings: what looks good, what might need attention, and any callers that may need updating
```

- [ ] **Step 2: Commit**

```bash
git add claude-plugin/skills/self-review/SKILL.md
git commit -m "feat(plugin): add /symdex:self-review skill"
```

---

### Task 6: End-to-end validation

No new files — this task validates the plugin works correctly.

- [ ] **Step 1: Verify plugin structure**

Run:
```bash
find claude-plugin -type f | sort
```

Expected output:
```
claude-plugin/.claude-plugin/plugin.json
claude-plugin/.gitattributes
claude-plugin/hooks/hooks.json
claude-plugin/scripts/ensure-indexed.sh
claude-plugin/scripts/pre-edit-check.sh
claude-plugin/skills/self-review/SKILL.md
```

- [ ] **Step 2: Validate JSON files**

Run:
```bash
python3 -c "import json; json.load(open('claude-plugin/.claude-plugin/plugin.json')); print('plugin.json: OK')"
python3 -c "import json; json.load(open('claude-plugin/hooks/hooks.json')); print('hooks.json: OK')"
```

Expected: Both print OK with no errors.

- [ ] **Step 3: Validate hooks.json structure**

Run:
```bash
python3 -c "
import json
h = json.load(open('claude-plugin/hooks/hooks.json'))
assert 'hooks' in h, 'Missing hooks wrapper'
assert 'SessionStart' in h['hooks'], 'Missing SessionStart'
assert 'PreToolUse' in h['hooks'], 'Missing PreToolUse'
# SessionStart should have no matcher
ss = h['hooks']['SessionStart'][0]
assert 'matcher' not in ss, 'SessionStart should not have matcher'
# PreToolUse should match Edit|Write
pt = h['hooks']['PreToolUse'][0]
assert pt['matcher'] == 'Edit|Write', f'Wrong matcher: {pt[\"matcher\"]}'
# Both hooks should be type: command
assert ss['hooks'][0]['type'] == 'command', 'SessionStart hook should be command type'
assert pt['hooks'][0]['type'] == 'command', 'PreToolUse hook should be command type'
print('hooks.json structure: OK')
"
```

Expected: `hooks.json structure: OK`

- [ ] **Step 4: Test ensure-indexed.sh with real project**

Run:
```bash
echo '{"cwd": "'$(pwd)'"}' | bash claude-plugin/scripts/ensure-indexed.sh
```

Verify: Output contains either "is active for repo" (if indexed) or "is not indexed" (if not). Either is correct — the script should never error.

- [ ] **Step 5: Test ensure-indexed.sh with empty input**

Run:
```bash
echo '{}' | bash claude-plugin/scripts/ensure-indexed.sh
echo "Exit code: $?"
```

Expected: Graceful handling (uses fallback or shows "Could not determine"), exit code 0.

- [ ] **Step 6: Final commit**

```bash
git add -A claude-plugin/
git commit -m "feat(plugin): complete symdex coding intelligence plugin

Adds a Claude Code plugin that automatically primes Claude with
FirekeepSymdex code comprehension context:
- SessionStart hook: architecture + conventions priming
- PreToolUse hook: advisory caller awareness before edits
- /symdex:self-review skill for post-change validation"
```
