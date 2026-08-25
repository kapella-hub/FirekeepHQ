"""SP1b §11: tip-shown A/B join closes only when briefing_id -> session_id
reconciliation is applied.

Tips are recorded in the briefing under the server-minted briefing_id (see
strategy_tips_section), NOT the session_id. compute_tip_effectiveness joins
tip logs against SessionFeatures.session_id, so without the map the join
never closes. With the map it does.
"""
from __future__ import annotations

from types import SimpleNamespace

import fakeredis.aioredis
import pytest
import pytest_asyncio

from app.patterns.models import PatternCard, SessionFeatures
from app.patterns.store import (
    store_features, store_patterns, record_tip_shown, compute_tip_effectiveness,
)

pytestmark = pytest.mark.asyncio


async def _seed(r):
    # >= 5 features required (compute returns [] below that threshold)
    await store_features(r, SessionFeatures(session_id="sess-win", outcome="success",
                                             outcome_source="task_result"))
    for i in range(4):
        await store_features(r, SessionFeatures(session_id=f"f{i}", outcome="failure",
                                                 outcome_source="task_result"))
    await store_patterns(r, [PatternCard(id="pat1", description="memory-first")])
    # Tip recorded under a BRIEFING_ID (32-hex), not the session_id — the live
    # strategy_tips_section path.
    await record_tip_shown(r, "bf_briefing_1", ["pat1"], group="treatment")


async def test_join_fails_without_map():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        await _seed(r)
        results = await compute_tip_effectiveness(r)  # no reconciliation map
        # briefing_id key matches no session_id -> pattern has no treatment arm -> skipped
        assert results == []
    finally:
        await r.aclose()


async def test_join_closes_with_map():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        await _seed(r)
        results = await compute_tip_effectiveness(
            r, briefing_to_session={"bf_briefing_1": "sess-win"},
        )
        assert len(results) == 1
        entry = results[0]
        assert entry["id"] == "pat1"
        assert entry["sessions_with_tip"] == 1          # sess-win joined via the map
        assert entry["success_rate_with_tip"] == 1.0    # sess-win was a success
    finally:
        await r.aclose()


async def test_direct_session_keyed_logs_pass_through():
    """Tips recorded directly under a session_id (POST /patterns/tip-shown)
    must still join when a (non-matching) briefing map is supplied."""
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        await _seed(r)  # seeds a briefing-keyed tip; add a session-keyed one too
        await store_features(r, SessionFeatures(session_id="direct-win", outcome="success",
                                                 outcome_source="task_result"))
        await record_tip_shown(r, "direct-win", ["pat1"], group="treatment")
        results = await compute_tip_effectiveness(
            r, briefing_to_session={"bf_briefing_1": "sess-win"},
        )
        entry = next(e for e in results if e["id"] == "pat1")
        # both sess-win (remapped) and direct-win (pass-through) count
        assert entry["sessions_with_tip"] == 2
    finally:
        await r.aclose()


async def test_remap_does_not_overwrite_authentic_session_keyed_entry(monkeypatch):
    """Controller fix (T34 review): if a tip log already exists directly under
    a session_id AND a separate briefing-keyed log remaps to that same
    session_id, the authentic session-keyed entry must survive the remap
    (non-overwrite guard in store.py's remap loop). The map builder can't
    resolve this collision structurally -- only the remap loop can.

    Redis SCAN order is unspecified (fakeredis happens to return keys in an
    order that would mask a naive last-write-wins bug here), so we pin the
    exact dict ordering `_load_tip_groups` returns via monkeypatch instead of
    relying on incidental scan ordering. This orders the authentic
    session-keyed entry BEFORE the colliding briefing-keyed entry -- the
    ordering that breaks a naive "later write wins" remap.
    """
    import app.patterns.store as store_module

    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        await store_features(r, SessionFeatures(session_id="sess-collide", outcome="success",
                                                 outcome_source="task_result"))
        await store_features(r, SessionFeatures(session_id="sess-treat", outcome="success",
                                                 outcome_source="task_result"))
        for i in range(4):
            await store_features(r, SessionFeatures(session_id=f"g{i}", outcome="failure",
                                                     outcome_source="task_result"))
        await store_patterns(r, [PatternCard(id="pat1", description="d")])

        fake_tip_groups = {
            # Authentic session-keyed entry: control group, withheld.
            "sess-collide": {"pattern_ids": ["pat1"], "group": "control"},
            # A genuine (non-colliding) treatment session, so the treatment
            # arm isn't empty regardless of how the collision resolves.
            "sess-treat": {"pattern_ids": ["pat1"], "group": "treatment"},
            # A different briefing_id that remaps onto sess-collide, treatment
            # group -- must NOT clobber sess-collide's authentic control log.
            "bf_collide": {"pattern_ids": ["pat1"], "group": "treatment"},
        }

        async def _fake_load_tip_groups(_r):
            return dict(fake_tip_groups)

        monkeypatch.setattr(store_module, "_load_tip_groups", _fake_load_tip_groups)

        results = await compute_tip_effectiveness(
            r, briefing_to_session={"bf_collide": "sess-collide"},
        )
        entry = next(e for e in results if e["id"] == "pat1")
        # The authentic session-keyed (control) entry must win -- sess-collide
        # is classified as control, not treatment.
        assert "ab_test" in entry
        assert entry["ab_test"]["control_sessions"] == 1
        assert entry["ab_test"]["treatment_sessions"] == 1
    finally:
        await r.aclose()


# ---------------------------------------------------------------------------
# T35: wiring + D6 A/B-control regression
# ---------------------------------------------------------------------------



class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeHTTP:
    """Stub of the shared httpx.AsyncClient (app.state.http_client)."""
    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    async def get(self, url, headers=None, params=None):
        self.calls.append({"url": url, "headers": headers, "params": params})
        return _FakeResp(self._payload)


async def test_build_briefing_map_skips_blank_ids():
    from app.patterns.api import _build_briefing_map
    http = _FakeHTTP({"sessions": [
        {"session_id": "s1", "briefing_id": "bf_1"},
        {"session_id": "s2", "briefing_id": ""},   # skipped
        {"session_id": "s3"},                      # skipped (no briefing_id)
    ]})
    m = await _build_briefing_map(http, "http://bridge:8070", None)
    assert m == {"bf_1": "s1"}
    # requests the Bridge list cap for dev-scale
    assert http.calls[0]["url"] == "http://bridge:8070/sessions"
    assert http.calls[0]["params"] == {"limit": 200}


async def test_build_briefing_map_degrades_on_bridge_error():
    from app.patterns.api import _build_briefing_map

    class _BoomHTTP:
        async def get(self, *a, **k):
            raise RuntimeError("bridge down")

    m = await _build_briefing_map(_BoomHTTP(), "http://bridge:8070", None)
    assert m == {}   # best-effort: effectiveness runs without reconciliation, never 500s


async def test_effectiveness_with_reconciliation_closes_join():
    """Full wiring: fetch briefing map from Bridge -> pass to compute -> join closes."""
    from app.patterns.api import _effectiveness_with_reconciliation

    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        await _seed(r)  # tip recorded under "bf_briefing_1"
        http = _FakeHTTP({"sessions": [
            {"session_id": "sess-win", "briefing_id": "bf_briefing_1"},
        ]})
        settings = SimpleNamespace(BRIDGE_URL="http://bridge:8070", FIREKEEP_INTERNAL_KEY=None)
        results = await _effectiveness_with_reconciliation(r, http, settings)
        assert len(results) == 1
        assert results[0]["sessions_with_tip"] == 1
    finally:
        await r.aclose()


async def test_d6_ab_control_survives_reconciliation():
    """D6 regression: the control arm (tips WITHHELD, recorded under a
    briefing_id) is still classified as control after the remap."""
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        await store_features(r, SessionFeatures(session_id="sess-t", outcome="success",
                                                 outcome_source="task_result"))  # treatment
        await store_features(r, SessionFeatures(session_id="sess-c", outcome="failure",
                                                 outcome_source="task_result"))  # control
        await store_features(r, SessionFeatures(session_id="n1", outcome="failure",
                                                 outcome_source="task_result"))
        await store_features(r, SessionFeatures(session_id="n2", outcome="success",
                                                 outcome_source="task_result"))
        await store_features(r, SessionFeatures(session_id="n3", outcome="failure",
                                                 outcome_source="task_result"))
        await store_patterns(r, [PatternCard(id="pat1", description="d")])
        await record_tip_shown(r, "bf_t", ["pat1"], group="treatment")
        await record_tip_shown(r, "bf_c", ["pat1"], group="control")  # withheld
        results = await compute_tip_effectiveness(
            r, briefing_to_session={"bf_t": "sess-t", "bf_c": "sess-c"},
        )
        assert len(results) == 1
        entry = results[0]
        assert "ab_test" in entry
        assert entry["ab_test"]["treatment_sessions"] == 1
        assert entry["ab_test"]["control_sessions"] == 1   # control classification survived remap
    finally:
        await r.aclose()


# ---------------------------------------------------------------------------
# KEEPTTL: neither persist site refreshes or resurrects card TTLs (D11/D9e)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def rr():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest.mark.asyncio
async def test_record_tip_shown_does_not_extend_or_resurrect_the_card(rr):
    """D11: the times_shown counter is bookkeeping — it must never renew a
    card's 30-day life NOR resurrect an expired card (xx=True). The features
    rewrite is DELETED, so the feature record is untouched here (finding 3)."""
    from app.patterns.models import PatternCard, SessionFeatures
    from app.patterns.store import (record_tip_shown, _PATTERN_PREFIX,
                                     _FEATURE_PREFIX)
    card = PatternCard(id="p1", description="d", recommendation="r")
    await rr.set(f"{_PATTERN_PREFIX}p1", card.model_dump_json(), ex=1000)
    await rr.set(f"{_FEATURE_PREFIX}s1",
                 SessionFeatures(session_id="s1").model_dump_json(), ex=1000)
    card_before = await rr.pttl(f"{_PATTERN_PREFIX}p1")
    feature_before = await rr.pttl(f"{_FEATURE_PREFIX}s1")
    feature_raw = await rr.get(f"{_FEATURE_PREFIX}s1")
    await record_tip_shown(rr, "s1", ["p1"])
    ttl = await rr.pttl(f"{_PATTERN_PREFIX}p1")
    assert 0 < ttl <= card_before, f"card TTL={ttl} (a bare <= would accept -1)"
    # the feature record's TTL is unchanged because record_tip_shown no longer
    # rewrites it at all
    feature_after = await rr.pttl(f"{_FEATURE_PREFIX}s1")
    assert 0 < feature_after <= feature_before
    assert await rr.get(f"{_FEATURE_PREFIX}s1") == feature_raw


@pytest.mark.asyncio
async def test_record_tip_shown_does_not_resurrect_after_get_set_race(
    rr, monkeypatch
):
    """Deterministically expire the card after record_tip_shown's GET and
    immediately before its SET. Bare KEEPTTL would recreate TTL=-1; XX must
    leave it absent."""
    from app.patterns.models import PatternCard
    from app.patterns.store import record_tip_shown, _PATTERN_PREFIX
    key = f"{_PATTERN_PREFIX}p1"
    await rr.set(key, PatternCard(id="p1").model_dump_json(), ex=1000)
    real_set = rr.set
    seen = []

    async def expiring_set(name, value, *args, **kwargs):
        if name == key and kwargs.get("keepttl"):
            seen.append(dict(kwargs))
            await rr.delete(key)
        return await real_set(name, value, *args, **kwargs)

    monkeypatch.setattr(rr, "set", expiring_set)
    await record_tip_shown(rr, "s1", ["p1"])
    assert seen == [{"xx": True, "keepttl": True}]
    assert await rr.exists(key) == 0


@pytest.mark.asyncio
async def test_effectiveness_does_not_extend_or_resurrect(rr):
    """compute_tip_effectiveness's card persist uses xx=True, keepttl=True."""
    from app.patterns.models import PatternCard, SessionFeatures
    from app.patterns.store import (store_features, record_tip_shown,
                                     compute_tip_effectiveness, _PATTERN_PREFIX)
    card = PatternCard(id="p1", description="d", recommendation="r")
    await rr.set(f"{_PATTERN_PREFIX}p1", card.model_dump_json(), ex=1000)
    # >=5 graded features, some shown p1, so compute reaches the persist
    for i in range(6):
        sid = f"s{i}"
        await store_features(rr, SessionFeatures(
            session_id=sid, outcome="success", outcome_source="task_result"))
        if i < 3:
            await record_tip_shown(rr, sid, ["p1"], group="treatment")
        elif i < 5:
            await record_tip_shown(rr, sid, ["p1"], group="control")
    before = await rr.pttl(f"{_PATTERN_PREFIX}p1")
    await compute_tip_effectiveness(rr)
    ttl = await rr.pttl(f"{_PATTERN_PREFIX}p1")
    assert 0 < ttl <= before, f"card TTL={ttl}"


@pytest.mark.asyncio
async def test_effectiveness_does_not_resurrect_after_get_set_race(
    rr, monkeypatch
):
    """Delete the card after get_patterns loaded it but before the stats SET."""
    from app.patterns.models import PatternCard, SessionFeatures
    from app.patterns.store import (
        _PATTERN_PREFIX, compute_tip_effectiveness, record_tip_shown,
        store_features, store_patterns,
    )
    key = f"{_PATTERN_PREFIX}p1"
    await store_patterns(rr, [PatternCard(id="p1")])
    for i in range(6):
        await store_features(rr, SessionFeatures(
            session_id=f"r{i}", outcome="success",
            outcome_source="task_result"))
    await record_tip_shown(rr, "r0", ["p1"], group="treatment")
    real_set = rr.set
    seen = []

    async def expiring_set(name, value, *args, **kwargs):
        if name == key and kwargs.get("keepttl"):
            seen.append(dict(kwargs))
            await rr.delete(key)
        return await real_set(name, value, *args, **kwargs)

    monkeypatch.setattr(rr, "set", expiring_set)
    await compute_tip_effectiveness(rr)
    assert seen == [{"xx": True, "keepttl": True}]
    assert await rr.exists(key) == 0
