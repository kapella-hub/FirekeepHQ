"""End-to-end: an anonymous caller on an auth-OFF box is refused admin routes.

Audit blocker 7. `auth/tests/test_anonymous_scopes.py` pins the primitives;
this drives real HTTP through a REAL admin-gated router to prove the refusal
survives the FastAPI dependency machinery.

The router under test is deliberately `app.ops` (POST /ops/dlq/requeue,
require_scope("admin")) and NOT /vault/* or /auth/keys — those two are ALSO
unmounted when auth is off (app/main.py, defence in depth), so testing the
refusal there would pass even if the scope fix regressed. /ops is the honest
probe: it stays mounted, so only the scope check can refuse it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.ops import create_ops_router
from auth import keys
from auth.middleware import require_scope


@pytest.fixture
def auth_disabled(monkeypatch):
    """AUTH_ENABLED=false, pinned explicitly.

    require_scope reads keys._AUTH_ENABLED (set only by init_auth). Stating it
    here rather than relying on the module default keeps this test honest once
    the shipped default flips to enabled.
    """
    monkeypatch.setattr(keys, "_AUTH_ENABLED", False)


@pytest.fixture
def client(auth_disabled) -> TestClient:
    app = FastAPI()
    app.include_router(create_ops_router())

    # Stand-in for the many non-admin gated routes in production (evals,
    # patterns, policy, briefing all use require_scope("eval:read") /
    # ("session:read")). Mirrors test_auth_consolidation.py's /memory/learn
    # stand-in: the dependency is the real one, only the handler is trivial.
    @app.get("/stand-in/eval-read")
    async def _eval_read(identity: dict = Depends(require_scope("eval:read"))) -> dict:
        return {"agent_id": identity["agent_id"]}

    return TestClient(app)


class TestAnonymousRefusedOnAdminRoute:
    def test_admin_route_403(self, client):
        """POST /ops/dlq/requeue is require_scope("admin"). Before this fix it
        returned 200 to any unauthenticated caller: require_scope returned the
        anonymous identity without ever comparing it to the required scope."""
        with patch("app.ops.requeue_dlq", AsyncMock(return_value={})) as spy:
            resp = client.post("/ops/dlq/requeue?limit=5")
        assert resp.status_code == 403
        # The gate must refuse BEFORE the handler touches the DLQ.
        spy.assert_not_awaited()

    def test_refusal_names_the_setting_and_the_fix(self, client):
        resp = client.post("/ops/dlq/requeue?limit=5")
        detail = resp.json()["detail"]
        assert "AUTH_ENABLED" in detail
        assert "bootstrap-keys.sh" in detail

    def test_admin_gate_precedes_param_validation(self, client):
        """An out-of-range limit would 422; the scope refusal wins. Pins that
        the gate is not reachable-around via a malformed request."""
        assert client.post("/ops/dlq/requeue?limit=99999").status_code == 403


class TestAnonymousStillWorksOnNormalRoutes:
    def test_non_admin_gated_route_allowed(self, client):
        """The product must still work for a single user with no keys — an
        empty anonymous scope set would have 403'd this too."""
        resp = client.get("/stand-in/eval-read")
        assert resp.status_code == 200
        assert resp.json()["agent_id"] == "anonymous"

    def test_ungated_route_allowed(self, client):
        """/ops/workers has no require_scope at all — unchanged by this fix."""
        with patch("app.ops._inspect_workers", return_value=[]):
            resp = client.get("/ops/workers")
        assert resp.status_code == 200
