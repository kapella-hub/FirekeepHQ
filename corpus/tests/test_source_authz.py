"""Spec §4.3 — source ownership, principal-aware authz, dex-reserved prefixes.

Source records carry server-stamped ownership (`workspace_id`, `member_id`,
`visibility`, `dex`); listing and mutation are principal-aware. Before this,
DELETE /corpus/sources had NO principal at all (spec §9 finding 1): any caller
could delete any member's source, and a private source's NAME — itself private
data (spec I1) — listed for everyone.

Two properties carry the weight here:
- A cross-workspace name and a nonexistent one answer identically (404): the
  delete route must not be an existence oracle.
- `docdex:`-prefixed names are reserved: writes and deletes require the
  `dex:docdex` credential scope (or admin) — a generic corpus credential
  cannot claim or mutate another dex's namespace, and even the OWNER needs
  the dex-scoped credential her docdex client holds.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import corpus.api as corpus_api
from corpus.api import create_corpus_router
from corpus.store import delete_source, list_sources, source_dex, track_source

_INGEST_RESULT = {
    "source_name": "Doc",
    "chunks_stored": 1,
    "entities_extracted": 0,
    "relationships_extracted": 0,
    "entity_types_discovered": [],
    "extraction_status": "skipped",
}

# Identities the way auth middleware attaches them (the verified principal —
# workspace/member are never client-asserted).
ALICE = {"workspace_id": "ws1", "member_id": "m-alice",
         "credential_id": "cred-alice", "scopes": ["memory:write"],
         "authenticated": True}
BOB = {"workspace_id": "ws1", "member_id": "m-bob",
       "credential_id": "cred-bob", "scopes": ["memory:write"],
       "authenticated": True}
ADMIN = {"workspace_id": "ws1", "member_id": "m-admin",
         "credential_id": "cred-admin", "scopes": ["admin"],
         "authenticated": True}
OUTSIDER = {"workspace_id": "ws2", "member_id": "m-out",
            "credential_id": "cred-out", "scopes": ["memory:write"],
            "authenticated": True}
DEXBOT = {"workspace_id": "ws1", "member_id": "m-dex",
          "credential_id": "cred-dex", "scopes": ["dex:docdex", "memory:write"],
          "authenticated": True}


class Harness:
    """Corpus router over the REAL store functions (fakeredis + fake vector),
    with a switchable verified principal — the closest corpus analogue of
    cortex's test_autopilot_api auth harness."""

    def __init__(self) -> None:
        self.redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
        self.vector = AsyncMock()
        self.vector.delete_by_filter = AsyncMock()
        self.ingest_mock = AsyncMock(return_value=dict(_INGEST_RESULT))
        self.principal = dict(ALICE)

        self.app = FastAPI()
        self.app.include_router(create_corpus_router())

        @self.app.middleware("http")
        async def _attach_identity(request, call_next):
            request.state.identity = self.principal
            return await call_next(request)

    def act_as(self, principal: dict) -> None:
        self.principal = dict(principal)

    async def seed(self) -> None:
        r = self.redis
        await track_source("alice-notes", "document", 1, redis_client=r,
                           visibility="member",
                           workspace_id="ws1", member_id="m-alice")
        await track_source("docdex:alice:f1", "document", 1, redis_client=r,
                           visibility="member",
                           workspace_id="ws1", member_id="m-alice")
        await track_source("team-wiki", "wiki", 2, redis_client=r,
                           visibility="workspace",
                           workspace_id="ws1", member_id="m-alice")
        await track_source("ws2-doc", "text", 1, redis_client=r,
                           workspace_id="ws2", member_id="m-out")
        # Pre-Phase-V record: no ownership fields at all.
        await track_source("legacy-doc", "text", 1, redis_client=r)

    async def get(self, path: str):
        async with self._client() as c:
            return await c.get(path)

    async def post(self, path: str, json: dict):
        async with self._client() as c:
            return await c.post(path, json=json)

    async def delete(self, path: str):
        async with self._client() as c:
            return await c.delete(path)

    def _client(self) -> AsyncClient:
        return AsyncClient(transport=ASGITransport(app=self.app),
                           base_url="http://t")


@pytest.fixture()
def h():
    harness = Harness()

    async def _sources():
        return await list_sources(redis_client=harness.redis)

    async def _delete(source_name):
        return await delete_source(source_name, harness.vector,
                                   redis_client=harness.redis)

    with patch.object(corpus_api, "get_corpus_sources", _sources), \
         patch.object(corpus_api, "delete_corpus_source", _delete), \
         patch.object(corpus_api, "ingest_document", harness.ingest_mock):
        yield harness


async def _listed_names(h: Harness) -> set[str]:
    resp = await h.get("/corpus/sources")
    assert resp.status_code == 200
    return {s["name"] for s in resp.json()["sources"]}


def _ingest_body(source_name: str) -> dict:
    return {"content": "some text", "source_name": source_name,
            "source_type": "document"}


# ---------------------------------------------------------------------------
# The dex helper and the scope table
# ---------------------------------------------------------------------------


class TestSourceDex:
    def test_source_dex_extraction(self):
        assert source_dex("docdex:abc:def") == "docdex"
        assert source_dex("docdex") == ""          # no ":" — not a claim
        assert source_dex("notadex:x") == ""       # unknown dex id
        assert source_dex("Untitled") == ""

    def test_scope_table_matches_the_dex_ids(self):
        # The plan's table, derived from the store's dex ids so the record's
        # `dex` field and the API gate can never disagree.
        assert corpus_api._DEX_SCOPE_PREFIXES == {
            "docdex:": "dex:docdex", "maildex:": "dex:maildex"}


# ---------------------------------------------------------------------------
# Source records carry server-stamped ownership
# ---------------------------------------------------------------------------


class TestSourceRecordOwnership:
    @pytest.mark.asyncio
    async def test_track_source_stamps_ownership_and_dex(self):
        r = fakeredis.aioredis.FakeRedis(decode_responses=False)
        await track_source("docdex:alice:f1", "document", 1, redis_client=r,
                           visibility="member",
                           workspace_id="ws1", member_id="m-alice")
        (record,) = await list_sources(redis_client=r)
        assert record["workspace_id"] == "ws1"
        assert record["member_id"] == "m-alice"
        assert record["dex"] == "docdex"

    @pytest.mark.asyncio
    async def test_unowned_record_fields_default_empty(self):
        r = fakeredis.aioredis.FakeRedis(decode_responses=False)
        await track_source("plain-doc", "text", 1, redis_client=r)
        (record,) = await list_sources(redis_client=r)
        assert record["workspace_id"] == ""
        assert record["member_id"] == ""
        assert record["dex"] == ""


# ---------------------------------------------------------------------------
# GET /corpus/sources — private names are private data (spec I1)
# ---------------------------------------------------------------------------


class TestSourcesListing:
    @pytest.mark.asyncio
    async def test_private_sources_hidden_from_other_members(self, h):
        await h.seed()
        h.act_as(BOB)
        names = await _listed_names(h)
        assert names == {"team-wiki", "legacy-doc"}

    @pytest.mark.asyncio
    async def test_owner_and_admin_see_private_sources(self, h):
        await h.seed()
        h.act_as(ALICE)
        assert await _listed_names(h) == {
            "alice-notes", "docdex:alice:f1", "team-wiki", "legacy-doc"}
        h.act_as(ADMIN)
        assert await _listed_names(h) == {
            "alice-notes", "docdex:alice:f1", "team-wiki", "legacy-doc"}

    @pytest.mark.asyncio
    async def test_listing_is_workspace_scoped(self, h):
        await h.seed()
        h.act_as(OUTSIDER)
        # Legacy pre-ownership records belong to the single-workspace world
        # and stay visible; everything else is the caller's workspace only.
        assert await _listed_names(h) == {"ws2-doc", "legacy-doc"}


# ---------------------------------------------------------------------------
# DELETE /corpus/sources/{source_name}
# ---------------------------------------------------------------------------


class TestDeleteAuthz:
    @pytest.mark.asyncio
    async def test_member_cannot_delete_anothers_private_source(self, h):
        await h.seed()
        h.act_as(BOB)
        resp = await h.delete("/corpus/sources/alice-notes")
        assert resp.status_code == 403
        h.vector.delete_by_filter.assert_not_called()

    @pytest.mark.asyncio
    async def test_owner_deletes_own_private_source(self, h):
        await h.seed()
        h.act_as(ALICE)
        resp = await h.delete("/corpus/sources/alice-notes")
        assert resp.status_code == 200
        assert "alice-notes" not in await _listed_names(h)

    @pytest.mark.asyncio
    async def test_admin_deletes_private_source(self, h):
        await h.seed()
        h.act_as(ADMIN)
        resp = await h.delete("/corpus/sources/alice-notes")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_workspace_member_deletes_workspace_source(self, h):
        await h.seed()
        h.act_as(BOB)
        resp = await h.delete("/corpus/sources/team-wiki")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_cross_workspace_delete_is_404_not_403(self, h):
        await h.seed()
        h.act_as(OUTSIDER)
        resp = await h.delete("/corpus/sources/team-wiki")
        assert resp.status_code == 404
        h.vector.delete_by_filter.assert_not_called()
        # Admin is workspace-scoped too — tenancy is not a privilege level.
        h.act_as(ADMIN)
        resp = await h.delete("/corpus/sources/ws2-doc")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_missing_source_indistinguishable_from_cross_workspace(self, h):
        await h.seed()
        h.act_as(OUTSIDER)
        other_ws = await h.delete("/corpus/sources/team-wiki")
        missing = await h.delete("/corpus/sources/definitely-not-there")
        # No existence oracle: same status, same body.
        assert other_ws.status_code == missing.status_code == 404
        assert other_ws.json() == missing.json()


# ---------------------------------------------------------------------------
# dex-reserved prefixes — writes
# ---------------------------------------------------------------------------


class TestDexReservedWrites:
    @pytest.mark.asyncio
    async def test_generic_credential_cannot_claim_reserved_name(self, h):
        h.act_as(BOB)
        resp = await h.post("/corpus/ingest", json=_ingest_body("docdex:x:y"))
        assert resp.status_code == 403
        assert "dex:docdex" in resp.text
        h.ingest_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_dex_scoped_credential_can_claim(self, h):
        h.act_as(DEXBOT)
        resp = await h.post("/corpus/ingest", json=_ingest_body("docdex:x:y"))
        assert resp.status_code == 200
        h.ingest_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_admin_can_claim_reserved_name(self, h):
        h.act_as(ADMIN)
        resp = await h.post("/corpus/ingest", json=_ingest_body("docdex:x:y"))
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_dex_scope_does_not_override_private_ownership(self, h):
        # DEXBOT holds the dex scope but is not the owner: re-ingesting
        # Alice's private source would replace her chunks — still 403.
        await h.seed()
        h.act_as(DEXBOT)
        resp = await h.post("/corpus/ingest",
                            json=_ingest_body("docdex:alice:f1"))
        assert resp.status_code == 403
        h.ingest_mock.assert_not_called()


# ---------------------------------------------------------------------------
# dex-reserved prefixes — deletes
# ---------------------------------------------------------------------------


class TestDexReservedDeletes:
    @pytest.mark.asyncio
    async def test_generic_credential_cannot_delete_reserved_name(self, h):
        await h.seed()
        h.act_as(BOB)
        resp = await h.delete("/corpus/sources/docdex:alice:f1")
        assert resp.status_code == 403
        # Even the OWNER needs the dex-scoped credential her docdex client
        # holds — the prefix is the dex's namespace, not the member's.
        h.act_as(ALICE)
        resp = await h.delete("/corpus/sources/docdex:alice:f1")
        assert resp.status_code == 403
        h.vector.delete_by_filter.assert_not_called()

    @pytest.mark.asyncio
    async def test_dex_scoped_non_owner_still_blocked_by_ownership(self, h):
        await h.seed()
        h.act_as(DEXBOT)
        resp = await h.delete("/corpus/sources/docdex:alice:f1")
        assert resp.status_code == 403
        h.vector.delete_by_filter.assert_not_called()

    @pytest.mark.asyncio
    async def test_admin_can_delete_reserved_name(self, h):
        await h.seed()
        h.act_as(ADMIN)
        resp = await h.delete("/corpus/sources/docdex:alice:f1")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Overwrite (re-ingest) is mutation and gets the same gate
# ---------------------------------------------------------------------------


class TestOverwriteGate:
    @pytest.mark.asyncio
    async def test_member_cannot_overwrite_anothers_private_source(self, h):
        await h.seed()
        h.act_as(BOB)
        resp = await h.post("/corpus/ingest", json=_ingest_body("alice-notes"))
        assert resp.status_code == 403
        h.ingest_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_owner_and_admin_can_reingest_private_source(self, h):
        await h.seed()
        h.act_as(ALICE)
        resp = await h.post("/corpus/ingest", json=_ingest_body("alice-notes"))
        assert resp.status_code == 200
        h.act_as(ADMIN)
        resp = await h.post("/corpus/ingest", json=_ingest_body("alice-notes"))
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_new_name_needs_no_ownership(self, h):
        await h.seed()
        h.act_as(BOB)
        resp = await h.post("/corpus/ingest", json=_ingest_body("bobs-new-doc"))
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_cross_workspace_name_collision_blocked(self, h):
        # The record key is global (corpus:source:<name>): letting ws2 re-use
        # ws1's name would clobber ws1's record AND sweep its chunks during
        # the generation swap (delete filters on source_name alone).
        await h.seed()
        h.act_as(OUTSIDER)
        resp = await h.post("/corpus/ingest", json=_ingest_body("team-wiki"))
        assert resp.status_code == 403
        h.ingest_mock.assert_not_called()
