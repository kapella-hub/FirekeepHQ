"""The markdown these tools return must not be duplicated as structuredContent.

All four tools below are declared `-> str`. FastMCP's default behavior for a
`-> str` tool infers a JSON output_schema (`{"result": "<the string>"}`) and
ships it a second time as `structuredContent` alongside the identical
`content[0].text`. Setting `output_schema=None` suppresses that duplicate
payload.

This suite runs against tests/conftest.py's `_FakeFastMCP` double, which is
installed into `sys.modules` at collection time, so a real
`FastMCP.get_tool(...).output_schema` is not observable here. The double
records each tool's decorator kwargs in `mcp.registered_tools`, which is what
these tests inspect instead.

That makes this HALF the verification: these tests prove the four production
tools DECLARE `output_schema=None`; they cannot prove the kwarg does anything.
The other half — that declaring it actually suppresses `structuredContent` at
the wire, against a real fastmcp — is `test_mcp_output_schema_wire.py`, which
runs a minimal two-tool reproduction in a subprocess (the only way past this
conftest's fake) and skips rather than false-passing when real fastmcp is
absent. It also pins the correction below and records exact byte counts.

Note on the win: an earlier design note claimed `-> str` tools ship JSON-escaped
markdown and that this setting changes what the runtime renders. That was
WRONG. `content[0].text` is byte-identical either way; the only saving is the
removed duplicate, roughly half the result bytes. Neither file can speak to how
any given runtime (Claude Code, kiro, ...) renders the result — that needs a
deploy.
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
