"""Per-request X-Session-Id injection and per-process session identity: the
shim attaches the current Bridge session id onto every proxied request — the
bridge tap's in-process captured id first, the agent-keyed disk stash only as
a restart/other-process fallback — so agent memory calls are attributed
without the agent ever passing session_id. The tap also injects its captured
id into no-arg ctx_complete_session/ctx_abandon_session, so two terminals
sharing one agent_id can no longer complete each other's sessions via the
server's shared active-session pointer.

The header is injected per-REQUEST (httpx.Auth), not as a static default,
because ctx_start_session happens AFTER the shim spawns and each JSON-RPC
message is its own HTTP POST (streamable-HTTP); a static default header would
be permanently absent for the whole connection.
"""
from __future__ import annotations

import json as _json

import anyio
import httpx
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCRequest, JSONRPCResponse

from firekeep_client import shim, state


def _run_auth_flow(auth, request):
    """Drive an httpx.Auth generator one step and return the request it yields."""
    gen = auth.auth_flow(request)
    return next(gen)


def _req():
    return httpx.Request("POST", "http://upstream/mcp")


def test_injects_x_session_id_when_stash_fresh(tmp_path, monkeypatch):
    """Disk stash as the explicit FALLBACK, not the authority. Reframed: the
    stash used to be the only session-id source, but it is one file per agent
    — two terminals sharing an agent_id share it, so it can hold the OTHER
    terminal's session. The tap's in-process id is now authoritative; the disk
    read survives only for a shim with no tap wired (non-bridge services, or
    a shim restarted mid-session whose tap never observed a start)."""
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(shim, "is_bypassed", lambda: False)
    state.write_session_stash("tester", session_id="sess-42")

    auth = shim._StashSessionAuth("tester")  # no tap -> disk fallback path
    out = _run_auth_flow(auth, _req())

    assert out.headers["X-Session-Id"] == "sess-42"


def test_auth_prefers_tap_in_memory_over_disk_stash(tmp_path, monkeypatch):
    """In-process authority: when the tap captured a session id, the auth must
    use it even though the shared disk stash holds a DIFFERENT id (i.e. another
    terminal's). Preferring the disk here is the deterministic-clobber trap:
    terminal B would inject terminal A's session id on every request."""
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(shim, "is_bypassed", lambda: False)
    state.write_session_stash("tester", session_id="sess-OTHER-TERMINAL")

    tap = shim._BridgeSessionTap("tester")
    tap.on_request(_tools_call(1, "ctx_start_session", {"goal": "g"}))
    tap.on_response(_tool_result(1, {"session_id": "sess-mine"}))

    auth = shim._StashSessionAuth("tester", tap=tap)
    out = _run_auth_flow(auth, _req())

    assert out.headers["X-Session-Id"] == "sess-mine"


def test_auth_with_tap_never_reads_disk_stash(tmp_path, monkeypatch):
    """A tap-wired auth whose tap captured nothing sends NO header — it must
    not fall back to the disk stash. A tap-present-but-empty shim is
    indistinguishable from fresh-terminal-B-with-no-session, and the stash is
    one file per agent: in the two-terminal case it holds the SIBLING's live
    session id, which a fallback would deliver to destructive bridge tools at
    above-pointer precedence — the exact 2026-08-11 clobber, made
    deterministic (external review 2026-08-12, blocker). The restart cost is
    accepted: a restarted single-terminal shim degrades to the server's
    pointer fallback."""
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(shim, "is_bypassed", lambda: False)
    state.write_session_stash("tester", session_id="sess-OTHER-TERMINAL")

    tap = shim._BridgeSessionTap("tester")  # never saw a start/resume
    auth = shim._StashSessionAuth("tester", tap=tap)
    out = _run_auth_flow(auth, _req())

    assert "X-Session-Id" not in out.headers


def test_tapless_auth_still_reads_disk_stash(tmp_path, monkeypatch):
    """Tap-less clients (cortex/relay/sentinel) keep the stash fallback:
    their header is non-destructive attribution, and after a shim restart the
    stash is the only channel left."""
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(shim, "is_bypassed", lambda: False)
    state.write_session_stash("tester", session_id="sess-42")

    auth = shim._StashSessionAuth("tester")  # no tap: non-bridge service
    out = _run_auth_flow(auth, _req())

    assert out.headers["X-Session-Id"] == "sess-42"


def test_no_header_when_stash_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(shim, "is_bypassed", lambda: False)

    auth = shim._StashSessionAuth("tester")
    out = _run_auth_flow(auth, _req())

    assert "X-Session-Id" not in out.headers


def test_no_header_when_only_briefing_id_stashed(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(shim, "is_bypassed", lambda: False)
    state.write_session_stash("tester", briefing_id="brf-1")  # no session_id yet

    auth = shim._StashSessionAuth("tester")
    out = _run_auth_flow(auth, _req())

    assert "X-Session-Id" not in out.headers


def test_no_header_when_bypassed(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(shim, "is_bypassed", lambda: True)  # live /personal on
    state.write_session_stash("tester", session_id="sess-42")

    auth = shim._StashSessionAuth("tester")
    out = _run_auth_flow(auth, _req())

    assert "X-Session-Id" not in out.headers  # no attribution while personal


def test_auth_flow_never_raises_on_stash_error(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(shim, "is_bypassed", lambda: False)
    monkeypatch.setattr(shim.state, "read_session_stash",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    auth = shim._StashSessionAuth("tester")
    out = _run_auth_flow(auth, _req())  # must not raise

    assert "X-Session-Id" not in out.headers


def test_build_client_wires_auth_when_agent_given(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    from firekeep_client.resolver import Endpoint

    ep = Endpoint(
        mcp_url="http://127.0.0.1:8100/mcp", rest_base="http://127.0.0.1:8100",
        headers={"X-Agent-Id": "tester"}, verify=False,
    )
    client = shim.build_client(ep, agent="tester")
    try:
        assert isinstance(client.auth, shim._StashSessionAuth)
    finally:
        # AsyncClient.aclose is a coroutine; closing the sync transport is enough here
        pass


def test_build_client_no_auth_without_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    from firekeep_client.resolver import Endpoint

    ep = Endpoint(
        mcp_url="http://127.0.0.1:8100/mcp", rest_base="http://127.0.0.1:8100",
        headers={}, verify=False,
    )
    client = shim.build_client(ep)
    assert client.auth is None


# --- bridge session tap (frame interception) --------------------------------


def _tools_call(rid, name, arguments):
    return SessionMessage(JSONRPCMessage(JSONRPCRequest(
        jsonrpc="2.0", id=rid, method="tools/call",
        params={"name": name, "arguments": arguments},
    )))


def _tool_result(rid, payload):
    return SessionMessage(JSONRPCMessage(JSONRPCResponse(
        jsonrpc="2.0", id=rid,
        result={"content": [{"type": "text", "text": _json.dumps(payload)}]},
    )))


def test_tap_injects_briefing_id_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    state.write_session_stash("tester", briefing_id="brf-9")
    tap = shim._BridgeSessionTap("tester")

    frame = _tools_call(1, "ctx_start_session", {"goal": "do a thing"})
    out = tap.on_request(frame)

    assert out.message.root.params["arguments"]["briefing_id"] == "brf-9"


def test_tap_does_not_override_explicit_briefing_id(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    state.write_session_stash("tester", briefing_id="brf-9")
    tap = shim._BridgeSessionTap("tester")

    frame = _tools_call(1, "ctx_start_session", {"goal": "g", "briefing_id": "explicit"})
    out = tap.on_request(frame)

    assert out.message.root.params["arguments"]["briefing_id"] == "explicit"


def test_tap_ignores_non_start_tool_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    state.write_session_stash("tester", briefing_id="brf-9")
    tap = shim._BridgeSessionTap("tester")

    frame = _tools_call(1, "memory_recall", {"task": "x"})
    out = tap.on_request(frame)

    assert "briefing_id" not in out.message.root.params["arguments"]


def test_tap_captures_session_id_from_start_response(tmp_path, monkeypatch):
    """Reframed for in-process authority: capture lands in the tap's OWN
    `_session_id` (the authoritative per-process identity — never shared with
    a second terminal's shim) AND still in the agent-keyed disk stash, which
    hook cores in other processes read and which serves as the restart
    fallback. The stash write is a deliberate side channel, not the source of
    truth it used to be."""
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    tap = shim._BridgeSessionTap("tester")

    tap.on_request(_tools_call(7, "ctx_start_session", {"goal": "g"}))
    tap.on_response(_tool_result(7, {"session_id": "sess-77", "created_at": "t"}))

    assert tap._session_id == "sess-77"  # in-process authority
    assert state.read_session_stash("tester")["session_id"] == "sess-77"  # shared fallback


def test_tap_response_without_matching_request_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    tap = shim._BridgeSessionTap("tester")

    # No prior on_request for id 7 → an unrelated tool's response must not stash.
    tap.on_response(_tool_result(7, {"session_id": "sess-77"}))
    assert state.read_session_stash("tester") is None


def test_tap_malformed_response_forwards_unchanged_and_stashes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    tap = shim._BridgeSessionTap("tester")

    tap.on_request(_tools_call(7, "ctx_start_session", {"goal": "g"}))
    bad = SessionMessage(JSONRPCMessage(JSONRPCResponse(
        jsonrpc="2.0", id=7, result={"content": [{"type": "text", "text": "not json {{{"}]},
    )))
    out = tap.on_response(bad)

    assert out is bad  # forwarded byte-identical
    assert state.read_session_stash("tester") is None


def test_tap_complete_clears_stash(tmp_path, monkeypatch):
    """Reframed for in-process authority: a completed end tool clears BOTH the
    tap's in-memory `_session_id` and the shared disk stash. The in-memory
    clear matters independently — a stale in-process id would be re-injected
    into the next end call and re-attributed onto every request; the stash
    clear keeps the shared restart-fallback file from resurrecting a finished
    session in other processes."""
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    tap = shim._BridgeSessionTap("tester")
    tap.on_request(_tools_call(8, "ctx_start_session", {"goal": "g"}))
    tap.on_response(_tool_result(8, {"session_id": "sess-77"}))
    assert tap._session_id == "sess-77"  # precondition: both stores populated

    tap.on_request(_tools_call(9, "ctx_complete_session", {"outcome": "done"}))
    tap.on_response(_tool_result(9, {"status": "completed"}))

    assert tap._session_id is None  # in-memory identity cleared
    assert state.read_session_stash("tester") is None  # shared fallback cleared


# --- per-process session identity: the two-terminals-one-agent_id clobber ---
#
# Live incident: two terminals on one machine share agent_id, hence ONE disk
# stash file and ONE server-side nb:active:{agent_id} pointer. Terminal B's
# no-arg ctx_complete_session resolved the shared pointer and completed
# terminal A's in-flight session. The fix is per-PROCESS identity: each shim's
# tap holds the session id it directly observed, injects it into no-arg end
# calls, and never trusts the shared file for that purpose.


def test_two_taps_sharing_agent_and_cache_keep_distinct_ids(tmp_path, monkeypatch):
    """Two _BridgeSessionTap instances = two terminals' shims, one agent id,
    one FIREKEEP_CACHE_DIR. Each must keep ITS OWN in-memory session id even
    though the last writer owns the shared disk stash."""
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    tap_a = shim._BridgeSessionTap("tester")
    tap_b = shim._BridgeSessionTap("tester")

    tap_a.on_request(_tools_call(1, "ctx_start_session", {"goal": "a"}))
    tap_a.on_response(_tool_result(1, {"session_id": "sess-A"}))
    tap_b.on_request(_tools_call(1, "ctx_start_session", {"goal": "b"}))
    tap_b.on_response(_tool_result(1, {"session_id": "sess-B"}))

    assert tap_a._session_id == "sess-A"
    assert tap_b._session_id == "sess-B"
    # The shared file necessarily holds only the last writer — that is exactly
    # why it cannot be the authority for either process.
    assert state.read_session_stash("tester")["session_id"] == "sess-B"


def test_tap_b_complete_leaves_tap_a_in_process_id_intact(tmp_path, monkeypatch):
    """Terminal B completing ITS session clears the shared stash (documented,
    unavoidable with an agent-keyed file) but must not disturb terminal A's
    in-process identity — A's later no-arg complete still targets sess-A."""
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    tap_a = shim._BridgeSessionTap("tester")
    tap_b = shim._BridgeSessionTap("tester")
    tap_a.on_request(_tools_call(1, "ctx_start_session", {"goal": "a"}))
    tap_a.on_response(_tool_result(1, {"session_id": "sess-A"}))
    tap_b.on_request(_tools_call(1, "ctx_start_session", {"goal": "b"}))
    tap_b.on_response(_tool_result(1, {"session_id": "sess-B"}))

    tap_b.on_request(_tools_call(2, "ctx_complete_session", {"outcome": "done"}))
    tap_b.on_response(_tool_result(2, {"status": "completed"}))

    assert tap_b._session_id is None
    assert tap_a._session_id == "sess-A"  # A's identity survives B's complete
    # ...and A's no-arg complete is aimed at A's own session, not the pointer.
    frame = _tools_call(3, "ctx_complete_session", {"outcome": "a-done"})
    out = tap_a.on_request(frame)
    assert out.message.root.params["arguments"]["session_id"] == "sess-A"


def test_tap_injects_captured_session_id_into_no_arg_complete(tmp_path, monkeypatch):
    """A ctx_complete_session the agent sends WITHOUT session_id gets this
    process's captured id injected, so Bridge never has to resolve the shared
    active-session pointer (which may point at another terminal's session)."""
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    tap = shim._BridgeSessionTap("tester")
    tap.on_request(_tools_call(1, "ctx_start_session", {"goal": "g"}))
    tap.on_response(_tool_result(1, {"session_id": "sess-9"}))

    out = tap.on_request(_tools_call(2, "ctx_complete_session", {"outcome": "done"}))

    assert out.message.root.params["arguments"]["session_id"] == "sess-9"


def test_tap_injects_captured_session_id_into_no_arg_abandon(tmp_path, monkeypatch):
    """ctx_abandon_session is the same destructive no-arg pointer resolution
    as complete; both end tools get the captured id."""
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    tap = shim._BridgeSessionTap("tester")
    tap.on_request(_tools_call(1, "ctx_resume_session", {"session_id": "sess-r"}))
    tap.on_response(_tool_result(1, {"session_id": "sess-r"}))

    out = tap.on_request(_tools_call(2, "ctx_abandon_session", {}))

    assert out.message.root.params["arguments"]["session_id"] == "sess-r"


def test_tap_no_captured_id_injects_nothing_into_complete(tmp_path, monkeypatch):
    """No in-process capture -> no injection, even when the shared disk stash
    holds an id (it may be the OTHER terminal's — injecting it would make the
    clobber deterministic). The server-side pointer fallback is the documented
    residual for this case."""
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    state.write_session_stash("tester", session_id="sess-OTHER-TERMINAL")
    tap = shim._BridgeSessionTap("tester")  # restarted shim: never saw a start

    out = tap.on_request(_tools_call(2, "ctx_complete_session", {"outcome": "done"}))

    assert "session_id" not in out.message.root.params["arguments"]


def test_tap_does_not_override_explicit_session_id_on_complete(tmp_path, monkeypatch):
    """An agent that names the session explicitly is exercising ownership the
    tap must not second-guess (e.g. completing a resumed older session)."""
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    tap = shim._BridgeSessionTap("tester")
    tap.on_request(_tools_call(1, "ctx_start_session", {"goal": "g"}))
    tap.on_response(_tool_result(1, {"session_id": "sess-9"}))

    out = tap.on_request(_tools_call(
        2, "ctx_complete_session", {"outcome": "done", "session_id": "sess-explicit"}
    ))

    assert out.message.root.params["arguments"]["session_id"] == "sess-explicit"


def test_tap_never_raises_on_garbage_frame(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    tap = shim._BridgeSessionTap("tester")

    class _Garbage:
        pass

    # No .message attribute at all — must be forwarded unchanged, no raise.
    g = _Garbage()
    assert tap.on_request(g) is g
    assert tap.on_response(g) is g


def test_tap_never_injects_since_into_an_agent_call(tmp_path, monkeypatch):
    """The client cannot observe the model's context, so it must never assert
    residency on the agent's behalf. Only the agent may pass `since`."""
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    tap = shim._BridgeSessionTap("tester")

    out = tap.on_request(_tools_call(4, "ctx_get_shadow", {}))

    # Behavioural: catches someone writing args["since"] directly.
    assert "since" not in out.message.root.params["arguments"]
    # Structural: catches set-membership drift, e.g. adding ctx_get_shadow to
    # _INJECT_TOOLS. Neither assertion implies the other — the injection
    # branch only ever writes briefing_id, so widening _INJECT_TOOLS would
    # leave the assertion above green while still asserting residency the
    # client cannot observe.
    assert "ctx_get_shadow" not in shim._BridgeSessionTap._INJECT_TOOLS


# --- pump-integration: tap must not perturb forwarding or dead-conn detection ---


def test_pump_forwards_frame_through_live_transform_and_captures():
    """End-to-end through _pump: a ctx_start_session response flows runtime-ward
    unchanged AND the tap captures its session_id (transform runs inline)."""
    import os
    import tempfile

    async def _run():
        with tempfile.TemporaryDirectory() as d:
            os.environ["FIREKEEP_CACHE_DIR"] = d
            try:
                tap = shim._BridgeSessionTap("tester")
                tap.on_request(_tools_call(3, "ctx_start_session", {"goal": "g"}))

                src_send, src_recv = anyio.create_memory_object_stream(10)
                dst_send, dst_recv = anyio.create_memory_object_stream(10)
                resp = _tool_result(3, {"session_id": "sess-abc"})
                await src_send.send(resp)
                await src_send.aclose()

                finishes = {}
                async with anyio.create_task_group() as tg:
                    tg.start_soon(shim._pump, src_recv, dst_send, tg, "http",
                                  finishes, tap.on_response)
                    with anyio.fail_after(5):
                        forwarded = await dst_recv.receive()
                    tg.cancel_scope.cancel()

                assert forwarded.message.root.result["content"][0]["text"]
                assert state.read_session_stash("tester")["session_id"] == "sess-abc"
            finally:
                os.environ.pop("FIREKEEP_CACHE_DIR", None)

    anyio.run(_run)


def test_bridge_still_detects_dead_upstream_with_transforms():
    """The identity transforms must not mask UpstreamDisconnected: the http
    source draining while the runtime is still attached still raises."""
    async def _run():
        tap = shim._BridgeSessionTap("tester")
        stdio_read_send, stdio_read_recv = anyio.create_memory_object_stream(10)
        stdio_write_send, stdio_write_recv = anyio.create_memory_object_stream(10)
        http_read_send, http_read_recv = anyio.create_memory_object_stream(10)
        http_write_send, http_write_recv = anyio.create_memory_object_stream(10)

        # Upstream read side closes immediately; runtime stdin never does.
        await http_read_send.aclose()

        try:
            with anyio.fail_after(5):
                await shim._bridge(stdio_read_recv, stdio_write_send,
                                   http_read_recv, http_write_send,
                                   req_transform=tap.on_request,
                                   resp_transform=tap.on_response)
            raised = False
        except shim.UpstreamDisconnected:
            raised = True
        assert raised

    anyio.run(_run)


def test_tap_does_not_inject_briefing_id_into_resume(tmp_path, monkeypatch):
    """bridge ctx_resume_session(session_id, agent_id) has NO briefing_id param;
    FastMCP rejects unexpected kwargs, so injecting it breaks every resume."""
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    state.write_session_stash("tester", briefing_id="brf-9")
    tap = shim._BridgeSessionTap("tester")

    frame = _tools_call(1, "ctx_resume_session", {"session_id": "sess-old"})
    out = tap.on_request(frame)

    assert "briefing_id" not in out.message.root.params["arguments"]


def test_tap_still_captures_session_id_from_resume_response(tmp_path, monkeypatch):
    """Resume must stay tracked for session_id capture even though it gets no
    briefing_id injection — resuming attributes subsequent calls too."""
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    tap = shim._BridgeSessionTap("tester")

    tap.on_request(_tools_call(5, "ctx_resume_session", {"session_id": "sess-r"}))
    tap.on_response(_tool_result(5, {"session_id": "sess-r", "created_at": "t"}))

    assert state.read_session_stash("tester")["session_id"] == "sess-r"
