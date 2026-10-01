"""Semantic skill matching — GET /skills?q= and the shared search_skill_points helper.

The bug this suite exists to prevent had TWO defects, and it shipped because no test
could see either:

  (a) a literal SUBSTRING post-filter (`ql in r.trigger.lower()`), so a task phrased
      any differently than the trigger matched nothing; and
  (b) `limit` applied to an ID-ordered Qdrant `scroll` BEFORE that filter, so the
      relevant skill was usually not even in the candidate page.

Every pre-existing Qdrant fake in this repo (FakeQdrantStore.query_points in
test_knowledge_e2e.py, _filtering_query_points in test_vector.py) accepts `query` and
`score_threshold` and ignores BOTH, returning filter-matches in insertion order. A test
built on those can only ever prove filtering — which is why defect (b) was invisible.
`scoring_query_points` below actually ranks by cosine and enforces the floor, and is a
hard prerequisite for the ordering proof rather than a nicety.
"""
import math

import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.skills.api import create_skills_router


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1e-9
    nb = math.sqrt(sum(x * x for x in b)) or 1e-9
    return dot / (na * nb)


def _match_values(match):
    """Normalize a Qdrant match condition (MatchValue or MatchAny) to the set
    of values it accepts. The briefing's skills_section (Task 3) filters
    skill_status with MatchAny(["active", "trial"]) rather than a single
    MatchValue, so this fake tests membership instead of equality."""
    values = getattr(match, "any", None)
    return set(values) if values is not None else {match.value}


def _matches(payload, flt):
    """Evaluate a Qdrant Filter's must/must_not the way _filtering_scroll does."""
    for c in (getattr(flt, "must", None) or []):
        if (payload or {}).get(c.key) not in _match_values(c.match):
            return False
    for c in (getattr(flt, "must_not", None) or []):
        if (payload or {}).get(c.key) in _match_values(c.match):
            return False
    return True


def scoring_query_points(points, vectors):
    """A query_points fake that RANKS. `vectors` maps point id -> embedding.

    Unlike every other fake in the repo this one honours `query` (cosine rank) and
    `score_threshold` (drops below-floor points), which is what makes the
    ID-ordering half of the original bug observable.
    """
    async def _qp(*, collection_name, query, query_filter, limit,
                  with_payload=True, score_threshold=None, **_kw):
        scored = []
        for p in points:
            if not _matches(p.payload, query_filter):
                continue
            vec = vectors.get(str(p.id))
            if vec is None:
                continue
            s = _cosine(query, vec)
            if score_threshold is not None and s < score_threshold:
                continue
            hit = MagicMock()
            hit.id, hit.payload, hit.score = p.id, p.payload, s
            scored.append(hit)
        scored.sort(key=lambda h: h.score, reverse=True)
        res = MagicMock()
        res.points = scored[:limit]
        return res
    return _qp


def scoring_scroll(points):
    """Insertion-ordered scroll, filter-aware — the legacy path, so a single test can
    distinguish 'ranked semantically' from 'happened to be first by ID'."""
    async def _scroll(*, scroll_filter, limit, **_kw):
        matched = [p for p in points if _matches(p.payload, scroll_filter)]
        return matched[:limit], None
    return _scroll


def _point(skill_id, trigger, status="active", domain="neo4j", **extra):
    p = MagicMock()
    p.id = skill_id
    p.payload = {
        "memory_type": "skill", "skill_status": status,
        "trigger": trigger, "symptoms": "Error Y",
        "content": f"trigger: {trigger}\n---\nbody",
        "domain": domain, "skill_score": 0.75,
        "source_session_id": "s1", "project": "myproject",
        "agent_id": "me", "namespace": "default",
        "timestamp": "2026-05-23T00:00:00+00:00",
    }
    p.payload.update(extra)
    return p


@pytest.fixture
def settings():
    s = MagicMock()
    s.QDRANT_COLLECTION = "firekeep_memory"
    s.SKILL_MATCH_SCORE_FLOOR = 0.30
    s.SKILL_MATCH_EMBED_TIMEOUT_SECONDS = 1.2
    return s


@pytest.fixture
def vector():
    v = MagicMock()
    v._client = AsyncMock()
    v._embed = AsyncMock(return_value=[1.0, 0.0, 0.0])
    return v


def _app(vector, settings):
    app = FastAPI()
    app.include_router(create_skills_router(lambda: settings))
    from app.main import get_vector
    app.dependency_overrides[get_vector] = lambda: vector
    return app


# ---------------------------------------------------------------------------
# PROOF OF BROKENNESS — must FAIL on the pre-fix code.
# ---------------------------------------------------------------------------
def test_semantically_adjacent_skill_is_returned(vector, settings):
    """A task that shares NO literal substring with the trigger must still match.

    Pre-fix this returns [] — `"the vector DB keeps dropping writes"` is not a
    substring of `"Qdrant upserts fail after a collection rebuild"`, so the post-filter
    at skills/api.py:85-90 rejects it regardless of relevance.
    """
    sk = _point("qdrant1", "Qdrant upserts fail after a collection rebuild")
    vector._client.scroll = scoring_scroll([sk])
    vector._client.query_points = scoring_query_points([sk], {"qdrant1": [1.0, 0.0, 0.0]})

    client = TestClient(_app(vector, settings))
    resp = client.get("/skills", params={"q": "the vector DB keeps dropping writes"})

    assert resp.status_code == 200
    ids = {d["id"] for d in resp.json()}
    assert "qdrant1" in ids, "semantically-adjacent skill was dropped by substring filter"


def test_best_match_wins_even_when_last_by_id_order(vector, settings):
    """The OTHER half of the bug: `limit` was applied to an ID-ordered scroll BEFORE
    filtering, so the right skill was usually not even a candidate.

    Ten skills, the correct one inserted LAST, limit=3. On the legacy path it never
    reaches the page; ranked, it comes first. Only a scoring fake can catch this —
    which is why every pre-existing fake in the repo cannot.
    """
    others = [_point(f"noise{i}", f"Unrelated procedure {i}") for i in range(9)]
    target = _point("target", "Restart the embeddings backend when recall returns empty")
    pts = others + [target]
    vecs = {f"noise{i}": [0.0, 1.0, 0.0] for i in range(9)}
    vecs["target"] = [1.0, 0.0, 0.0]

    vector._client.scroll = scoring_scroll(pts)
    vector._client.query_points = scoring_query_points(pts, vecs)

    client = TestClient(_app(vector, settings))
    resp = client.get("/skills", params={"q": "embeddings are down", "limit": 3})

    data = resp.json()
    assert data[0]["id"] == "target", f"expected best match first, got {[d['id'] for d in data]}"


def test_draft_status_with_query_still_returns_drafts(vector, settings):
    """HIGHEST SEVERITY. The dashboard review queue calls GET /skills?status=draft.

    This is the exact property that makes VectorClient.search() unusable here — its
    `must_not skill_status="draft"` is unconditional. The pre-existing draft test
    sends no `q`, so it cannot catch a regression on the semantic path.
    """
    draft = _point("doc1", "Rotate the Confluence PAT", status="draft")
    active = _point("abc", "Rotate the Confluence PAT", status="active")
    vector._client.scroll = scoring_scroll([draft, active])
    vector._client.query_points = scoring_query_points(
        [draft, active], {"doc1": [1.0, 0.0, 0.0], "abc": [1.0, 0.0, 0.0]}
    )

    client = TestClient(_app(vector, settings))
    resp = client.get("/skills", params={"status": "draft", "q": "how do I rotate the PAT"})

    data = resp.json()
    assert [d["id"] for d in data] == ["doc1"]
    assert data[0]["skill_status"] == "draft"


def test_semantic_path_is_actually_taken(vector, settings):
    """Positive assertion that the NEW code ran.

    Without this, the broad embed catch lets a misconfigured fixture fall through to
    the legacy scroll and report false coverage — the exact way the original bug
    stayed invisible.
    """
    sk = _point("s1", "Anything at all")
    vector._client.scroll = AsyncMock(return_value=([], None))
    vector._client.query_points = scoring_query_points([sk], {"s1": [1.0, 0.0, 0.0]})

    client = TestClient(_app(vector, settings))
    resp = client.get("/skills", params={"q": "some task description"})

    assert resp.status_code == 200
    vector._embed.assert_awaited_once_with("some task description")
    vector._client.scroll.assert_not_awaited()


def test_filter_is_carried_through_verbatim(vector, settings):
    """The caller's `must` must reach Qdrant unchanged: memory_type, the requested
    status, a LOWERCASED project, domain — and no `stale` condition when the param is
    None (its three-state append-only contract)."""
    captured = {}

    async def _qp(*, query_filter, **kw):
        captured["must"] = {c.key: c.match.value for c in (query_filter.must or [])}
        captured["score_threshold"] = kw.get("score_threshold")
        res = MagicMock()
        res.points = []
        return res

    vector._client.query_points = _qp
    vector._client.scroll = AsyncMock(return_value=([], None))

    client = TestClient(_app(vector, settings))
    client.get("/skills", params={"q": "x", "project": "MyProj", "domain": "neo4j"})

    must = captured["must"]
    assert must["memory_type"] == "skill"
    assert must["skill_status"] == "active"
    assert must["project"] == "myproj", "project must be lowercased, as the legacy path did"
    assert must["domain"] == "neo4j"
    assert "stale" not in must, "stale must be absent when unset, not hardcoded False"
    assert captured["score_threshold"] == settings.SKILL_MATCH_SCORE_FLOOR


def test_below_floor_points_are_dropped_then_fall_back(vector, settings):
    """The floor is enforced — and cannot regress below the legacy path.

    Nothing clears the floor, so the semantic path yields nothing; the helper then
    hands back the scroll page and the caller re-applies its substring filter, which
    still finds the literal match. This is the mechanical proof that no floor value
    can make matching worse than before the fix.
    """
    sk = _point("lit1", "kubectl drain hangs on a stuck finalizer")
    vector._client.scroll = scoring_scroll([sk])
    # Orthogonal vector -> cosine 0.0, below the 0.30 floor.
    vector._client.query_points = scoring_query_points([sk], {"lit1": [0.0, 1.0, 0.0]})

    client = TestClient(_app(vector, settings))
    resp = client.get("/skills", params={"q": "kubectl drain"})

    assert [d["id"] for d in resp.json()] == ["lit1"]


def test_no_query_never_embeds(vector, settings):
    """The dashboard's exact request shape. Listing and the review queue must acquire
    ZERO dependency on the embedding backend — they render with Ollama down today and
    must keep doing so."""
    sk = _point("d1", "T", status="draft")
    vector._client.scroll = scoring_scroll([sk])
    vector._client.query_points = AsyncMock()

    client = TestClient(_app(vector, settings))
    resp = client.get("/skills", params={"status": "draft", "limit": 50})

    assert resp.status_code == 200
    vector._embed.assert_not_awaited()
    vector._client.query_points.assert_not_awaited()


def test_embed_failure_degrades_to_scroll_with_200(vector, settings):
    """An embeddings outage must not 502 skill listing."""
    sk = _point("lit1", "restart the ollama pod")
    vector._client.scroll = scoring_scroll([sk])
    vector._client.query_points = AsyncMock()
    vector._embed = AsyncMock(side_effect=RuntimeError("embeddings backend down"))

    client = TestClient(_app(vector, settings))
    resp = client.get("/skills", params={"q": "restart the ollama pod"})

    assert resp.status_code == 200
    assert [d["id"] for d in resp.json()] == ["lit1"]  # legacy substring path still works
    vector._client.query_points.assert_not_awaited()


def test_hung_embed_is_bounded_by_timeout(vector, settings):
    """A hung backend must cost the timeout, not three 30s httpx retries plus backoff."""
    import asyncio as _a
    settings.SKILL_MATCH_EMBED_TIMEOUT_SECONDS = 0.01

    async def _hang(_q):
        await _a.sleep(5)
        return [1.0, 0.0, 0.0]

    sk = _point("lit1", "anything")
    vector._embed = _hang
    vector._client.scroll = scoring_scroll([sk])
    vector._client.query_points = AsyncMock()

    client = TestClient(_app(vector, settings))
    resp = client.get("/skills", params={"q": "zzz"})

    assert resp.status_code == 200
    vector._client.query_points.assert_not_awaited()


def test_qdrant_failure_propagates(vector, settings):
    """Fail-soft is scoped to the EMBED. A storage failure must surface exactly as the
    legacy scroll's would — never be laundered into an empty result set."""
    vector._client.query_points = AsyncMock(side_effect=RuntimeError("qdrant unreachable"))
    vector._client.scroll = AsyncMock(return_value=([], None))

    client = TestClient(_app(vector, settings), raise_server_exceptions=False)
    resp = client.get("/skills", params={"q": "x"})

    assert resp.status_code >= 500
    vector._client.scroll.assert_not_awaited()  # no silent fallback on a storage error


# ---------------------------------------------------------------------------
# Briefing section — the second call site that carried the same bug inline.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_briefing_empty_goal_does_not_embed(vector, settings):
    """The production-NORMAL path: a standard Claude Code SessionStart sends no goal."""
    from app.briefing import sections as S

    sk = _point("s1", "Some skill")
    vector._client.scroll = scoring_scroll([sk])
    vector._client.query_points = AsyncMock()

    sec = await S.skills_section(vector, settings, goal="", project=None)

    assert sec["status"] == "ok"
    assert [s["id"] for s in sec["data"]["skills"]] == ["s1"]
    vector._embed.assert_not_awaited()


@pytest.mark.asyncio
async def test_briefing_matches_semantically(vector, settings):
    """The goal is NOT a literal substring of the trigger — the pre-fix inline filter
    would have rejected this."""
    from app.briefing import sections as S

    sk = _point("s1", "Qdrant upserts fail after a collection rebuild")
    vector._client.scroll = scoring_scroll([sk])
    vector._client.query_points = scoring_query_points([sk], {"s1": [1.0, 0.0, 0.0]})

    sec = await S.skills_section(
        vector, settings, goal="the vector DB keeps dropping writes", project=None
    )

    assert [s["id"] for s in sec["data"]["skills"]] == ["s1"]
    # Positive proof the semantic branch ran, not the scroll fallback. Two
    # awaits, not one: the main recallable lookup found no trial, so the
    # tier-scoped trial fallback ran too. It embeds the SAME goal, which
    # `vector._embed` caches by content hash, so the second is a cache hit
    # rather than a second round trip — asserting the query text is what
    # matters, and that no other string is ever embedded here.
    assert vector._embed.await_count == 2
    assert {call.args[0] for call in vector._embed.await_args_list} == {
        "the vector DB keeps dropping writes"
    }


@pytest.mark.asyncio
async def test_briefing_never_unavailable_on_embed_failure(vector, settings):
    """`_run_section` turns any raise into status='unavailable', which sets
    degraded=true on the whole envelope and prints '[SKILLS unavailable: ...]' into
    EVERY session's briefing. An embeddings outage must never do that."""
    from app.briefing import sections as S

    vector._embed = AsyncMock(side_effect=RuntimeError("backend down"))
    vector._client.scroll = scoring_scroll([_point("s1", "T")])
    vector._client.query_points = AsyncMock()

    sec = await S.skills_section(vector, settings, goal="a real goal", project=None)

    assert sec["status"] in ("ok", "empty")
    # The section reports the degradation instead of hiding it. It must NOT
    # become 'unavailable' (that is what flips the envelope), but a bare
    # status='empty' with a null error was indistinguishable from an empty
    # skill store — which is how a live deployment reported "no skills" with
    # 28 skills present and degraded=false, and why nobody noticed for weeks.
    assert sec["error"] == "skill match degraded to scroll"
    assert sec["data"]["match"] == "degraded-scroll"
    # And the trial fallback does NOT run here. With the backend down a second
    # attempt would spend another embed timeout inside a section capped at
    # 2.0s, and the degraded path's substring narrowing would reject whatever
    # came back anyway.
    assert vector._embed.await_count == 1
