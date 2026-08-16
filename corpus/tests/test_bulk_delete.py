"""Docdex §3 bulk delete — DELETE /corpus/dex-sources/{source_id}.

Removing a synced folder is one bounded operation, not thousands of sequential
requests (spec review #6): every tracked source in the caller's workspace named
``docdex:<source_id>:<file-hash>`` goes in one call. The deletion set is driven
from the TRACKED source records — a scan of the caller's workspace records,
then one exact-name delete per record — never a Qdrant prefix query, so it is
bounded by what was actually ingested.

Authz is Task 4's, unchanged: the ``docdex:`` prefix requires the
``dex:docdex`` scope (or admin) even for the owner; private sources
additionally owner-or-admin, refused atomically before anything is removed;
and a source_id with no records in the caller's workspace answers exactly like
a nonexistent one (404 — no existence oracle).
"""

from __future__ import annotations

import pytest

from corpus.store import delete_dex_source, dex_source_prefix, list_sources, track_source
from corpus.tests.test_source_authz import (  # noqa: F401  (h is a fixture)
    ADMIN,
    ALICE,
    BOB,
    DEXBOT,
    OUTSIDER,
    Harness,
    h,
)

# The credential a member's own docdex client holds: the member's identity
# plus the dex scope (spec §4.3 — "each dex's scoped key carries its dex id").
ALICE_DEX = {**ALICE, "scopes": ["dex:docdex", "memory:write"]}
OUTSIDER_DEX = {**OUTSIDER, "scopes": ["dex:docdex", "memory:write"]}

_SRC1_WS1 = {"docdex:src1:f1", "docdex:src1:f2", "docdex:src1:f3"}


async def _seed_bulk(h: Harness) -> None:
    r = h.redis
    for f in ("f1", "f2", "f3"):
        await track_source(f"docdex:src1:{f}", "document", 2, redis_client=r,
                           visibility="member",
                           workspace_id="ws1", member_id="m-alice")
    # A DIFFERENT dex source of Alice's: must survive src1's bulk delete.
    await track_source("docdex:src2:g1", "document", 1, redis_client=r,
                       visibility="member",
                       workspace_id="ws1", member_id="m-alice")
    # src10 shares src1 as a string prefix but is another source: the
    # trailing colon in the match prefix is load-bearing.
    await track_source("docdex:src10:h1", "document", 1, redis_client=r,
                       visibility="member",
                       workspace_id="ws1", member_id="m-alice")
    # Same source_id in ANOTHER workspace: bulk delete must not cross tenancy.
    await track_source("docdex:src1:zz", "document", 1, redis_client=r,
                       visibility="member",
                       workspace_id="ws2", member_id="m-out")
    await track_source("team-wiki", "wiki", 2, redis_client=r,
                       visibility="workspace",
                       workspace_id="ws1", member_id="m-alice")


async def _tracked_names(h: Harness) -> set[str]:
    return {r["name"] for r in await list_sources(redis_client=h.redis)}


def _qdrant_deleted_names(h: Harness) -> list[str]:
    """The exact metadata.source_name each Qdrant delete filtered on."""
    names = []
    for call in h.vector.delete_by_filter.call_args_list:
        (flt,) = call.args
        for cond in flt.must:
            if cond.key == "metadata.source_name":
                names.append(cond.match.value)
    return names


# ---------------------------------------------------------------------------
# The prefix helper
# ---------------------------------------------------------------------------


class TestDexSourcePrefix:
    def test_prefix_shape(self):
        assert dex_source_prefix("abc123") == "docdex:abc123:"


# ---------------------------------------------------------------------------
# The route — one bounded call removes a multi-file source
# ---------------------------------------------------------------------------


class TestBulkDelete:
    @pytest.mark.asyncio
    async def test_multi_file_source_removed_in_one_call(self, h):
        await _seed_bulk(h)
        h.act_as(ALICE_DEX)
        resp = await h.delete("/corpus/dex-sources/src1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted_sources"] == 3
        # delete_source reports "all", not a count — never fabricated.
        assert body["deleted_chunks"] == "unknown"
        remaining = await _tracked_names(h)
        assert remaining & _SRC1_WS1 == set()
        # Other sources — src2, the src10 prefix-neighbour, the ws2 twin,
        # the plain workspace source — are untouched.
        assert {"docdex:src2:g1", "docdex:src10:h1",
                "docdex:src1:zz", "team-wiki"} <= remaining

    @pytest.mark.asyncio
    async def test_deletes_are_exact_names_from_tracked_records(self, h):
        await _seed_bulk(h)
        h.act_as(ALICE_DEX)
        resp = await h.delete("/corpus/dex-sources/src1")
        assert resp.status_code == 200
        # Per-source exact-name Qdrant deletes, bounded by the records —
        # never a prefix query.
        assert sorted(_qdrant_deleted_names(h)) == sorted(_SRC1_WS1)

    @pytest.mark.asyncio
    async def test_bulk_delete_is_workspace_bounded(self, h):
        await _seed_bulk(h)
        h.act_as(OUTSIDER_DEX)
        resp = await h.delete("/corpus/dex-sources/src1")
        assert resp.status_code == 200
        assert resp.json()["deleted_sources"] == 1
        remaining = await _tracked_names(h)
        assert "docdex:src1:zz" not in remaining
        assert _SRC1_WS1 <= remaining


# ---------------------------------------------------------------------------
# Authz — Task 4's gates, applied to the whole set
# ---------------------------------------------------------------------------


class TestBulkDeleteAuthz:
    @pytest.mark.asyncio
    async def test_generic_credential_403(self, h):
        await _seed_bulk(h)
        h.act_as(BOB)
        resp = await h.delete("/corpus/dex-sources/src1")
        assert resp.status_code == 403
        assert "dex:docdex" in resp.text
        # Even the OWNER needs the dex-scoped credential her docdex client
        # holds — the prefix is the dex's namespace, not the member's.
        h.act_as(ALICE)
        resp = await h.delete("/corpus/dex-sources/src1")
        assert resp.status_code == 403
        h.vector.delete_by_filter.assert_not_called()
        assert _SRC1_WS1 <= await _tracked_names(h)

    @pytest.mark.asyncio
    async def test_dex_scoped_non_owner_still_blocked_by_ownership(self, h):
        await _seed_bulk(h)
        h.act_as(DEXBOT)
        resp = await h.delete("/corpus/dex-sources/src1")
        assert resp.status_code == 403
        h.vector.delete_by_filter.assert_not_called()
        assert _SRC1_WS1 <= await _tracked_names(h)

    @pytest.mark.asyncio
    async def test_admin_can_bulk_delete(self, h):
        await _seed_bulk(h)
        h.act_as(ADMIN)
        resp = await h.delete("/corpus/dex-sources/src1")
        assert resp.status_code == 200
        assert resp.json()["deleted_sources"] == 3

    @pytest.mark.asyncio
    async def test_cross_workspace_indistinguishable_from_missing(self, h):
        await _seed_bulk(h)
        h.act_as(OUTSIDER_DEX)  # ws2; src2 exists only in ws1
        other_ws = await h.delete("/corpus/dex-sources/src2")
        missing = await h.delete("/corpus/dex-sources/definitely-not-there")
        assert other_ws.status_code == missing.status_code == 404
        assert other_ws.json() == missing.json()
        h.vector.delete_by_filter.assert_not_called()


# ---------------------------------------------------------------------------
# The store helper — honest chunk counts
# ---------------------------------------------------------------------------


class TestDeleteDexSourceCounts:
    @pytest.mark.asyncio
    async def test_counts_summed_only_when_store_returns_numbers(self):
        async def numeric(source_name):
            return {"chunks_deleted": 2}

        result = await delete_dex_source("s", ["a", "b"], numeric)
        assert result == {"deleted_sources": 2, "deleted_chunks": 4}

    @pytest.mark.asyncio
    async def test_counts_never_fabricated(self):
        async def alltext(source_name):
            return {"chunks_deleted": "all"}

        result = await delete_dex_source("s", ["a"], alltext)
        assert result == {"deleted_sources": 1, "deleted_chunks": "unknown"}
