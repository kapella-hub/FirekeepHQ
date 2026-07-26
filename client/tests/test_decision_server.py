"""Failing-first tests for the SP4 `firekeep-decision` local MCP board server (Task 5).

Drives the plain async cores `_run_decision_board` / `_run_decision_board_check`
directly (no live MCP client — the @mcp.tool() wrappers in main() are thin).

Binding constraints under test (per .superpowers/sdd/task-5-brief.md + the task
prompt's frozen constraints):
  - headless guard FIRST: FIREKEEP_DECISION_HEADLESS truthy -> inline text, no
    socket bound, Cortex NOT called.
  - transport success -> loopback server bound; a JSON POST to /board/<id>/answer
    (driven with urllib) makes the poll return the rendered answers; post_json is
    called with timeout == the configured client timeout (> the synth timeout).
  - transport failure -> local degraded board (spec from draft_questions,
    knowledge_found False); no crash.
  - poll expiry -> {status: pending, board_id, next: ...decision_board_check...}.
  - unknown board id -> {status: unknown}.
  - answer POST with a cross-site Origin -> 403 (answers not stored).
  - local (degraded) board_id is secrets.token_urlsafe (URL-safe charset+length).
"""
import functools
import json
import re
import threading
import time
import urllib.error
import urllib.request

import anyio
import pytest

from firekeep_client.decision import server
from tests.conftest import DEFAULT_PERSONAL


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _reset_boards():
    """Isolate the per-process board store between tests; shut any survivors."""
    server._BOARDS.clear()
    yield
    for board in list(server._BOARDS.values()):
        server._shutdown_board(board)
    server._BOARDS.clear()


@pytest.fixture(autouse=True)
def _no_browser(monkeypatch):
    """Never actually launch a browser during the suite (covers BOTH carriers:
    webbrowser and the darwin /usr/bin/open LaunchServices path)."""
    monkeypatch.setattr(server, "_open_browser", lambda url: True)


def _run(coro_func, *args, **kwargs):
    return anyio.run(functools.partial(coro_func, *args, **kwargs))


def _wait_for_board(board_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        board = server._BOARDS.get(board_id)
        if board is not None and getattr(board, "url", None):
            return board
        time.sleep(0.02)
    raise AssertionError(f"board {board_id!r} was never served within {timeout}s")


def _http(method, url, *, headers=None, data=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    return urllib.request.urlopen(req, timeout=5)


# --------------------------------------------------------------------------- #
# Headless guard (FIRST — no Cortex, no socket)                               #
# --------------------------------------------------------------------------- #


def test_headless_returns_inline_text_and_binds_no_socket(monkeypatch):
    monkeypatch.setenv("FIREKEEP_DECISION_HEADLESS", "1")

    def _must_not_call(*a, **k):
        raise AssertionError("Cortex must NOT be called on the headless path (guard is FIRST)")

    out = _run(
        server._run_decision_board,
        "should we restart the worker?",
        ["Restart the ingest worker now?"],
        post_json=_must_not_call,
    )
    assert isinstance(out, str)
    assert "Restart the ingest worker now?" in out
    # No server bound, nothing registered.
    assert server._BOARDS == {}


# --------------------------------------------------------------------------- #
# Transport success -> serve + answer round-trip                              #
# --------------------------------------------------------------------------- #


def test_transport_success_serves_and_poll_returns_answers(write_config, monkeypatch):
    write_config(active="personal", personal=DEFAULT_PERSONAL)
    monkeypatch.setattr(server, "_is_headless", lambda: False)
    monkeypatch.setenv("DECISION_POLL_SECONDS", "8")

    board_id = "success-board-abc"
    captured = {}

    def fake_post_json(url, body, *, headers, timeout, verify):
        captured["url"] = url
        captured["timeout"] = timeout
        captured["body"] = body
        return {
            "board_id": board_id,
            "context": body.get("context"),
            "questions": [
                {"id": "q0", "text": "Restart worker?", "knowledge_found": True,
                 "evidence": [], "suggested_answers": ["Yes"], "suggested_actions": []},
            ],
            "knowledge_found": True,
        }

    result_holder = {}

    def run_board():
        result_holder["result"] = _run(
            server._run_decision_board, "ctx text", ["Restart worker?"],
            post_json=fake_post_json,
        )

    t = threading.Thread(target=run_board)
    t.start()
    try:
        board = _wait_for_board(board_id)
        answer_url = board.url.rstrip("/") + "/answer"
        payload = json.dumps({
            "answers": {"q0": {"answer": "Yes restart it", "actions_confirmed": [], "skipped": False}}
        }).encode("utf-8")
        resp = _http("POST", answer_url, headers={"Content-Type": "application/json"}, data=payload)
        assert resp.status == 204
    finally:
        t.join(timeout=10)

    assert not t.is_alive(), "decision-board core hung past the poll deadline"
    result = result_holder["result"]
    assert isinstance(result, str)
    assert "Yes restart it" in result

    # post_json called against /decision/synthesize with the client timeout (> synth).
    assert captured["url"].endswith("/decision/synthesize")
    assert captured["timeout"] == server._ingest_client_timeout()
    assert server._ingest_client_timeout() > server._synth_timeout()
    assert captured["body"]["context"] == "ctx text"

    # Board was shut down + removed after the answer.
    assert board_id not in server._BOARDS


def test_serve_get_board_returns_html_with_csp_and_spec(monkeypatch):
    board_id = "get-board-xyz"
    spec = {"board_id": board_id, "questions": [{"id": "q0", "text": "hi"}]}
    board = server._Board(board_id, spec)
    srv, url = server._serve(board_id, board)
    board.server = srv
    server._BOARDS[board_id] = board
    try:
        resp = _http("GET", url)
        body = resp.read().decode("utf-8")
        assert resp.status == 200
        assert "<!DOCTYPE html>" in body
        assert resp.headers.get("Content-Security-Policy") == server.BOARD_CSP

        spec_resp = _http("GET", url.rstrip("/") + "/spec")
        assert spec_resp.headers.get("Content-Type", "").startswith("application/json")
        got = json.loads(spec_resp.read().decode("utf-8"))
        assert got["board_id"] == board_id
    finally:
        server._shutdown_board(board)


# --------------------------------------------------------------------------- #
# CSRF: cross-site Origin -> 403                                              #
# --------------------------------------------------------------------------- #


def test_answer_post_cross_site_origin_is_rejected():
    board_id = "csrf-board"
    spec = {"board_id": board_id, "questions": []}
    board = server._Board(board_id, spec)
    srv, url = server._serve(board_id, board)
    board.server = srv
    server._BOARDS[board_id] = board
    try:
        answer_url = url.rstrip("/") + "/answer"
        with pytest.raises(urllib.error.HTTPError) as ei:
            _http(
                "POST", answer_url,
                headers={
                    "Content-Type": "application/json",
                    "Origin": "https://evil.example",
                    "Sec-Fetch-Site": "cross-site",
                },
                data=b"{}",
            )
        assert ei.value.code == 403
        assert board.answers is None
    finally:
        server._shutdown_board(board)


def test_answer_post_non_json_content_type_is_rejected():
    board_id = "ctype-board"
    board = server._Board(board_id, {"board_id": board_id, "questions": []})
    srv, url = server._serve(board_id, board)
    board.server = srv
    server._BOARDS[board_id] = board
    try:
        with pytest.raises(urllib.error.HTTPError) as ei:
            _http("POST", url.rstrip("/") + "/answer",
                  headers={"Content-Type": "text/plain"}, data=b"nope")
        assert ei.value.code == 403
        assert board.answers is None
    finally:
        server._shutdown_board(board)


# --------------------------------------------------------------------------- #
# Transport failure -> local degraded board                                  #
# --------------------------------------------------------------------------- #


def test_transport_failure_builds_local_degraded_board(write_config, monkeypatch):
    write_config(active="personal", personal=DEFAULT_PERSONAL)
    monkeypatch.setattr(server, "_is_headless", lambda: False)
    monkeypatch.setenv("DECISION_POLL_SECONDS", "0.01")

    def boom(*a, **k):
        raise server.transport.TransportError("cortex unreachable")

    result = _run(
        server._run_decision_board, "ctx", ["Should we roll back?"],
        post_json=boom,
    )
    # Poll expires with no answer -> pending, but a degraded board was built + served.
    assert isinstance(result, dict)
    assert result["status"] == "pending"
    board_id = result["board_id"]
    board = server._BOARDS[board_id]
    assert board.spec.get("degraded") is True
    assert board.spec.get("knowledge_found") is False
    questions = board.spec["questions"]
    assert len(questions) == 1
    assert questions[0]["text"] == "Should we roll back?"
    assert questions[0]["knowledge_found"] is False


# --------------------------------------------------------------------------- #
# Poll expiry -> pending                                                      #
# --------------------------------------------------------------------------- #


def test_poll_expiry_returns_pending(write_config, monkeypatch):
    write_config(active="personal", personal=DEFAULT_PERSONAL)
    monkeypatch.setattr(server, "_is_headless", lambda: False)
    monkeypatch.setenv("DECISION_POLL_SECONDS", "0.01")

    def fake_post_json(url, body, *, headers, timeout, verify):
        return {"board_id": "pending-board", "questions": [], "knowledge_found": True}

    result = _run(server._run_decision_board, "ctx", [], post_json=fake_post_json)
    assert isinstance(result, dict)
    assert result["status"] == "pending"
    assert result["board_id"] == "pending-board"
    assert "decision_board_check" in result["next"]


# --------------------------------------------------------------------------- #
# Check: unknown board id                                                     #
# --------------------------------------------------------------------------- #


def test_check_unknown_board_returns_unknown():
    result = _run(server._run_decision_board_check, "never-existed")
    assert result["status"] == "unknown"


def test_check_pending_board_returns_pending(monkeypatch):
    monkeypatch.setenv("DECISION_POLL_SECONDS", "0.01")
    board_id = "known-pending"
    board = server._Board(board_id, {"board_id": board_id, "questions": []})
    srv, url = server._serve(board_id, board)
    board.server = srv
    server._BOARDS[board_id] = board
    try:
        result = _run(server._run_decision_board_check, board_id)
        assert result["status"] == "pending"
        assert result["board_id"] == board_id
    finally:
        server._shutdown_board(board)


# --------------------------------------------------------------------------- #
# board_id minting: token_urlsafe                                             #
# --------------------------------------------------------------------------- #


def test_local_degraded_board_id_is_token_urlsafe():
    spec = server._local_degraded_spec("ctx", ["q?"])
    board_id = spec["board_id"]
    assert isinstance(board_id, str)
    # secrets.token_urlsafe(16) -> ~22 chars, URL-safe base64 alphabet only.
    assert len(board_id) >= 20
    assert re.fullmatch(r"[A-Za-z0-9_-]+", board_id)
    assert "=" not in board_id and "+" not in board_id and "/" not in board_id


def test_local_degraded_spec_shape():
    spec = server._local_degraded_spec("some context", ["Q one?", "Q two?"])
    assert spec["degraded"] is True
    assert spec["knowledge_found"] is False
    assert [q["text"] for q in spec["questions"]] == ["Q one?", "Q two?"]
    assert all(q["knowledge_found"] is False for q in spec["questions"])


# --------------------------------------------------------------------------- #
# Rich embeds (sandboxed-iframe design, board 2b2a7b59 2026-07-14)            #
# --------------------------------------------------------------------------- #


def test_normalize_embeds_validates_and_clamps():
    out = server._normalize_embeds([
        {"html": "<!doctype html><b>chart</b>", "title": "T", "question": 0, "height": 50},
        {"html": "<svg></svg>"},
    ])
    assert out[0]["height"] == server._EMBED_HEIGHT_MIN  # clamped up from 50
    assert out[0]["question"] == 0
    assert out[1]["question"] is None
    assert out[1]["height"] == server._EMBED_HEIGHT_DEFAULT

    import pytest as _pytest
    with _pytest.raises(ValueError, match=r"embeds\[0\]\.html"):
        server._normalize_embeds([{"title": "no html"}])
    with _pytest.raises(ValueError, match="exceeds"):
        server._normalize_embeds([{"html": "x" * (server._EMBED_MAX_BYTES + 1)}])
    with _pytest.raises(ValueError, match=r"embeds\[0\]\.question"):
        server._normalize_embeds([{"html": "<p>x</p>", "question": "q0"}])


def test_attach_embeds_maps_questions_and_degrades_unknown_to_board_level():
    spec = {"questions": [{"id": "q0"}, {"id": "q1"}]}
    embeds = server._normalize_embeds([
        {"html": "<p>a</p>", "question": 0},
        {"html": "<p>b</p>"},              # board-level by omission
        {"html": "<p>c</p>", "question": 7},  # no q7 -> degrades to board-level
    ])
    server._attach_embeds(spec, embeds)
    assert [m["i"] for m in spec["embeds"]["by_question"]["q0"]] == [0]
    assert [m["i"] for m in spec["embeds"]["board"]] == [1, 2]
    # metadata only — the HTML itself must never ride the spec JSON
    assert "html" not in json.dumps(spec)
    # note: '<p>a</p>' would be caught by the html-key check only; assert content too
    assert "<p>" not in json.dumps(spec)


def test_serve_get_embed_returns_html_and_bounds_are_404(monkeypatch):
    board_id = "embed-board"
    spec = {"board_id": board_id, "questions": [{"id": "q0", "text": "hi"}]}
    embeds = server._normalize_embeds(
        [{"html": "<!doctype html><h1>viz</h1><script>1+1</script>", "title": "Viz"}])
    server._attach_embeds(spec, embeds)
    board = server._Board(board_id, spec, embeds=embeds)
    srv, url = server._serve(board_id, board)
    board.server = srv
    server._BOARDS[board_id] = board
    try:
        resp = _http("GET", url.rstrip("/") + "/embed/0")
        assert resp.status == 200
        assert resp.headers.get("Content-Type", "").startswith("text/html")
        # the sandbox is the isolation boundary — the embed doc must NOT get the board CSP
        assert resp.headers.get("Content-Security-Policy") is None
        body = resp.read().decode("utf-8")
        assert "<h1>viz</h1>" in body and "<script>" in body

        import urllib.error
        for bad in ("/embed/1", "/embed/-1", "/embed/x"):
            try:
                _http("GET", url.rstrip("/") + bad)
                raise AssertionError(f"{bad} should 404")
            except urllib.error.HTTPError as e:
                assert e.code == 404
    finally:
        server._shutdown_board(board)
        server._BOARDS.pop(board_id, None)


# --------------------------------------------------------------------------- #
# Browser launch (the "board does not launch" class, field report 2026-07-18) #
# --------------------------------------------------------------------------- #

# Captured at import time — BEFORE the autouse _no_browser fixture stubs
# server._open_browser — so these unit tests exercise the real opener (whose
# carriers are still monkeypatched per-test; no real browser launches).
_REAL_OPEN_BROWSER = server._open_browser


def test_open_browser_uses_launchservices_open_on_macos(monkeypatch):
    """webbrowser's osascript carrier is TCC-fragile under app-spawned MCP server
    processes — on darwin the opener must go straight to /usr/bin/open."""
    import types
    calls = []
    monkeypatch.setattr(server.sys, "platform", "darwin")
    monkeypatch.setattr(
        server.subprocess, "run",
        lambda argv, **kw: calls.append(argv) or types.SimpleNamespace(returncode=0, stderr=b""))

    def _no_webbrowser(*a, **k):
        raise AssertionError("webbrowser must not run when open(1) succeeded")

    monkeypatch.setattr(server.webbrowser, "open", _no_webbrowser)
    assert _REAL_OPEN_BROWSER("http://127.0.0.1:1/board/x") is True
    assert calls == [["/usr/bin/open", "http://127.0.0.1:1/board/x"]]


def test_open_browser_falls_back_and_reports_failure(monkeypatch):
    """Both carriers failing must return False AND leave a hooklog trace — a
    silent no-launch is precisely the bug this function exists to prevent."""
    import types
    failures = []
    monkeypatch.setattr(server.hooklog, "log_failure",
                        lambda hook, msg: failures.append((hook, msg)))
    monkeypatch.setattr(server.sys, "platform", "darwin")
    monkeypatch.setattr(server.subprocess, "run",
                        lambda argv, **kw: types.SimpleNamespace(returncode=1, stderr=b"tcc"))
    monkeypatch.setattr(server.webbrowser, "open", lambda *a, **k: False)
    assert _REAL_OPEN_BROWSER("http://127.0.0.1:1/board/x") is False
    assert failures, "browser-open failure must be hooklogged, never silent"


def test_open_browser_off_darwin_uses_webbrowser_only(monkeypatch):
    def _no_open1(*a, **k):
        raise AssertionError("/usr/bin/open is darwin-only")

    monkeypatch.setattr(server.sys, "platform", "linux")
    monkeypatch.setattr(server.subprocess, "run", _no_open1)
    monkeypatch.setattr(server.webbrowser, "open", lambda url: True)
    assert _REAL_OPEN_BROWSER("http://127.0.0.1:1/board/x") is True


def test_open_browser_raise_from_open1_still_tries_webbrowser(monkeypatch):
    """Adversarial-review finding (wf_0364042a): open(1) RAISING (TimeoutExpired on a
    hung LaunchServices, PermissionError in a sandboxed spawn) must still fall
    through to the webbrowser carrier — the pre-fix code always tried it."""
    def boom(*a, **k):
        raise server.subprocess.TimeoutExpired(cmd="/usr/bin/open", timeout=10)

    attempts = []
    monkeypatch.setattr(server.sys, "platform", "darwin")
    monkeypatch.setattr(server.subprocess, "run", boom)
    monkeypatch.setattr(server.webbrowser, "open", lambda url: attempts.append(url) or True)
    assert _REAL_OPEN_BROWSER("http://127.0.0.1:1/board/x") is True
    assert attempts == ["http://127.0.0.1:1/board/x"]


def test_open_browser_never_raises(monkeypatch):
    def boom(*a, **k):
        raise OSError("no exec")

    monkeypatch.setattr(server.sys, "platform", "darwin")
    monkeypatch.setattr(server.subprocess, "run", boom)
    monkeypatch.setattr(server.webbrowser, "open", boom)
    assert _REAL_OPEN_BROWSER("http://127.0.0.1:1/board/x") is False


# --------------------------------------------------------------------------- #
# Pending envelope: manual-recovery URL                                       #
# --------------------------------------------------------------------------- #


def test_pending_carries_board_url(write_config, monkeypatch):
    write_config(active="personal", personal=DEFAULT_PERSONAL)
    monkeypatch.setattr(server, "_is_headless", lambda: False)
    monkeypatch.setenv("DECISION_POLL_SECONDS", "0.01")

    def fake_post_json(url, body, *, headers, timeout, verify):
        return {"board_id": "url-board", "questions": [], "knowledge_found": True}

    result = _run(server._run_decision_board, "ctx", [], post_json=fake_post_json)
    assert result["status"] == "pending"
    assert result["board_url"].startswith("http://127.0.0.1:")
    assert "/board/url-board" in result["board_url"]


def test_pending_notes_manual_open_when_launch_failed(write_config, monkeypatch):
    write_config(active="personal", personal=DEFAULT_PERSONAL)
    monkeypatch.setattr(server, "_is_headless", lambda: False)
    monkeypatch.setenv("DECISION_POLL_SECONDS", "0.01")
    monkeypatch.setattr(server, "_open_browser", lambda url: False)  # launch FAILED

    def fake_post_json(url, body, *, headers, timeout, verify):
        return {"board_id": "manual-board", "questions": [], "knowledge_found": True}

    result = _run(server._run_decision_board, "ctx", [], post_json=fake_post_json)
    assert result["status"] == "pending"
    assert "could not be opened" in result["note"]
    assert result["board_url"] in result["note"]  # the human needs the exact URL


def test_check_pending_also_carries_board_url(monkeypatch):
    monkeypatch.setenv("DECISION_POLL_SECONDS", "0.01")
    board_id = "known-pending-url"
    board = server._Board(board_id, {"board_id": board_id, "questions": []})
    srv, url = server._serve(board_id, board)
    board.server = srv
    board.url = url
    server._BOARDS[board_id] = board
    try:
        result = _run(server._run_decision_board_check, board_id)
        assert result["status"] == "pending"
        assert result["board_url"] == url
    finally:
        server._shutdown_board(board)
