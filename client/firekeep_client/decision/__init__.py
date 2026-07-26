"""Decision Board: static XSS-safe board HTML + local MCP board server (SP4).

This subpackage is client-tree code — see the repo-root CLAUDE.md's client
import boundary (client/tests/test_import_boundary.py): every module here
except a future shim-like file must stay stdlib-only (no `mcp`, no `httpx`,
no server packages).
"""
