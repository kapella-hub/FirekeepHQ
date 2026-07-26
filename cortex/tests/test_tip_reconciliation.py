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

from app.patterns.models import PatternCard, SessionFeatures
from app.patterns.store import (
    store_features, store_patterns, record_tip_shown, compute_tip_effectiveness,
)

pytestmark = pytest.mark.asyncio


async def _seed(r):
    # >= 5 features required (compute returns [] below that threshold)
    await store_features(r, SessionFeatures(session_id="sess-win", outcome="success"))
    for i in range(4):
        await store_features(r, SessionFeatures(session_id=f"f{i}", outcome="failure"))
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
        await store_features(r, SessionFeatures(session_id="direct-win", outcome="success"))
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
        await store_features(r, SessionFeatures(session_id="sess-collide", outcome="success"))
        await store_features(r, SessionFeatures(session_id="sess-treat", outcome="success"))
        for i in range(4):
            await store_features(r, SessionFeatures(session_id=f"g{i}", outcome="failure"))
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
        await store_features(r, SessionFeatures(session_id="sess-t", outcome="success"))  # treatment
        await store_features(r, SessionFeatures(session_id="sess-c", outcome="failure"))  # control
        await store_features(r, SessionFeatures(session_id="n1", outcome="failure"))
        await store_features(r, SessionFeatures(session_id="n2", outcome="success"))
        await store_features(r, SessionFeatures(session_id="n3", outcome="failure"))
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
