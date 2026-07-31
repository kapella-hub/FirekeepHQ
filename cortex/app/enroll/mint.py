"""Join-code creation shared by the API and deploy/firekeep-admin."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from auth.config import get_auth_settings

from .store import EnrollmentStore


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def encode_join_code(payload: dict[str, Any]) -> str:
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    checksum = _b64url(hashlib.sha256(body.encode("ascii")).digest()[:3])
    return f"fk_join_{body}.{checksum}"


def ca_fingerprint(ca_pem: str) -> str:
    return _b64url(hashlib.sha256(ca_pem.encode("utf-8")).digest()[:16])


async def mint_invite(
    store: EnrollmentStore,
    *,
    agent_label: str = "",
    transport: str,
    kind: str,
    host: str = "",
    base_url: str = "",
    ca_pem: str = "",
    ca_mode: str = "",
    ssh_target: str = "",
    issuer: str = "dashboard",
    key_expires_days: int | None = None,
    device_id: str = "",
    dist_base: str = "https://firekeep.ai",
) -> dict[str, Any]:
    if transport not in {"tls", "tunnel", "http"}:
        raise ValueError("invalid enrollment transport")
    if kind not in {"ports", "paths"}:
        raise ValueError("invalid connection kind")
    if kind == "ports" and not host:
        raise ValueError("kind=ports requires host")
    if kind == "paths" and not base_url:
        raise ValueError("kind=paths requires base_url")
    if transport == "tls" and not (ca_pem or ca_mode == "os"):
        raise ValueError("t=tls requires a CA file or --ca os")
    if transport == "tunnel" and not ssh_target:
        raise ValueError("t=tunnel requires --ssh-target")
    if key_expires_days is not None and not 0 <= key_expires_days <= 365:
        raise ValueError("--expires-days must be from 0 to 365")
    ticket, tid, record = await store.issue(
        agent_label=agent_label,
        transport=transport,
        kind=kind,
        host=host,
        base_url=base_url,
        ca_pem=ca_pem,
        ssh_target=ssh_target,
        issuer=issuer,
        key_expires_days=key_expires_days,
        device_id=device_id,
    )
    payload: dict[str, Any] = {
        "v": 1,
        "t": transport,
        "k": kind,
        "x": datetime.fromisoformat(record["expires_at"]).strftime("%Y%m%dT%H%M%SZ"),
        "q": ticket,
    }
    if kind == "ports":
        payload["h"] = host
    else:
        payload["u"] = base_url
    if transport == "tls":
        payload["f"] = "os" if ca_mode == "os" else ca_fingerprint(ca_pem)
    if transport == "tunnel":
        payload["s"] = ssh_target

    code = encode_join_code(payload)
    dist = dist_base.rstrip("/")
    sh_command = f"curl -fsSL {dist}/latest/install.sh | FIREKEEP_JOIN={code} sh"
    ps_command = (
        f"$env:FIREKEEP_JOIN='{code}'; irm {dist}/latest/install.ps1 | iex"
    )
    return {
        "code": code,
        "tid": tid,
        "expires_at": record["expires_at"],
        "credential_expires_days": int(record["key_expires_days"]),
        "install_command_sh": sh_command,
        "install_command_powershell": ps_command,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mint a single-use Firekeep join code")
    parser.add_argument("--agent", default="")
    parser.add_argument("--transport", choices=("tls", "tunnel", "http"), required=True)
    parser.add_argument("--kind", choices=("ports", "paths"), required=True)
    parser.add_argument("--host", default="")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--ca-file")
    parser.add_argument("--ca-pem-b64")
    parser.add_argument("--ca", choices=("os",))
    parser.add_argument("--ssh-target", default="")
    parser.add_argument("--issuer", default="firekeep-admin")
    parser.add_argument("--expires-days", type=int)
    parser.add_argument("--device-id", default="")
    parser.add_argument("--dist-base", default=os.getenv("FIREKEEP_DIST_BASE", "https://firekeep.ai"))
    parser.add_argument("--insecure-http", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


async def _run(args: argparse.Namespace) -> int:
    auth_settings = get_auth_settings()
    if not auth_settings.ENABLED:
        raise SystemExit(
            "this server enforces no authentication (AUTH_ENABLED=false), so there "
            "is no key to issue. Set AUTH_ENABLED=true and reissue the code."
        )
    if args.transport == "http" and not args.insecure_http:
        raise SystemExit("plain HTTP enrollment requires --insecure-http")
    if args.ca_file and args.ca_pem_b64:
        raise SystemExit("use only one of --ca-file or --ca-pem-b64")
    if args.ca_file:
        ca_pem = Path(args.ca_file).read_text(encoding="utf-8")
    elif args.ca_pem_b64:
        try:
            ca_pem = base64.b64decode(args.ca_pem_b64, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise SystemExit("--ca-pem-b64 is not a valid UTF-8 CA document") from exc
    else:
        ca_pem = ""
    import redis.asyncio as aioredis

    redis_client = aioredis.from_url(auth_settings.REDIS_URL, decode_responses=True)
    try:
        result = await mint_invite(
            EnrollmentStore(redis_client),
            agent_label=args.agent,
            transport=args.transport,
            kind=args.kind,
            host=args.host,
            base_url=args.base_url,
            ca_pem=ca_pem,
            ca_mode=args.ca or "",
            ssh_target=args.ssh_target,
            issuer=args.issuer,
            key_expires_days=args.expires_days,
            device_id=args.device_id,
            dist_base=args.dist_base,
        )
    finally:
        await redis_client.aclose()
    print(json.dumps(result) if args.json else result["code"])
    return 0


def main() -> int:
    return asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
