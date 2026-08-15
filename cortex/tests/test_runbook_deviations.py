"""Enforced Runbooks Phase C — the deviation ledger.

Every enforcement moment that diverges from the runbook lands one record in
the workspace's ledger (`proc:deviations:{workspace}`, newest first): a block
refusal (kind="block"), an acknowledged override (kind="ack", detail = the
reason), and a reconciled-but-unsuccessful command (kind="failed_attempt",
detail = exit_status/success/workspace-mismatch). The ledger caps at
store.MAX_DEVIATIONS — a DISCLOSED cap — and never stores raw command text,
only the command hash (the same secrets rule the pending records apply).
"""
from __future__ import annotations

import json

import pytest
import fakeredis.aioredis as fr
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.agent_gateway.models import (
    Action,
    ActionAfterRequest,
    ActionBeforeRequest,
    Outcome,
)
from app.agent_gateway.service import AgentGatewayService, RethinkCounter
from app.procedures import enforce, store
from app.procedures.api import create_procedures_router
from app.procedures.observe import ProcedureObserver
from tests.test_procedures_api import auth_keys  # noqa: F401 — pytest fixture

WS = "workspace-local"  # the deployment workspace (FIREKEEP_WORKSPACE_ID unset)
MEMBER = "member-owner"


class _Settings:
    PROCEDURE_ENABLED = True
    PROCEDURE_WARN_ENABLED = True
    PROCEDURE_EXEC_TTL_DAYS = 90
    PROCEDURE_INDEX_CACHE_SECONDS = 0
    PROCEDURE_MAX_SPECS = 50
    AGENT_RECONCILE_DEADLINE_SECONDS = 300
    QDRANT_COLLECTION = "c"


class _Decision:
    action = "allow"
    risk_score = 0.0
    reasons: list = []
    signals: dict = {}


class _Engine:
    async def evaluate(self, ctx):
        return _Decision()


def _service(r):
    async def _no(*a, **k):
        return False

    async def _emit(**kw):
        return None

    return AgentGatewayService(
        policy_engine=_Engine(), recent_failure_check=_no, fastpath_check=_no,
        session_touched_check=_no, replay_emitter=_emit,
        rethink_counter=RethinkCounter(r), prediction_redis=r,
        fastpath_redis=r, policy_decision_redis=r,
        procedure_observer=ProcedureObserver(
            get_redis=lambda: r, settings_fn=lambda: _Settings()),
    )


def _cmd_req(command, session="sess", ws=WS, member=MEMBER):
    req = ActionBeforeRequest(
        session_id=session, agent_id="ag", adapter="shell-hook",
        action=Action(type="run_command", target=command),
    )
    req._verified_workspace = ws
    req._verified_member = member
    return req


def _after(action_id, *, success=True, exit_status=0, ws=WS):
    req = ActionAfterRequest(
        action_id=action_id, outcome=Outcome(success=success),
        exit_status=exit_status,
    )
    req._verified_workspace = ws
    return req


async def _seed_runbook(r, ws=""):
    """backup (command, load-bearing, order 0) -> deploy (command, terminal,
    order 1)."""
    await r.set(store.INDEX_KEY, json.dumps([
        {"skill_id": "rb1", "skill_trigger": "vps deploy", "step_id": "backup",
         "step_text": "run the backup", "kind": "command",
         "pattern": "bash backup.sh*", "load_bearing": True, "order": 0,
         "workspace_id": ws},
        {"skill_id": "rb1", "skill_trigger": "vps deploy", "step_id": "deploy",
         "step_text": "deploy to the vps", "kind": "command",
         "pattern": "bash deploy.sh*", "load_bearing": False, "order": 1,
         "workspace_id": ws},
    ]))
    await r.set(store.COVERAGE_KEY, json.dumps({
        "rb1": {"trigger": "vps deploy", "spec_count": 2, "observable": 2,
                "workspace_id": ws},
    }))


def _record(i=0, **over):
    rec = {"at": f"t{i}", "kind": "block", "skill_id": "rb1",
           "step_id": f"s{i}", "session": "sess", "member": MEMBER,
           "agent": "ag", "command_hash": "h", "detail": ""}
    rec.update(over)
    return rec


def _client_for(r):
    app = FastAPI()
    app.include_router(create_procedures_router(
        get_redis=lambda: r, get_vector=lambda: None,
        settings_fn=lambda: _Settings(),
    ))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


@pytest.fixture
def r():
    return fr.FakeRedis(decode_responses=True)


class TestLedgerStore:
    @pytest.mark.asyncio
    async def test_the_ledger_caps_at_max_deviations_newest_first(self, r):
        for i in range(205):
            await store.record_deviation(r, _Settings(), WS, _record(i))
        got = await store.list_deviations(r, WS, limit=205)
        assert store.MAX_DEVIATIONS == 200
        assert len(got) == 200
        assert got[0]["step_id"] == "s204"   # newest first
        assert got[-1]["step_id"] == "s5"    # the oldest five fell off

    @pytest.mark.asyncio
    async def test_undecodable_and_non_dict_entries_are_skipped(self, r):
        await store.record_deviation(r, _Settings(), WS, _record(kind="ack"))
        key = f"proc:deviations:{WS}"
        await r.lpush(key, "{not json")
        await r.lpush(key, json.dumps([1, 2]))
        got = await store.list_deviations(r, WS)
        assert [d["kind"] for d in got] == ["ack"]

    @pytest.mark.asyncio
    async def test_the_default_read_limit_is_50(self, r):
        for i in range(60):
            await store.record_deviation(r, _Settings(), WS, _record(i))
        assert len(await store.list_deviations(r, WS)) == 50

    @pytest.mark.asyncio
    async def test_the_ledger_carries_the_exec_ttl(self, r):
        await store.record_deviation(r, _Settings(), WS, _record())
        ttl = await r.ttl(f"proc:deviations:{WS}")
        assert 0 < ttl <= _Settings.PROCEDURE_EXEC_TTL_DAYS * 86400

    @pytest.mark.asyncio
    async def test_neither_function_raises_on_a_broken_client(self):
        class _Broken:
            def __getattr__(self, name):
                raise RuntimeError("redis down")

        await store.record_deviation(_Broken(), _Settings(), WS, _record())
        assert await store.list_deviations(_Broken(), WS) == []


class TestEnforcePathsWriteTheLedger:
    @pytest.mark.asyncio
    async def test_a_block_verdict_lands_one_block_record(self, r):
        await _seed_runbook(r)
        await store.set_mode(r, WS, "rb1", "block", "human")
        svc = _service(r)
        resp = await svc.decide(_cmd_req("bash deploy.sh --now"))
        assert resp.decision == "block"
        got = await store.list_deviations(r, WS)
        assert len(got) == 1
        d = got[0]
        assert d["kind"] == "block"
        assert d["skill_id"] == "rb1"
        assert d["step_id"] == "deploy"
        assert d["session"] == "sess"
        assert d["member"] == MEMBER
        assert d["agent"] == "ag"
        assert d["command_hash"] == enforce.command_hash("bash deploy.sh --now")
        assert d["detail"] == ""
        assert d["at"]

    @pytest.mark.asyncio
    async def test_an_ack_lands_one_ack_record_with_the_reason(self, r):
        await _seed_runbook(r)
        await store.set_mode(r, WS, "rb1", "require_ack", "human")
        svc = _service(r)
        resp = await svc.decide(_cmd_req("bash deploy.sh --now"))
        assert resp.decision == "rethink"
        cid = [a for a in resp.advisories
               if a.code == "runbook_ack_required"][0].evidence_event_id
        # The challenge itself is not a deviation — only the accepted override.
        assert await store.list_deviations(r, WS) == []
        res = await enforce.acknowledge(
            r, _Settings(), challenge_id=cid, reason="backup ran out of band",
            workspace=WS, member=MEMBER, session="sess")
        assert res["ok"] is True
        got = await store.list_deviations(r, WS)
        assert [d["kind"] for d in got] == ["ack"]
        d = got[0]
        assert d["detail"] == "backup ran out of band"
        assert d["skill_id"] == "rb1"
        assert d["step_id"] == "deploy"
        assert d["member"] == MEMBER
        assert d["agent"] == ""  # the ack arrives over REST/MCP, no agent id
        assert d["command_hash"] == enforce.command_hash("bash deploy.sh --now")

    @pytest.mark.asyncio
    async def test_a_nonzero_exit_lands_a_failed_attempt(self, r):
        await _seed_runbook(r)
        svc = _service(r)
        resp = await svc.decide(_cmd_req("bash backup.sh --full"))
        await svc.record(_after(resp.action_id, success=True, exit_status=1))
        got = await store.list_deviations(r, WS)
        assert [d["kind"] for d in got] == ["failed_attempt"]
        d = got[0]
        assert d["detail"] == "exit_status=1"
        assert d["skill_id"] == "rb1"
        assert d["step_id"] == "backup"
        assert d["session"] == "sess"
        assert d["agent"] == "ag"
        assert d["member"] == ""  # pending records carry no member id
        assert d["command_hash"] == enforce.command_hash("bash backup.sh --full")

    @pytest.mark.asyncio
    async def test_a_reported_failure_lands_success_false(self, r):
        await _seed_runbook(r)
        svc = _service(r)
        resp = await svc.decide(_cmd_req("bash backup.sh --full"))
        await svc.record(_after(resp.action_id, success=False, exit_status=0))
        got = await store.list_deviations(r, WS)
        assert [d["detail"] for d in got] == ["success=false"]

    @pytest.mark.asyncio
    async def test_a_workspace_mismatch_lands_in_the_owning_ledger(self, r):
        await _seed_runbook(r)
        svc = _service(r)
        resp = await svc.decide(_cmd_req("bash backup.sh --full"))
        await svc.record(_after(resp.action_id, ws="workspace-other"))
        got = await store.list_deviations(r, WS)
        assert [d["detail"] for d in got] == ["workspace mismatch"]
        assert got[0]["kind"] == "failed_attempt"
        # The mismatched caller's own ledger records nothing.
        assert await store.list_deviations(r, "workspace-other") == []

    @pytest.mark.asyncio
    async def test_a_successful_command_lands_nothing(self, r):
        await _seed_runbook(r)
        svc = _service(r)
        resp = await svc.decide(_cmd_req("bash backup.sh --full"))
        await svc.record(_after(resp.action_id, success=True, exit_status=0))
        assert await store.list_deviations(r, WS) == []

    @pytest.mark.asyncio
    async def test_raw_command_text_never_reaches_the_ledger(self, r):
        """All three kinds on the wire, then the RAW stored entries: the
        command text ("bash backup.sh --full", "bash deploy.sh --now") must
        appear nowhere — records carry the sha256[:16] hash only."""
        await _seed_runbook(r)
        await store.set_mode(r, WS, "rb1", "block", "human")
        svc = _service(r)
        failed = await svc.decide(_cmd_req("bash backup.sh --full"))
        await svc.record(_after(failed.action_id, exit_status=1))
        blocked = await svc.decide(_cmd_req("bash deploy.sh --now"))
        assert blocked.decision == "block"
        await store.set_mode(r, WS, "rb1", "require_ack", "human")
        challenged = await svc.decide(_cmd_req("bash deploy.sh --now"))
        cid = [a for a in challenged.advisories
               if a.code == "runbook_ack_required"][0].evidence_event_id
        await enforce.acknowledge(
            r, _Settings(), challenge_id=cid, reason="restore tested",
            workspace=WS, member=MEMBER, session="sess")
        raw = "\n".join(await r.lrange(f"proc:deviations:{WS}", 0, -1))
        kinds = {d["kind"] for d in await store.list_deviations(r, WS)}
        assert kinds == {"block", "ack", "failed_attempt"}
        for fragment in ("backup.sh", "deploy.sh", "--full", "--now"):
            assert fragment not in raw


class TestRollupPhaseC:
    @pytest.mark.asyncio
    async def test_rows_carry_mode_and_command_steps(self, r):
        await _seed_runbook(r)
        await store.set_mode(r, WS, "rb1", "block", "human")
        async with _client_for(r) as client:
            body = (await client.get("/procedures")).json()
        row = body["procedures"][0]
        assert row["mode"] == "block"
        assert row["command_steps"] == 2

    @pytest.mark.asyncio
    async def test_default_mode_is_advise_and_file_steps_do_not_count(self, r):
        # Round-1 index shape: file_glob entries with no `kind` at all.
        await r.set(store.INDEX_KEY, json.dumps([
            {"skill_id": "s1", "skill_trigger": "release", "step_id": "a",
             "step_text": "bump", "pattern": "*.toml", "load_bearing": True,
             "order": 0},
        ]))
        async with _client_for(r) as client:
            body = (await client.get("/procedures")).json()
        row = body["procedures"][0]
        assert row["mode"] == "advise"
        assert row["command_steps"] == 0

    @pytest.mark.asyncio
    async def test_bundle_summary_counts_current_vs_stale_sessions(self, r):
        await _seed_runbook(r)
        async with _client_for(r) as client:
            body = (await client.get("/procedures")).json()
            ver = body["bundle"]["version"]
            assert len(ver) == 12
            assert body["bundle"]["sessions_current"] == 0
            assert body["bundle"]["sessions_stale"] == 0
            await store.record_bundle_ack(r, WS, "sess-1", ver)
            await store.record_bundle_ack(r, WS, "sess-2", ver)
            await store.record_bundle_ack(r, WS, "sess-3", "stale-version")
            body = (await client.get("/procedures")).json()
        assert body["bundle"]["version"] == ver
        assert body["bundle"]["sessions_current"] == 2
        assert body["bundle"]["sessions_stale"] == 1


class TestDeviationsRoute:
    @pytest.mark.asyncio
    async def test_serves_the_ledger_newest_first(self, r):
        await store.record_deviation(r, _Settings(), WS, _record(0))
        await store.record_deviation(
            r, _Settings(), WS, _record(1, kind="ack", detail="why"))
        async with _client_for(r) as client:
            body = (await client.get("/procedures/deviations")).json()
        assert body["count"] == 2
        assert [d["step_id"] for d in body["deviations"]] == ["s1", "s0"]
        assert body["deviations"][0]["kind"] == "ack"

    @pytest.mark.asyncio
    async def test_the_limit_param_bounds_the_read(self, r):
        for i in range(3):
            await store.record_deviation(r, _Settings(), WS, _record(i))
        async with _client_for(r) as client:
            body = (await client.get("/procedures/deviations?limit=2")).json()
        assert body["count"] == 2
        assert [d["step_id"] for d in body["deviations"]] == ["s2", "s1"]

    @pytest.mark.asyncio
    async def test_scoped_to_the_caller_workspace(self, r):
        # Another workspace's ledger reads as empty, not as an error.
        await store.record_deviation(
            r, _Settings(), "workspace-other", _record())
        async with _client_for(r) as client:
            body = (await client.get("/procedures/deviations")).json()
        assert body == {"deviations": [], "count": 0}

    @pytest.mark.asyncio
    async def test_the_route_is_scope_gated(self, r, auth_keys):  # noqa: F811
        """Keyless under enforcement: 401. The memory:read key is served —
        the proof that read_dep is actually attached."""
        async with _client_for(r) as client:
            assert (await client.get(
                "/procedures/deviations")).status_code == 401
            ok = await client.get(
                "/procedures/deviations",
                headers={"X-API-Key": auth_keys["reader"]})
        assert ok.status_code == 200
