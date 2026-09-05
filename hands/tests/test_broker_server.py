import json, urllib.request, urllib.error
import pytest
from firekeep_hands import paths
from firekeep_hands.broker import server as server_module
from firekeep_hands.broker.permits import PermitStore
from firekeep_hands.broker.server import BrokerServer
from firekeep_hands.broker.client import BrokerClient


@pytest.fixture
def broker(isolated_home):
    store = PermitStore(ttl_s=60)
    srv = BrokerServer(store, chord="ctrl+alt+y", listeners={"chord": "unavailable", "phone": "offline"})
    port, token = srv.start()
    yield srv, store, port, token
    srv.stop()


def _req(port, token, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=2) as r: return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"null")


def test_health_requires_token_and_writes_broker_json(broker):
    srv, store, port, token = broker
    assert _req(port, "wrong", "GET", "/health")[0] == 401
    status, body = _req(port, token, "GET", "/health")
    assert status == 200 and body["ok"] is True and body["chord"] == "ctrl+alt+y"
    info = json.loads(paths.broker_info_path().read_text())
    assert info["port"] == port and info["token"] == token


def test_permit_lifecycle_over_http(broker):
    srv, store, port, token = broker
    status, p = _req(port, token, "POST", "/permits", {"challenge": "c", "title": "Send", "classes": ["send"], "task_id": "t", "step_index": 1})
    assert status == 201 and p["state"] == "pending"
    assert _req(port, token, "POST", "/permits/c/consume")[0] == 409
    store.decide("c", "approve", via="chord")           # what a listener does
    assert _req(port, token, "GET", "/permits/c")[1]["state"] == "approved"
    assert _req(port, token, "POST", "/permits/c/consume") == (200, {"state": "consumed"})
    assert _req(port, token, "POST", "/permits/c/consume")[0] == 409
    assert _req(port, token, "GET", "/permits/nope")[0] == 404


def test_client_from_disk_and_wait(broker):
    srv, store, port, token = broker
    c = BrokerClient.from_disk()
    assert c is not None
    c.request(challenge="w", title="x", classes=["send"], task_id="t", step_index=0)
    import threading, time
    threading.Timer(0.3, lambda: store.decide("w", "approve", via="chord")).start()
    assert c.wait("w", timeout_s=3)["state"] == "approved"
    assert c.consume("w") is True


def test_no_broker_json_means_no_client(isolated_home):
    assert BrokerClient.from_disk() is None


# --- additions -------------------------------------------------------------


def test_every_route_rejects_a_missing_or_wrong_bearer(broker):
    """The token gate runs before routing, so a bad token cannot even be used
    to enumerate which paths exist."""
    srv, store, port, token = broker
    for method, path, body in [
        ("GET", "/health", None),
        ("POST", "/permits", {"challenge": "x", "title": "t", "classes": [], "task_id": "t", "step_index": 0}),
        ("GET", "/permits/x", None),
        ("POST", "/permits/x/consume", None),
        ("GET", "/does-not-exist", None),
    ]:
        assert _req(port, "wrong-token", method, path, body)[0] == 401
    # no Authorization header at all
    req = urllib.request.Request(f"http://127.0.0.1:{port}/health", method="GET")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=2)
    assert exc.value.code == 401


def test_head_and_options_go_through_the_same_bearer_gate(broker):
    """Without these, the stdlib answers HEAD and OPTIONS with an
    unauthenticated 501 — a probe that tells an unauthenticated caller the
    broker is there."""
    srv, store, port, token = broker
    for method in ("HEAD", "OPTIONS"):
        assert _req(port, "wrong-token", method, "/health")[0] == 401
        assert _req(port, token, method, "/health")[0] == 405
    # HEAD must not carry a body, whatever the status
    req = urllib.request.Request(f"http://127.0.0.1:{port}/health", method="HEAD",
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        urllib.request.urlopen(req, timeout=2)
        body = b""
    except urllib.error.HTTPError as exc:
        body = exc.read()
    assert body == b""


def test_there_is_no_route_that_approves_a_permit(broker):
    """The whole point of the broker: nothing over HTTP can approve. Only a
    real chord or the phone bridge writes `approved`."""
    srv, store, port, token = broker
    _req(port, token, "POST", "/permits", {"challenge": "c", "title": "x", "classes": ["send"], "task_id": "t", "step_index": 0})
    for method, path in [("POST", "/permits/c/approve"), ("POST", "/permits/c/decide"),
                         ("PUT", "/permits/c"), ("POST", "/approve")]:
        assert _req(port, token, method, path, {"decision": "approve"})[0] in (404, 405, 501)
    assert store.get("c").state == "pending"


def test_oversized_and_malformed_bodies_are_refused(broker):
    srv, store, port, token = broker
    big = {"challenge": "c", "title": "x" * 20000, "classes": [], "task_id": "t", "step_index": 0}
    assert _req(port, token, "POST", "/permits", big)[0] == 413
    req = urllib.request.Request(f"http://127.0.0.1:{port}/permits", data=b"not json", method="POST",
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            status = r.status
    except urllib.error.HTTPError as e:
        status = e.code
    assert status == 400
    assert _req(port, token, "POST", "/permits", {"title": "no challenge"})[0] == 400


def test_health_reports_live_listener_state_and_pending_count(broker):
    """`listeners` is read live, so a listener thread that dies flips the
    doctor row without restarting the broker."""
    srv, store, port, token = broker
    assert _req(port, token, "GET", "/health")[1]["pending"] == 0
    store.request(challenge="c", title="x", classes=("send",), task_id="t", step_index=0)
    body = _req(port, token, "GET", "/health")[1]
    assert body["pending"] == 1 and body["listeners"]["chord"] == "unavailable"
    srv.listeners["chord"] = "active"
    assert _req(port, token, "GET", "/health")[1]["listeners"]["chord"] == "active"


def test_permit_json_carries_what_the_session_needs(broker):
    srv, store, port, token = broker
    _, p = _req(port, token, "POST", "/permits", {"challenge": "c", "title": "Send", "classes": ["send"], "task_id": "t", "step_index": 3})
    assert p["challenge"] == "c" and p["title"] == "Send" and p["classes"] == ["send"]
    assert p["task_id"] == "t" and p["step_index"] == 3 and p["via"] is None
    assert 0 < p["expires_in_s"] <= 60
    assert "token" not in p
    store.decide("c", "approve", via="phone")
    assert _req(port, token, "GET", "/permits/c")[1]["via"] == "phone"


def test_stop_removes_our_broker_json_and_releases_the_port(isolated_home):
    store = PermitStore(ttl_s=60)
    srv = BrokerServer(store, chord="ctrl+alt+y", listeners={"chord": "unavailable", "phone": "offline"})
    port, token = srv.start()
    assert paths.broker_info_path().exists()
    srv.stop()
    assert not paths.broker_info_path().exists()
    assert BrokerClient.from_disk() is None


def test_broker_json_is_written_privately(isolated_home):
    """0600 on POSIX; on Windows `state._private` shells out to icacls and the
    mode bits do not carry the same meaning, so only the POSIX case asserts."""
    import sys, stat
    store = PermitStore(ttl_s=60)
    srv = BrokerServer(store, chord="ctrl+alt+y", listeners={"chord": "unavailable", "phone": "offline"})
    srv.start()
    try:
        info = paths.broker_info_path()
        assert info.exists()
        if sys.platform != "win32":
            assert stat.S_IMODE(info.stat().st_mode) == 0o600
    finally:
        srv.stop()


def test_a_stalled_client_cannot_hold_a_handler_thread_forever(isolated_home, monkeypatch):
    """A connection that goes quiet mid-request must be dropped. Enough of
    them otherwise starve the broker of threads at the moment a human is
    trying to approve something."""
    import socket
    import time

    monkeypatch.setattr(server_module, "_HANDLER_TIMEOUT_S", 0.5)
    srv = BrokerServer(PermitStore(ttl_s=60), chord="ctrl+alt+y",
                       listeners={"chord": "unavailable", "phone": "off"})
    port, _token = srv.start()
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        sock.sendall(b"GET /health HTTP/1.0\r\n")   # headers deliberately never terminated
        started = time.monotonic()
        sock.settimeout(5)
        assert sock.recv(1024) == b""               # server gave up and closed
        assert time.monotonic() - started < 4
    finally:
        sock.close()
        srv.stop()


def test_a_dropped_connection_does_not_print_a_traceback(isolated_home, capsys):
    """The ordinary consequence of the timeout above firing. The stdlib
    prints a full traceback for it, which on a foreground broker reads like a
    crash when the connection is simply gone."""
    srv = BrokerServer(PermitStore(ttl_s=60), chord="ctrl+alt+y",
                       listeners={"chord": "unavailable", "phone": "off"})
    srv.start()
    try:
        capsys.readouterr()
        try:
            raise ConnectionAbortedError("client hung up")
        except ConnectionAbortedError:
            srv._httpd.handle_error(None, ("127.0.0.1", 1234))
        captured = capsys.readouterr()
        assert captured.err == "" and "Traceback" not in captured.out
    finally:
        srv.stop()


def test_the_shipped_handler_timeout_is_bounded():
    assert 0 < server_module._HANDLER_TIMEOUT_S <= 30
    assert server_module._Handler.timeout == server_module._HANDLER_TIMEOUT_S


def test_client_wait_gives_up_on_an_unknown_challenge(broker):
    srv, store, port, token = broker
    assert BrokerClient.from_disk().wait("never-requested", timeout_s=5)["state"] == "unknown"


def test_client_against_a_dead_broker_fails_closed(broker):
    srv, store, port, token = broker
    c = BrokerClient.from_disk()
    c.request(challenge="c", title="x", classes=["send"], task_id="t", step_index=0)
    srv.stop()
    assert c.get("c") is None
    assert c.consume("c") is False
    assert c.wait("c", timeout_s=5)["state"] == "unreachable"
