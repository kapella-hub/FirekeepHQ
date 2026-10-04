"""Who wrote a memory is recorded from verified identity, never from a label.

Before 2026-10-04 a memory carried workspace/member but no credential or
runtime, and ``agent_id`` -- the client-chosen ``X-Agent-Id`` -- was the only
"who" a reader could see. And every Bridge distillate was written with
FIREKEEP_BRIDGE_KEY, minted as the deployment OWNER, so a teammate's whole
session history landed attributed to the owner.

Two contracts now:
- ``/memory/learn``: provenance is the caller's verified principal plus a
  runtime id namespaced by its credential; the label stays a label.
- ``/memory/learn/delegated``: a key holding ``memory:write:delegated``
  LITERALLY names the member it writes for; Cortex verifies the member (active,
  same workspace) and any named credential (same member) in the auth store.

Behavioural, not route-table scans (FastAPI 0.140+ wraps included routers):
real keys minted into fakeredis, the real scope dependency, mocked stores.
Ported in spirit from ae1f973's test_delegated_memory_attribution.py.
"""

from __future__ import annotations

import httpx
import fakeredis.aioredis
import pytest
import pytest_asyncio

from auth import keys
from auth.principal import runtime_id_for

from app.main import app, get_graph, get_rag_engine, get_redis, get_vector

WORKSPACE = "workspace-local"
OWNER = "member-owner"
ALICE = "member-alice"


@pytest.fixture(autouse=True)
def _deployment(monkeypatch):
    monkeypatch.setenv("FIREKEEP_WORKSPACE_ID", WORKSPACE)
    monkeypatch.setenv("FIREKEEP_OWNER_MEMBER_ID", OWNER)


@pytest_asyncio.fixture
async def auth_on():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await keys.init_auth(redis_client=redis, enabled=True)
    for member, role in ((OWNER, "owner"), (ALICE, "member")):
        await redis.hset(f"auth:member:{member}", mapping={
            "member_id": member, "workspace_id": WORKSPACE,
            "role": role, "status": "active"})
    try:
        yield redis
    finally:
        await keys.init_auth(redis_client=None, enabled=False)
        await redis.aclose()


async def _mint(redis, name: str, scopes: list[str], *, member: str = OWNER,
                extra_scopes: tuple[str, ...] = ()) -> dict:
    """create_key, then the fields bootstrap-keys.sh / member enrollment set
    that create_key cannot (a member other than the owner; service scopes)."""
    created = await keys.create_key(name, scopes)
    key_hash = keys._hash_key(created["api_key"])
    await redis.hset(f"auth:key:{key_hash}", mapping={
        "member_id": member, "workspace_id": WORKSPACE})
    if extra_scopes:
        import json

        await redis.hset(f"auth:key:{key_hash}", "scopes",
                         json.dumps(sorted(set(scopes) | set(extra_scopes))))
    return created


@pytest_asyncio.fixture
async def client(mock_graph, mock_vector, mock_redis):
    async def _graph():
        return mock_graph

    async def _vector():
        return mock_vector

    async def _redis():
        return mock_redis

    async def _engine():
        return None

    app.dependency_overrides.update({
        get_graph: _graph, get_vector: _vector,
        get_redis: _redis, get_rag_engine: _engine,
    })
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://cortex",
        ) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


BODY = {"action": "changed attribution", "outcome": "tests pass"}


def _provenance(mock_vector) -> dict:
    """The provenance a write recorded: top-level identity + nested metadata."""
    md = mock_vector.upsert.call_args.kwargs["metadata"]
    return md


# ---------------------------------------------------------------------------
# /memory/learn: provenance from the verified principal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_learn_records_credential_and_runtime_from_the_principal(
    client, mock_graph, mock_vector, mock_redis, monkeypatch
):
    monkeypatch.setattr("auth.principal.request_principal", lambda _r: {
        "workspace_id": WORKSPACE, "member_id": ALICE,
        "credential_id": "a11ce0000000a11c", "scopes": ["memory:write"],
        "authenticated": True,
    })
    resp = await client.post("/memory/learn", json=BODY,
                             headers={"X-Agent-Id": OWNER, "X-Session-Id": "s-1"})
    assert resp.status_code == 200, resp.text

    md = _provenance(mock_vector)
    assert md["member_id"] == ALICE
    assert md["credential_id"] == "a11ce0000000a11c"
    assert md["runtime_label"] == OWNER          # a label, even one that looks like a member
    assert md["runtime_id"] == runtime_id_for("a11ce0000000a11c", OWNER)
    assert md["delegated_by_credential_id"] is None
    assert md["agent_id"] == OWNER                # display field unchanged
    assert mock_graph.merge_action_log.call_args.kwargs["member_id"] == ALICE


@pytest.mark.asyncio
async def test_ordinary_learn_refuses_delegation_headers(client, mock_vector):
    """Delegation is a separate, separately-scoped route -- a header on the
    ordinary one must not be silently ignored (it would look like it worked)."""
    resp = await client.post("/memory/learn", json=BODY, headers={
        "X-Firekeep-Delegated-Member-Id": ALICE})
    assert resp.status_code == 400
    assert "/memory/learn/delegated" in resp.json()["detail"]
    mock_vector.upsert.assert_not_awaited()


# ---------------------------------------------------------------------------
# /memory/learn/delegated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delegated_learn_attributes_the_named_member(
    auth_on, client, mock_graph, mock_vector
):
    bridge = await _mint(auth_on, "firekeep-bridge",
                         ["memory:read", "memory:write"],
                         extra_scopes=("memory:write:delegated", "eval:grade"))
    alice = await _mint(auth_on, "alice-laptop", ["memory:write"], member=ALICE)

    resp = await client.post("/memory/learn/delegated", json=BODY, headers={
        "X-API-Key": bridge["api_key"],
        "X-Firekeep-Delegated-Member-Id": ALICE,
        "X-Firekeep-Delegated-Credential-Id": alice["credential_id"],
        "X-Agent-Id": "codex", "X-Session-Id": "s-42",
    })
    assert resp.status_code == 200, resp.text

    md = _provenance(mock_vector)
    assert md["workspace_id"] == WORKSPACE
    assert md["member_id"] == ALICE
    assert md["credential_id"] == alice["credential_id"]
    assert md["runtime_id"] == runtime_id_for(alice["credential_id"], "codex")
    assert md["delegated_by_credential_id"] == bridge["credential_id"]
    assert md["agent_id"] == "codex"
    assert md["session_id"] == "s-42"
    graph = mock_graph.merge_action_log.call_args.kwargs
    assert graph["member_id"] == ALICE
    assert graph["workspace_id"] == WORKSPACE


@pytest.mark.asyncio
async def test_delegated_learn_for_owner_session_is_the_owner(auth_on, client, mock_vector):
    """Legacy sessions (no recorded owner) belong to the deployment owner: the
    distiller names the owner, without a credential."""
    bridge = await _mint(auth_on, "firekeep-bridge", ["memory:write"],
                         extra_scopes=("memory:write:delegated",))
    resp = await client.post("/memory/learn/delegated", json=BODY, headers={
        "X-API-Key": bridge["api_key"], "X-Firekeep-Delegated-Member-Id": OWNER})
    assert resp.status_code == 200, resp.text
    md = _provenance(mock_vector)
    assert md["member_id"] == OWNER
    assert md["credential_id"] == ""
    assert md["delegated_by_credential_id"] == bridge["credential_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("scopes,extra", [
    (["memory:write"], ()),           # an ordinary writer
    (["*"], ()),                       # the dashboard / owner wildcard key
    (sorted(keys.ENROLLABLE_SCOPES), ()),  # a teammate's enrolled key
])
async def test_delegated_learn_refuses_keys_without_the_literal_scope(
    auth_on, client, mock_vector, scopes, extra
):
    key = await _mint(auth_on, "caller", scopes, extra_scopes=extra)
    resp = await client.post("/memory/learn/delegated", json=BODY, headers={
        "X-API-Key": key["api_key"], "X-Firekeep-Delegated-Member-Id": ALICE})
    assert resp.status_code == 403
    mock_vector.upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_delegated_learn_hides_why_attribution_failed(auth_on, client, mock_vector):
    """Unknown member, foreign credential: one answer, so the service route is
    not an oracle for which members and credentials exist."""
    bridge = await _mint(auth_on, "firekeep-bridge", ["memory:write"],
                         extra_scopes=("memory:write:delegated",))
    alice = await _mint(auth_on, "alice-laptop", ["memory:write"], member=ALICE)
    details = set()
    for headers in (
        {"X-Firekeep-Delegated-Member-Id": "member-mallory"},
        {"X-Firekeep-Delegated-Member-Id": OWNER,
         "X-Firekeep-Delegated-Credential-Id": alice["credential_id"]},
        {},
    ):
        resp = await client.post("/memory/learn/delegated", json=BODY,
                                 headers={"X-API-Key": bridge["api_key"], **headers})
        assert resp.status_code == 403, (headers, resp.text)
        details.add(resp.json()["detail"])
    assert details == {"Delegated attribution could not be verified"}
    mock_vector.upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_delegated_learn_is_closed_when_auth_is_disabled(client, mock_vector):
    """Auth-off has one principal (the owner); there is nobody to delegate for,
    and the anonymous caller never holds a service-only scope."""
    await keys.init_auth(redis_client=None, enabled=False)
    resp = await client.post("/memory/learn/delegated", json=BODY, headers={
        "X-Firekeep-Delegated-Member-Id": ALICE})
    assert resp.status_code == 403
    mock_vector.upsert.assert_not_awaited()
