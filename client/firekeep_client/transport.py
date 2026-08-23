"""stdlib-only HTTP transport for the hook cores. NOT used by shim.py (which
needs mcp+httpx). Every JSON call here is one-shot request/response -- no SSE
or other streaming. `get_file` is the one exception and is deliberately narrow:
it streams a response body to a file for `firekeep backup pull`, whose archives
are gigabytes and must never be read into memory.

verify=False builds an unverified SSL context. It is used for plain HTTP and
for exactly one HTTPS bootstrap: GET /enroll/anchor, whose returned CA bytes
are accepted only after their fingerprint matches the commitment inside the
out-of-band join code. Ordinary configured requests reach this module through
the resolver's TLS guard and never weaken verification.
"""
from __future__ import annotations

import errno
import json
import os
import socket
import ssl
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT = 10.0
# A separate, far larger default for `get_file`: DEFAULT_TIMEOUT is urllib's
# per-socket-operation timeout, and a multi-gigabyte archive over a home uplink
# spends legitimate minutes between reads. Applying the JSON budget to it would
# abort every real download.
DEFAULT_FILE_TIMEOUT = 300.0
_CHUNK = 1024 * 256


class TransportError(Exception):
    def __init__(
        self,
        msg,
        *,
        status: int | None = None,
        response_is_json: bool = False,
        category: str | None = None,
    ) -> None:
        super().__init__(msg)
        self.status = status
        # Enrollment must distinguish an old server's HTML/plain-text 404 from
        # the current route's structured "unknown ticket" 404.  Keep the body
        # in the human-readable message as before; expose only its format here.
        self.response_is_json = response_is_json
        # Structured failure class assigned AT WRAP TIME (field-failure spec,
        # "The mapper's input contract"): the report mapper consumes only
        # (category, status) and never re-traverses causes or reads messages.
        self.category = category


_CATEGORY_ERRNOS = {
    errno.ECONNREFUSED: "connection-refused",
    errno.ENETUNREACH: "network-unreachable",
    errno.EHOSTUNREACH: "network-unreachable",
}


def _failure_category(exc: Exception) -> str | None:
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "tls-verify-failed"
    if isinstance(reason, socket.gaierror):
        return "dns-failure"
    if isinstance(reason, TimeoutError):
        return "timeout"
    if isinstance(reason, OSError):
        return _CATEGORY_ERRNOS.get(reason.errno)
    return None


def _build_ssl_context(verify: bool | str | ssl.SSLContext) -> ssl.SSLContext | None:
    if isinstance(verify, ssl.SSLContext):
        # Caller-built context (updater's scoped OS-trust context for release-host
        # fetches). Used verbatim: config-derived bool/str verify below stays stock
        # stdlib, so [server] ca_path verification is never widened.
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


def _as_transport_error(exc: Exception, *, method: str, url: str, timeout: float):
    """The ONE translation from urllib's exception zoo into TransportError.

    Shared by `_request` and `get_file` so a streamed download reports failure
    in exactly the wording and with exactly the `.status` a JSON call does --
    `backup link` reads `.status == 403` to tell "this key is not admin" from
    "the server is down", and that distinction must not depend on which
    transport function happened to make the call.
    """
    if isinstance(exc, urllib.error.HTTPError):
        raw = exc.read() or b""
        detail = raw.decode("utf-8", "replace")[:500] if raw else exc.reason
        response_is_json = False
        if raw:
            try:
                json.loads(raw.decode("utf-8"))
                response_is_json = True
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
        return TransportError(
            f"{method} {url} failed: {exc.code} {detail}",
            status=exc.code,
            response_is_json=response_is_json,
        )
    if isinstance(exc, TimeoutError):
        return TransportError(f"{method} {url} timed out after {timeout}s",
                              category="timeout")
    return TransportError(f"{method} {url} unreachable: {exc.reason}",
                          category=_failure_category(exc))


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
    except (urllib.error.URLError, TimeoutError) as e:
        raise _as_transport_error(e, method=method, url=url, timeout=timeout) from e
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


def get_file(url, dest, *, headers, timeout=DEFAULT_FILE_TIMEOUT, verify=True) -> int:
    """Stream a GET response body into `dest`; return the bytes written.

    Same TLS context, same TransportError contract, same header handling as
    `get_json` -- only the body handling differs. Three properties the one
    caller (`firekeep backup pull`) depends on:

      * The bytes land under a TEMPORARY name in dest's own directory and are
        moved into place with `os.replace` only after the stream completes. A
        download interrupted halfway therefore leaves NO file at `dest` rather
        than a short one, which matters because there is no resume: the
        manifest's sha256 is the only thing standing between a truncated
        archive and a restore that fails at the worst possible moment, and a
        half-file that never appears cannot be mistaken for a whole one.
      * The temp file is created in dest's directory (never the system temp
        dir), so `os.replace` is a rename within one filesystem and cannot
        fall back to a copy -- and a 3GB archive is never written twice.
      * Nothing is buffered in memory beyond one chunk.
    """
    dest = Path(dest)
    req_headers = dict(headers or {})
    req_headers.setdefault("Accept", "application/octet-stream")
    req = urllib.request.Request(url, headers=req_headers, method="GET")
    ctx = _build_ssl_context(verify)
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp_name = ""
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            written = 0
            with tempfile.NamedTemporaryFile(
                dir=dest.parent, prefix=f".{dest.name}.", suffix=".part", delete=False,
            ) as handle:
                temp_name = handle.name
                while True:
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)
                    written += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(temp_name, dest)
        temp_name = ""
        return written
    except (urllib.error.URLError, TimeoutError) as e:
        raise _as_transport_error(e, method="GET", url=url, timeout=timeout) from e
    except OSError as e:
        raise TransportError(f"GET {url} could not be written to {dest}: {e}") from e
    finally:
        if temp_name:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass
