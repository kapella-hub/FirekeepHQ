"""SP1b-server: Cortex GET /briefing aggregator endpoint tests."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.briefing import sections as S
from app.briefing.api import create_briefing_router
from app.version import get_version_info
from auth.entitlements import Entitlement

# The 12 sections that MUST always be present (fail-loud: never omitted).
# `observed` is the N=1 learning surface (descriptive, unvalidated) added in the
# n1-learning-loop work — it always ships alongside the original 11.
ALL_SECTIONS = {
    "environment", "tasks", "bulletins", "quality", "strategy_tips", "observed",
    "cross_agent", "skills", "vault", "discipline", "dlq", "resumable_sessions",
}


async def _zero_depths():
    return {"celery": 0, "event_stream": 0, "event_dlq": 0,
            "memory_backfill": 0, "memory_backfill_dlq": 0, "distill_dlq": 0}


def _make_app(monkeypatch, section_timeout: float = 2.0) -> FastAPI:
    """Build a minimal app with the briefing router and working fake app.state.

    Task 7 made 7 in-process sections real (they now dereference the shared
    clients on app.state), so None-valued state is no longer sufficient — a
    None client is a genuine upstream failure that the orchestrator (correctly)
    converts to "unavailable". These fakes give every in-process section a
    live-but-empty backend so it reports "empty" instead of degrading:
    - replay_redis / redis_client: real fakeredis (evals, patterns, untagged).
    - vector_client._client.scroll: async, returns ([], None) (skills → empty).
    - collect_queue_depths: patched to all-zero so the dlq section neither hits
      real Redis nor times out (in production it reads live broker/data/bridge
      DBs; that path is covered by test_ops.py, not this fixture).

    http_client is a no-op that raises on every call. Task 8 made the 4
    outbound sections (environment, tasks, bulletins, resumable_sessions) real,
    so they now genuinely dereference http_client and correctly degrade to
    "unavailable" here (asserted in test_briefing_sections_status_and_vault_fail_loud
    below) — full success-path coverage for those 4 lives in
    test_briefing_sections_outbound.py.

    Vault is the one section that legitimately stays "unavailable" here: the
    anonymous dev identity carries scopes=["*"] which passes vault_visible, so
    vault_section calls the real list_secrets(), which raises
    RuntimeError("Vault not initialized") because init_vault() is never called
    in this minimal app. That is correct fail-loud behavior, asserted below.
    """
    monkeypatch.setattr(S, "collect_queue_depths", _zero_depths)

    app = FastAPI()
    app.include_router(create_briefing_router(section_timeout=section_timeout))
    app.state.replay_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app.state.redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)

    vector = MagicMock()

    async def _scroll(**_kwargs):
        return ([], None)

    async def _query_points(**_kwargs):
        res = MagicMock()
        res.points = []
        return res

    vector._client = MagicMock()
    vector._client.scroll = _scroll
    # The skills section takes the SEMANTIC path whenever a goal is present (tests at
    # :81 and :89 do send one). Without an awaitable _embed those requests silently
    # degrade to scroll and assert nothing about the real path — and since those tests
    # check only section keys and envelope shape, nothing would fail.
    vector._embed = AsyncMock(return_value=[1.0, 0.0, 0.0])
    vector._client.query_points = _query_points
    app.state.vector_client = vector

    class _NoopClient:
        async def get(self, *_a, **_k):
            raise RuntimeError("offline")  # outbound sections degrade; not asserted here

    app.state.http_client = _NoopClient()
    return app


def test_briefing_returns_200_with_all_12_sections(monkeypatch):
    client = TestClient(_make_app(monkeypatch))
    resp = client.get("/briefing?agent_id=moganes&goal=fix+the+collector")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["sections"].keys()) == ALL_SECTIONS


def test_briefing_envelope_shape(monkeypatch):
    client = TestClient(_make_app(monkeypatch))
    resp = client.get("/briefing?agent_id=moganes&goal=g")
    body = resp.json()
    for key in ("generated_at", "server_version", "agent_id", "goal",
                "project", "briefing_id", "degraded", "sections",
                "instructions", "rendered"):
        assert key in body, f"missing envelope key: {key}"
    assert body["agent_id"] == "moganes"
    assert body["goal"] == "g"
    assert body["project"] is None
    # briefing_id is minted server-side (D2) and surfaced top-level.
    assert isinstance(body["briefing_id"], str) and body["briefing_id"]
    assert body["entitlement"]["plan"] == "solo"


def test_briefing_server_version_matches_version_module(monkeypatch):
    client = TestClient(_make_app(monkeypatch))
    resp = client.get("/briefing?agent_id=x")
    assert resp.json()["server_version"] == get_version_info()["version"]


def test_briefing_surfaces_licence_expiry_without_gating_sections(monkeypatch):
    app = _make_app(monkeypatch)
    app.state.auth_redis = object()

    async def expiring(*_args, **_kwargs):
        return Entitlement(
            workspace_id="workspace-local",
            customer="Acme",
            plan="team",
            max_members=5,
            issued_at="2026-01-01T00:00:00+00:00",
            expires_at="2026-08-10T00:00:00+00:00",
            verified=True,
            source="redis",
            reason="verified",
            warning="licence expires in 10 day(s)",
        )

    monkeypatch.setattr("app.briefing.api.load_entitlement", expiring)
    body = TestClient(app).get("/briefing?agent_id=a").json()
    assert "LICENCE: licence expires in 10 day(s)" in body["rendered"]
    assert set(body["sections"]) == ALL_SECTIONS


def test_briefing_sections_status_and_vault_fail_loud(monkeypatch):
    """Real (post-Task-8) behavior with live-but-empty fakes.

    The 6 in-process sections with a live-but-empty backend (quality,
    strategy_tips, cross_agent, skills, discipline, dlq) report "empty" —
    nothing to show. Five sections correctly degrade to "unavailable" because
    their upstream genuinely fails in this minimal app:
    - vault: see below. As of 2026-07-26 the anonymous identity no longer
      carries ["*"], so on the auth-disabled path vault_section is OMITTED
      for scope rather than attempted — which is the correct new behaviour
      and is asserted separately.
    - environment / tasks / bulletins / resumable_sessions (Task 8): the
      no-op http_client fake raises RuntimeError("offline") on every call, so
      each outbound section's primary _get_json() call raises and the
      orchestrator converts it to "unavailable" with that error preserved.
    These 5 genuine upstream failures make degraded=True. This is the
    fail-loud contract working: a real backend failure is surfaced, not
    silently swallowed.
    """
    client = TestClient(_make_app(monkeypatch))
    body = client.get("/briefing?agent_id=x").json()
    for name, sec in body["sections"].items():
        assert set(sec.keys()) == {"status", "error", "data"}, name

    unavailable_sections = {"environment", "tasks", "bulletins", "resumable_sessions"}
    for name, sec in body["sections"].items():
        if name in unavailable_sections or name == "vault":
            continue
        assert sec["status"] == "empty", f"{name}: {sec}"

    for name in unavailable_sections:
        sec = body["sections"][name]
        assert sec["status"] == "unavailable", f"{name}: {sec}"
        assert "offline" in (sec["error"] or ""), f"{name}: {sec}"

    # Vault is now OMITTED, not attempted: the anonymous identity lost "*" when
    # audit blocker 7 was fixed, so it no longer passes the admin gate. The
    # distinction the briefing schema draws matters here -- "empty with an
    # omitted_reason" is a deliberate withholding, NOT a silent failure, and it
    # must not count toward degraded.
    vault = body["sections"]["vault"]
    assert vault["status"] == "empty", vault
    assert vault["error"] is None, vault
    assert vault["data"]["omitted_reason"] == "insufficient scope", vault

    # 4 genuine upstream failures (the outbound sections hitting the offline
    # http_client fake) => degraded True. Vault contributes nothing either way.
    assert body["degraded"] is True


def test_vault_section_still_fails_loud_for_an_admin_caller(monkeypatch):
    """Withholding for scope must not have replaced the fail-loud contract.

    The test above lost its vault-failure coverage when the anonymous identity
    stopped being admin -- the section is now withheld before it can fail. That
    would leave "a broken vault backend is reported, not swallowed" untested, so
    assert it directly with a caller that DOES pass the gate. Without this, a
    regression that silently ate vault errors would go unnoticed, because the
    only test that covered it now takes the omitted branch.
    """
    import asyncio

    import pytest as _pytest

    from app.briefing.sections import vault_section

    # The section itself does not produce "unavailable" — it PROPAGATES, and the
    # orchestrator (briefing/api.py) converts the raise into that status. So the
    # contract to guard here is "an admin caller reaches the backend and a broken
    # backend is not swallowed". A section that caught its own errors and returned
    # {"status": "empty"} would look identical to the withheld case above and the
    # briefing would report a dead vault as nothing-to-show.
    with _pytest.raises(RuntimeError, match="[Vv]ault"):
        asyncio.run(vault_section(["admin"]))

    # ...and the withheld path returns instead of raising, for the same caller
    # shape minus the scope. These two branches must stay distinguishable.
    withheld = asyncio.run(vault_section(["memory:read"]))
    assert withheld["status"] == "empty"
    assert withheld["data"]["omitted_reason"] == "insufficient scope"
