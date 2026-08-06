"""The two MCP surfaces that let an agent compile specs.

Round 1's answer to the cold start: 25 active skills exist and none has step
specs, so the procedure machinery is inert for every one of them. There is no
PATCH tool on this server, so without `skill_add_step_specs` an agent that reads
a skill and works out its matchers has nowhere to put them.

The tool SCHEMA is the product surface here — it is what the authoring agent
reads and acts on (the spec's Stage 1 argument: the client holds the session
context and the capable model, the server runs no LLM for this). So the
docstring's content is asserted, not just its existence: an agent told nothing
about the round-1 shell caveat will author a `file_glob` spec for a step that is
a shell command, and that step is then silently unobservable forever.

Proxy stubbing mirrors tests/test_mcp_knowledge.py exactly — patch
`app.mcp_server._get_client`, hand back an AsyncMock, inspect `call_args`.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _sig(name: str) -> inspect.Signature:
    """Signature of an MCP tool, through the decorator if there is one.

    conftest's `_FakeFastMCP.tool` returns the undecorated function, but a real
    fastmcp returns a FunctionTool wrapping it as `.fn` — so this reads the same
    parameters under either.
    """
    from app import mcp_server

    fn = getattr(mcp_server, name)
    return inspect.signature(getattr(fn, "fn", fn))


def _doc(name: str) -> str:
    from app import mcp_server

    fn = getattr(mcp_server, name)
    return (getattr(fn, "fn", fn).__doc__ or "")


def _ok_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(return_value=payload)
    resp.raise_for_status = MagicMock()
    return resp


# --------------------------------------------------------------------------
# Signatures — the plan's two anchor assertions.
# --------------------------------------------------------------------------


def test_skill_create_accepts_step_specs():
    assert "step_specs" in _sig("skill_create").parameters


def test_skill_add_step_specs_exists_and_takes_a_skill_id():
    params = _sig("skill_add_step_specs").parameters
    assert "skill_id" in params and "step_specs" in params


# --------------------------------------------------------------------------
# skill_create forwards the specs.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skill_create_forwards_step_specs_in_the_body():
    specs = [
        {"text": "bump the wheel", "kind": "file_glob",
         "pattern": "client/pyproject.toml", "load_bearing": True},
        {"text": "ask the customer", "kind": "unobservable"},
    ]
    with patch("app.mcp_server._get_client") as mock_get:
        client = AsyncMock()
        client.post = AsyncMock(return_value=_ok_response(
            {"id": "abc123", "trigger": "Publish a client release"}
        ))
        mock_get.return_value = client

        from app.mcp_server import skill_create
        await skill_create(
            trigger="Publish a client release",
            symptoms="a release is due",
            steps="1. bump the wheel",
            step_specs=specs,
        )

    body = client.post.call_args[1]["json"]
    assert body["step_specs"] == specs


@pytest.mark.asyncio
async def test_skill_create_omits_step_specs_when_the_author_supplied_none():
    """An ordinary skill is not a procedure and must not claim to be one.

    Mirrors how `project` is handled on this call: the key is absent, not
    present-and-null, so nothing downstream has to distinguish the two.
    """
    with patch("app.mcp_server._get_client") as mock_get:
        client = AsyncMock()
        client.post = AsyncMock(return_value=_ok_response({"id": "abc123", "trigger": "X"}))
        mock_get.return_value = client

        from app.mcp_server import skill_create
        await skill_create(trigger="X", symptoms="Y", steps="1. Z")

    assert "step_specs" not in client.post.call_args[1]["json"]


# --------------------------------------------------------------------------
# skill_add_step_specs — the cold-start path.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skill_add_step_specs_patches_the_existing_skill():
    specs = [{"text": "bump the wheel", "kind": "file_glob", "pattern": "client/pyproject.toml"}]
    with patch("app.mcp_server._get_client") as mock_get:
        client = AsyncMock()
        client.patch = AsyncMock(return_value=_ok_response(
            {"id": "sk-1", "trigger": "Publish a client release", "step_specs": specs}
        ))
        mock_get.return_value = client

        from app.mcp_server import skill_add_step_specs
        await skill_add_step_specs(skill_id="sk-1", step_specs=specs)

    client.patch.assert_awaited_once()
    args, kwargs = client.patch.call_args
    assert args[0] == "/skills/sk-1"
    assert kwargs["json"] == {"step_specs": specs}


@pytest.mark.asyncio
async def test_skill_add_step_specs_reports_stored_and_observable_counts():
    """The coverage number is the point: H2 says a meaningful fraction of every
    procedure is unobservable in round 1, and a coverage number the author
    cannot see is the silent-cap failure this repo bans elsewhere."""
    stored = [
        {"id": "a", "text": "bump the wheel", "kind": "file_glob",
         "pattern": "client/pyproject.toml", "load_bearing": True},
        {"id": "b", "text": "run the release script", "kind": "unobservable",
         "pattern": "", "load_bearing": False},
        {"id": "c", "text": "edit the changelog", "kind": "file_glob",
         "pattern": "CHANGELOG.md", "load_bearing": False},
    ]
    with patch("app.mcp_server._get_client") as mock_get:
        client = AsyncMock()
        client.patch = AsyncMock(return_value=_ok_response(
            {"id": "sk-1", "trigger": "Publish a client release", "step_specs": stored}
        ))
        mock_get.return_value = client

        from app.mcp_server import skill_add_step_specs
        result = await skill_add_step_specs(skill_id="sk-1", step_specs=stored)

    assert isinstance(result, str)
    assert "3" in result and "2" in result, result
    assert "observable" in result.lower(), result


@pytest.mark.asyncio
async def test_skill_add_step_specs_counts_what_the_server_stored_not_what_was_sent():
    """The confirmation is a report, not an echo.

    The server mints ids, force-clears a pattern on an `unobservable` spec and
    caps the list — so counting the REQUEST would tell the agent it created
    coverage the deployment does not have.
    """
    sent = [
        {"text": "one", "kind": "file_glob", "pattern": "a.py"},
        {"text": "two", "kind": "file_glob", "pattern": "b.py"},
    ]
    with patch("app.mcp_server._get_client") as mock_get:
        client = AsyncMock()
        client.patch = AsyncMock(return_value=_ok_response({
            "id": "sk-1",
            "trigger": "T",
            # The server stored only one, and it is not observable.
            "step_specs": [{"id": "a", "text": "one", "kind": "unobservable",
                            "pattern": "", "load_bearing": False}],
        }))
        mock_get.return_value = client

        from app.mcp_server import skill_add_step_specs
        result = await skill_add_step_specs(skill_id="sk-1", step_specs=sent)

    assert "2" not in result, f"reported the request, not the stored specs: {result}"
    assert "1" in result and "0" in result, result


@pytest.mark.asyncio
async def test_skill_add_step_specs_uses_the_shared_client_for_caller_key_forwarding():
    """Must go through _get_client(): that is what attaches _CallerKeyAuth so the
    caller's own X-API-Key reaches the REST layer (confused-deputy fix, SP1a)."""
    with patch("app.mcp_server._get_client") as mock_get:
        client = AsyncMock()
        client.patch = AsyncMock(return_value=_ok_response({"id": "sk-1", "step_specs": []}))
        mock_get.return_value = client

        from app.mcp_server import skill_add_step_specs
        await skill_add_step_specs(skill_id="sk-1", step_specs=[])

    mock_get.assert_awaited_once()


@pytest.mark.asyncio
async def test_skill_add_step_specs_surfaces_an_http_error():
    import httpx

    request = httpx.Request("PATCH", "http://cortex-api/skills/sk-1")
    response = httpx.Response(status_code=404, request=request, text="Skill not found")

    with patch("app.mcp_server._get_client") as mock_get:
        client = AsyncMock()
        client.patch = AsyncMock(
            side_effect=httpx.HTTPStatusError("404", request=request, response=response)
        )
        mock_get.return_value = client

        from app.mcp_server import skill_add_step_specs
        result = await skill_add_step_specs(skill_id="sk-1", step_specs=[])

    assert "404" in result


@pytest.mark.asyncio
async def test_skill_add_step_specs_surfaces_a_connection_error():
    import httpx

    request = httpx.Request("PATCH", "http://cortex-api/skills/sk-1")

    with patch("app.mcp_server._get_client") as mock_get:
        client = AsyncMock()
        client.patch = AsyncMock(side_effect=httpx.ConnectError("boom", request=request))
        mock_get.return_value = client

        from app.mcp_server import skill_add_step_specs
        result = await skill_add_step_specs(skill_id="sk-1", step_specs=[])

    assert "Error" in result


# --------------------------------------------------------------------------
# The schema text is the product surface.
# --------------------------------------------------------------------------


def test_skill_create_docstring_teaches_the_spec_shape():
    doc = _doc("skill_create")
    for token in ("step_specs", "file_glob", "unobservable", "pattern", "load_bearing"):
        assert token in doc, f"skill_create's schema never mentions {token}"


def test_skill_create_docstring_states_the_round_one_shell_caveat():
    """Spec §4 Stage 1: a shell step must be authored `unobservable` even though
    the command is precise, because round 1 does not observe run_command (H2).
    An agent not told this authors a file_glob that can never match."""
    doc = _doc("skill_create").lower()
    assert "shell" in doc, "the shell caveat is missing from the tool schema"
    assert "unobservable" in doc


def test_skill_create_docstring_warns_against_a_broad_glob():
    """H4: recognition is not intent, so a broad glob opens executions for work
    that has nothing to do with the procedure."""
    doc = _doc("skill_create")
    assert "*.py" in doc, "the schema does not warn that a catch-all glob is not a step"


def test_skill_add_step_specs_docstring_says_it_replaces_the_whole_list():
    """It PATCHes, and the PATCH path replaces `step_specs` wholesale (I3). An
    agent that believes it appends will send one spec and silently delete the
    rest."""
    doc = _doc("skill_add_step_specs").lower()
    assert "replace" in doc
