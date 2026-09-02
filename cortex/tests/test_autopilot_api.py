"""The Autopilot inbox and digest.

Two properties carry most of the weight here.

FIRST, the inbox must survive its own dependencies. It joins five stores, and
the day one of them is unhappy is precisely the day an operator wants to read
the other four — so a section that raises degrades to an error marker in place
and the rest still render. A surface that 500s on its first bad dependency is
worthless exactly when it is needed, and the only way to keep that true is to
prove each section can fail alone.

SECOND, round 1 is READ-ONLY, and the numbers it reports must be honest: a
`total_actionable` that silently omits a queue it could not read is the same
confident-wrong-signal failure this repo bans elsewhere ("3 things to do" reads
identically whether the other three queues are empty or unreadable). So the
total is the sum of what was actually counted and `degraded` names the rest.

EVERY test here runs under real auth enforcement with a real admin key, which
is not ceremony: both routes are admin-scoped, and `require_scope("admin")`
refuses even the ANONYMOUS identity (auth blocker 7 — `ANONYMOUS_SCOPES` is
`SCOPES - {admin, *, vault:read}` and the auth-disabled path runs the check
too). A keyless client would 403 on every one of these, so there is no version
of this suite that exercises the endpoints without also proving the gate.

The Qdrant double is deliberately dumb — it matches `Filter(must=[...])` by
exact payload equality and pages by list slice. That is enough to pin the
queries these endpoints actually issue; anything richer would be testing
qdrant-client rather than this code.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import fakeredis.aioredis as fr
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.autopilot.api import create_autopilot_router
from app.autopilot import digest as digest_mod
from app.procedures import store as proc_store
from auth import keys

NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    """`build_digest` defaults `now` to wall-clock; the fixtures are anchored to
    NOW, so the window has to be anchored with them."""

    @classmethod
    def now(cls, tz=None):
        return NOW


def iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


class _Settings:
    QDRANT_COLLECTION = "c"
    PROCEDURE_ENABLED = True
    PROCEDURE_EXEC_TTL_DAYS = 90
    PROCEDURE_MAX_SPECS = 50


class _Off(_Settings):
    PROCEDURE_ENABLED = False


class _Point:
    def __init__(self, pid: str, payload: dict):
        self.id = pid
        self.payload = payload


def _matches(payload: dict, scroll_filter) -> bool:
    if scroll_filter is None:
        return True
    for cond in scroll_filter.must or []:
        if cond.match is not None:
            if payload.get(cond.key) != cond.match.value:
                return False
        elif cond.range is not None:
            value = payload.get(cond.key)
            r = cond.range
            if value is None:
                return False
            if r.gte is not None and not (value >= r.gte):
                return False
            if r.gt is not None and not (value > r.gt):
                return False
            if r.lte is not None and not (value <= r.lte):
                return False
            if r.lt is not None and not (value < r.lt):
                return False
    return True


class _FakeQdrant:
    """Enough Qdrant to answer the two shapes this package issues: a filtered
    single-page scroll (inbox sections) and an unfiltered paged walk (digest)."""

    def __init__(self, points=None):
        self.points = list(points or [])
        self.fail_on_filter = None  # a payload key; scrolls filtering on it raise
        self.fail_all = False

    async def scroll(self, collection_name, scroll_filter=None, limit=10,
                     offset=None, with_payload=True, with_vectors=False):
        if self.fail_all:
            raise RuntimeError("qdrant unreachable")
        if self.fail_on_filter and scroll_filter is not None:
            if any(c.key == self.fail_on_filter for c in (scroll_filter.must or [])):
                raise RuntimeError("payload index missing")
        matched = [p for p in self.points if _matches(p.payload, scroll_filter)]
        start = int(offset or 0)
        page = matched[start:start + limit]
        nxt = start + limit if start + limit < len(matched) else None
        return page, nxt


class _FakeVector:
    def __init__(self, client):
        self._client = client


class _DeadReplayRedis:
    def scan_iter(self, *a, **k):
        raise RuntimeError("replay redis down")


class _DeadRedis:
    async def lrange(self, *a, **k):
        raise RuntimeError("redis down")


def skill(pid, *, status="draft", stale=None, rereview=None, title="T",
          created=None, reviewed=None, efficacy=None, efficacy_n=None):
    payload = {
        "memory_type": "skill", "skill_status": status, "status": "active",
        "procedure_title": title, "trigger": f"when {title}",
        "timestamp": created or iso(30), "source_doc": "Runbook",
    }
    if stale is not None:
        payload["stale"] = stale
    if rereview is not None:
        payload["needs_rereview"] = rereview
    if reviewed is not None:
        payload["stale_reviewed_at"] = reviewed
    if efficacy is not None:
        payload["skill_efficacy"] = efficacy
    if efficacy_n is not None:
        payload["skill_efficacy_n"] = efficacy_n
    return _Point(pid, payload)


@pytest.fixture
def stores():
    return fr.FakeRedis(decode_responses=True), fr.FakeRedis(decode_responses=True)


def _ledger_key() -> str:
    """The real deviation-ledger key, derived exactly as the section derives
    it — a hardcoded workspace here would pass while the section read the
    wrong key."""
    from app.procedures.api import _deployment_workspace

    return f"proc:deviations:{_deployment_workspace()}"


def _deviation(days_ago=1.0, *, kind="block", skill_id="sk1", step_id="s1",
               session="sess-1", detail=""):
    return json.dumps({
        "at": iso(days_ago), "kind": kind, "skill_id": skill_id,
        "step_id": step_id, "session": session, "member": "member-owner",
        "agent": "agent-1", "command_hash": "a" * 12, "detail": detail,
    })


@pytest_asyncio.fixture
async def auth_keys():
    """Real keys with enforcement on. Two of them, because `scopes_allow` does
    not treat `admin` as a superset — the reader key is what proves the gate is
    admin-STRENGTH rather than merely present.

    Auth state is module-global (`keys._AUTH_ENABLED` / `keys._redis`), so the
    teardown restoring it is load-bearing for every other test in the process.
    """
    auth_redis = fr.FakeRedis(decode_responses=True)
    await keys.init_auth(redis_client=auth_redis, enabled=True)
    admin = await keys.create_key("owner", ["admin"])
    reader = await keys.create_key("teammate", ["memory:read"])
    try:
        yield {"admin": admin["api_key"], "reader": reader["api_key"]}
    finally:
        await keys.init_auth(redis_client=None, enabled=False)
        await auth_redis.aclose()


@pytest.fixture
def mk(auth_keys):
    """Build a client for a given set of doubles. `key=None` builds a KEYLESS
    client — used only by the auth tests."""
    def _make(vector, redis_client, replay_redis, settings=None, key="admin"):
        app = FastAPI()
        app.include_router(create_autopilot_router(
            get_redis=lambda: redis_client,
            get_replay_redis=lambda: replay_redis,
            get_vector=lambda: vector,
            settings_fn=lambda: settings or _Settings(),
        ))
        headers = {"X-API-Key": auth_keys[key]} if key else {}
        return AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://t", headers=headers)
    return _make


# ------------------------------------------------------------------- inbox --

@pytest.mark.asyncio
async def test_inbox_aggregates_every_queue(mk, stores):
    redis_client, replay_redis = stores
    qdrant = _FakeQdrant([
        skill("d1", status="draft"), skill("d2", status="draft"),
        skill("s1", status="active", stale=True),
        skill("r1", status="active", rereview=True),
        _Point("m1", {"status": "active", "contested": True,
                      "contested_with": "m2", "contested_at": iso(1),
                      "text": "the VPS is at 10.0.0.1"}),
        _Point("m3", {"status": "active", "text": "uncontested"}),
    ])
    await proc_store.write_proposals(redis_client, "sk1", [
        {"id": "p1", "kind": "dead_step", "skill_id": "sk1", "step_id": "b",
         "detail": "skipped in 9 of 10 executions"},
    ])
    await replay_redis.set("rp:eval_dlq:sess-1", json.dumps({
        "session_id": "sess-1", "error": "qdrant timeout",
        "failure_type": "infra", "timestamp": iso(1),
    }))
    await redis_client.lpush(_ledger_key(), _deviation(kind="block"))

    async with mk(_FakeVector(qdrant), redis_client, replay_redis) as c:
        resp = await c.get("/autopilot/inbox")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    items = body["items"]
    assert items["draft_skills"]["count"] == 2
    assert items["stale_skills"]["count"] == 1
    assert items["rereview_skills"]["count"] == 1
    assert items["runbook_deviations"]["count"] == 1
    assert items["procedure_proposals"] == {
        "enabled": True, "count": 1,
        "items": [{"id": "p1", "kind": "dead_step", "skill_id": "sk1",
                   "step_id": "b", "detail": "skipped in 9 of 10 executions"}],
    }
    assert items["contested_memories"]["count"] == 1
    pair = items["contested_memories"]["pairs"][0]
    assert pair["contested_with"] == "m2"
    assert pair["contested_at"] == iso(1), (
        "the flag time is what tells an operator how long a dispute has sat "
        "unresolved — the whole reason a contested pair belongs in an inbox"
    )
    assert items["eval_dlq"]["count"] == 1
    assert body["total_actionable"] == 8
    assert "degraded" not in body
    assert body["generated_at"]


@pytest.mark.asyncio
async def test_an_empty_deployment_reports_zero_not_an_error(mk, stores):
    redis_client, replay_redis = stores
    async with mk(_FakeVector(_FakeQdrant()), redis_client, replay_redis) as c:
        body = (await c.get("/autopilot/inbox")).json()
    assert body["total_actionable"] == 0
    assert body["items"]["draft_skills"]["count"] == 0
    assert body["items"]["eval_dlq"]["count"] == 0
    assert body["items"]["runbook_deviations"] == {
        "enabled": True, "count": 0, "approximate": False, "items": [],
    }
    assert "degraded" not in body


@pytest.mark.asyncio
async def test_a_broken_store_degrades_one_section_not_the_inbox(mk, stores):
    """THE case the whole surface rests on. A dead Qdrant must not also cost
    the operator the eval DLQ and the procedure proposals — five queues taken
    out by one dependency."""
    redis_client, replay_redis = stores
    qdrant = _FakeQdrant()
    qdrant.fail_all = True
    await replay_redis.set("rp:eval_dlq:sess-1", json.dumps({
        "session_id": "sess-1", "error": "boom", "failure_type": "scoring",
        "timestamp": iso(1),
    }))

    async with mk(_FakeVector(qdrant), redis_client, replay_redis) as c:
        resp = await c.get("/autopilot/inbox")

    assert resp.status_code == 200, "one broken store must not 500 the inbox"
    body = resp.json()
    assert body["items"]["draft_skills"]["error"]
    assert body["items"]["contested_memories"]["error"]
    assert body["items"]["eval_dlq"]["count"] == 1, (
        "the Redis-backed sections must still be served"
    )
    assert body["items"]["procedure_proposals"]["count"] == 0


@pytest.mark.asyncio
async def test_a_degraded_section_is_named_and_not_counted_as_zero_work(mk, stores):
    """A total that silently absorbs an unreadable queue claims there is less
    to do than anyone knows. `degraded` is what stops the number from lying."""
    redis_client, replay_redis = stores
    qdrant = _FakeQdrant([skill("d1", status="draft")])
    qdrant.fail_on_filter = "contested"

    async with mk(_FakeVector(qdrant), redis_client, replay_redis) as c:
        body = (await c.get("/autopilot/inbox")).json()

    assert body["degraded"] == ["contested_memories"]
    assert body["total_actionable"] == 1
    assert body["items"]["draft_skills"]["count"] == 1


@pytest.mark.asyncio
async def test_a_broken_replay_redis_costs_only_the_dlq_section(mk, stores):
    """The DLQ lives on `app.state.replay_redis`, a different client from the
    main one. Passing the main client instead would make this section silently
    always-empty rather than fail — which is why the factory takes both."""
    redis_client, _ = stores
    qdrant = _FakeQdrant([skill("d1", status="draft")])
    async with mk(_FakeVector(qdrant), redis_client, _DeadReplayRedis()) as c:
        body = (await c.get("/autopilot/inbox")).json()

    assert body["degraded"] == ["eval_dlq"]
    assert body["items"]["draft_skills"]["count"] == 1


@pytest.mark.asyncio
async def test_the_dlq_section_reads_planted_keys_and_survives_junk(mk, stores):
    """These keys have had NO reader since they were introduced — `compute.py`
    writes them inside a bare `except: pass` and nothing ever looked."""
    redis_client, replay_redis = stores
    for i in range(3):
        await replay_redis.set(f"rp:eval_dlq:sess-{i}", json.dumps({
            "session_id": f"sess-{i}", "error": "x" * 400,
            "failure_type": "infra", "timestamp": iso(1),
        }))
    await replay_redis.set("rp:eval_dlq:garbage", "{not json")
    await replay_redis.set("rp:other:key", "ignored")

    async with mk(_FakeVector(_FakeQdrant()), redis_client, replay_redis) as c:
        section = (await c.get("/autopilot/inbox")).json()["items"]["eval_dlq"]

    assert section["count"] == 4, "the unrelated rp:other key must not be counted"
    by_id = {i["session_id"]: i for i in section["items"]}
    assert by_id["garbage"]["error"] == "unparsed"
    assert len(by_id["sess-0"]["error"]) <= 200, "error text is truncated for the list"
    assert by_id["sess-0"]["failure_type"] == "infra"


@pytest.mark.asyncio
async def test_disabled_procedures_say_so_rather_than_returning_an_empty_list(mk, stores):
    """`enabled: false` and "nothing to propose" are different states with
    different remedies, and an empty array cannot tell them apart."""
    redis_client, replay_redis = stores
    async with mk(_FakeVector(_FakeQdrant()), redis_client,
                  replay_redis, settings=_Off()) as c:
        items = (await c.get("/autopilot/inbox")).json()["items"]

    assert items["procedure_proposals"] == {"enabled": False, "count": 0, "items": []}
    assert items["runbook_deviations"] == {"enabled": False, "count": 0, "items": []}


@pytest.mark.asyncio
async def test_sections_cap_their_item_lists_but_not_their_counts(mk, stores):
    redis_client, replay_redis = stores
    qdrant = _FakeQdrant([skill(f"d{i}", status="draft") for i in range(35)])
    async with mk(_FakeVector(qdrant), redis_client, replay_redis) as c:
        section = (await c.get("/autopilot/inbox")).json()["items"]["draft_skills"]
    assert section["count"] == 35
    assert len(section["items"]) == 20
    assert section["approximate"] is False


@pytest.mark.asyncio
async def test_a_client_authored_skill_still_gets_a_label(mk, stores):
    """`skill_create` writes no `procedure_title`, so a title-only row would
    render as a blank line next to a UUID."""
    redis_client, replay_redis = stores
    p = skill("d1", status="draft")
    del p.payload["procedure_title"]
    p.payload["trigger"] = "publishing a client release"
    async with mk(_FakeVector(_FakeQdrant([p])), redis_client, replay_redis) as c:
        row = (await c.get("/autopilot/inbox")).json()["items"]["draft_skills"]["items"][0]
    assert row["title"] == "publishing a client release"


@pytest.mark.asyncio
async def test_contested_rows_carry_the_fleet_proposal(mk, stores):
    """Fleet-as-GPU: a Night Shift proposal on a pair renders beside it in the
    inbox — resolving stays a human call to /memory/contested/resolve."""
    redis_client, replay_redis = stores
    qdrant = _FakeQdrant([
        _Point("m1", {"status": "active", "contested": True, "contested_with": "m2",
                      "contested_at": iso(1), "text": "A",
                      "proposed_verdict": {"action": "coexist", "winner_id": None},
                      "proposed_rationale": "both true", "proposed_by": "night-shift",
                      "proposed_at": iso(0.5)}),
        _Point("m2", {"status": "active", "contested": True, "contested_with": "m1",
                      "contested_at": iso(1), "text": "B"}),
    ])
    async with mk(_FakeVector(qdrant), redis_client, replay_redis) as c:
        body = (await c.get("/autopilot/inbox")).json()
    rows = {p["id"]: p for p in body["items"]["contested_memories"]["pairs"]}
    assert rows["m1"]["proposed_verdict"] == {"action": "coexist", "winner_id": None}
    assert rows["m1"]["proposed_by"] == "night-shift" and rows["m1"]["proposed_rationale"] == "both true"
    assert rows["m2"]["proposed_verdict"] is None and rows["m2"]["proposed_by"] == ""


@pytest.mark.asyncio
async def test_contested_previews_are_bounded(mk, stores):
    redis_client, replay_redis = stores
    qdrant = _FakeQdrant([_Point("m1", {
        "status": "active", "contested": True, "contested_with": "m2",
        "text": "y" * 500,
    })])
    async with mk(_FakeVector(qdrant), redis_client, replay_redis) as c:
        pair = (await c.get("/autopilot/inbox")).json()["items"]["contested_memories"]["pairs"][0]
    assert len(pair["text_preview"]) <= 120


# ------------------------------------------------- low-efficacy skills (D4) --

@pytest.mark.asyncio
async def test_low_efficacy_skills_flags_only_sufficient_evidence_below_threshold(mk, stores):
    """VISIBILITY ONLY (D4): a skill needs BOTH enough evidence
    (skill_efficacy_n >= MIN_N) and a below-neutral score (skill_efficacy <
    THRESHOLD) to be flagged. A low-n score is mostly the OWM prior, not
    signal, so it must be excluded rather than shown as if it were a
    measurement; a high-n but healthy score has nothing to triage."""
    redis_client, replay_redis = stores
    qdrant = _FakeQdrant([
        skill("flagged", status="active", title="Flagged", efficacy=0.2, efficacy_n=8),
        skill("low_n", status="active", title="LowN", efficacy=0.1, efficacy_n=1),
        skill("healthy", status="active", title="Healthy", efficacy=0.9, efficacy_n=8),
    ])
    async with mk(_FakeVector(qdrant), redis_client, replay_redis) as c:
        section = (await c.get("/autopilot/inbox")).json()["items"]["low_efficacy_skills"]

    assert section["count"] == 1
    assert len(section["items"]) == 1
    row = section["items"][0]
    assert row["id"] == "flagged"
    assert row["skill_efficacy"] == 0.2
    assert row["skill_efficacy_n"] == 8, (
        "n rides along on every row so a reader can never mistake the "
        "low-n neutral prior for signal"
    )


@pytest.mark.asyncio
async def test_low_efficacy_skills_ignores_non_active_and_unscored_skills(mk, stores):
    redis_client, replay_redis = stores
    qdrant = _FakeQdrant([
        skill("draft_low", status="draft", title="DraftLow", efficacy=0.1, efficacy_n=8),
        skill("unscored", status="active", title="Unscored"),
    ])
    async with mk(_FakeVector(qdrant), redis_client, replay_redis) as c:
        section = (await c.get("/autopilot/inbox")).json()["items"]["low_efficacy_skills"]
    assert section["count"] == 0
    assert section["items"] == []


# ------------------------------------------------- runbook deviations (C) --

@pytest.mark.asyncio
async def test_deviation_rows_carry_the_triage_fields_and_nothing_more(mk, stores):
    """The ledger record carries member/agent/command_hash; the panel row must
    not — triage needs what happened and where, not who. Pinning the exact key
    set is what keeps a later field addition a decision rather than a leak."""
    redis_client, replay_redis = stores
    await redis_client.lpush(_ledger_key(), _deviation(
        days_ago=2, kind="block", skill_id="sk9", step_id="deploy",
        session="sess-a"))
    await redis_client.lpush(_ledger_key(), _deviation(
        days_ago=1, kind="ack", detail="rollback drill, block not applicable " * 10))

    async with mk(_FakeVector(_FakeQdrant()), redis_client, replay_redis) as c:
        section = (await c.get("/autopilot/inbox")).json()["items"]["runbook_deviations"]

    assert section["count"] == 2
    assert section["approximate"] is False
    newest, older = section["items"]
    assert newest["kind"] == "ack", "LPUSH ledger renders newest-first"
    assert newest["at"] == iso(1)
    assert len(newest["detail"]) <= 120, "ack reasons are previewed, not shipped whole"
    assert older == {"at": iso(2), "kind": "block", "skill_id": "sk9",
                     "step_id": "deploy", "session": "sess-a", "detail": ""}
    assert set(newest) == {"at", "kind", "skill_id", "step_id", "session", "detail"}


@pytest.mark.asyncio
async def test_deviation_rows_are_capped_but_the_count_is_not(mk, stores):
    redis_client, replay_redis = stores
    for i in range(35):
        await redis_client.lpush(_ledger_key(), _deviation(days_ago=i))
    async with mk(_FakeVector(_FakeQdrant()), redis_client, replay_redis) as c:
        section = (await c.get("/autopilot/inbox")).json()["items"]["runbook_deviations"]
    assert section["count"] == 35
    assert len(section["items"]) == 20
    assert section["approximate"] is False


@pytest.mark.asyncio
async def test_a_full_ledger_is_reported_approximate(mk, stores, monkeypatch):
    """MAX_DEVIATIONS is a disclosed cap — the ledger LTRIMs itself, so a full
    read means older deviations are already gone and the count is a floor, not
    a census."""
    monkeypatch.setattr(proc_store, "MAX_DEVIATIONS", 5, raising=False)
    redis_client, replay_redis = stores
    for i in range(5):
        await redis_client.lpush(_ledger_key(), _deviation(days_ago=i))
    async with mk(_FakeVector(_FakeQdrant()), redis_client, replay_redis) as c:
        section = (await c.get("/autopilot/inbox")).json()["items"]["runbook_deviations"]
    assert section["count"] == 5
    assert section["approximate"] is True


@pytest.mark.asyncio
async def test_a_broken_ledger_read_degrades_only_the_deviation_section(mk, stores, monkeypatch):
    """`list_deviations` promises never to raise, but the inbox's isolation
    must not depend on another module keeping its promise — the `_section`
    guard is what makes that promise non-load-bearing here."""
    redis_client, replay_redis = stores

    async def boom(*a, **k):
        raise RuntimeError("ledger unreadable")
    monkeypatch.setattr(proc_store, "list_deviations", boom, raising=False)

    qdrant = _FakeQdrant([skill("d1", status="draft")])
    async with mk(_FakeVector(qdrant), redis_client, replay_redis) as c:
        resp = await c.get("/autopilot/inbox")

    assert resp.status_code == 200, "one broken ledger must not 500 the inbox"
    body = resp.json()
    assert body["degraded"] == ["runbook_deviations"]
    assert body["items"]["runbook_deviations"]["error"]
    assert body["items"]["draft_skills"]["count"] == 1
    assert body["total_actionable"] == 1


# ------------------------------------------------------------------ digest --

@pytest.mark.asyncio
async def test_digest_counts_the_window(mk, stores, monkeypatch):
    redis_client, replay_redis = stores
    monkeypatch.setattr(digest_mod, "datetime", _FrozenDatetime)
    qdrant = _FakeQdrant([
        _Point("m1", {"status": "active", "timestamp": iso(2), "text": "in"}),
        _Point("m2", {"status": "active", "timestamp": iso(3), "text": "in"}),
        _Point("m3", {"status": "active", "timestamp": iso(40), "text": "old"}),
        _Point("m4", {"status": "active", "timestamp": iso(1), "source": "corpus"}),
        _Point("m5", {"status": "active", "timestamp": iso(1), "source": "dream"}),
        _Point("m6", {"status": "archived", "timestamp": iso(50),
                      "archived_at": iso(2)}),
        # Superseded BY m1, which was written inside the window.
        _Point("m7", {"status": "superseded", "timestamp": iso(60),
                      "superseded_by": "m1"}),
        # Superseded by something written long before the window.
        _Point("m8", {"status": "superseded", "timestamp": iso(70),
                      "superseded_by": "m3"}),
        _Point("m9", {"status": "active", "timestamp": iso(80),
                      "feedback_last_at": iso(3)}),
        skill("sk1", status="draft", created=iso(2)),
        skill("sk2", status="active", created=iso(60), reviewed=iso(1)),
    ])
    await redis_client.lpush("gc:eviction:log", json.dumps(
        {"action": "archived", "id": "m6", "occurred_at": iso(2)}))
    await redis_client.lpush("gc:eviction:log", json.dumps(
        {"id": "legacy", "evicted_at": iso(4)}))
    await redis_client.lpush("gc:eviction:log", json.dumps(
        {"action": "purged", "id": "old", "occurred_at": iso(60)}))

    async with mk(_FakeVector(qdrant), redis_client, replay_redis) as c:
        body = (await c.get("/autopilot/digest?days=7")).json()

    counts = body["counts"]
    assert counts["memories_learned"] == 2, "corpus, dream and skills are not memories"
    assert counts["dream_insights"] == 1
    assert counts["memories_archived"] == 1
    assert counts["memories_superseded"] == 1, (
        "only the one superseded by a memory written inside the window"
    )
    assert counts["feedback_given"] == 1
    assert counts["skills_drafted"] == 1
    assert counts["skills_activated"] == 1
    assert counts["gc_actions"] == 2, "the legacy evicted_at entry counts too"
    assert body["window_days"] == 7
    assert body["approximate"] is False
    assert "errors" not in body


@pytest.mark.asyncio
async def test_legacy_supersession_without_stamp_falls_back_to_keeper_window(mk, stores, monkeypatch):
    """Supersessions from before `superseded_at` existed carry no stamp, and
    for those the window is recovered from the fact that learn-time
    supersession happens INSIDE the learn that causes it: a memory superseded
    by a memory written last month is not this week's news even though it is
    still `status=superseded` today. Stamped supersessions never reach this
    fallback (next test)."""
    redis_client, replay_redis = stores
    monkeypatch.setattr(digest_mod, "datetime", _FrozenDatetime)
    qdrant = _FakeQdrant([
        _Point("new", {"status": "active", "timestamp": iso(1)}),
        _Point("mid", {"status": "active", "timestamp": iso(45)}),
        _Point("a", {"status": "superseded", "timestamp": iso(200),
                     "superseded_by": "new"}),
        _Point("b", {"status": "superseded", "timestamp": iso(200),
                     "superseded_by": "mid"}),
        _Point("c", {"status": "superseded", "timestamp": iso(200)}),
    ])
    async with mk(_FakeVector(qdrant), redis_client, replay_redis) as c:
        week = (await c.get("/autopilot/digest?days=7")).json()["counts"]
        quarter = (await c.get("/autopilot/digest?days=90")).json()["counts"]

    assert week["memories_superseded"] == 1
    assert quarter["memories_superseded"] == 2, (
        "widening the window must pick up the older supersession"
    )


@pytest.mark.asyncio
async def test_stamped_supersession_counts_regardless_of_keeper_age(mk, stores, monkeypatch):
    """`superseded_at` (stamped by update_status and the deep pass since
    0.4.0) is authoritative when present. The keeper heuristic alone scored a
    nightly-pass supersession under a months-old confirmed keeper as zero —
    a week in which the deep pass superseded ten memories produced
    `memories_superseded: 0` and a digest that confidently reported real
    activity as absent."""
    redis_client, replay_redis = stores
    monkeypatch.setattr(digest_mod, "datetime", _FrozenDatetime)
    qdrant = _FakeQdrant([
        # The keeper is months old — the legacy heuristic would say "not
        # this week's news". The stamp says otherwise, and wins.
        _Point("keeper", {"status": "active", "timestamp": iso(180),
                          "confirmed_count": 3}),
        _Point("deep-pass-loser", {"status": "superseded", "timestamp": iso(200),
                                   "superseded_by": "keeper",
                                   "superseded_at": iso(2)}),
        # Stamped OUTSIDE the window: must not count, even though its keeper
        # exists — the stamp is authoritative in both directions.
        _Point("old-loser", {"status": "superseded", "timestamp": iso(200),
                             "superseded_by": "keeper",
                             "superseded_at": iso(60)}),
    ])
    async with mk(_FakeVector(qdrant), redis_client, replay_redis) as c:
        week = (await c.get("/autopilot/digest?days=7")).json()["counts"]

    assert week["memories_superseded"] == 1


@pytest.mark.asyncio
async def test_digest_summary_is_a_sentence_a_human_can_read(mk, stores, monkeypatch):
    redis_client, replay_redis = stores
    monkeypatch.setattr(digest_mod, "datetime", _FrozenDatetime)
    qdrant = _FakeQdrant([
        _Point("m1", {"status": "active", "timestamp": iso(1), "text": "a"}),
        _Point("m2", {"status": "active", "timestamp": iso(1), "text": "b"}),
        _Point("m3", {"status": "archived", "timestamp": iso(50),
                      "archived_at": iso(1)}),
    ])
    async with mk(_FakeVector(qdrant), redis_client, replay_redis) as c:
        body = (await c.get("/autopilot/digest")).json()
    assert body["summary"] == (
        "In the last 7 days Firekeep learned 2 memories and archived 1."
    )


@pytest.mark.asyncio
async def test_a_quiet_week_says_so_instead_of_listing_zeros(mk, stores, monkeypatch):
    redis_client, replay_redis = stores
    monkeypatch.setattr(digest_mod, "datetime", _FrozenDatetime)
    async with mk(_FakeVector(_FakeQdrant()), redis_client, replay_redis) as c:
        body = (await c.get("/autopilot/digest")).json()
    assert body["summary"] == "No knowledge-base activity in the last 7 days."
    assert body["counts"]["memories_learned"] == 0


@pytest.mark.asyncio
async def test_days_is_clamped_by_the_route(mk, stores):
    redis_client, replay_redis = stores
    async with mk(_FakeVector(_FakeQdrant()), redis_client, replay_redis) as c:
        assert (await c.get("/autopilot/digest?days=0")).status_code == 422
        assert (await c.get("/autopilot/digest?days=91")).status_code == 422
        assert (await c.get("/autopilot/digest?days=90")).status_code == 200
        assert (await c.get("/autopilot/digest?days=1")).status_code == 200


@pytest.mark.asyncio
async def test_a_capped_scan_is_reported_as_approximate(mk, stores, monkeypatch):
    """Scroll pages by point ID, uncorrelated with time, so a capped scan is an
    arbitrary sample rather than "the most recent N". Reporting it as a census
    is the failure; saying so is the fix available without a payload index on
    `timestamp` (see `app/dreams/task.py` for why there isn't one)."""
    redis_client, replay_redis = stores
    monkeypatch.setattr(digest_mod, "datetime", _FrozenDatetime)
    monkeypatch.setattr(digest_mod, "SCAN_CAP", 4)
    monkeypatch.setattr(digest_mod, "SCAN_BATCH", 2)
    qdrant = _FakeQdrant([
        _Point(f"m{i}", {"status": "active", "timestamp": iso(1)}) for i in range(10)
    ])
    async with mk(_FakeVector(qdrant), redis_client, replay_redis) as c:
        body = (await c.get("/autopilot/digest")).json()

    assert body["approximate"] is True
    assert body["scanned"] == 4
    assert body["counts"]["memories_learned"] == 4
    assert any("capped" in n for n in body["notes"])
    assert body["summary"].startswith("At least in the last 7 days"), (
        "an under-count must not be phrased as a measurement"
    )


@pytest.mark.asyncio
async def test_the_digest_documents_its_two_proxies(mk, stores):
    redis_client, replay_redis = stores
    async with mk(_FakeVector(_FakeQdrant()), redis_client, replay_redis) as c:
        notes = " ".join((await c.get("/autopilot/digest")).json()["notes"])
    assert "stale_reviewed_at" in notes, "the activation proxy must be stated"
    assert "feedback_last_at" in notes


@pytest.mark.asyncio
async def test_a_dead_redis_still_yields_the_memory_numbers(mk, stores, monkeypatch):
    """All-or-nothing would report nothing on the day one dependency is
    unhappy — the day the digest is most worth reading."""
    _, replay_redis = stores
    monkeypatch.setattr(digest_mod, "datetime", _FrozenDatetime)
    qdrant = _FakeQdrant([_Point("m1", {"status": "active", "timestamp": iso(1)})])
    async with mk(_FakeVector(qdrant), _DeadRedis(), replay_redis) as c:
        body = (await c.get("/autopilot/digest")).json()

    assert body["counts"]["memories_learned"] == 1
    assert body["counts"]["gc_actions"] == 0
    assert "gc_actions" in body["errors"]


@pytest.mark.asyncio
async def test_a_dead_qdrant_still_yields_the_gc_number(mk, stores, monkeypatch):
    redis_client, replay_redis = stores
    monkeypatch.setattr(digest_mod, "datetime", _FrozenDatetime)
    qdrant = _FakeQdrant()
    qdrant.fail_all = True
    await redis_client.lpush("gc:eviction:log", json.dumps(
        {"action": "archived", "id": "x", "occurred_at": iso(1)}))

    async with mk(_FakeVector(qdrant), redis_client, replay_redis) as c:
        body = (await c.get("/autopilot/digest")).json()

    assert body["counts"]["gc_actions"] == 1
    assert "memories" in body["errors"]


# -------------------------------------------------------------------- auth --

@pytest.mark.asyncio
async def test_both_routes_refuse_a_keyless_caller(mk, stores):
    """The dependencies are built inside a try/except, so a broken auth import
    would serve this operator surface — session ids, error strings, whole-store
    activity — completely ungated with nothing else in the suite noticing."""
    redis_client, replay_redis = stores
    async with mk(_FakeVector(_FakeQdrant()), redis_client, replay_redis, key=None) as c:
        assert (await c.get("/autopilot/inbox")).status_code == 401
        assert (await c.get("/autopilot/digest")).status_code == 401
        assert (await c.get("/autopilot/compliance")).status_code == 401


@pytest.mark.asyncio
async def test_a_memory_read_key_cannot_open_the_inbox(mk, stores):
    """`memory:read` is held by every agent key in the deployment. This is an
    operator surface, and admin here is a strength claim, not decoration —
    `scopes_allow` does not treat admin as a superset, so the reader key is a
    real discriminator."""
    redis_client, replay_redis = stores
    async with mk(_FakeVector(_FakeQdrant()), redis_client,
                  replay_redis, key="reader") as c:
        assert (await c.get("/autopilot/inbox")).status_code == 403
        assert (await c.get("/autopilot/digest")).status_code == 403
        assert (await c.get("/autopilot/compliance")).status_code == 403


# -------------------------------------------------- compliance (Living Instructions) --

def _eval_record(sid, days_ago=1.0, task_result=None, task_result_source=None,
                  experiment_group=None, **metrics):
    body = {
        "session_id": sid,
        "created_at": iso(days_ago),
        "trigger": "session_complete",
        "metrics": metrics,
    }
    if task_result is not None:
        body["task_result"] = task_result
    if task_result_source is not None:
        body["task_result_source"] = task_result_source
    if experiment_group is not None:
        body["experiment_group"] = experiment_group
    return json.dumps(body)


@pytest.mark.asyncio
async def test_compliance_scores_the_founding_predicates(mk, stores):
    """Each row must reproduce the 2026-08-11 founding-measurement predicate
    exactly — the spec is a pre-registration, and a drifted predicate would
    orphan the baseline every later comparison stands on."""
    redis_client, replay_redis = stores
    # One compliant-on-everything session (incl. a self-reported grade, so
    # the new grade_self_reported row — PR4 D2 — is also 1/2 like the rest),
    # one blank one.
    await replay_redis.set("rp:eval:s1", _eval_record(
        "s1", memory_read_count=2, memory_write_count=1, recall_used_rate=0.5,
        context_snapshot_count=3, brier_score=0.11, outcome_event_count=2,
        task_result="success", task_result_source="self_reported"))
    await replay_redis.set("rp:eval:s2", _eval_record(
        "s2", memory_read_count=0, memory_write_count=0, recall_used_rate=0.0,
        context_snapshot_count=0, outcome_event_count=1))

    async with mk(_FakeVector(_FakeQdrant()), redis_client, replay_redis) as c:
        resp = await c.get("/autopilot/compliance")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sessions_evaluated"] == 2
    assert body["unparsed"] == 0
    rows = {r["key"]: r for r in body["instructions"]}
    assert set(rows) == {
        "recall_before_work", "write_as_you_go", "recall_visibly_used",
        "ctx_working_state", "declared_predictions", "outcome_bearing",
        "grade_self_reported",
    }
    for key in rows:
        assert rows[key]["hits"] == 1, key
        assert rows[key]["rate"] == 0.5, key
    # brier presence, not truthiness: a PERFECT calibration score of 0.0 is
    # compliance, and a truthiness predicate would count it as silence.
    await replay_redis.set("rp:eval:s3", _eval_record("s3", brier_score=0.0))
    async with mk(_FakeVector(_FakeQdrant()), redis_client, replay_redis) as c:
        rows3 = {r["key"]: r for r in
                 (await c.get("/autopilot/compliance")).json()["instructions"]}
    assert rows3["declared_predictions"]["hits"] == 2


@pytest.mark.asyncio
async def test_compliance_counts_unparsed_instead_of_dropping(mk, stores):
    """A record that fails to parse is COUNTED — '32 sessions' must never
    quietly mean '32 of an unknown many'."""
    redis_client, replay_redis = stores
    await replay_redis.set("rp:eval:good", _eval_record("good", memory_read_count=1))
    await replay_redis.set("rp:eval:bad", "not json")
    await replay_redis.set("rp:eval:nometrics", json.dumps({"session_id": "x"}))

    async with mk(_FakeVector(_FakeQdrant()), redis_client, replay_redis) as c:
        body = (await c.get("/autopilot/compliance")).json()

    assert body["sessions_evaluated"] == 1
    assert body["unparsed"] == 2


@pytest.mark.asyncio
async def test_compliance_ignores_the_dlq_and_the_index(mk, stores):
    """rp:eval_dlq:* and rp:eval_index share the neighborhood but not the
    prefix — a scan that swept them in would score failure records as
    sessions."""
    redis_client, replay_redis = stores
    await replay_redis.set("rp:eval:s1", _eval_record("s1"))
    await replay_redis.set("rp:eval_dlq:s2", json.dumps({"error": "boom"}))
    await replay_redis.zadd("rp:eval_index", {"s1": 1.0})

    async with mk(_FakeVector(_FakeQdrant()), redis_client, replay_redis) as c:
        body = (await c.get("/autopilot/compliance")).json()

    assert body["sessions_evaluated"] == 1
    assert body["unparsed"] == 0


@pytest.mark.asyncio
async def test_compliance_trend_is_withheld_below_the_floor(mk, stores):
    """With a handful of sessions a halves-split arrow is noise; the honest
    move is absence, not a small-print asterisk."""
    redis_client, replay_redis = stores
    for i in range(4):
        await replay_redis.set(f"rp:eval:s{i}", _eval_record(f"s{i}", days_ago=i,
                                                             memory_read_count=1))
    async with mk(_FakeVector(_FakeQdrant()), redis_client, replay_redis) as c:
        rows = (await c.get("/autopilot/compliance")).json()["instructions"]
    assert all("recent_rate" not in r and "earlier_rate" not in r for r in rows)


@pytest.mark.asyncio
async def test_compliance_trend_splits_halves_by_eval_time(mk, stores):
    """Older half all non-compliant, newer half all compliant: the split must
    put the improvement where it happened, not average it away."""
    redis_client, replay_redis = stores
    for i in range(5):  # older half: no reads
        await replay_redis.set(f"rp:eval:old{i}",
                               _eval_record(f"old{i}", days_ago=20 + i,
                                            memory_read_count=0))
    for i in range(5):  # newer half: reads
        await replay_redis.set(f"rp:eval:new{i}",
                               _eval_record(f"new{i}", days_ago=1 + i,
                                            memory_read_count=2))
    async with mk(_FakeVector(_FakeQdrant()), redis_client, replay_redis) as c:
        rows = {r["key"]: r for r in
                (await c.get("/autopilot/compliance")).json()["instructions"]}
    row = rows["recall_before_work"]
    assert row["earlier_rate"] == 0.0
    assert row["recent_rate"] == 1.0
    assert row["rate"] == 0.5


@pytest.mark.asyncio
async def test_compliance_empty_store_is_honest_zeros(mk, stores):
    redis_client, replay_redis = stores
    async with mk(_FakeVector(_FakeQdrant()), redis_client, replay_redis) as c:
        body = (await c.get("/autopilot/compliance")).json()
    assert body["sessions_evaluated"] == 0
    assert all(r["rate"] is None for r in body["instructions"])
    assert any("BEHAVIOR" in n for n in body["notes"]), (
        "the compliance!=quality caveat is part of the response contract — "
        "the dashboard must not be able to show the numbers without it"
    )


@pytest.mark.asyncio
async def test_compliance_survives_a_non_numeric_metric(mk, stores):
    """External review, 2026-08-11: a string where a count belongs raised
    TypeError inside build_rows and 500'd the endpoint — one poisoned metric
    blanking the table, contradicting the per-record isolation the scan
    claims. Non-numeric values read as absent; the record still counts."""
    redis_client, replay_redis = stores
    await replay_redis.set("rp:eval:ok", _eval_record("ok", memory_read_count=2))
    await replay_redis.set("rp:eval:poisoned", _eval_record(
        "poisoned", memory_read_count="not-a-number", brier_score="also-not"))

    async with mk(_FakeVector(_FakeQdrant()), redis_client, replay_redis) as c:
        resp = await c.get("/autopilot/compliance")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sessions_evaluated"] == 2
    rows = {r["key"]: r for r in body["instructions"]}
    assert rows["recall_before_work"]["hits"] == 1
    # A non-numeric brier is not a declared prediction, and a bool must not
    # masquerade as a count either.
    assert rows["declared_predictions"]["hits"] == 0


@pytest.mark.asyncio
async def test_compliance_trend_floor_counts_dated_evals_not_all(mk, stores):
    """External review, 2026-08-11: flooring on ALL evals let ten records with
    two dates render a 1-vs-1 comparison as a trend."""
    redis_client, replay_redis = stores
    for i in range(2):
        await replay_redis.set(f"rp:eval:dated{i}",
                               _eval_record(f"dated{i}", days_ago=i + 1,
                                            memory_read_count=1))
    for i in range(8):
        raw = json.loads(_eval_record(f"undated{i}", memory_read_count=1))
        del raw["created_at"]
        await replay_redis.set(f"rp:eval:undated{i}", json.dumps(raw))

    async with mk(_FakeVector(_FakeQdrant()), redis_client, replay_redis) as c:
        body = (await c.get("/autopilot/compliance")).json()

    assert body["sessions_evaluated"] == 10
    assert body["dated_sessions"] == 2
    assert all("recent_rate" not in r and "earlier_rate" not in r
               for r in body["instructions"])
