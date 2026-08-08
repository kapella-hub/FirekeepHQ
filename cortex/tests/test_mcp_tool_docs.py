"""Guards on what the MCP tool surface TELLS an agent.

A tool description is not a comment — it is the product surface the agent reads
and acts on. Two classes of defect are caught here, both accuracy defects rather
than cosmetic ones:

1. A tool that points the agent at a tool which does not exist. The agent
   obediently tries to call it, fails, and either gives up on checking progress
   or invents a substitute.
2. A tool that advertises a feature whose code path always returns zeros
   (corpus entity extraction — `corpus/pipeline.py` returns
   `entities_extracted: 0` / `extraction_status: "skipped"` on BOTH its return
   paths, unconditionally).
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from app import mcp_server

# `initialize` is the MCP protocol handshake method, not a Firekeep tool.
# `namespace` is a REQUEST FIELD named in memory_recall's Args, not a tool.
NON_TOOL_BACKTICKED = {"initialize", "namespace"}

_SRC = Path(mcp_server.__file__).read_text(encoding="utf-8")


def _registered_tool_names() -> set[str]:
    return {
        name
        for name, obj in vars(mcp_server).items()
        if inspect.iscoroutinefunction(obj) and not name.startswith("_")
    }


def test_no_backticked_reference_to_a_nonexistent_tool():
    """Every `snake_case` identifier in a description/response resolves to a
    real tool. Guards against a rename or removal leaving the agent chasing a
    tool that isn't there."""
    referenced = set(re.findall(r"`([a-z][a-z0-9_]{2,})`", _SRC))
    dangling = referenced - _registered_tool_names() - NON_TOOL_BACKTICKED
    assert not dangling, (
        f"MCP tool text references non-existent tool(s): {sorted(dangling)}. "
        "Name a surface that exists, or add it to NON_TOOL_BACKTICKED if it is "
        "not a tool reference."
    )


def test_corpus_tools_do_not_advertise_entity_extraction():
    """corpus_* descriptions must not promise entities/relationships: the
    extraction path was removed and both `ingest_document` return paths report
    zero, unconditionally."""
    for tool in (mcp_server.corpus_ingest, mcp_server.corpus_sources, mcp_server.corpus_delete):
        doc = (tool.__doc__ or "").lower()
        for claim in ("entit", "relationship"):
            assert claim not in doc, (
                f"{tool.__name__} description advertises '{claim}...' but corpus "
                "extraction always returns zero — see corpus/pipeline.py."
            )
