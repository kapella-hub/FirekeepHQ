"""FirekeepCortex MCP Server.

Exposes FirekeepCortex memory operations as MCP tools via Streamable HTTP transport.
Calls the FirekeepCortex REST API internally using httpx.
"""

from __future__ import annotations

import atexit
import asyncio
import logging

import httpx
from fastmcp import FastMCP

try:
    from fastmcp.server.dependencies import get_http_headers
except ImportError as exc:
    logging.getLogger(__name__).error(
        "fastmcp get_http_headers unavailable — header-based identity DISABLED; "
        "all MCP calls will default to unknown/default identity: %s",
        exc,
    )

    def get_http_headers(*_args, **_kwargs) -> dict[str, str]:
        """Fallback used when fastmcp does not provide get_http_headers."""
        return {}

from app.config import get_settings

# Served in the MCP `initialize` handshake. This is the ONLY instruction channel
# that needs no client-side adapter, so it is the only one that reaches Codex (which
# has no hook surface and no instruction file) and a user who has deleted the
# rendered block from their own instruction file.
#
# It exists because of a real failure: a user asked "deploy to my vps" and the agent
# said it did not know, while the answer sat in memory as a 100%-confidence first
# result. Storage and retrieval worked; nothing triggered them. Tool descriptions do
# not fix this -- memory_recall's description already states its trigger and still
# does not fire (same lesson as decision_board in client 0.1.11).
#
# Keep it SHORT. It is sent once per session, not per request, but it competes for
# attention with everything else in the handshake.
_INSTRUCTIONS = """Firekeep -- persistent team memory for agents.

Recall BEFORE answering, and treat not knowing as the trigger: if the user names a
host, IP, path, service, credential or convention you cannot name from the current
conversation ("my VPS", "our server"), or uses history words ("again", "still",
"last time", "how did we"), call memory_recall(task=<their request>) first. Never
claim you don't know about the user's own systems before calling it once. If a
result names a vault key, follow up with vault_retrieve.

Write as you go: ctx_update after each meaningful step, memory_learn the moment a
fix works (including what failed first), skill_create after a hard-won fix,
ctx_complete_session when done. Secrets go to vault_store, never memory_learn.
"""

mcp = FastMCP("FirekeepCortex", instructions=_INSTRUCTIONS)

_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


def _header_identity() -> tuple[str | None, str | None]:
    """Read (agent_id, session_id) from the MCP connection's HTTP headers.

    Reads X-Agent-Id / X-Session-Id. get_http_headers() never raises and
    returns {} outside a request context, so this is safe in unit tests
    and stdio transports.
    """
    headers = get_http_headers()
    return (headers.get("x-agent-id") or None, headers.get("x-session-id") or None)


def _resolve_identity(session_id: str, agent_id: str) -> tuple[str, str]:
    """Resolve identity: explicit param > connection header > "unknown".

    A per-connection header is a transport property of THIS client's
    connection, so it cannot leak attribution between clients sharing the
    process (unlike the module-global approach this guard originally
    protected against) — spec SP0 D3.
    """
    header_agent, header_session = _header_identity()
    resolved_session = (
        session_id
        if session_id and session_id != "unknown"
        else (header_session or "unknown")
    )
    resolved_agent = (
        agent_id if agent_id and agent_id != "unknown" else (header_agent or "unknown")
    )
    return resolved_session, resolved_agent


class _CallerKeyAuth(httpx.Auth):
    """Forward the MCP caller's X-API-Key onto proxied REST calls (SP1a §4.2).

    The front-gate validator rejects keyless MCP calls with 401 when auth is
    enabled, so every call reaching this proxy carries a valid caller key —
    cortex-mcp needs no key of its own. The confused deputy is eliminated,
    not re-pointed: a non-admin teammate's vault_retrieve presents THEIR key
    to require_scope("admin") and gets 403.

    FIREKEEP_INTERNAL_KEY is a fallback for server-initiated calls only (no
    request context to forward); it is scoped memory:write/session:read/
    eval:write — never admin.
    """

    def auth_flow(self, request):
        caller_key = get_http_headers().get("x-api-key")
        if not caller_key:
            caller_key = get_settings().FIREKEEP_INTERNAL_KEY
        if caller_key:
            request.headers["X-API-Key"] = caller_key
        yield request


async def _get_client() -> httpx.AsyncClient:
    """Return a shared httpx client, lazily initialized.

    No static X-API-Key header: the caller's key is attached per-request by
    _CallerKeyAuth (confused-deputy fix, SP1a §4.2). httpx runs auth_flow in
    the request's task, so fastmcp's get_http_headers() contextvar is visible.
    """
    global _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            settings = get_settings()
            _client = httpx.AsyncClient(
                base_url=settings.FIREKEEP_API_URL,
                timeout=settings.MCP_CLIENT_TIMEOUT,
                auth=_CallerKeyAuth(),
            )
    return _client


def _format_error(exc: httpx.HTTPStatusError) -> str:
    """Format an HTTP error into a human-readable message with suggestions."""
    status = exc.response.status_code
    if status == 503:
        return (
            "Error: FirekeepCortex API is unavailable. "
            "Suggestion: Check service health with memory_health tool."
        )
    if status == 401:
        return (
            "Error: Authentication failed. "
            "Suggestion: Check API key configuration."
        )
    if status == 422:
        try:
            detail = exc.response.json().get("detail", str(exc))
        except Exception:
            detail = exc.response.text
        return (
            f"Error: Invalid input \u2014 {detail}. "
            "Suggestion: Check parameter values."
        )
    return (
        f"Error: API returned {status}. "
        "Suggestion: Check FirekeepCortex logs."
    )


def _connection_error(exc: httpx.RequestError) -> str:
    """Format a connection/transport error."""
    api_url = get_settings().FIREKEEP_API_URL
    return (
        f"Error: Cannot connect to FirekeepCortex API at {api_url}. "
        "Suggestion: Ensure FirekeepCortex is running."
    )


@mcp.tool(output_schema=None)
async def memory_recall(
    task: str,
    tags: list[str] | None = None,
    top_k: int = 3,
    namespace: str = "default",
    session_id: str = "unknown",
    agent_id: str = "unknown",
    token_budget: int = 600,
    format: str = "synthesized",
    project: str | None = None,
) -> str:
    """Recall relevant memories for a task (graph + vector, Markdown output).

    Call before non-trivial tasks to surface past solutions and pitfalls.

    Args:
        task: What the agent is trying to do.
        tags: Optional filter tags.
        top_k: Max results (default 3).
        session_id, agent_id: For replay tracing.
        token_budget: Max tokens in response (default 600).
        format: "synthesized" (LLM-compressed) or "raw" (numbered list).
        project: Optional project scope for team memory.
    """
    try:
        session_id, agent_id = _resolve_identity(session_id, agent_id)
        client = await _get_client()
        body: dict = {
            "task": task,
            "top_k": top_k,
            "namespace": namespace,
            "token_budget": token_budget,
            "format": format,
        }
        if tags:
            body["tags"] = tags
        if project is not None:
            body["project"] = project
        headers: dict[str, str] = {}
        if session_id and session_id != "unknown":
            headers["X-Session-Id"] = session_id
        if agent_id and agent_id != "unknown":
            headers["X-Agent-Id"] = agent_id
        resp = await client.post("/memory/recall", json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        context_block = data["context_block"]
        if data.get("degraded"):
            return (
                "WARNING: vector search unavailable — results are graph-only; "
                "semantic matches may be missing.\n\n" + context_block
            )
        return context_block
    except httpx.HTTPStatusError as exc:
        return _format_error(exc)
    except httpx.RequestError as exc:
        return _connection_error(exc)


@mcp.tool()
async def memory_handoff(
    project: str,
    since_days: int = 7,
    agent_id: str = "unknown",
    session_id: str = "unknown",
) -> str:
    """Generate a team handoff summary for a project: what was done, open items, where to pick up.

    Fetches contributor activity and recent memories, then synthesizes a handoff
    summary via LLM. Falls back to raw recall results if LLM is unavailable.

    Args:
        project: Project name to scope the handoff.
        since_days: How many days back to look for contributor activity (default 7).
        agent_id, session_id: For replay tracing.
    """
    from datetime import datetime, timedelta, timezone

    session_id, agent_id = _resolve_identity(session_id, agent_id)
    client = await _get_client()
    settings = get_settings()

    since_dt = datetime.now(timezone.utc) - timedelta(days=since_days)
    since_iso = since_dt.isoformat()

    # Fetch contributors
    contributors_text = "(contributor data unavailable)"
    try:
        resp = await client.get(
            "/memory/contributors",
            params={"project": project, "since": since_iso},
        )
        resp.raise_for_status()
        contributors = resp.json()
        if contributors:
            lines = [
                f"- {c['contributor_id']}: {c['memory_count']} memories, "
                f"last active {c.get('last_active', 'unknown')}, "
                f"top domain: {c.get('top_domain', 'unknown')}"
                for c in contributors
            ]
            contributors_text = "Contributors:\n" + "\n".join(lines)
    except Exception:
        pass

    # Fetch recent memories for the project
    memories_text = "(memory recall unavailable)"
    try:
        body: dict = {
            "task": f"recent work on {project}",
            "project": project,
            "top_k": 10,
            "format": "raw",
            "namespace": "default",
            "token_budget": 800,
        }
        headers: dict[str, str] = {}
        if session_id and session_id != "unknown":
            headers["X-Session-Id"] = session_id
        if agent_id and agent_id != "unknown":
            headers["X-Agent-Id"] = agent_id
        resp = await client.post("/memory/recall", json=body, headers=headers)
        resp.raise_for_status()
        memories_text = resp.json().get("context_block", "")
    except Exception:
        pass

    combined_context = f"{contributors_text}\n\nRecent memories:\n{memories_text}"

    # Synthesize via LLM using a separate client (LLM may have a different base URL)
    handoff_prompt = (
        "Given the following recent memories and contributor activity, produce a handoff summary "
        "with three sections: (1) What was done, (2) Open items or incomplete work, "
        "(3) Where to pick up (file path or component if identifiable). Be specific. Under 300 words."
    )
    try:
        llm_headers: dict[str, str] = {"Content-Type": "application/json"}
        if getattr(settings, "LLM_API_KEY", ""):
            llm_headers["Authorization"] = f"Bearer {settings.LLM_API_KEY}"
        async with httpx.AsyncClient(timeout=60.0) as llm_client:
            synthesis_resp = await llm_client.post(
                f"{settings.LLM_BASE_URL}/chat/completions",
                json={
                    "model": settings.LLM_MODEL,
                    "messages": [
                        {"role": "system", "content": handoff_prompt},
                        {"role": "user", "content": combined_context},
                    ],
                    "temperature": 0.1,
                },
                headers=llm_headers,
            )
            synthesis_resp.raise_for_status()
            data = synthesis_resp.json()
            return data["choices"][0]["message"]["content"].strip()
    except Exception:
        return f"Handoff summary unavailable — raw recall results returned:\n\n{memories_text}"


@mcp.tool()
async def memory_learn(
    action: str,
    outcome: str,
    resolution: str | None = None,
    tags: list[str] | None = None,
    domain: str = "general",
    namespace: str = "default",
    session_id: str = "unknown",
    agent_id: str = "unknown",
    project: str | None = None,
) -> str:
    """Store a structured action/outcome pair in long-term memory.

    Call after completing a task. For raw event streams (CI logs, IDE events,
    monitoring), use memory_stream instead — it buffers for background LLM
    extraction rather than writing directly.

    Args:
        action: What the agent did.
        outcome: What resulted.
        resolution: How it was resolved, if applicable.
        tags: Categorization tags.
        domain: Knowledge domain (default "general").
        session_id, agent_id: For replay tracing.
        project: Project name for team memory scoping (optional).
    """
    try:
        session_id, agent_id = _resolve_identity(session_id, agent_id)
        client = await _get_client()
        body: dict = {"action": action, "outcome": outcome, "domain": domain, "namespace": namespace}
        if resolution is not None:
            body["resolution"] = resolution
        if tags:
            body["tags"] = tags
        if project is not None:
            body["project"] = project
        headers: dict[str, str] = {}
        if session_id and session_id != "unknown":
            headers["X-Session-Id"] = session_id
        if agent_id and agent_id != "unknown":
            headers["X-Agent-Id"] = agent_id
        resp = await client.post("/memory/learn", json=body, headers=headers)
        resp.raise_for_status()
        try:
            data = resp.json()
        except Exception:
            return (
                "WARNING: /memory/learn returned HTTP 200 but the response body "
                "could not be parsed — write status unknown. Check FirekeepCortex logs."
            )
        status = data.get("status", "")
        graph_id = data.get("graph_id")
        vector_id = data.get("vector_id")
        if status == "partial" and not vector_id:
            msg = (
                f"WARNING: partial write. Memory in domain '{domain}' was stored "
                f"WITHOUT a vector (graph_id={graph_id}) — it is NOT semantically "
                "recallable until backfilled."
            )
            if data.get("backfill_queued"):
                msg += (
                    " It was queued for automatic backfill — check memory_health "
                    "for backfill queue status."
                )
            else:
                msg += (
                    " No backfill was queued — re-run memory_learn once the "
                    "embedding service recovers."
                )
            return msg
        if status == "partial" and not graph_id:
            return (
                f"WARNING: partial write. Memory in domain '{domain}' was stored in "
                f"the vector store only (vector_id={vector_id}) — graph relationships "
                "(resolutions, supersession chains) were NOT recorded."
            )
        truncated = action[:80]
        suffix = "..." if len(action) > 80 else ""
        return (
            f"Stored memory in domain '{domain}': '{truncated}{suffix}'. "
            "Outcome and resolution are now available for future recall."
        )
    except httpx.HTTPStatusError as exc:
        return _format_error(exc)
    except httpx.RequestError as exc:
        return _connection_error(exc)


@mcp.tool()
async def memory_stream(
    source: str,
    payload: dict,
    tags: list[str] | None = None,
    namespace: str = "default",
    session_id: str = "unknown",
    agent_id: str = "unknown",
) -> str:
    """Queue a raw event for background LLM extraction (Sleep Cycle worker).

    For high-volume raw events (CI logs, IDE events, monitoring). If you
    already know the structured action/outcome, use memory_learn — it writes
    directly to both stores instead of queuing.

    Args:
        source: Origin of the event (e.g. "ci-pipeline").
        payload: Event data as key-value pairs.
        tags: Optional tags.
        session_id, agent_id: For replay tracing.
    """
    try:
        session_id, agent_id = _resolve_identity(session_id, agent_id)
        client = await _get_client()
        body: dict = {"source": source, "payload": payload, "namespace": namespace}
        if tags:
            body["tags"] = tags
        headers: dict[str, str] = {}
        if session_id and session_id != "unknown":
            headers["X-Session-Id"] = session_id
        if agent_id and agent_id != "unknown":
            headers["X-Agent-Id"] = agent_id
        resp = await client.post("/memory/stream", json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return f"Queued {data['queued']} event(s)"
    except httpx.HTTPStatusError as exc:
        return _format_error(exc)
    except httpx.RequestError as exc:
        return _connection_error(exc)


@mcp.tool()
async def memory_health() -> str:
    """Check FirekeepCortex connectivity (Neo4j, Qdrant, Redis). Use when other memory tools return errors."""
    try:
        client = await _get_client()
        resp = await client.get("/health")
        resp.raise_for_status()
        data = resp.json()
        lines = [f"Status: {data['status']}"]
        for name, svc in data.get("services", {}).items():
            detail = f" ({svc['detail']})" if svc.get("detail") else ""
            lines.append(f"  {name}: {svc['status']}{detail}")
        queue_depth = data.get("backfill_queue_depth")
        dlq_depth = data.get("backfill_dlq_depth")
        if queue_depth is not None:
            lines.append(f"  backfill queue: {queue_depth} pending")
        if dlq_depth is not None:
            marker = (
                " — ATTENTION: these memories are NOT semantically recallable "
                "until manually reprocessed"
                if dlq_depth > 0
                else ""
            )
            lines.append(f"  backfill DLQ: {dlq_depth}{marker}")
        return "\n".join(lines)
    except httpx.HTTPStatusError as exc:
        return _format_error(exc)
    except httpx.RequestError as exc:
        return _connection_error(exc)


# ---------------------------------------------------------------------------
# Replay Engine MCP Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def replay_timeline(
    session_id: str,
    event_type: str | None = None,
    limit: int = 20,
) -> str:
    """Trace event timeline for a session (memory ops, ctx updates, claims, coordination).

    Args:
        session_id: Bridge session ID.
        event_type: Optional filter (e.g. "memory_read", "ctx_update").
        limit: Max events (default 20).
    """
    try:
        client = await _get_client()
        params: dict = {"limit": limit}
        if event_type:
            params["event_type"] = event_type
        resp = await client.get(f"/replay/sessions/{session_id}/events", params=params)
        resp.raise_for_status()
        data = resp.json()
        events = data.get("events", [])
        if not events:
            return f"No trace events found for session {session_id}"
        lines = [f"## Trace Timeline — {session_id} ({data['total']} events)"]
        for ev in events:
            ts = ev.get("timestamp", "")[:19]
            et = ev.get("event_type", "")
            outcome = f" [{ev['outcome']}]" if ev.get("outcome") else ""
            lines.append(f"- `{ts}` **{et}**{outcome}")
            # Show key payload fields
            payload = ev.get("payload", {})
            for key in ("query", "category", "source", "resource_id", "channel", "action_summary"):
                if key in payload:
                    lines.append(f"  {key}: {str(payload[key])[:100]}")
        if data.get("has_more"):
            lines.append(f"\n*{data['total'] - len(events)} more events not shown*")
        return "\n".join(lines)
    except httpx.HTTPStatusError as exc:
        return _format_error(exc)
    except httpx.RequestError as exc:
        return _connection_error(exc)


@mcp.tool()
async def replay_inspect(event_id: str, brief: bool = False) -> str:
    """Full details of a single trace event.

    Args:
        event_id: Trace event ID.
        brief: If True, summarize payload as one-line-per-field (saves tokens
            when you only need shape, not exact values). Default False.
    """
    try:
        client = await _get_client()
        resp = await client.get(f"/replay/events/{event_id}")
        resp.raise_for_status()
        ev = resp.json()
        import json
        lines = [
            f"## Event {ev['id'][:12]}",
            f"- **Type:** {ev['event_type']}",
            f"- **Session:** {ev['session_id']}",
            f"- **Agent:** {ev['agent_id']}",
            f"- **Time:** {ev['timestamp']}",
        ]
        if ev.get("outcome"):
            lines.append(f"- **Outcome:** {ev['outcome']}")
        if ev.get("error"):
            lines.append(f"- **Error:** {ev['error']}")
        if ev.get("context_ref"):
            lines.append(f"- **Context snapshot:** {ev['context_ref']}")
        if ev.get("trace_links"):
            lines.append(f"- **Trace links:** {len(ev['trace_links'])}")
            for link in ev["trace_links"]:
                lines.append(f"  - {link['relationship']} → {link['target_event_id'][:12]} ({link['link_type']}, confidence={link['confidence']})")
        payload = ev.get("payload", {})
        if brief and isinstance(payload, dict):
            lines.append(f"- **Payload ({len(payload)} fields):**")
            for k, v in payload.items():
                lines.append(f"  - {k}: {str(v)[:80]}")
        else:
            lines.append(f"- **Payload:**\n```json\n{json.dumps(payload, indent=2)}\n```")
        return "\n".join(lines)
    except httpx.HTTPStatusError as exc:
        return _format_error(exc)
    except httpx.RequestError as exc:
        return _connection_error(exc)


@mcp.tool()
async def replay_context_at(session_id: str, event_id: str) -> str:
    """Reconstruct the agent's context at an event (nearest snapshot if no exact match).

    Args:
        session_id: Session containing the event.
        event_id: Event to inspect context at.
    """
    try:
        client = await _get_client()
        resp = await client.get(f"/replay/sessions/{session_id}/context-at/{event_id}")
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            return f"Error: {data['error']}"
        snapshot_type = data.get("snapshot_type", "none")
        if snapshot_type == "none":
            return "No context snapshot available for this event or session."
        lines = [f"## Context at event {event_id[:12]}"]
        lines.append(f"- **Snapshot type:** {snapshot_type}")
        if snapshot_type == "nearest":
            lines.append(f"- **Nearest snapshot event:** {data.get('nearest_event_id', '')[:12]}")
            lines.append(f"- **Events since snapshot:** {data.get('events_since_snapshot', '?')}")
        context = data.get("context", "")
        if context:
            lines.append(f"\n{context}")
        return "\n".join(lines)
    except httpx.HTTPStatusError as exc:
        return _format_error(exc)
    except httpx.RequestError as exc:
        return _connection_error(exc)


@mcp.tool()
async def replay_narrow(session_id: str, failure_event_id: str, max_depth: int = 10) -> str:
    """Rank likely contributing events for a failure (walks trace links backward).

    Use after finding a failure event to identify root-cause candidates —
    stale memory, bad retrieval, env change, etc.

    Args:
        session_id: Session with the failure.
        failure_event_id: The failure event ID.
        max_depth: Hops to walk back (default 10).
    """
    try:
        client = await _get_client()
        resp = await client.post(
            f"/replay/sessions/{session_id}/narrow",
            params={"failure_event_id": failure_event_id, "max_depth": max_depth},
        )
        resp.raise_for_status()
        data = resp.json()
        suspects = data.get("suspects", [])
        if not suspects:
            return f"No suspects found for failure event {failure_event_id[:12]}. The failure may have no trace links to follow."
        lines = [
            f"## Narrowing Results — {len(suspects)} suspects (walked {data['total_events_walked']} events)",
        ]
        for i, s in enumerate(suspects, 1):
            ev = s["event"]
            score = s["suspicion_score"]
            depth = s["depth"]
            lines.append(f"\n### #{i} (score={score:.3f}, depth={depth})")
            lines.append(f"- **Type:** {ev['event_type']}")
            lines.append(f"- **Time:** {ev['timestamp']}")
            payload = ev.get("payload", {})
            for key in ("query", "category", "source", "resource_id", "action_summary"):
                if key in payload:
                    lines.append(f"- **{key}:** {str(payload[key])[:150]}")
        return "\n".join(lines)
    except httpx.HTTPStatusError as exc:
        return _format_error(exc)
    except httpx.RequestError as exc:
        return _connection_error(exc)


@mcp.tool()
async def replay_summary(session_id: str) -> str:
    """Session trace summary: event counts by type, duration, failures, agents.

    Args:
        session_id: Session to summarize.
    """
    try:
        client = await _get_client()
        resp = await client.get(f"/replay/sessions/{session_id}/summary")
        resp.raise_for_status()
        data = resp.json()
        if data.get("event_count", 0) == 0:
            return f"No trace events for session {session_id}"
        lines = [
            f"## Session Trace Summary — {session_id}",
            f"- **Events:** {data['event_count']}",
        ]
        if data.get("duration_ms") is not None:
            lines.append(f"- **Duration:** {data['duration_ms']}ms")
        if data.get("first_event_at"):
            lines.append(f"- **Started:** {data['first_event_at'][:19]}")
        if data.get("last_event_at"):
            lines.append(f"- **Ended:** {data['last_event_at'][:19]}")
        if data.get("agents"):
            lines.append(f"- **Agents:** {', '.join(data['agents'])}")
        if data.get("has_failures"):
            lines.append("- **Has failures:** Yes")
        if data.get("event_type_counts"):
            lines.append("\n**Event types:**")
            for et, count in sorted(data["event_type_counts"].items()):
                lines.append(f"  - {et}: {count}")
        return "\n".join(lines)
    except httpx.HTTPStatusError as exc:
        return _format_error(exc)
    except httpx.RequestError as exc:
        return _connection_error(exc)


# ---------------------------------------------------------------------------
# Audit MCP Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def audit_memory(
    action: str | None = None,
    memory_chain_id: str | None = None,
    agent_id: str | None = None,
    limit: int = 20,
) -> str:
    """Memory access audit trail from replay events. Use to investigate stale memory or verify provenance.

    Args:
        action: "read" or "write" (default: both).
        memory_chain_id: Filter by chain ID.
        agent_id: Filter by agent.
        limit: Max entries (default 20).
    """
    try:
        client = await _get_client()
        params = {"limit": limit}
        if action:
            params["action"] = action
        if memory_chain_id:
            params["memory_chain_id"] = memory_chain_id
        if agent_id:
            params["agent_id"] = agent_id
        resp = await client.get("/audit/memory", params=params)
        resp.raise_for_status()
        data = resp.json()
        events = data.get("events", [])
        if not events:
            return "No memory access events found matching filters."
        lines = [f"## Memory Audit Trail ({len(events)} events)"]
        for ev in events:
            ts = (ev.get("timestamp") or "")[:19]
            et = ev["event_type"].replace("memory_", "")
            agent = ev.get("agent_id", "?")
            p = ev.get("payload", {})
            detail = p.get("query", p.get("action_summary", p.get("memory_chain_id", "")))
            if isinstance(detail, str):
                detail = detail[:80]
            lines.append(f"- `{ts}` **{et}** by {agent} — {detail}")
        return "\n".join(lines)
    except httpx.HTTPStatusError as exc:
        return _format_error(exc)
    except httpx.RequestError as exc:
        return _connection_error(exc)


# ---------------------------------------------------------------------------
# Auto-Eval MCP Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def eval_session(session_id: str) -> str:
    """Eval metrics for a completed session (tool success rate, failure rate, duration, etc).

    Auto-computed on session completion, available immediately after.

    Args:
        session_id: Session to evaluate.
    """
    try:
        client = await _get_client()
        resp = await client.get(f"/evals/sessions/{session_id}")
        resp.raise_for_status()
        data = resp.json()
        metrics = data.get("metrics", {})
        if not metrics:
            return f"No eval metrics for session {session_id}"
        lines = [f"## Session Eval — {session_id}"]
        lines.append(f"- **Trigger:** {data.get('trigger', '?')}")
        lines.append(f"- **Events:** {data.get('event_count', 0)}")
        if data.get("duration_ms"):
            lines.append(f"- **Duration:** {data['duration_ms']}ms")
        if data.get("has_failures"):
            lines.append(f"- **Failures:** {len(data.get('failure_event_ids', []))} events")
        lines.append("\n**Metrics:**")
        for name, value in sorted(metrics.items()):
            if isinstance(value, float) and value <= 1.0 and name.endswith("_rate"):
                lines.append(f"  - {name}: {value:.1%}")
            else:
                lines.append(f"  - {name}: {value}")
        return "\n".join(lines)
    except httpx.HTTPStatusError as exc:
        return _format_error(exc)
    except httpx.RequestError as exc:
        return _connection_error(exc)


@mcp.tool()
async def eval_summary(limit: int = 20) -> str:
    """Aggregate eval metrics across recent sessions (avg scores, failure rates, trends).

    Args:
        limit: Sessions to aggregate (default 20).
    """
    try:
        client = await _get_client()
        resp = await client.get("/evals/summary", params={"limit": limit})
        resp.raise_for_status()
        data = resp.json()
        total = data.get("total_sessions_evaluated", 0)
        if total == 0:
            return "No eval data yet. Evals are computed automatically when sessions complete."
        lines = [
            f"## Eval Summary — {total} sessions",
            f"- **Sessions with failures:** {data.get('sessions_with_failures', 0)}",
        ]
        avg = data.get("avg_metrics", {})
        if avg:
            lines.append("\n**Average Metrics:**")
            for name, value in sorted(avg.items()):
                ranges = data.get("metric_ranges", {}).get(name, {})
                range_str = ""
                if ranges:
                    range_str = f" (min={ranges.get('min', '?')}, max={ranges.get('max', '?')})"
                if isinstance(value, float) and value <= 1.0 and name.endswith("_rate"):
                    lines.append(f"  - {name}: {value:.1%}{range_str}")
                else:
                    lines.append(f"  - {name}: {value}{range_str}")
        return "\n".join(lines)
    except httpx.HTTPStatusError as exc:
        return _format_error(exc)
    except httpx.RequestError as exc:
        return _connection_error(exc)


# ---------------------------------------------------------------------------
# Vault MCP Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def vault_store(
    key: str,
    value: str,
    description: str | None = None,
    category: str | None = None,
    tags: list[str] | None = None,
) -> str:
    """Store an encrypted secret (Fernet). Use for real secrets only.

    For non-secret operational facts (VPS IPs, service URLs, hostnames) use
    memory_learn with namespace="infrastructure" instead — they don't need
    encryption and are easier to recall.

    Args:
        key: Unique secret name (alphanumeric, hyphens, underscores, dots).
        value: Secret value to encrypt.
        description: Optional description.
        category: Optional category (e.g. "ssh", "api_key").
        tags: Optional tags.
    """
    try:
        client = await _get_client()
        body: dict = {"key": key, "value": value}
        if description is not None:
            body["description"] = description
        if category is not None:
            body["category"] = category
        if tags is not None:
            body["tags"] = tags
        resp = await client.post("/vault/secrets", json=body)
        resp.raise_for_status()
        return f"Secret '{key}' stored securely in the vault."
    except httpx.HTTPStatusError as exc:
        return _format_error(exc)
    except httpx.RequestError as exc:
        return _connection_error(exc)


@mcp.tool()
async def vault_retrieve(key: str) -> str:
    """Retrieve and decrypt a secret from the vault.

    Args:
        key: Secret name.
    """
    try:
        client = await _get_client()
        resp = await client.get(f"/vault/secrets/{key}")
        resp.raise_for_status()
        data = resp.json()
        lines = [f"## Secret: {data['key']}"]
        lines.append(f"- **Value:** {data['value']}")
        if data.get("description"):
            lines.append(f"- **Description:** {data['description']}")
        if data.get("category"):
            lines.append(f"- **Category:** {data['category']}")
        if data.get("tags"):
            lines.append(f"- **Tags:** {', '.join(data['tags'])}")
        lines.append(f"- **Updated:** {data.get('updated_at', data.get('created_at', 'unknown'))}")
        return "\n".join(lines)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return f"Secret '{key}' not found in the vault."
        return _format_error(exc)
    except httpx.RequestError as exc:
        return _connection_error(exc)


@mcp.tool(output_schema=None)
async def vault_list(category: str | None = None) -> str:
    """List vault secrets (metadata only — names, categories, tags; never values).

    Args:
        category: Optional filter (e.g. "ssh", "api_key").
    """
    try:
        client = await _get_client()
        params: dict = {}
        if category:
            params["category"] = category
        resp = await client.get("/vault/secrets", params=params)
        resp.raise_for_status()
        data = resp.json()
        secrets = data.get("secrets", [])
        if not secrets:
            suffix = f" in category '{category}'" if category else ""
            return f"No secrets found in the vault{suffix}."
        lines = [f"## Vault Secrets ({len(secrets)} entries)"]
        for s in secrets:
            cat = f" [{s['category']}]" if s.get("category") else ""
            desc = f" — {s['description']}" if s.get("description") else ""
            lines.append(f"- **{s['key']}**{cat}{desc}")
        return "\n".join(lines)
    except httpx.HTTPStatusError as exc:
        return _format_error(exc)
    except httpx.RequestError as exc:
        return _connection_error(exc)


@mcp.tool()
async def vault_delete(key: str) -> str:
    """Delete a secret from the vault (irreversible).

    Args:
        key: Secret name.
    """
    try:
        client = await _get_client()
        resp = await client.delete(f"/vault/secrets/{key}")
        resp.raise_for_status()
        return f"Secret '{key}' deleted from the vault."
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return f"Secret '{key}' not found in the vault."
        return _format_error(exc)
    except httpx.RequestError as exc:
        return _connection_error(exc)


# ---------------------------------------------------------------------------
# Corpus — Business Knowledge Graph
# ---------------------------------------------------------------------------


@mcp.tool()
async def corpus_ingest(
    content: str,
    source_name: str = "Untitled",
    source_type: str = "text",
) -> str:
    """Ingest a business document (wiki, Jira, API docs, SOP) into the team corpus.

    Chunked and embedded; every chunk is searchable via memory_recall.
    For your own action/outcome notes use memory_learn instead — not for code
    or git history.

    Args:
        content: Document text.
        source_name: Label (e.g. "Provisioning Wiki").
        source_type: 'text', 'wiki', 'jira', or 'api-doc'.
    """
    try:
        client = await _get_client()
        body: dict = {
            "content": content,
            "source_name": source_name,
            "source_type": source_type,
        }
        resp = await client.post("/corpus/ingest", json=body, timeout=120.0)
        resp.raise_for_status()
        data = resp.json()

        status = data.get("extraction_status", "unknown")
        status_msg = {
            "queued": "Entity extraction queued (background worker will process)",
            "skipped": "Entity extraction skipped (no LLM configured)",
            "queue_failed": "Entity extraction queue failed (chunks still stored)",
        }.get(status, status)
        return (
            f"Ingested **{data['source_name']}**.\n"
            f"- Chunks stored: {data['chunks_stored']} (searchable via memory_recall immediately)\n"
            f"- Extraction: {status_msg}"
        )
    except httpx.HTTPStatusError as exc:
        return _format_error(exc)
    except httpx.RequestError as exc:
        return _connection_error(exc)


@mcp.tool()
async def corpus_sources() -> str:
    """List ingested business documents with their chunk counts."""
    try:
        client = await _get_client()
        resp = await client.get("/corpus/sources")
        resp.raise_for_status()
        data = resp.json()

        sources = data.get("sources", [])
        if not sources:
            return "No documents have been ingested yet."

        lines = [f"**{data['count']} ingested sources:**\n"]
        for s in sources:
            lines.append(
                f"- **{s['name']}** ({s.get('source_type', '?')}) — "
                f"{s.get('entities', 0)} entities, {s.get('chunks', 0)} chunks, "
                f"last ingested: {s.get('last_ingested', '?')}"
            )
        return "\n".join(lines)
    except httpx.HTTPStatusError as exc:
        return _format_error(exc)
    except httpx.RequestError as exc:
        return _connection_error(exc)


@mcp.tool()
async def corpus_delete(source_name: str) -> str:
    """Delete an ingested source and every chunk of it. Irreversible.

    Args:
        source_name: Source name as shown by corpus_sources.
    """
    try:
        client = await _get_client()
        resp = await client.delete(f"/corpus/sources/{source_name}", timeout=60.0)
        resp.raise_for_status()
        data = resp.json()
        return (
            f"Deleted **{source_name}**.\n"
            f"- Chunks removed: {data.get('chunks_deleted', '?')}\n"
            f"- Entities removed: {data.get('entities_deleted', '?')}"
        )
    except httpx.HTTPStatusError as exc:
        return _format_error(exc)
    except httpx.RequestError as exc:
        return _connection_error(exc)


# ---------------------------------------------------------------------------
# Knowledge — Docs -> Skills Front Door
# ---------------------------------------------------------------------------


@mcp.tool()
async def knowledge_ingest(
    content: str,
    source_name: str = "Untitled",
    source_type: str = "text",
) -> str:
    """Ingest a document through the docs->skills front door: corpus-ingests it
    synchronously (immediately searchable via memory_recall) and returns right
    away (202), while classification + per-procedure skill drafting run in the
    background. Track progress on the dashboard Knowledge tab.

    Distinct from corpus_ingest — use this when you want procedural content
    auto-drafted into reviewable skills; use corpus_ingest for plain
    corpus-only ingestion.

    Args:
        content: Document text.
        source_name: Label (e.g. "Provisioning Wiki").
        source_type: 'text', 'wiki', 'jira', or 'api-doc'.
    """
    try:
        client = await _get_client()
        body: dict = {
            "content": content,
            "source_name": source_name,
            "source_type": source_type,
        }
        resp = await client.post("/knowledge/ingest", json=body)  # normal MCP_CLIENT_TIMEOUT
        resp.raise_for_status()
        data = resp.json()

        lines = [
            f"Ingested **{data['corpus_source']}** to the corpus (searchable now).",
            f"- Status: {data.get('status', 'queued')} — classification + skill drafting are running in the background.",
            "- Check the dashboard Knowledge tab (or GET /knowledge/sources) for progress.",
        ]
        if data.get("note"):
            lines.append(f"- Note: {data['note']}")
        return "\n".join(lines)
    except httpx.HTTPStatusError as exc:
        return _format_error(exc)
    except httpx.RequestError as exc:
        return _connection_error(exc)


@mcp.tool()
async def knowledge_ingest_url(
    url: str,
    depth: int = 0,
) -> str:
    """Crawl a URL (SSRF-guarded, bounded depth/pages) and ingest each fetched
    page through the docs->skills front door — same pipeline as
    knowledge_ingest, but the content is fetched from the web instead of
    pasted in. Runs entirely in the background (crawl, corpus-ingest,
    classification, skill drafting); returns right away (202).

    Args:
        url: Page to crawl. Must resolve to a public host (SSRF-guarded —
            loopback/private/link-local/cloud-metadata addresses are rejected).
        depth: How many hops of same-site links to follow. 0 = just this page.
    """
    try:
        client = await _get_client()
        body: dict = {"url": url, "depth": depth}
        resp = await client.post("/knowledge/ingest-url", json=body)  # normal MCP_CLIENT_TIMEOUT
        resp.raise_for_status()
        data = resp.json()

        lines = [
            f"Crawl queued for **{data.get('url', url)}**.",
            f"- Status: {data.get('status', 'queued')} — crawling + ingest are running in the background.",
            "- Check the dashboard Knowledge tab (or GET /knowledge/sources) for progress.",
        ]
        if data.get("note"):
            lines.append(f"- Note: {data['note']}")
        return "\n".join(lines)
    except httpx.HTTPStatusError as exc:
        return _format_error(exc)
    except httpx.RequestError as exc:
        return _connection_error(exc)


@mcp.tool(output_schema=None)
async def skill_recall(
    task: str,
    project: str | None = None,
    namespace: str = "default",
    top_k: int = 3,
) -> str:
    """Retrieve relevant active skills for the current task.

    Call at session start or when approaching a complex problem to surface
    team-shared procedures from past breakthroughs.

    Args:
        task: What you are working on.
        project: Optional project scope.
        top_k: Max skills to return (default 3).
    """
    try:
        client = await _get_client()
        params: dict = {"status": "active", "limit": top_k}
        if project:
            params["project"] = project
        # Send the FULL task. The old five-word truncation existed only to make a
        # literal substring match against a trigger plausible — and it still almost
        # never matched. `q` is now a semantic query, where more of the task is
        # strictly more signal; `_embed` already caps input at EMBED_MAX_CHARS, so a
        # long task cannot 400 the embeddings endpoint.
        params["q"] = task
        # Opt into usage recording: a skill surfaced through this tool must advance
        # last_recalled_at, otherwise skill_staleness_pass keeps flagging
        # genuinely-used skills stale.
        params["record_recall"] = True
        resp = await client.get("/skills", params=params)
        resp.raise_for_status()
        skills = resp.json()
        if not skills:
            return "No relevant skills found."
        lines = ["## Relevant Skills\n"]
        for s in skills[:top_k]:
            lines.append(f"**{s.get('trigger', 'Skill')}**")
            lines.append(s.get("content", ""))
            lines.append("")
        return "\n".join(lines)
    except httpx.HTTPStatusError as exc:
        return _format_error(exc)
    except httpx.RequestError as exc:
        return _connection_error(exc)


@mcp.tool()
async def skill_create(
    trigger: str,
    symptoms: str,
    steps: str,
    gotchas: str = "",
    domain: str = "",
    project: str | None = None,
    status: str = "active",
    session_id: str = "unknown",
    agent_id: str = "unknown",
) -> str:
    """Create a skill manually from the current session.

    Use when you've solved a tricky problem and want to preserve the solution
    for your team. Also the client-side path for turning a document into skills
    on a deploy with no server generation model: classify the doc yourself and
    call this once per procedure you identify.

    Args:
        trigger: One sentence describing when this skill applies.
        symptoms: Observable signals that indicate this situation.
        steps: Step-by-step solution (numbered list as plain text).
        gotchas: Common mistakes or things that look like the fix but aren't.
        domain: Single word domain tag (e.g. neo4j, docker, python).
        project: Project this skill was discovered in.
        status: "active" (default, immediately recallable) or "draft" (lands in
            the human review queue, excluded from recall until approved) — use
            "draft" for skills drafted from an ingested document when you want a
            human to review before the team relies on them.
    """
    try:
        session_id, agent_id = _resolve_identity(session_id, agent_id)
        client = await _get_client()
        body = {
            "trigger": trigger, "symptoms": symptoms,
            "steps": steps, "gotchas": gotchas,
            "domain": domain, "status": status,
        }
        if project:
            body["project"] = project
        # Forward the resolved identity so POST /skills persists provenance —
        # previously resolved and DISCARDED, so every skill lost its origin
        # (wf_02954176; memory_learn already did this correctly).
        headers = {}
        if session_id and session_id != "unknown":
            headers["X-Session-Id"] = session_id
        if agent_id and agent_id != "unknown":
            headers["X-Agent-Id"] = agent_id
        resp = await client.post("/skills", json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return f"Skill created: {data.get('id')} — \"{data.get('trigger')}\""
    except httpx.HTTPStatusError as exc:
        return _format_error(exc)
    except httpx.RequestError as exc:
        return _connection_error(exc)


@mcp.tool(output_schema=None)
async def skill_list(
    status: str = "active",
    project: str | None = None,
    limit: int = 10,
) -> str:
    """List existing skills. Use to check before creating a duplicate.

    Args:
        status: Filter by lifecycle state: active, draft, deprecated.
        project: Optional project scope.
        limit: Max results (default 10).
    """
    try:
        client = await _get_client()
        params: dict = {"status": status, "limit": limit}
        if project:
            params["project"] = project
        resp = await client.get("/skills", params=params)
        resp.raise_for_status()
        skills = resp.json()
        if not skills:
            return f"No {status} skills found."
        lines = [f"## {status.capitalize()} Skills ({len(skills)})\n"]
        for s in skills:
            domain = f" [{s.get('domain')}]" if s.get("domain") else ""
            lines.append(f"- **{s.get('trigger', '?')}**{domain} (id: {s.get('id', '')[:8]})")
        return "\n".join(lines)
    except httpx.HTTPStatusError as exc:
        return _format_error(exc)
    except httpx.RequestError as exc:
        return _connection_error(exc)


@mcp.tool()
async def action_before(
    session_id: str,
    agent_id: str,
    action_type: str,
    target: str,
    preview: str = "",
    intent: str = "",
    expected_changes: list[str] | None = None,
    success_criteria: list[str] | None = None,
    confidence: float | None = None,
) -> dict:
    """Submit an action for policy + prediction evaluation.

    Returns a decision: 'allow' (proceed), 'rethink' (reflect and resubmit), 'block'.
    The MCP server stamps `adapter: 'mcp'` automatically.

    Args:
        session_id: Current session identifier.
        agent_id: Agent identifier (from FIREKEEP_AGENT_ID).
        action_type: One of: edit_file, run_command, call_api, delete, other.
        target: File path or resource being acted on.
        preview: Optional short preview of the change (max 2048 chars).
        intent: Optional description of what you intend to achieve.
        expected_changes: Optional list of file paths or artefacts expected to change.
        success_criteria: Optional list of observable conditions that confirm success.
        confidence: Optional confidence in the prediction (0.0–1.0).
    """
    payload: dict = {
        "session_id": session_id,
        "agent_id": agent_id,
        "adapter": "mcp",
        "action": {"type": action_type, "target": target},
    }
    if preview:
        payload["action"]["preview"] = preview
    if intent or expected_changes or success_criteria or confidence is not None:
        payload["prediction"] = {
            "intent": intent or "(unspecified)",
            "expected_changes": expected_changes or [],
            "success_criteria": success_criteria or [],
            "confidence": confidence if confidence is not None else 0.5,
        }
    try:
        client = await _get_client()
        response = await client.post("/agent/action/before", json=payload)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        return _format_error(exc)
    except httpx.RequestError as exc:
        return _connection_error(exc)


@mcp.tool()
async def action_after(
    action_id: str,
    success: bool,
    actual_changes: list[str] | None = None,
    observed_criteria_met: list[str] | None = None,
    deviation_notes: str = "",
) -> dict:
    """Report the outcome of a previously-submitted action.

    Returns the prediction_match_score if a prediction was associated with the action.

    Args:
        action_id: The action_id returned by action_before.
        success: Whether the action succeeded.
        actual_changes: List of file paths or artefacts that actually changed.
        observed_criteria_met: List of success criteria confirmed as met.
        deviation_notes: Optional notes on deviations from the prediction.
    """
    payload: dict = {
        "action_id": action_id,
        "outcome": {
            "success": success,
            "actual_changes": actual_changes or [],
            "observed_criteria_met": observed_criteria_met or [],
        },
    }
    if deviation_notes:
        payload["outcome"]["deviation_notes"] = deviation_notes
    try:
        client = await _get_client()
        response = await client.post("/agent/action/after", json=payload)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        return _format_error(exc)
    except httpx.RequestError as exc:
        return _connection_error(exc)


def _shutdown_client() -> None:
    """Close the httpx client on process exit."""
    global _client
    if _client is not None and not _client.is_closed:
        try:
            loop = asyncio.get_event_loop()
            if not loop.is_running():
                loop.run_until_complete(_client.aclose())
            # If loop is running during atexit, we can't safely close.
            # The process is exiting — let the OS reclaim the socket.
        except RuntimeError:
            pass
        _client = None


atexit.register(_shutdown_client)


if __name__ == "__main__":
    from auth.asgi import build_auth_middleware
    from auth.config import get_auth_settings

    _settings = get_settings()
    mcp.run(
        transport="http",
        host=_settings.MCP_HOST,
        port=_settings.MCP_PORT,
        stateless_http=True,
        middleware=build_auth_middleware(get_auth_settings(), skip_paths=("/health",)),
    )
