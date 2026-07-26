"""SP1a: cortex-mcp run() wiring for the auth middleware.

cortex/tests/conftest.py replaces fastmcp with a stub (tools are plain
functions), so the production http_app cannot be built in this suite.
Behavioral coverage for middleware-wraps-FastMCP lives in
auth/tests/test_asgi_fastmcp.py; this test pins the wiring line itself.
"""

from __future__ import annotations

from pathlib import Path

CORTEX_ROOT = Path(__file__).resolve().parents[1]


def test_run_call_wires_auth_middleware():
    src = (CORTEX_ROOT / "app" / "mcp_server.py").read_text(encoding="utf-8")
    main_block = src[src.index('if __name__ == "__main__":'):]
    assert "middleware=build_auth_middleware(" in main_block
    assert "from auth.asgi import build_auth_middleware" in main_block
    assert "from auth.config import get_auth_settings" in main_block
