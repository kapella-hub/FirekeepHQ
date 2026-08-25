"""PR4 D4: the ctx_complete_session tool description is the ONE live
server->kit-agent channel (FastMCP serves the docstring AS the tool
description, and the local gateway forwards it verbatim to every kit agent,
including hookless Codex/generic ones). This asserts the description makes
honest task grading the stated default and explicitly marks failure/partial
as safe to report — reducing the optimism bias where agents default to
reporting success. See docs/superpowers/sdd/2026-08-25-outcome-truth-pr4-adoption/task-4-brief.md.
"""
import pytest


@pytest.mark.asyncio
async def test_tool_description_leads_with_task_result_as_the_default():
    """The FastMCP-served description (the channel that actually reaches
    kit agents) must name task_result up front as the expected default call
    shape, not just document it as an optional parameter."""
    from app import mcp_server
    from fastmcp import Client

    async with Client(mcp_server.mcp) as client:
        tools = await client.list_tools()
        tool = next(t for t in tools if t.name == "ctx_complete_session")

    description = tool.description
    assert description is not None
    # "Opening" = the prose before the Args: reference section, restricted to
    # its first two paragraphs -- lets the first line stay the short FastMCP
    # summary while still requiring task_result to lead the explanatory prose,
    # not be buried down in the per-param Args docs.
    prose = description.split("Args:")[0].strip()
    lead = "\n\n".join(prose.split("\n\n")[:2])
    assert "task_result" in lead, (
        "task_result should be named in the opening of the description, "
        "not buried in the Args section"
    )


def test_tool_description_marks_failure_and_partial_as_safe_to_report():
    """Optimism-bias reduction: the description must say, in plain terms,
    that reporting failure/partial is expected and useful -- not just that
    the values are accepted."""
    from app import mcp_server

    doc = mcp_server.ctx_complete_session.__doc__ or ""
    assert "failure" in doc and "partial" in doc
    # The docstring should explicitly frame an honest failure/partial as
    # useful/expected/safe, not merely enumerate the accepted values.
    safety_phrases = ("expected and useful", "safe to report", "honest failure")
    assert any(phrase in doc for phrase in safety_phrases), (
        "docstring should explicitly say reporting failure/partial is "
        "expected and safe, to counter optimism bias toward always "
        "reporting success"
    )


def test_tool_description_distinguishes_task_grade_from_rpc_success():
    """The grade is for the TASK, not for whether the RPC call itself
    succeeded -- this distinction is the crux of the nudge."""
    from app import mcp_server

    doc = mcp_server.ctx_complete_session.__doc__ or ""
    assert "task_evidence" in doc
    assert "RPC" in doc or "the call" in doc or "this call" in doc
