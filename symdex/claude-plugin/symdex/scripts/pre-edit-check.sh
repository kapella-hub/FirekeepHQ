#!/bin/bash
# FirekeepSymdex PreToolUse hook (Edit|Write)
# Advisory reminder injected before edit/write tool calls.
# Plain text stdout on exit 0 is added to Claude's context (non-blocking).
cat <<'EOF'
FirekeepSymdex reminder: Before applying this edit, consider whether you've checked what other code depends on what you're changing. If you're modifying a function signature, return type, class interface, or public API, call get_callers() and get_impact() on the affected symbol first. If this is a simple internal change (string, comment, local variable), proceed without additional checks.
EOF
