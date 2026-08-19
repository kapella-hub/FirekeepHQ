#!/usr/bin/env bash
# The MCP server the ChatGPT tunnel forwards to: the machine's installed
# Firekeep gateway, narrowed to the chat toolset.
#
# FIREKEEP_TOOLSET is exported HERE, inside the exec'd script, on purpose:
# whether tunnel-client passes its own environment to the MCP child is
# undocumented, and the toolset must not depend on it. If this script runs at
# all, the chat surface is on — there is no configuration where the tunnel
# serves the full ~90-tool surface by accident. (A typo'd toolset name makes
# the gateway refuse to start; it never falls back to unfiltered.)
set -euo pipefail
export FIREKEEP_TOOLSET=chat
exec "$HOME/.firekeep/shims/firekeep" gateway --runtime chatgpt
