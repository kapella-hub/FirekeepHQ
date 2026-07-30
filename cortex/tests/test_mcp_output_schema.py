"""The markdown these tools return must not be duplicated as structuredContent.

All four tools below are declared `-> str`. FastMCP's default behavior for a
`-> str` tool infers a JSON output_schema (`{"result": "<the string>"}`) and
ships it a second time as `structuredContent` alongside the identical
`content[0].text`. Setting `output_schema=None` suppresses that duplicate
payload.

Verified empirically against the pinned fastmcp==3.4.4 (cortex/requirements.lock,
bridge/requirements.lock; range fastmcp>=3.1,<4) in an isolated venv:
- `output_schema` is accepted by `FastMCP.tool()` on 3.4.4.
- `content[0].text` is byte-identical with and without `output_schema=None` —
  only the duplicated `structuredContent` copy disappears.

This suite runs against tests/conftest.py's `_FakeFastMCP` double (a real
fastmcp is not installed at the pinned version in this dev environment, and
even where it is, the fake is unconditionally installed at collection time),
so a real `FastMCP.get_tool(...).output_schema` is not observable here. The
double now records each tool's decorator kwargs in `mcp.registered_tools`,
which is what these tests inspect instead.
"""

from __future__ import annotations


def test_memory_recall_output_schema_is_none():
    from app import mcp_server

    kwargs = mcp_server.mcp.registered_tools["memory_recall"]
    assert "output_schema" in kwargs and kwargs["output_schema"] is None, (
        "a -> str tool with an inferred output schema ships its markdown a "
        "second time as a duplicated structuredContent copy"
    )


def test_skill_recall_output_schema_is_none():
    from app import mcp_server

    kwargs = mcp_server.mcp.registered_tools["skill_recall"]
    assert "output_schema" in kwargs and kwargs["output_schema"] is None


def test_skill_list_output_schema_is_none():
    from app import mcp_server

    kwargs = mcp_server.mcp.registered_tools["skill_list"]
    assert "output_schema" in kwargs and kwargs["output_schema"] is None


def test_vault_list_output_schema_is_none():
    from app import mcp_server

    kwargs = mcp_server.mcp.registered_tools["vault_list"]
    assert "output_schema" in kwargs and kwargs["output_schema"] is None


def test_other_str_tools_unaffected():
    """Spot-check a couple of -> str tools NOT in scope for this task still
    get no output_schema kwarg at all (default inferred-schema behavior)."""
    from app import mcp_server

    for name in ("memory_health", "vault_retrieve", "corpus_sources"):
        kwargs = mcp_server.mcp.registered_tools[name]
        assert "output_schema" not in kwargs
