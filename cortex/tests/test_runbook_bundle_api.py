"""Enforced Runbooks Phase A — the REST surface: bundle, bundle ack, modes,
and POST /procedures/ack; plus the MCP `runbook_ack` proxy and the proof that
the skill PATCH path cannot arm a runbook.

Mounted on the EXISTING procedures router (never main.py — concurrent
install-story work owns that file).
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import fakeredis.aioredis as fr
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.procedures import enforce, store
from app.procedures.api import create_procedures_router
from tests.test_procedures_api import auth_keys  # noqa: F401 — pytest fixture

WS = "workspace-local"


class _Settings:
    PROCEDURE_ENABLED = True
    PROCEDURE_EXEC_TTL_DAYS = 90
    PROCEDURE_MAX_SPECS = 50
    AGENT_RECONCILE_DEADLINE_SECONDS = 300
    QDRANT_COLLECTION = "c"


def _client_for(r):
    app = FastAPI()
    app.include_router(create_procedures_router(
        get_redis=lambda: r, get_vector=lambda: None,
        settings_fn=lambda: _Settings(),
    ))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


def _entry(skill="rb1", step="backup", pattern="bash backup.sh*", order=0,
           lb=True, ws="", kind="command"):
    return {"skill_id": skill, "skill_trigger": "vps deploy", "step_id": step,
            "step_text": step, "kind": kind, "pattern": pattern,
            "load_bearing": lb, "order": order, "workspace_id": ws}


async def _seed(r):
    await r.set(store.INDEX_KEY, json.dumps([
        _entry(step="backup", pattern="bash backup.sh*", order=0, lb=True),
        _entry(step="deploy", pattern="bash deploy.sh*", order=1, lb=False),
        # A file step of the same runbook: NOT a bundle entry.
        _entry(step="conf", pattern="*.conf", order=2, lb=False,
               kind="file_glob"),
        # Another workspace's runbook: invisible to this caller.
        _entry(skill="rb-theirs", step="x", pattern="terraform apply*",
               order=0, ws="workspace-other"),
    ]))


@pytest.fixture
def r():
    return fr.FakeRedis(decode_responses=True)


class TestBundle:
    @pytest.mark.asyncio
    async def test_bundle_serves_command_steps_of_my_workspace_only(self, r):
        await _seed(r)
        async with _client_for(r) as client:
            body = (await client.get("/procedures/bundle")).json()
        assert body["workspace_id"] == WS
        assert [(e["skill_id"], e["step_id"]) for e in body["entries"]] == [
            ("rb1", "backup"), ("rb1", "deploy"),
        ]
        for e in body["entries"]:
            assert e["kind"] == "command"
            assert e["mode"] == "advise"
            assert e["fail_posture"] == "open"
            assert set(e) == {"skill_id", "step_id", "pattern", "kind",
                              "mode", "load_bearing", "fail_posture"}
        assert body["entries"][0]["load_bearing"] is True

    @pytest.mark.asyncio
    async def test_version_is_deterministic_and_recomputable(self, r):
        await _seed(r)
        async with _client_for(r) as client:
            one = (await client.get("/procedures/bundle")).json()
            two = (await client.get("/procedures/bundle")).json()
        assert one["version"] == two["version"]
        assert len(one["version"]) == 12
        assert one["version"] == enforce.bundle_version(one["entries"])

    @pytest.mark.asyncio
    async def test_arming_a_runbook_changes_the_version(self, r):
        """The version binds permits and bundle acks; a mode flip is a new
        contract and must read as one."""
        await _seed(r)
        async with _client_for(r) as client:
            before = (await client.get("/procedures/bundle")).json()
            await store.set_mode(r, WS, "rb1", "block", "human")
            after = (await client.get("/procedures/bundle")).json()
        assert before["version"] != after["version"]
        modes = {e["step_id"]: (e["mode"], e["fail_posture"])
                 for e in after["entries"]}
        assert modes["backup"] == ("block", "closed")

    @pytest.mark.asyncio
    async def test_a_cold_deployment_serves_an_empty_bundle(self, r):
        async with _client_for(r) as client:
            body = (await client.get("/procedures/bundle")).json()
        assert body["entries"] == []
        assert len(body["version"]) == 12

    @pytest.mark.asyncio
    async def test_bundle_is_scope_gated(self, r, auth_keys):  # noqa: F811
        """Keyless under enforcement: 401. A key without session:read: 403.
        (The reader fixture key holds memory:read only.)"""
        await _seed(r)
        async with _client_for(r) as client:
            assert (await client.get("/procedures/bundle")).status_code == 401
            assert (await client.get(
                "/procedures/bundle",
                headers={"X-API-Key": auth_keys["reader"]},
            )).status_code == 403


class TestBundleAck:
    @pytest.mark.asyncio
    async def test_ack_records_the_sessions_holding(self, r):
        async with _client_for(r) as client:
            resp = await client.post(
                "/procedures/bundle/ack", json={"version": "abc123def456"},
                headers={"X-Session-Id": "sess-9"})
        assert resp.status_code == 200
        acks = await store.list_bundle_acks(r, WS)
        assert acks["sess-9"]["version"] == "abc123def456"
        assert acks["sess-9"]["at"]

    @pytest.mark.asyncio
    async def test_session_comes_from_the_header_never_the_body(self, r):
        """Client pin (Phase B): the body is exactly {"version"}; an extra
        session_id field is ignored, not honoured."""
        async with _client_for(r) as client:
            resp = await client.post(
                "/procedures/bundle/ack",
                json={"version": "v1", "session_id": "spoofed"},
                headers={"X-Session-Id": "real-session"})
        assert resp.status_code == 200
        acks = await store.list_bundle_acks(r, WS)
        assert "real-session" in acks
        assert "spoofed" not in acks

    @pytest.mark.asyncio
    async def test_no_session_header_is_a_422(self, r):
        async with _client_for(r) as client:
            resp = await client.post(
                "/procedures/bundle/ack", json={"version": "v1"})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_a_missing_version_is_a_422(self, r):
        async with _client_for(r) as client:
            resp = await client.post(
                "/procedures/bundle/ack", json={},
                headers={"X-Session-Id": "s"})
        assert resp.status_code == 422


class TestRunbookAckEndpoint:
    async def _challenge(self, r, cid="rbc_test", session="sess-1", ws=WS):
        await store.mint_challenge(r, cid, {
            "workspace": ws, "member": "member-owner", "session": session,
            "skill": "rb1", "step_id": "deploy", "command_hash": "abcd",
            "bundle_version": "v" * 12, "missing": "run the backup",
            "created": "now",
        })
        return cid

    @pytest.mark.asyncio
    async def test_ack_mints_the_permit(self, r):
        cid = await self._challenge(r)
        async with _client_for(r) as client:
            resp = await client.post("/procedures/ack", json={
                "challenge_id": cid, "reason": "backup ran out of band",
                "session_id": "sess-1"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["permit_expires_in_seconds"] == 600
        permit = json.loads(await r.get(f"proc:permit:{cid}"))
        assert permit["workspace"] == WS
        assert permit["session"] == "sess-1"
        assert permit["command_hash"] == "abcd"
        assert permit["bundle_version"] == "v" * 12
        # TTL 10 minutes, one-use (consumption is pinned in the permit suite).
        assert 0 < await r.ttl(f"proc:permit:{cid}") <= 600

    @pytest.mark.asyncio
    async def test_the_session_can_come_from_the_header(self, r):
        cid = await self._challenge(r)
        async with _client_for(r) as client:
            resp = await client.post(
                "/procedures/ack",
                json={"challenge_id": cid, "reason": "known good"},
                headers={"X-Session-Id": "sess-1"})
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_the_wrong_session_is_a_403(self, r):
        cid = await self._challenge(r)
        async with _client_for(r) as client:
            resp = await client.post("/procedures/ack", json={
                "challenge_id": cid, "reason": "x",
                "session_id": "wrong-session"})
        assert resp.status_code == 403
        assert await r.get(f"proc:permit:{cid}") is None

    @pytest.mark.asyncio
    async def test_an_unknown_challenge_is_a_404(self, r):
        async with _client_for(r) as client:
            resp = await client.post("/procedures/ack", json={
                "challenge_id": "rbc_nope", "reason": "x",
                "session_id": "sess-1"})
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_an_empty_reason_never_reaches_the_store(self, r):
        cid = await self._challenge(r)
        async with _client_for(r) as client:
            resp = await client.post("/procedures/ack", json={
                "challenge_id": cid, "reason": "", "session_id": "sess-1"})
        assert resp.status_code == 422
        assert await r.get(f"proc:permit:{cid}") is None


class TestModeEndpoints:
    @pytest.mark.asyncio
    async def test_get_mode_defaults_to_advise(self, r):
        await _seed(r)
        async with _client_for(r) as client:
            body = (await client.get("/procedures/rb1/mode")).json()
        assert body == {"skill_id": "rb1", "mode": "advise", "set_by": "",
                        "set_at": ""}

    @pytest.mark.asyncio
    async def test_put_mode_refuses_the_anonymous_identity(self, r):
        """The arming control is admin-STRENGTH: with auth off the anonymous
        identity holds every scope below admin and is still refused."""
        await _seed(r)
        async with _client_for(r) as client:
            resp = await client.put("/procedures/rb1/mode",
                                    json={"mode": "block"})
        assert resp.status_code == 403
        assert (await store.get_mode(r, WS, "rb1"))["mode"] == "advise"

    @pytest.mark.asyncio
    async def test_put_mode_requires_a_real_admin_key(self, r, auth_keys):  # noqa: F811
        await _seed(r)
        async with _client_for(r) as client:
            refused = await client.put(
                "/procedures/rb1/mode", json={"mode": "block"},
                headers={"X-API-Key": auth_keys["reader"]})
            assert refused.status_code == 403
            armed = await client.put(
                "/procedures/rb1/mode", json={"mode": "block"},
                headers={"X-API-Key": auth_keys["admin"]})
        assert armed.status_code == 200
        assert armed.json()["mode"] == "block"
        assert (await store.get_mode(r, WS, "rb1"))["mode"] == "block"

    @pytest.mark.asyncio
    async def test_an_invalid_mode_is_a_422(self, r, auth_keys):  # noqa: F811
        await _seed(r)
        async with _client_for(r) as client:
            resp = await client.put(
                "/procedures/rb1/mode", json={"mode": "yolo"},
                headers={"X-API-Key": auth_keys["admin"]})
        assert resp.status_code == 422
        assert (await store.get_mode(r, WS, "rb1"))["mode"] == "advise"

    @pytest.mark.asyncio
    async def test_another_workspaces_skill_404s(self, r, auth_keys):  # noqa: F811
        await _seed(r)
        async with _client_for(r) as client:
            resp = await client.put(
                "/procedures/rb-theirs/mode", json={"mode": "block"},
                headers={"X-API-Key": auth_keys["admin"]})
        assert resp.status_code == 404
        assert (await store.get_mode(
            r, "workspace-other", "rb-theirs"))["mode"] == "advise"


class TestTheSkillPatchPathCannotArmARunbook:
    """Agents may propose runbooks, never arm them: mode lives under
    proc:mode:{workspace}:{skill}, written by the admin-gated PUT alone."""

    def test_the_skills_router_has_no_path_to_the_mode_key(self):
        src = (Path(__file__).resolve().parents[1]
               / "app" / "skills" / "api.py").read_text(encoding="utf-8")
        assert "proc:mode" not in src
        assert "set_mode" not in src

    def test_the_patch_request_model_carries_no_mode_field(self):
        from app.models import SkillPatchRequest

        assert "mode" not in SkillPatchRequest.model_fields

    def test_a_step_spec_smuggling_a_mode_is_dropped(self):
        from app.procedures.models import StepSpec

        spec = StepSpec(text="deploy", kind="command", pattern="bash d.sh*",
                        **{"mode": "block"})
        assert "mode" not in spec.model_dump()


class TestRunbookAckMcpTool:
    @pytest.fixture(autouse=True)
    def _reset_client(self):
        import app.mcp_server as mod

        mod._client = None
        yield
        if mod._client and not mod._client.is_closed:
            mod._client = None

    @pytest.mark.asyncio
    async def test_the_tool_posts_to_procedures_ack(self):
        mock_resp = httpx.Response(
            status_code=200,
            json={"ok": True, "challenge_id": "rbc_x",
                  "permit_expires_in_seconds": 600},
            request=httpx.Request("POST", "http://test"),
        )
        with __import__("unittest.mock", fromlist=["patch"]).patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock,
            return_value=mock_resp,
        ) as mock_post:
            from app.mcp_server import runbook_ack

            fn = getattr(runbook_ack, "fn", None) or runbook_ack
            if hasattr(fn, "__wrapped__"):
                fn = fn.__wrapped__

            result = await fn(challenge_id="rbc_x",
                              reason="restore verified today",
                              session_id="sess-1")

        assert mock_post.call_args[0][0] == "/procedures/ack"
        payload = mock_post.call_args[1]["json"]
        assert payload == {"challenge_id": "rbc_x",
                           "reason": "restore verified today",
                           "session_id": "sess-1"}
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_an_unknown_session_is_omitted_not_sent(self):
        mock_resp = httpx.Response(
            status_code=200, json={"ok": True},
            request=httpx.Request("POST", "http://test"),
        )
        with __import__("unittest.mock", fromlist=["patch"]).patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock,
            return_value=mock_resp,
        ) as mock_post:
            from app.mcp_server import runbook_ack

            fn = getattr(runbook_ack, "fn", None) or runbook_ack
            if hasattr(fn, "__wrapped__"):
                fn = fn.__wrapped__

            await fn(challenge_id="rbc_x", reason="r")

        assert "session_id" not in mock_post.call_args[1]["json"]
