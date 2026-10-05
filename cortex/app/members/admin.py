"""Server-shell member administration, run by deploy/firekeep-admin.

    python -m app.members.admin list
    python -m app.members.admin remove MEMBER_ID
    python -m app.members.admin restore MEMBER_ID

Runs inside cortex-api (`docker compose exec -T cortex-api ...`), like
`app.enroll.mint` does for `firekeep-admin invite`, so the shell path and
`DELETE /members/{id}` share ONE implementation (auth/members.py) instead of a
bash port of the owner / last-admin / sweep rules. The shell holder is the
deployment owner, as for every other firekeep-admin subcommand.

`restore` only reactivates the member; it issues no credential. Mint their
new join code with `firekeep-admin invite --member MEMBER_ID`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from auth.config import get_auth_settings
from auth.members import MemberRemovalError, remove_member, restore_member
from auth.workspace import MEMBER_INDEX, MEMBER_PREFIX, ensure_workspace

ISSUER = "firekeep-admin"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Remove, restore or list workspace members")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    sub.add_parser("remove").add_argument("member_id")
    sub.add_parser("restore").add_argument("member_id")
    return parser


async def run(argv: list[str], redis_client) -> int:
    args = _parser().parse_args(argv)
    workspace = await ensure_workspace(redis_client)
    try:
        if args.command == "list":
            rows = []
            for member_id in await redis_client.zrange(MEMBER_INDEX, 0, -1):
                row = await redis_client.hgetall(f"{MEMBER_PREFIX}{member_id}")
                if row:
                    rows.append(row)
            result: dict = {"workspace_id": workspace.workspace_id, "members": rows}
        elif args.command == "remove":
            result = await remove_member(
                redis_client,
                args.member_id,
                workspace_id=workspace.workspace_id,
                owner_member_id=workspace.owner_member_id,
                removed_by=ISSUER,
            )
        else:
            member = await restore_member(
                redis_client,
                args.member_id,
                workspace_id=workspace.workspace_id,
                restored_by=ISSUER,
            )
            result = {
                "status": member.get("status"),
                "member_id": args.member_id,
                "member": member,
                "next": f"firekeep-admin invite --member {args.member_id}",
            }
    except MemberRemovalError as exc:
        print(f"ERROR: {exc.detail}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


async def _main(argv: list[str]) -> int:
    import redis.asyncio as aioredis

    client = aioredis.from_url(get_auth_settings().REDIS_URL, decode_responses=True)
    try:
        return await run(argv, client)
    finally:
        await client.aclose()


def main() -> int:
    return asyncio.run(_main(sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
