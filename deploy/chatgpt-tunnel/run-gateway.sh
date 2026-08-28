#!/usr/bin/env bash
# The MCP server the ChatGPT tunnel forwards to: the machine's installed
# Firekeep gateway, narrowed to the chat toolset.
#
# FIREKEEP_TOOLSET is exported HERE, immediately after clearing the explicit
# allowlist that otherwise wins over a preset. The curated surface therefore
# cannot be replaced by ambient service or tunnel-client environment. A typo'd
# toolset name makes the gateway refuse to start; it never falls back to the
# unfiltered gateway.
set -euo pipefail
unset FIREKEEP_TOOLS_ALLOW
export FIREKEEP_TOOLSET=chat
exec "$HOME/.firekeep/shims/firekeep" gateway --runtime chatgpt
