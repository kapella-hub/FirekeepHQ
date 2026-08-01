"""Crash-safe, zero-prompt client enrollment."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import socket
import ssl
import time
from pathlib import Path
from urllib.parse import quote, urlparse

from firekeep_client import config_write, resolver
from firekeep_client.joincode import JoinCode, JoinCodeError, decode_join_code
from firekeep_client.transport import (
    TransportError,
    _build_ssl_context,
    get_json,
    post_json,
)

PENDING_TTL_HOURS = 24


class JoinError(RuntimeError):
    def __init__(self, message: str, *, exit_code: int = 1) -> None:
        self.exit_code = exit_code
        super().__init__(message)


def pending_path(config_path: Path | None = None) -> Path:
    return (config_path or resolver._config_path()).expanduser().resolve().parent / "pending-join.json"


def sweep_pending(config_path: Path | None = None, *, now: float | None = None) -> bool:
    path = pending_path(config_path)
    if not path.exists():
        return False
    now = time.time() if now is None else now
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        created = float(data.get("created_at_epoch", 0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        created = 0
    if now - created <= PENDING_TTL_HOURS * 3600:
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def _write_private_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _new_pending(code: JoinCode) -> dict:
    return {
        "ticket_id": code.tid,
        "secret": "nxs_" + secrets.token_bytes(32).hex(),
        "device_nonce": secrets.token_hex(8),
        "created_at_epoch": time.time(),
    }


def _prepare_pending(code: JoinCode, *, resume: bool, config_path: Path) -> dict:
    path = pending_path(config_path)
    sweep_pending(config_path)
    if resume:
        try:
            pending = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise JoinError("no readable pending enrollment exists to resume", exit_code=3) from exc
        if pending.get("ticket_id") != code.tid:
            raise JoinError(
                "pending enrollment belongs to a different join code; retry without "
                "--resume or remove pending-join.json",
                exit_code=3,
            )
        if not re.fullmatch(r"nxs_[0-9a-f]{64}", str(pending.get("secret", ""))):
            raise JoinError("pending enrollment contains an invalid credential", exit_code=3)
        if not re.fullmatch(r"[0-9a-f]{16}", str(pending.get("device_nonce", ""))):
            raise JoinError("pending enrollment contains an invalid device nonce", exit_code=3)
        return pending
    if path.exists():
        raise JoinError(
            f"a pending enrollment already exists at {path}; run this command with "
            "--resume to reuse its credential safely",
            exit_code=3,
        )
    pending = _new_pending(code)
    try:
        _write_private_json(path, pending)
    except OSError as exc:
        raise JoinError(
            f"cannot protect pending enrollment state at {path}: {exc}. Nothing was sent.",
            exit_code=1,
        ) from exc
    return pending


def _rest_base(code: JoinCode) -> str:
    if code.transport == "tunnel":
        return "http://127.0.0.1:8100"
    scheme = "https" if code.transport == "tls" else "http"
    if code.kind == "ports":
        return f"{scheme}://{code.host}:8100"
    return f"{str(code.base_url).rstrip('/')}/api/cortex"


def _fingerprint(ca_pem: str) -> str:
    digest = hashlib.sha256(ca_pem.encode("utf-8")).digest()[:16]
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _transport(code: JoinCode, rest_base: str):
    """Establish reachability and return (verify, fetched_ca_pem)."""
    if code.transport == "tunnel":
        from firekeep_client import connect

        try:
            if not connect._tunnel_running():
                connect._start_tunnel(str(code.ssh_target))
        except (connect.ConnectError, FileNotFoundError) as exc:
            raise JoinError(str(exc), exit_code=7) from exc
        return False, ""
    if code.transport == "http":
        print(
            f"WARNING: this code redeems over plain http to {code.host or code.base_url}. "
            "Your credential is generated locally and is not sent during enrollment, "
            "but it IS sent as X-API-Key on every request afterwards, in cleartext on "
            "this transport. The join code itself crosses the network now. Continue "
            "only on a trusted network."
        )
        return False, ""
    if code.fingerprint == "os":
        return _build_ssl_context("os"), ""

    anchor_path = (
        "/members/invites/anchor" if code.purpose == "member" else "/enroll/anchor"
    )
    anchor = get_json(
        f"{rest_base}{anchor_path}?tid={quote(code.tid)}",
        headers={},
        timeout=10,
        verify=False,
    )
    ca_pem = anchor.get("ca_pem", "") if isinstance(anchor, dict) else ""
    if not ca_pem or _fingerprint(ca_pem) != code.fingerprint:
        raise JoinError(
            "SERVER IDENTITY MISMATCH — the server presented a CA that does not match "
            "the fingerprint in your join code. Nothing was sent. Do not retry; "
            "confirm the code out of band with its issuer.",
            exit_code=5,
        )
    try:
        verify = ssl.create_default_context(cadata=ca_pem)
    except ssl.SSLError as exc:
        raise JoinError(f"the pinned server CA is invalid: {exc}", exit_code=5) from exc
    return verify, ca_pem


def _write_ca(config_path: Path, code: JoinCode, ca_pem: str) -> Path:
    locator = code.host or urlparse(str(code.base_url)).hostname or "server"
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", locator).strip("-") or "server"
    path = config_path.parent / f"{slug}-ca.crt"
    path.write_text(ca_pem, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def _agent_id(response: dict, override: str | None, config_path: Path) -> str:
    if override:
        return override
    suggested = response.get("suggested_agent_id")
    if isinstance(suggested, str) and suggested.strip():
        return suggested.strip()
    try:
        existing = resolver.agent_id(resolver.load_config(config_path))
        if existing != "CHANGEME":
            return existing
    except resolver.ConfigError:
        pass
    return "agent-" + socket.gethostname().split(".", 1)[0].lower()


def _server_values(
    code: JoinCode,
    response: dict,
    secret: str,
    ca_path: Path | None,
) -> dict[str, str]:
    scheme = "https" if code.transport == "tls" else "http"
    server = {
        "kind": str(response["kind"]),
        "scheme": scheme,
        "verify_tls": "true" if code.transport == "tls" else "false",
        "api_key": secret,
        "credential_id": str(response["credential_id"]),
        "device_id": str(response["device_id"]),
    }
    expires = response.get("credential_expires_at")
    if expires:
        server["credential_expires_at"] = str(expires)
    if response["kind"] == "ports":
        server["host"] = "127.0.0.1" if code.transport == "tunnel" else str(response["host"])
    else:
        server["base_url"] = str(response["base_url"])
    if code.transport == "tls":
        server["ca_path"] = "os" if code.fingerprint == "os" else str(ca_path)
    return server


def join(
    pasted_code: str,
    *,
    agent_id: str | None = None,
    force: bool = False,
    print_key: bool = False,
    resume: bool = False,
) -> int:
    """Join one server and run doctor. Raises JoinError on actionable failure."""
    if resolver.is_bypassed():
        raise JoinError(
            "personal mode is ON, so Firekeep is dormant and join would be a no-op. "
            "Run: firekeep personal off",
            exit_code=1,
        )
    try:
        code = decode_join_code(pasted_code)
    except JoinCodeError as exc:
        raise JoinError(str(exc), exit_code=2) from exc

    config_path = resolver._config_path()
    pending = (
        _prepare_pending(code, resume=resume, config_path=config_path)
        if code.purpose == "device"
        else None
    )
    rest_base = _rest_base(code)
    try:
        verify, ca_pem = _transport(code, rest_base)
        # Cheap keyless reachability check before the one-time ticket crosses the wire.
        get_json(f"{rest_base}/health", headers={}, timeout=10, verify=verify)
    except JoinError:
        raise
    except TransportError as exc:
        raise JoinError(
            f"{rest_base} did not answer before enrollment, so the code was NOT "
            f"redeemed and is still valid: {exc}",
            exit_code=4,
        ) from exc

    if code.purpose == "member":
        try:
            accepted = post_json(
                f"{rest_base}/members/invites/accept",
                {"ticket": code.ticket},
                headers={},
                timeout=10,
                verify=verify,
            )
        except TransportError as exc:
            # Seat refusals already name plan, counts, and upgrade path. Preserve
            # that server text instead of reducing it to a generic join error.
            raise JoinError(str(exc), exit_code=3) from exc
        if not isinstance(accepted, dict) or not isinstance(
            accepted.get("join_code"), str
        ):
            raise JoinError("server returned an invalid member-accept response", exit_code=6)
        membership = accepted.get("membership") or {}
        entitlement = accepted.get("entitlement") or {}
        print(
            f"member invite accepted for {membership.get('label') or membership.get('email') or 'member'} "
            f"— {str(entitlement.get('plan', 'solo')).title()} workspace"
        )
        return join(
            accepted["join_code"],
            agent_id=agent_id,
            force=force,
            print_key=print_key,
            resume=resume,
        )

    if pending is None:  # defensive: the member branch above always returns
        raise JoinError("device enrollment state was not prepared", exit_code=3)

    ca_path = _write_ca(config_path, code, ca_pem) if ca_pem else None

    for attempt in range(2):
        secret = str(pending["secret"])
        body = {
            "ticket": code.ticket,
            "credential_hash": hashlib.sha256(secret.encode("utf-8")).hexdigest(),
            "device_nonce": pending["device_nonce"],
            "hostname": socket.gethostname(),
        }
        try:
            response = post_json(
                f"{rest_base}/enroll", body, headers={}, timeout=10, verify=verify
            )
            break
        except TransportError as exc:
            if exc.status == 409 and "already registered" in str(exc) and attempt == 0:
                pending = _new_pending(code)
                _write_private_json(pending_path(config_path), pending)
                continue
            if exc.status == 404 and not exc.response_is_json:
                raise JoinError(
                    "this server predates client enrollment (no POST /enroll). On "
                    "the server: git pull && bash update.sh — then ask for a new "
                    "code. Meanwhile: firekeep connect <user@host> works over ssh.",
                    exit_code=6,
                ) from exc
            if exc.status == 429 or "AUTH_ENABLED=false" in str(exc):
                exit_code = 6
            elif exc.status == 500 and "privileges the server refuses" in str(exc):
                exit_code = 5
            else:
                exit_code = 3
            raise JoinError(str(exc), exit_code=exit_code) from exc
    else:  # pragma: no cover - loop always breaks or raises
        raise JoinError("enrollment failed", exit_code=3)

    if not isinstance(response, dict) or "api_key" in response:
        raise JoinError("server returned an invalid enrollment response", exit_code=6)
    required = {"device_id", "credential_id", "scopes", "kind", "server_version"}
    if not required.issubset(response):
        raise JoinError("server returned incomplete enrollment metadata", exit_code=6)

    selected_agent = _agent_id(response, agent_id, config_path)
    values = _server_values(code, response, str(pending["secret"]), ca_path)
    try:
        written = config_write.upsert_server(
            config_path,
            agent_id=selected_agent,
            server=values,
            force=force,
        )
    except config_write.ConfigWriteError as exc:
        raise JoinError(str(exc), exit_code=8) from exc
    for change in written.changes:
        print(f"[server] updating: {change}")
    try:
        pending_path(config_path).unlink()
    except FileNotFoundError:
        pass
    if print_key:
        print(f"credential: {pending['secret']}")

    expires = response.get("credential_expires_at") or "never"
    if response.get("replay"):
        print(
            "this join code was already redeemed by this device — reusing the "
            f"existing credential {response['credential_id']}. Nothing changed "
            "on the server."
        )
    print(
        f"joined as {selected_agent} on device {response['device_id']} — credential "
        f"{response['credential_id']} expires {expires}"
    )
    from firekeep_client.cli import run_doctor

    rows = run_doctor()
    for name, status, detail in rows:
        print(f"[{status.upper()}] {name}: {detail}")
    return 1 if any(status == "fail" for _, status, _ in rows) else 0
