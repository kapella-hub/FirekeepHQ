"""The server-shell side of member removal (deploy/firekeep-admin members ...).

`python -m app.members.admin` shares auth/members.py with DELETE
/members/{id}; `python -m app.enroll.mint --member-id` mints the new code for
a restored member. THREAT-MODEL §5.17.
"""

from __future__ import annotations

import argparse
import json

import fakeredis.aioredis
import pytest

import app.enroll.mint as mint
from app.members.admin import run
from auth.config import AuthSettings
from auth.workspace import MEMBER_INDEX

MEMBER = "member-leaver"


@pytest.fixture
async def redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await client.hset(
        f"auth:member:{MEMBER}",
        mapping={"member_id": MEMBER, "workspace_id": "workspace-local",
                 "role": "member", "status": "active"},
    )
    await client.zadd(MEMBER_INDEX, {MEMBER: 1})
    try:
        yield client
    finally:
        await client.aclose()


async def test_remove_and_restore_from_the_shell(redis, capsys):
    assert await run(["remove", MEMBER], redis) == 0
    removed = json.loads(capsys.readouterr().out)
    assert removed["status"] == "removed"
    assert await redis.hget(f"auth:member:{MEMBER}", "removed_by") == "firekeep-admin"

    assert await run(["list"], redis) == 0
    listed = {m["member_id"]: m for m in json.loads(capsys.readouterr().out)["members"]}
    assert listed[MEMBER]["status"] == "removed"

    assert await run(["restore", MEMBER], redis) == 0
    restored = json.loads(capsys.readouterr().out)
    assert restored["status"] == "active"
    assert restored["next"] == f"firekeep-admin invite --member {MEMBER}"


async def test_the_owner_cannot_be_removed_from_the_shell_either(redis, capsys):
    assert await run(["remove", "member-owner"], redis) == 1
    assert "owner can never be removed" in capsys.readouterr().err
    assert await redis.hget("auth:member:member-owner", "status") == "active"


def _mint_args(**overrides) -> argparse.Namespace:
    args = mint._parser().parse_args(
        ["--transport", "tunnel", "--kind", "ports", "--host", "127.0.0.1",
         "--ssh-target", "a@example", "--json"]
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


@pytest.fixture
def mint_env(redis, monkeypatch):
    import redis.asyncio as aioredis

    monkeypatch.setattr(mint, "get_auth_settings", lambda: AuthSettings(ENABLED=True))
    monkeypatch.setattr(aioredis, "from_url", lambda *a, **k: redis)
    # _run closes its client; keep the shared fake usable afterwards.
    monkeypatch.setattr(redis, "aclose", _noop)
    return redis


async def _noop(*_a, **_k):
    return None


async def test_mint_member_id_names_the_member_of_the_new_code(mint_env, capsys):
    assert await mint._run(_mint_args(member_id=MEMBER)) == 0
    tid = json.loads(capsys.readouterr().out)["tid"]
    assert await mint_env.hget(f"auth:enroll:{tid}", "member_id") == MEMBER


async def test_mint_member_id_refuses_a_removed_member(mint_env):
    await run(["remove", MEMBER], mint_env)
    with pytest.raises(SystemExit) as exc:
        await mint._run(_mint_args(member_id=MEMBER))
    assert "not active" in str(exc.value)


async def test_mint_member_id_and_device_id_are_exclusive(mint_env):
    with pytest.raises(SystemExit) as exc:
        await mint._run(_mint_args(member_id=MEMBER, device_id="0123456789abcdef"))
    assert "only one of --member-id or --device-id" in str(exc.value)
