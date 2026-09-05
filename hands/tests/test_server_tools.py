"""The MCP adapter: the eight-tool surface, the JSON/image envelope, and the
single backend thread.

`dispatch` is deliberately a plain synchronous function so the envelope rules
(every error is a normal result the model can read and recover from; a
screenshot rides alongside as an image block) can be tested without an event
loop or a stdio pipe.
"""
from __future__ import annotations

import json
import threading

import mcp.types as t
import pytest

from firekeep_hands import server
from firekeep_hands.backends.base import UnsupportedBackend
from firekeep_hands.broker.permits import PermitStore
from test_session import FakeBrokerClient, FakeBrowser, build_session

SPEC_NAMES = [
    "hands_status",
    "hands_task_start",
    "hands_observe",
    "hands_find",
    "hands_act",
    "hands_request_permit",
    "hands_browser",
    "hands_task_end",
]


@pytest.fixture
def server_tools():
    return server.TOOLS


@pytest.fixture
def store():
    return PermitStore(ttl_s=60)


@pytest.fixture
def broker(store):
    return FakeBrokerClient(store)


def _session(app, broker):
    session = build_session(app, broker=broker)
    session.browser = FakeBrowser()
    return session


@pytest.fixture
def session(broker):
    return _session("Notepad", broker)


@pytest.fixture
def mail_session(broker):
    return _session("Mail", broker)


def _text(blocks) -> dict:
    assert isinstance(blocks[0], t.TextContent)
    return json.loads(blocks[0].text)


# -- the surface -----------------------------------------------------------


def test_tools_are_exposed_with_the_spec_names(server_tools):
    assert [tool.name for tool in server_tools] == SPEC_NAMES


def test_every_tool_carries_a_description_and_an_object_schema(server_tools):
    for tool in server_tools:
        assert isinstance(tool, t.Tool)
        assert tool.description and len(tool.description) > 40
        assert tool.inputSchema["type"] == "object"


def test_required_arguments_match_the_spec(server_tools):
    required = {tool.name: tool.inputSchema.get("required", []) for tool in server_tools}
    assert required["hands_status"] == []
    assert required["hands_task_start"] == ["goal"]
    assert required["hands_find"] == ["query"]
    assert required["hands_act"] == ["action"]
    assert required["hands_request_permit"] == ["challenge"]
    assert required["hands_browser"] == ["op"]
    assert required["hands_task_end"] == ["outcome"]


def test_perception_arguments_advertise_their_bounds(server_tools):
    by_name = {tool.name: tool.inputSchema["properties"] for tool in server_tools}
    assert by_name["hands_observe"]["max_nodes"] == {
        "type": "integer", "minimum": 1, "maximum": 200}
    assert by_name["hands_find"]["limit"] == {"type": "integer", "minimum": 1, "maximum": 50}
    assert by_name["hands_browser"]["limit"] == {"type": "integer", "minimum": 1, "maximum": 50}


def test_the_browser_schema_is_closed_and_declares_every_op(server_tools):
    browser = next(tool for tool in server_tools if tool.name == "hands_browser")
    assert browser.inputSchema["additionalProperties"] is False
    assert set(browser.inputSchema["properties"]["op"]["enum"]) == {
        "open", "tabs", "navigate", "read", "find", "click", "fill", "screenshot"}
    # Everything the session honours must be declared, or a closed schema
    # would reject an argument the implementation actually reads.
    for key in ("url", "ref", "text", "query", "limit", "tab", "permit"):
        assert key in browser.inputSchema["properties"], key


def test_act_describes_the_permit_loop(server_tools):
    act = next(tool for tool in server_tools if tool.name == "hands_act")
    assert "needs_permit" in act.description
    assert "hands_request_permit" in act.description
    assert "coordinates are refused" in act.description


# -- the envelope ----------------------------------------------------------


def test_a_full_task_runs_through_dispatch(session):
    started = _text(server.dispatch(session, "hands_task_start",
                                    {"goal": "save the note", "apps": ["Notepad"]}))
    assert started["ok"] and started["task_id"].startswith("h-")
    _text(server.dispatch(session, "hands_observe", {}))
    acted = _text(server.dispatch(session, "hands_act",
                                  {"action": {"kind": "invoke", "ref": "c1"}}))
    assert acted["ok"] and acted["route"] == "accessibility"
    ended = _text(server.dispatch(session, "hands_task_end",
                                  {"outcome": "done", "summary": "saved"}))
    assert ended["ok"] and ended["steps"] == 1


def test_a_hands_error_is_a_normal_result_not_a_protocol_error():
    """The model has to be able to read the failure and recover from it, so
    nothing below the tool layer is allowed to raise out of `dispatch`."""
    session = build_session(backend=UnsupportedBackend())
    _text(server.dispatch(session, "hands_task_start", {"goal": "x"}))
    result = _text(server.dispatch(session, "hands_observe", {}))
    assert result["ok"] is False
    assert result["error"].startswith("unsupported: ")


def test_an_unexpected_exception_is_also_a_normal_result(session):
    def boom(**_kwargs):
        raise RuntimeError("uiautomation exploded")

    _text(server.dispatch(session, "hands_task_start", {"goal": "x"}))
    session.backend.observe = boom
    result = _text(server.dispatch(session, "hands_observe", {}))
    assert result["ok"] is False and "uiautomation exploded" in result["error"]


def test_only_hands_status_answers_without_a_task(session):
    for name, arguments in [
        ("hands_observe", {}),
        ("hands_find", {"query": "save"}),
        ("hands_act", {"action": {"kind": "wait", "seconds": 0}}),
        ("hands_browser", {"op": "tabs"}),
    ]:
        result = _text(server.dispatch(session, name, arguments))
        assert result["error"] == "no_task: call hands_task_start first", name
    assert _text(server.dispatch(session, "hands_status", {}))["ok"] is True


def test_every_declared_schema_actually_validates(server_tools):
    """The SDK validates arguments against these before dispatch, so a schema
    that does not parse would fail every call to that tool at runtime."""
    import jsonschema

    for tool in server_tools:
        jsonschema.Draft202012Validator.check_schema(tool.inputSchema)


def test_an_unknown_tool_is_a_normal_result(session):
    result = _text(server.dispatch(session, "hands_teleport", {}))
    assert result["ok"] is False and "hands_teleport" in result["error"]


def test_a_screenshot_rides_alongside_as_an_image_block(session):
    _text(server.dispatch(session, "hands_task_start", {"goal": "x"}))
    blocks = server.dispatch(session, "hands_observe", {"detail": "screenshot"})
    assert len(blocks) == 2
    payload = _text(blocks)
    assert "screenshot_png" not in payload  # bytes never go into the JSON
    image = blocks[1]
    assert isinstance(image, t.ImageContent) and image.mimeType == "image/png"
    assert image.data  # base64


def test_a_browser_screenshot_is_an_image_block_too(session):
    _text(server.dispatch(session, "hands_task_start", {"goal": "x"}))
    blocks = server.dispatch(session, "hands_browser", {"op": "screenshot"})
    assert len(blocks) == 2 and isinstance(blocks[1], t.ImageContent)


def test_every_result_is_json_serialisable(session):
    """Anything `dispatch` returns is about to be `json.dumps`ed onto a pipe;
    a value that is not serialisable would kill the connection, not the call."""
    _text(server.dispatch(session, "hands_task_start", {"goal": "x", "apps": ["Notepad"]}))
    for name, arguments in [
        ("hands_status", {}),
        ("hands_observe", {"detail": "summary"}),
        ("hands_find", {"query": "save"}),
        ("hands_act", {"action": {"kind": "wait", "seconds": 0}}),
        ("hands_request_permit", {"challenge": "nope", "wait_s": 0}),
        ("hands_browser", {"op": "tabs"}),
        ("hands_task_end", {"outcome": "abandoned"}),
    ]:
        blocks = server.dispatch(session, name, arguments)
        json.loads(blocks[0].text)


def test_act_reports_needs_permit_through_the_envelope(mail_session, store):
    session = mail_session
    _text(server.dispatch(session, "hands_task_start", {"goal": "x", "apps": ["Mail"]}))
    _text(server.dispatch(session, "hands_observe", {}))
    result = _text(server.dispatch(session, "hands_act",
                                   {"action": {"kind": "invoke", "ref": "send"}}))
    assert result["needs_permit"]["classes"] == ["send"]
    challenge = result["needs_permit"]["challenge"]
    waited = _text(server.dispatch(session, "hands_request_permit",
                                   {"challenge": challenge, "wait_s": 0}))
    assert waited["state"] == "pending"
    store.decide(challenge, "approve", via="chord")
    _text(server.dispatch(session, "hands_observe", {}))
    ran = _text(server.dispatch(session, "hands_act",
                                {"action": {"kind": "invoke", "ref": "send"},
                                 "permit": challenge}))
    assert ran["ok"] is True


# -- the single backend thread ---------------------------------------------


def test_backend_work_all_runs_on_one_dedicated_thread():
    """Windows UI Automation binds to the first thread that touches it, so
    every backend call for the life of the process has to be that thread."""
    worker = server.Worker()
    try:
        first = worker.run(threading.get_ident)
        second = worker.run(threading.get_ident)
        assert first == second
        assert first != threading.get_ident()
        assert worker.run(lambda: threading.current_thread().name).startswith("hands-backend")
    finally:
        worker.shutdown()


def test_the_worker_propagates_the_return_value_and_the_exception():
    worker = server.Worker()
    try:
        assert worker.run(lambda: 41 + 1) == 42
        with pytest.raises(ValueError):
            worker.run(lambda: (_ for _ in ()).throw(ValueError("nope")))
    finally:
        worker.shutdown()
