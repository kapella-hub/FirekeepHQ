# Studio 0.5 prerequisite 1/3 — `replay_timeline` JSON format (server) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the `replay_timeline` MCP tool an opt-in machine-readable format, so Studio can build per-turn Keep receipts from each event's `id`, `agent_id` and `payload.memory_ids`.

**Architecture:** `replay_timeline` in `cortex/app/mcp_server.py` wraps `GET /replay/sessions/{id}/events`, which already returns a JSON `TimelineResponse`. The tool currently flattens that into Markdown and drops the fields receipts need. We add `format: "markdown" | "json"` (default `"markdown"`, byte-identical to today) and `offset` (paging). `"json"` returns the route's JSON verbatim. No REST route, model or replay storage changes.

**Tech Stack:** Python 3.11, fastmcp, httpx, pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-10-11-studio-keep-window-design.md` §3 (prerequisites) and §7.3 ("Server change (required)").

**Release order:** this plan (server tag) → plan 2 (`2026-10-11-studio-05-p2-kit.md`, client release) → plan 3 (`2026-10-11-studio-05-p3-studio.md`, Studio 0.5.0). The three are independent at the code level, and Studio degrades to "partial" receipts if either prerequisite is missing.

## Global Constraints

- Default behaviour is unchanged: `replay_timeline(session_id)` with no new arguments must send the identical HTTP request (`params={"limit": 20}`) and return the identical Markdown.
- `format` accepts exactly `"markdown"` or `"json"`; anything else returns an error string and makes no HTTP call.
- `"json"` returns `json.dumps(<route JSON>)` unchanged — no field renaming, no truncation.
- In `"json"` mode, HTTP and connection errors return `json.dumps({"error": "<message>"})`, so a JSON caller can tell "server error" (parseable, has `error`) from "old server" (not parseable as JSON, or no `events` key).
- No new REST endpoint (spec §11).

## Review Focus

1. **An old caller passing only `session_id`** — must get byte-identical Markdown and an identical request. Pinned by `test_default_markdown_is_unchanged` (Task 1).
2. **An empty session in JSON mode** — must return `{"events": [], "total": 0, ...}`, never the Markdown sentence "No trace events found…", or Studio would misread a new server as an old one. Pinned by `test_json_empty_session_is_json` (Task 1).
3. **A JSON caller hitting a 403 or unreachable server** — must get parseable JSON with `error`, not prose. Pinned by `test_json_mode_http_error_is_json` and `test_json_mode_connection_error_is_json` (Task 1).
4. **`offset=0`** — must not add an `offset` query parameter (keeps the default request identical). Pinned by `test_default_markdown_is_unchanged`.
5. **A typo'd format (`"JSON"`, `"md"`)** — must fail loudly without calling the server. Pinned by `test_unknown_format_is_rejected_without_a_call` (Task 1).

---

## File Structure

- Modify: `cortex/app/mcp_server.py` — `replay_timeline` (currently lines ~661–703): two new parameters, a JSON branch.
- Create: `cortex/tests/test_replay_timeline_json.py` — the tool's contract (uses the existing `_get_client` patch pattern from `cortex/tests/test_untested_fixes_from_the_sweep.py:146-156`).
- Modify: `docs/guides/replay-evals-patterns.md:17` — the MCP tools line.
- Modify: `docs/RELEASE-GITHUB.md` — release-notes entry for the server tag.

### Task 1: `replay_timeline` gains `format` and `offset`

**Files:**
- Modify: `cortex/app/mcp_server.py` (function `replay_timeline`, ~line 661)
- Create: `cortex/tests/test_replay_timeline_json.py`
- Modify: `docs/guides/replay-evals-patterns.md:17`

**Interfaces:**
- Consumes: `GET /replay/sessions/{session_id}/events?limit=&offset=&event_type=` → `TimelineResponse` JSON `{"session_id", "events": [TraceEventResponse...], "total", "has_more"}`, events oldest first (`replay/api.py:50-62`, `replay/reader.py:113`). Each event carries `id`, `session_id`, `agent_id`, `timestamp`, `event_type`, `payload` (`memory_read` payload has `query`, `trigger`, `result_count`, `memory_ids`; `memory_write` payload has `action_summary`, `memory_id` — `cortex/app/main.py:1337-1367, 1597-1612`).
- Produces: MCP tool `replay_timeline(session_id: str, event_type: str | None = None, limit: int = 20, offset: int = 0, format: str = "markdown") -> str`. Plan 3's Studio receipt builder calls it with `format="json", limit=200, offset=<cursor>`.

- [ ] **Step 1: Write the failing tests**

Create `cortex/tests/test_replay_timeline_json.py`:

```python
"""replay_timeline's machine-readable format (Studio 0.5 receipts, spec §7.3).

The Markdown view drops each event's id, agent_id and memory_ids — exactly
what a per-turn receipt needs — so format="json" returns the REST route's
TimelineResponse verbatim. The default must stay byte-identical."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

_EVENTS = {
    "session_id": "sess-1",
    "events": [
        {
            "id": "e1",
            "session_id": "sess-1",
            "agent_id": "agent-a",
            "timestamp": "2026-10-11T10:00:00.000000+00:00",
            "event_type": "memory_read",
            "payload": {"query": "deploy vps", "trigger": "prompt-hook",
                        "result_count": 2, "memory_ids": ["m1", "m2"]},
            "outcome": None,
        },
        {
            "id": "e2",
            "session_id": "sess-1",
            "agent_id": "agent-a-studio",
            "timestamp": "2026-10-11T10:00:01.000000+00:00",
            "event_type": "memory_write",
            "payload": {"action_summary": "fixed the tunnel", "memory_id": "m3"},
            "outcome": "success",
        },
    ],
    "total": 2,
    "has_more": False,
}


def _client_returning(payload):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=payload)
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    return client


async def _call(client, *args, **kwargs):
    from app import mcp_server

    with patch.object(mcp_server, "_get_client", new=AsyncMock(return_value=client)):
        return await mcp_server.replay_timeline(*args, **kwargs)


@pytest.mark.asyncio
async def test_default_markdown_is_unchanged():
    client = _client_returning(_EVENTS)
    out = await _call(client, "sess-1")
    client.get.assert_awaited_once_with(
        "/replay/sessions/sess-1/events", params={"limit": 20}
    )
    assert out == (
        "## Trace Timeline — sess-1 (2 events)\n"
        "- `2026-10-11T10:00:00` **memory_read**\n"
        "  query: deploy vps\n"
        "- `2026-10-11T10:00:01` **memory_write** [success]\n"
        "  action_summary: fixed the tunnel"
    )


@pytest.mark.asyncio
async def test_json_returns_the_route_payload_verbatim():
    client = _client_returning(_EVENTS)
    out = await _call(client, "sess-1", format="json", limit=200, offset=5,
                      event_type="memory_read")
    client.get.assert_awaited_once_with(
        "/replay/sessions/sess-1/events",
        params={"limit": 200, "offset": 5, "event_type": "memory_read"},
    )
    data = json.loads(out)
    assert data == _EVENTS
    assert data["events"][0]["payload"]["memory_ids"] == ["m1", "m2"]
    assert data["events"][1]["agent_id"] == "agent-a-studio"


@pytest.mark.asyncio
async def test_json_empty_session_is_json():
    empty = {"session_id": "s", "events": [], "total": 0, "has_more": False}
    out = await _call(_client_returning(empty), "s", format="json")
    assert json.loads(out) == empty


@pytest.mark.asyncio
async def test_unknown_format_is_rejected_without_a_call():
    client = _client_returning(_EVENTS)
    out = await _call(client, "sess-1", format="JSON")
    assert out.startswith("Error: format must be 'markdown' or 'json'")
    client.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_json_mode_http_error_is_json():
    request = httpx.Request("GET", "http://cortex/replay/sessions/s/events")
    response = httpx.Response(403, request=request, json={"detail": "nope"})
    resp = MagicMock()
    resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("403", request=request, response=response)
    )
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    data = json.loads(await _call(client, "s", format="json"))
    assert set(data) == {"error"}
    assert data["error"]


@pytest.mark.asyncio
async def test_json_mode_connection_error_is_json():
    client = AsyncMock()
    client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    data = json.loads(await _call(client, "s", format="json"))
    assert set(data) == {"error"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd cortex && pytest tests/test_replay_timeline_json.py -v`
Expected: `test_default_markdown_is_unchanged` PASSES (it pins today's behaviour — if it fails, fix the expected string to match today's output before changing any code, because that string is the compatibility contract). The other five FAIL with `TypeError: replay_timeline() got an unexpected keyword argument 'format'`.

- [ ] **Step 3: Implement**

In `cortex/app/mcp_server.py`, replace the `replay_timeline` signature, docstring and body up to the Markdown loop (`json` is already imported at the top of the module):

```python
@mcp.tool()
async def replay_timeline(
    session_id: str,
    event_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
    format: str = "markdown",
) -> str:
    """Trace event timeline for a session (memory ops, ctx updates, claims, coordination).

    Args:
        session_id: Bridge session ID.
        event_type: Optional filter (e.g. "memory_read", "ctx_update").
        limit: Max events (default 20).
        offset: Events to skip, oldest first (default 0) — for paging.
        format: "markdown" (default, for reading) or "json" — the timeline
            exactly as the REST route returns it, including each event's id,
            agent_id and full payload (e.g. memory_ids). For programs such as
            Firekeep Studio's per-turn Keep receipts.
    """
    if format not in ("markdown", "json"):
        return f"Error: format must be 'markdown' or 'json', got {format!r}"
    as_json = format == "json"
    try:
        client = await _get_client()
        params: dict = {"limit": limit}
        if offset:
            params["offset"] = offset
        if event_type:
            params["event_type"] = event_type
        resp = await client.get(f"/replay/sessions/{session_id}/events", params=params)
        resp.raise_for_status()
        data = resp.json()
        if as_json:
            return json.dumps(data)
        events = data.get("events", [])
        if not events:
            return f"No trace events found for session {session_id}"
```

Keep the rest of the Markdown loop exactly as it is. Replace the two `except` clauses at the end of the function with:

```python
    except httpx.HTTPStatusError as exc:
        message = _format_error(exc)
        return json.dumps({"error": message}) if as_json else message
    except httpx.RequestError as exc:
        message = _connection_error(exc)
        return json.dumps({"error": message}) if as_json else message
```

Note: the parameter name `format` shadows the builtin inside this function only. It is the name on the MCP wire, so keep it. Do not call `format(...)` inside the function.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd cortex && pytest tests/test_replay_timeline_json.py tests/test_untested_fixes_from_the_sweep.py -v`
Expected: all PASS.

- [ ] **Step 5: Update the guide**

In `docs/guides/replay-evals-patterns.md` line 17, change `` `replay_timeline` (default limit=20) `` to:

```markdown
`replay_timeline` (default limit=20; `offset` pages oldest-first; `format="json"` returns the REST `TimelineResponse` verbatim — event `id`, `agent_id`, full `payload` incl. `memory_ids` — and errors as `{"error": …}`; used by Firekeep Studio's per-turn Keep receipts)
```

- [ ] **Step 6: Run the cortex suite**

Run: `cd cortex && pytest tests/ -q -x`
Expected: no new failures compared with `main` (note any pre-existing failures in the PR description rather than fixing them here).

- [ ] **Step 7: Commit**

```bash
git add cortex/app/mcp_server.py cortex/tests/test_replay_timeline_json.py docs/guides/replay-evals-patterns.md
git commit -m "feat(cortex): replay_timeline format=json + offset for Studio receipts"
```

### Task 2: Ship it in a server release

**Files:**
- Modify: `docs/RELEASE-GITHUB.md` (the "Release notes" section, ~line 170)

**Interfaces:**
- Consumes: Task 1 merged to `main` (rebase-merge only — `main` has a linear-history rule).
- Produces: server tag `v1.6.2` (the next patch after `v1.6.1`), deployed to the VPS. Plan 3 records this version as the minimum for full receipts.

- [ ] **Step 1: Add the release note**

Under "## Release notes" in `docs/RELEASE-GITHUB.md`, add a server entry in the same style as the existing ones:

```markdown
- **server v1.6.2** — `replay_timeline` accepts `format="json"` (the REST
  `TimelineResponse` verbatim, with event ids, `agent_id` and `memory_ids`) and
  `offset`. The default Markdown output is unchanged. Needed for Firekeep Studio
  0.5 per-turn Keep receipts; older servers make Studio show "partial" receipts.
```

- [ ] **Step 2: Open the PR, wait for green CI, rebase-merge**

```bash
git push -u origin HEAD
gh pr create --title "feat(cortex): replay_timeline format=json + offset" --body "Studio 0.5 prerequisite 1/3 (spec docs/superpowers/specs/2026-10-11-studio-keep-window-design.md §7.3). Default output byte-identical; pinned by test_default_markdown_is_unchanged.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
gh pr checks --watch
gh pr merge --rebase
```

- [ ] **Step 3: Tag the server release from `main`**

```bash
git checkout main && git pull --ff-only
git tag v1.6.2 && git push origin v1.6.2
gh run watch "$(gh run list --workflow server-release.yml --limit 1 --json databaseId -q '.[0].databaseId')"
```

Expected: `server-release.yml` succeeds and publishes `server/v1.6.2/` and `server/latest/`.

- [ ] **Step 4: Deploy and verify live**

On the VPS (tailnet host `srv1143982`): run `firekeep-admin keys audit`, then `bash update.sh`. From a workstation agent session, call `replay_timeline(session_id=<your current session>, format="json", limit=5)`.
Expected: a JSON string whose `events[*]` each have `id`, `agent_id` and `payload`.

- [ ] **Step 5: Confirm the ids are feedback-able.** Take one id from a `memory_read` event's `payload.memory_ids` in that reply and call `memory_feedback(memory_ids=[<id>], useful=true, comment="studio 0.5 id check")`.
Expected: accepted, not "unknown memory id". `memory_ids` are the recall's `accessed_ids`, and Studio's receipt thumbs send exactly these, so if they are not feedback-able, plan 3's feedback path needs a different id source. Report it before plan 3 starts.
