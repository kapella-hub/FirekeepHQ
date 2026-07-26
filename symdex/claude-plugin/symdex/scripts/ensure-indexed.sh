#!/bin/bash
# FirekeepSymdex SessionStart hook
# Checks if current project is indexed and instructs Claude to auto-index if not.
# Outputs JSON {"systemMessage": "..."} on exit 0, injected into Claude's context.

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
  echo '{"systemMessage":"FirekeepSymdex: Could not determine project directory. Code intelligence unavailable."}'
  exit 0
fi

FOLDER_NAME=$(basename "$PROJECT_DIR")

# Try to check via MCP tool call (works with VPS-hosted Symdex)
MCP_URL=""
MCP_JSON="$PROJECT_DIR/.mcp.json"
if [ -f "$MCP_JSON" ]; then
  MCP_URL=$($PYTHON -c "
import json
with open('$MCP_JSON') as f:
    cfg = json.load(f)
url = cfg.get('mcpServers', {}).get('firekeep-symdex', {}).get('url', '')
print(url)
" 2>/dev/null)
fi

# JSON-escape the project dir for safe embedding
SAFE_DIR=$($PYTHON -c "import json; print(json.dumps('$PROJECT_DIR')[1:-1])" 2>/dev/null)
if [ -z "$SAFE_DIR" ]; then
  SAFE_DIR="$PROJECT_DIR"
fi

# If we have an MCP URL, try to check whether the repo is already indexed
if [ -n "$MCP_URL" ]; then
  REPO_PATH="/repos/$FOLDER_NAME"
  # Attempt a lightweight check — get_file_tree with depth 0
  TREE_RESULT=$(curl -sf --max-time 3 -X POST "$MCP_URL" \
    -H "Content-Type: application/json" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"get_file_tree\",\"arguments\":{\"repo\":\"$FOLDER_NAME\",\"max_depth\":0}}}" 2>/dev/null)

  if echo "$TREE_RESULT" | $PYTHON -c "import sys,json; r=json.load(sys.stdin); exit(0 if 'result' in r and not r['result'].get('isError') else 1)" 2>/dev/null; then
    # Indexed — check for staleness via diff_since_index
    DIFF_RESULT=$(curl -sf --max-time 5 -X POST "$MCP_URL" \
      -H "Content-Type: application/json" \
      -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"diff_since_index\",\"arguments\":{\"repo\":\"$FOLDER_NAME\"}}}" 2>/dev/null)

    HAS_CHANGES=$($PYTHON -c "
import sys, json
r = json.load(sys.stdin)
content = r.get('result', {}).get('content', [{}])
text = content[0].get('text', '') if content else ''
# If diff reports changes, flag it
if 'modified' in text.lower() or 'added' in text.lower() or 'deleted' in text.lower():
    # Check if it's just 'no changes' or similar
    if 'no changes' in text.lower() or 'up to date' in text.lower():
        print('no')
    else:
        print('yes')
else:
    print('no')
" <<< "$DIFF_RESULT" 2>/dev/null)

    if [ "$HAS_CHANGES" = "yes" ]; then
      echo "{\"systemMessage\":\"=== FIREKEEP SYMDEX: INDEX STALE ===\\nProject '$FOLDER_NAME' has changes since last index.\\nACTION REQUIRED: Call index_folder(path='$REPO_PATH') to refresh code intelligence before working on code.\\n=== END SYMDEX ===\"}"
    else
      # Up to date — emit coding guidelines
      echo "{\"systemMessage\":\"=== FIREKEEP SYMDEX: CODE INTELLIGENCE ACTIVE ===\\nRepo '$FOLDER_NAME' is indexed and up to date.\\n\\nBefore writing code:\\n1. get_architecture_map(repo='$FOLDER_NAME') — understand layers and module roles\\n2. extract_conventions(repo='$FOLDER_NAME') — learn naming patterns\\n\\nGuidelines:\\n- Prefer search_symbols/suggest_symbols over raw Grep/Glob\\n- Use get_file_outline instead of reading entire files\\n- Before modifying public APIs, call get_callers and get_impact\\n- After multi-file changes, call get_review_context to self-check\\n- If tools fail with 'Repository not found', call list_repos()\\n=== END SYMDEX ===\"}"
    fi
  else
    # Not indexed — instruct agent to index
    echo "{\"systemMessage\":\"=== FIREKEEP SYMDEX: PROJECT NOT INDEXED ===\\nProject '$FOLDER_NAME' is not yet indexed for code intelligence.\\nACTION REQUIRED: Call index_folder(path='$REPO_PATH') NOW to enable code search, architecture maps, and impact analysis.\\nThis is a one-time operation that takes 10-30 seconds.\\n=== END SYMDEX ===\"}"
  fi
  exit 0
fi

# Fallback: check local index file (for stdio mode)
INDEX_FILE="$HOME/.code-index/local-${FOLDER_NAME}.json"
if [ ! -f "$INDEX_FILE" ]; then
  echo "{\"systemMessage\":\"=== FIREKEEP SYMDEX: PROJECT NOT INDEXED ===\\nProject '$FOLDER_NAME' is not indexed for code intelligence.\\nACTION REQUIRED: Call index_folder(path='$SAFE_DIR') NOW to enable code search, architecture maps, and impact analysis.\\nThis is a one-time operation that takes 10-30 seconds.\\n=== END SYMDEX ===\"}"
  exit 0
fi

echo "{\"systemMessage\":\"=== FIREKEEP SYMDEX: CODE INTELLIGENCE ACTIVE ===\\nRepo 'local/$FOLDER_NAME' is indexed.\\n\\nBefore writing code:\\n1. get_architecture_map(repo='local/$FOLDER_NAME') — understand layers and module roles\\n2. extract_conventions(repo='local/$FOLDER_NAME') — learn naming patterns\\n\\nGuidelines:\\n- Prefer search_symbols/suggest_symbols over raw Grep/Glob\\n- Use get_file_outline instead of reading entire files\\n- Before modifying public APIs, call get_callers and get_impact\\n- After multi-file changes, call get_review_context to self-check\\n- If tools fail with 'Repository not found', call list_repos()\\n=== END SYMDEX ===\"}"
