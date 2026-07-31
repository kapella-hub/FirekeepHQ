"""Pure, stdlib-only Firekeep join-code decoding and validation."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any


class JoinCodeError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class JoinCode:
    transport: str
    kind: str
    expires_at: str
    tid: str
    host: str | None = None
    base_url: str | None = None
    fingerprint: str | None = None
    ssh_target: str | None = None
    ticket: str = field(default="", repr=False)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _decode_b64(value: str, field_name: str) -> bytes:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        raise JoinCodeError("E_MALFORMED", f"malformed join code: {field_name} is invalid") from exc
    if _b64url(raw) != value:
        raise JoinCodeError("E_MALFORMED", f"malformed join code: {field_name} is invalid")
    return raw


def _malformed(field_name: str, reason: str = "missing or invalid") -> JoinCodeError:
    return JoinCodeError("E_MALFORMED", f"malformed join code: {field_name} is {reason}")


def decode_join_code(pasted: str) -> JoinCode:
    """Decode locally. Exceptions never contain the ticket secret."""
    value = pasted.strip()
    value = re.sub(r"^firekeep\s+join\s+", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", "", value)
    if not value.startswith("fk_join_"):
        raise JoinCodeError(
            "E_NOT_A_CODE",
            "that does not look like a Firekeep join code (expected it to start "
            "with 'fk_join_')",
        )
    rest = value[len("fk_join_"):]
    if rest.count(".") != 1:
        raise _malformed("separator")
    body, checksum = rest.split(".", 1)
    expected = _b64url(hashlib.sha256(body.encode("ascii", "strict")).digest()[:3])
    if checksum != expected:
        raise JoinCodeError(
            "E_DAMAGED",
            "this join code is damaged (checksum mismatch) — it was probably "
            "truncated or line-wrapped in transit. Nothing was sent to the server.",
        )
    try:
        payload: Any = json.loads(_decode_b64(body, "body").decode("utf-8"))
    except JoinCodeError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _malformed("body") from exc
    if not isinstance(payload, dict):
        raise _malformed("body")
    version = payload.get("v")
    if version != 1:
        raise JoinCodeError(
            "E_VERSION",
            f"this code was issued by a newer Firekeep server (format v{version}, "
            "this client understands v1). Run: firekeep update",
        )
    transport = payload.get("t")
    if transport not in {"tls", "tunnel", "http"}:
        raise _malformed("t")
    kind = payload.get("k")
    if kind not in {"ports", "paths"}:
        raise _malformed("k")
    host, base_url = payload.get("h"), payload.get("u")
    if kind == "ports" and (not isinstance(host, str) or not host or base_url is not None):
        raise _malformed("h/u", "invalid for k=ports")
    if kind == "paths" and (not isinstance(base_url, str) or not base_url or host is not None):
        raise _malformed("h/u", "invalid for k=paths")
    fingerprint = payload.get("f")
    if transport == "tls":
        if not isinstance(fingerprint, str) or not (
            fingerprint == "os" or re.fullmatch(r"[A-Za-z0-9_-]{22}", fingerprint)
        ):
            raise _malformed("f", "missing or invalid (t=tls requires a CA fingerprint)")
    elif fingerprint is not None:
        raise _malformed("f", "present when t is not tls")
    ssh_target = payload.get("s")
    if transport == "tunnel":
        if not isinstance(ssh_target, str) or not ssh_target:
            raise _malformed("s", "missing or invalid for t=tunnel")
    elif ssh_target is not None:
        raise _malformed("s", "present when t is not tunnel")
    expires_at = payload.get("x")
    if not isinstance(expires_at, str) or not re.fullmatch(r"\d{8}T\d{6}Z", expires_at):
        raise _malformed("x")
    ticket = payload.get("q")
    if not isinstance(ticket, str):
        raise _malformed("q")
    ticket_bytes = _decode_b64(ticket, "q")
    if len(ticket_bytes) != 32:
        raise _malformed("q", "not a 32-byte ticket")
    tid = hashlib.sha256(ticket_bytes).hexdigest()[:16]
    return JoinCode(
        transport=transport,
        kind=kind,
        expires_at=expires_at,
        tid=tid,
        host=host,
        base_url=base_url,
        fingerprint=fingerprint,
        ssh_target=ssh_target,
        ticket=ticket,
    )
