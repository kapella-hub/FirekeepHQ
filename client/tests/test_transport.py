"""Tests for firekeep_client.transport — stdlib urllib HTTP for hook cores.

Happy paths run against a real threaded http.server (no mocking of the wire
protocol). Timeout propagation and SSL-context selection are verified by
monkeypatching urllib.request.urlopen / transport.ssl.create_default_context
so we can assert on the exact kwargs passed through, without needing a real
TLS-terminating server.
"""
from __future__ import annotations

import json
import socket
import ssl
import threading

import pytest
from http.server import BaseHTTPRequestHandler, HTTPServer

from firekeep_client import transport
from firekeep_client.transport import DEFAULT_TIMEOUT, TransportError, get_json, post_json


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - silence server noise in test output
        pass

    def _send(self, status: int, body: bytes, content_type: str | None = "application/json"):
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length) if length else b""

    def do_GET(self):
        if self.path == "/ok":
            self._send(200, json.dumps({"ok": True}).encode("utf-8"))
        elif self.path == "/empty":
            self._send(200, b"")
        elif self.path == "/badjson":
            self._send(200, b"not-json{")
        elif self.path == "/err":
            self._send(404, json.dumps({"error": "not found"}).encode("utf-8"))
        elif self.path == "/headers":
            self._send(200, json.dumps(dict(self.headers.items())).encode("utf-8"))
        else:
            self._send(404, b"")

    def do_POST(self):
        body = self._read_body()
        if self.path == "/echo":
            self._send(200, body or b"{}")
        elif self.path == "/headers":
            payload = dict(self.headers.items())
            payload["_body"] = body.decode("utf-8")
            self._send(200, json.dumps(payload).encode("utf-8"))
        elif self.path == "/err":
            self._send(500, json.dumps({"error": "boom"}).encode("utf-8"))
        else:
            self._send(404, b"")


@pytest.fixture
def server():
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base_url
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


class _FakeResponse:
    """Stand-in for the context-managed object returned by urlopen()."""

    def __init__(self, body: bytes):
        self._body = body
        self.headers = {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self) -> bytes:
        return self._body


# --- Happy paths (real threaded http.server) ---------------------------------


def test_get_json_success(server):
    assert get_json(f"{server}/ok", headers={"X-Agent-Id": "mogan"}) == {"ok": True}


def test_post_json_success(server):
    assert post_json(f"{server}/echo", {"a": 1}, headers={"X-Agent-Id": "mogan"}) == {"a": 1}


def test_get_json_empty_body_returns_none(server):
    assert get_json(f"{server}/empty", headers={}) is None


def test_get_json_invalid_json_raises_transport_error(server):
    with pytest.raises(TransportError):
        get_json(f"{server}/badjson", headers={})


def test_get_non_2xx_raises_transport_error_with_status(server):
    with pytest.raises(TransportError) as exc_info:
        get_json(f"{server}/err", headers={})
    assert exc_info.value.status == 404


def test_post_non_2xx_raises_transport_error_with_status(server):
    with pytest.raises(TransportError) as exc_info:
        post_json(f"{server}/err", {"x": 1}, headers={})
    assert exc_info.value.status == 500


def test_custom_headers_are_sent_and_accept_defaulted(server):
    result = get_json(f"{server}/headers", headers={"X-Agent-Id": "mogan", "X-Session-Id": "s-1"})
    assert result["X-Agent-Id"] == "mogan"
    assert result["X-Session-Id"] == "s-1"
    assert result["Accept"] == "application/json"


def test_explicit_accept_header_is_not_clobbered(server):
    result = get_json(f"{server}/headers", headers={"Accept": "application/vnd.custom+json"})
    assert result["Accept"] == "application/vnd.custom+json"


def test_post_sets_content_type_json_and_body(server):
    result = post_json(f"{server}/headers", {"k": "v"}, headers={})
    assert result["Content-Type"] == "application/json"
    assert json.loads(result["_body"]) == {"k": "v"}


def test_get_does_not_set_content_type(server):
    result = get_json(f"{server}/headers", headers={})
    assert "Content-Type" not in result


def test_connection_refused_raises_transport_error():
    # Bind-then-close to obtain a port nothing is listening on -> ECONNREFUSED.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(TransportError):
        get_json(f"http://127.0.0.1:{port}/nope", headers={}, timeout=1)


# --- TransportError -----------------------------------------------------------


def test_transport_error_default_status_is_none():
    err = TransportError("boom")
    assert err.status is None
    assert err.response_is_json is False
    assert str(err) == "boom"


def test_transport_error_carries_status():
    err = TransportError("boom", status=500, response_is_json=True)
    assert err.status == 500
    assert err.response_is_json is True


# --- timeout propagation (monkeypatched urlopen) -------------------------------


def test_timeout_kwarg_propagates_to_urlopen(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["timeout"] = timeout
        return _FakeResponse(b'{"ok": true}')

    monkeypatch.setattr(transport.urllib.request, "urlopen", fake_urlopen)
    get_json("http://example.invalid/ok", headers={}, timeout=3.25)
    assert captured["timeout"] == 3.25


def test_default_timeout_used_when_not_specified(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["timeout"] = timeout
        return _FakeResponse(b"{}")

    monkeypatch.setattr(transport.urllib.request, "urlopen", fake_urlopen)
    get_json("http://example.invalid/ok", headers={})
    assert captured["timeout"] == DEFAULT_TIMEOUT


def test_timeout_error_raises_transport_error(monkeypatch):
    def fake_urlopen(req, timeout=None, context=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(transport.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(TransportError):
        get_json("http://example.invalid/ok", headers={}, timeout=0.01)


# --- verify -> ssl context selection (monkeypatched ssl / urlopen) ------------


def test_verify_false_returns_unverified_context():
    ctx = transport._build_ssl_context(False)
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False


def test_verify_str_uses_create_default_context_with_cafile(monkeypatch):
    captured = {}

    def fake_create_default_context(*, cafile=None, **kwargs):
        captured["cafile"] = cafile
        return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    monkeypatch.setattr(transport.ssl, "create_default_context", fake_create_default_context)
    transport._build_ssl_context("/path/to/ca.pem")
    assert captured["cafile"] == "/path/to/ca.pem"


def test_verify_true_uses_default_context_without_cafile(monkeypatch):
    captured = {"called": False, "kwargs": None}

    def fake_create_default_context(*args, **kwargs):
        captured["called"] = True
        captured["kwargs"] = kwargs
        return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    monkeypatch.setattr(transport.ssl, "create_default_context", fake_create_default_context)
    transport._build_ssl_context(True)
    assert captured["called"] is True
    assert captured["kwargs"] == {}


def test_request_passes_built_context_through_to_urlopen(monkeypatch):
    sentinel_ctx = object()
    monkeypatch.setattr(transport, "_build_ssl_context", lambda verify: sentinel_ctx)
    captured = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["context"] = context
        return _FakeResponse(b"{}")

    monkeypatch.setattr(transport.urllib.request, "urlopen", fake_urlopen)
    get_json("http://example.invalid/ok", headers={}, verify="ca.pem")
    assert captured["context"] is sentinel_ctx


def test_build_ssl_context_passes_through_a_ready_context():
    """The updater hands a scoped truststore.SSLContext in via `verify`; transport must
    use it verbatim, not wrap or rebuild it."""
    import ssl
    from firekeep_client.transport import _build_ssl_context
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    assert _build_ssl_context(ctx) is ctx


def test_build_ssl_context_cafile_path_is_unchanged():
    """Office ca_path pinning must stay a stock stdlib context (the no-global-injection
    guarantee): a str verify builds via ssl.create_default_context(cafile=...)."""
    import ssl
    import firekeep_client.transport as transport
    recorded = {}
    orig = ssl.create_default_context

    def spy(*a, **kw):
        # Record the kwargs the real call would receive, but don't actually invoke `orig`
        # here: `ssl.create_default_context(cafile=...)` eagerly parses the file as PEM via
        # `load_verify_locations`, so a real (non-cert) file path -- e.g. this test module
        # itself -- raises SSLError before the assertion below is ever reached. The kwarg
        # capture is what this test pins; building a real context from `__file__` is not.
        recorded.update(kw)
        return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    transport.ssl.create_default_context = spy
    try:
        transport._build_ssl_context(__file__)  # any existing file path
    finally:
        transport.ssl.create_default_context = orig
    assert recorded.get("cafile") == __file__


def test_verify_os_returns_a_verifying_context():
    """The OS-trust sentinel ('os') must yield a VERIFYING context — whether from
    truststore (normal: it is a hard kit dependency) or the stdlib fallback, hostname
    checking and cert verification stay on. Anything less would be a silent MITM hole
    spelled 'os'."""
    ctx = transport._build_ssl_context("os")
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_verify_os_falls_back_to_stdlib_without_truststore(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_truststore(name, *a, **k):
        if name == "truststore":
            raise ImportError("truststore not installed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_truststore)
    ctx = transport._build_ssl_context("os")
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
