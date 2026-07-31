"""Offline Firekeep licence key generation and signing utility.

Run only on a trusted operator machine. Private key files are never needed by
the Firekeep server; it receives only FIREKEEP_LICENCE_PUBLIC_KEY.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from auth.entitlements import sign_licence


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _write_private(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value + "\n")


def keygen(args: argparse.Namespace) -> int:
    private = Ed25519PrivateKey.generate()
    private_raw = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_raw = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    private_path = Path(args.private_key).expanduser().resolve()
    _write_private(private_path, _b64url(private_raw))
    print(f"Private signing key written once to {private_path}")
    print("Back it up offline; losing it prevents licence renewals.")
    print(f"FIREKEEP_LICENCE_PUBLIC_KEY={_b64url(public_raw)}")
    return 0


def mint(args: argparse.Namespace) -> int:
    raw = _decode(Path(args.private_key).expanduser().read_text(encoding="utf-8").strip())
    if len(raw) != 32:
        raise SystemExit("private key file must contain 32 raw Ed25519 bytes as base64url")
    private = Ed25519PrivateKey.from_private_bytes(raw)
    issued = datetime.now(timezone.utc)
    expires = issued + timedelta(days=args.days)
    payload = {
        "workspace_id": args.workspace_id,
        "customer": args.customer,
        "plan": args.plan,
        "max_members": args.max_members,
        "issued_at": issued.isoformat(),
        "expires_at": expires.isoformat(),
    }
    if args.plan == "solo" and args.max_members != 1:
        raise SystemExit("Solo requires --max-members 1")
    if args.plan == "team" and args.max_members < 2:
        raise SystemExit("Team requires --max-members 2 or greater")
    document = sign_licence(payload, private)
    if args.output:
        output = Path(args.output).expanduser().resolve()
        output.write_text(document + "\n", encoding="utf-8")
        print(f"Licence written to {output}")
    else:
        print(document)
    if args.json:
        print(json.dumps(payload, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Offline Firekeep licence utility")
    commands = root.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("keygen", help="create a new Ed25519 signing key")
    generate.add_argument("--private-key", required=True)
    generate.set_defaults(func=keygen)

    issue = commands.add_parser("mint", help="sign one workspace entitlement")
    issue.add_argument("--private-key", required=True)
    issue.add_argument("--workspace-id", required=True)
    issue.add_argument("--customer", required=True)
    issue.add_argument("--plan", choices=("solo", "team"), required=True)
    issue.add_argument("--max-members", type=int, required=True)
    issue.add_argument("--days", type=int, default=365)
    issue.add_argument("--output")
    issue.add_argument("--json", action="store_true")
    issue.set_defaults(func=mint)
    return root


def main() -> int:
    args = parser().parse_args()
    if getattr(args, "days", 1) < 1:
        raise SystemExit("--days must be positive")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
