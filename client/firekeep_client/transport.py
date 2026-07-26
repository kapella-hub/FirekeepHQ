"""stdlib-only HTTP transport for the hook cores. NOT used by shim.py (which
needs mcp+httpx). All calls here are one-shot request/response -- no SSE or
other streaming.

verify=False builds an unverified SSL context and is legal ONLY when the
target URL is plain http (no TLS handshake occurs in that case, so the
unverified context is never actually used to validate a peer certificate).
The resolver's Task-5 TLS guard (firekeep_client.resolver._verify_for) is what
guarantees verify=False never reaches an https URL in practice -- this
module does not independently re-check the URL scheme against verify.
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from typing import Any

DEFAULT_TIMEOUT = 10.0


class TransportError(Exception):
    def __init__(self, msg, *, status: int | None = None) -> None:
        super().__init__(msg)
        self.status = status


def _build_ssl_context(verify: bool | str | ssl.SSLContext) -> ssl.SSLContext | None:
    if isinstance(verify, ssl.SSLContext):
        # Caller-built context (updater's scoped OS-trust context for release-host
        # fetches). Used verbatim: profile-derived bool/str verify below stays stock
        # stdlib, so office ca_path pinning is never widened.
        return verify
    if verify is False:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    if isinstance(verify, str):
        if verify.strip().lower() == "os":
            # resolver.OS_TRUST sentinel (`ca_path = os`): verify against the
            # operating-system trust store — where MDM-managed corporate CAs
            # live. truststore is a hard kit dependency; the stdlib fallback
            # (public roots only) turns a broken venv into a CLEAR certificate
            # error instead of an ImportError at request time.
            try:
                import truststore
                return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            except ImportError:
                return ssl.create_default_context()
        return ssl.create_default_context(cafile=verify)
    return ssl.create_default_context()


def _parse_sse_body(text: str, *, url: str) -> Any:
    """Extract the JSON payload from a COMPLETE one-shot SSE-framed body.

    FastMCP's streamable-HTTP endpoint answers a single `tools/call` POST with
    an event-stream body that is already fully buffered by the time we read it
    (this is NOT iterative streaming — that stays the shim's job). Events are
    blank-line separated; each carries one or more `data:` lines. A body may
    contain notification events before the response, so prefer the LAST frame
    that looks like a JSON-RPC response (has "jsonrpc" and "result"/"error"),
    falling back to the last parseable frame.
    """
    frames: list[Any] = []
    for event in text.replace("\r\n", "\n").split("\n\n"):
        data_lines = [ln[5:].lstrip() for ln in event.split("\n") if ln.startswith("data:")]
        if not data_lines:
            continue
        try:
            frames.append(json.loads("\n".join(data_lines)))
        except json.JSONDecodeError:
            continue
    if not frames:
        raise TransportError(f"POST {url} returned an SSE body with no JSON data frame")
    for frame in reversed(frames):
        if isinstance(frame, dict) and "jsonrpc" in frame and ("result" in frame or "error" in frame):
            return frame
    return frames[-1]


def _request(url, *, method, headers, body=None, timeout=DEFAULT_TIMEOUT, verify=True) -> Any:
    req_headers = dict(headers or {})
    req_headers.setdefault("Accept", "application/json")
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    ctx = _build_ssl_context(verify)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read()
            content_type = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        raw = e.read() or b""
        detail = raw.decode("utf-8", "replace")[:500] if raw else e.reason
        raise TransportError(f"{method} {url} failed: {e.code} {detail}", status=e.code) from e
    except TimeoutError as e:
        raise TransportError(f"{method} {url} timed out after {timeout}s") from e
    except urllib.error.URLError as e:
        raise TransportError(f"{method} {url} unreachable: {e.reason}") from e
    if not raw:
        return None
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise TransportError(f"{method} {url} returned a non-UTF-8 body: {e}") from e
    if "text/event-stream" in content_type:
        return _parse_sse_body(decoded, url=url)
    try:
        return json.loads(decoded)
    except json.JSONDecodeError as e:
        raise TransportError(f"{method} {url} returned invalid JSON body: {e}") from e


def get_json(url, *, headers, timeout=DEFAULT_TIMEOUT, verify=True) -> Any:
    return _request(url, method="GET", headers=headers, timeout=timeout, verify=verify)


def post_json(url, body, *, headers, timeout=DEFAULT_TIMEOUT, verify=True) -> Any:
    return _request(url, method="POST", headers=headers, body=body, timeout=timeout, verify=verify)
