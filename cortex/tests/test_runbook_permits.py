"""Enforced Runbooks Phase A — verdicts and the permit protocol.

advise      -> allow + advisory (the warning text reaches the agent)
require_ack -> valid permit? consume (one-use, atomic GETDEL) -> allow
               else -> rethink + challenge (advisory carries the id)
block       -> a load-bearing predecessor lacks SUCCESSFUL evidence in the
               CURRENT execution -> block; else allow

Loops are impossible by construction: the challenge id is deterministic over
(workspace, session, skill, step, command_hash, bundle_version), so a retry
either consumes the permit minted for exactly that tuple or lands back on the
same challenge.
"""
from __future__ import annotations

import json

import pytest
import fakeredis.aioredis as fr

from app.agent_gateway.models import (
    Action,
    ActionAfterRequest,
    ActionBeforeRequest,
    Outcome,
)
from app.agent_gateway.service import AgentGatewayService, RethinkCounter
from app.procedures import enforce, store
from app.procedures.observe import ProcedureObserver

WS = "workspace-local"
MEMBER = "member-owner"



# Review 2026-08-15: every genuinely-evaluated verdict now carries the
# `runbook_evaluated` receipt (empty message) as its FIRST advisory — the
# client's block-mode branch refuses a bare allow without it. These helpers
# split the receipt from the human-facing advisories so each is pinned for
# what it is.
def _human(resp):
    return [a for a in resp.advisories if a.code != "runbook_evaluated"]


def _codes(resp):
    return [a.code for a in _human(resp)]


def _evaluated(resp):
    return any(a.code == "runbook_evaluated" for a in resp.advisories)


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


def _after(action_id, *, exit_status=0, ws=WS):
    req = ActionAfterRequest(
        action_id=action_id, outcome=Outcome(success=True),
        exit_status=exit_status,
    )
    req._verified_workspace = ws
    return req


async def _seed_runbook(r, ws=""):
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


def _ack_advisory(resp):
    got = [a for a in resp.advisories if a.code == "runbook_ack_required"]
    assert got, f"no runbook_ack_required advisory in {resp.advisories}"
    return got[0]


@pytest.fixture
def r():
    return fr.FakeRedis(decode_responses=True)


class TestAdviseMode:
    @pytest.mark.asyncio
    async def test_default_mode_is_advise_allow_plus_advisory(self, r):
        await _seed_runbook(r)
        svc = _service(r)
        resp = await svc.decide(_cmd_req("bash deploy.sh --now"))
        assert resp.decision == "allow"
        assert _codes(resp) == ["procedure_step_missing"]
        assert "run the backup" in _human(resp)[0].message

    @pytest.mark.asyncio
    async def test_no_missing_predecessor_means_no_advisory(self, r):
        await _seed_runbook(r)
        svc = _service(r)
        resp = await svc.decide(_cmd_req("bash backup.sh --full"))
        assert resp.decision == "allow"
        assert _human(resp) == []


class TestRequireAckProtocol:
    async def _challenged(self, r):
        await _seed_runbook(r)
        await store.set_mode(r, WS, "rb1", "require_ack", "human")
        svc = _service(r)
        resp = await svc.decide(_cmd_req("bash deploy.sh --now"))
        assert resp.decision == "rethink"
        cid = _ack_advisory(resp).evidence_event_id
        assert cid and cid.startswith("rbc_")
        assert await store.get_challenge(r, cid) is not None
        return svc, cid

    @pytest.mark.asyncio
    async def test_challenge_then_ack_then_retry_allows(self, r):
        svc, cid = await self._challenged(r)
        res = await enforce.acknowledge(
            r, _Settings(), challenge_id=cid, reason="backup ran out of band",
            workspace=WS, member=MEMBER, session="sess")
        assert res["ok"] is True
        assert await r.get(f"proc:permit:{cid}") is not None
        retry = await svc.decide(_cmd_req("bash deploy.sh --now"))
        assert retry.decision == "allow"
        # Consumed atomically: the permit is gone.
        assert await r.get(f"proc:permit:{cid}") is None
        # And the allowed command pends its evidence like any other.
        assert await r.get(f"proc:pending:{retry.action_id}") is not None

    @pytest.mark.asyncio
    async def test_a_permit_is_one_use(self, r):
        svc, cid = await self._challenged(r)
        await enforce.acknowledge(
            r, _Settings(), challenge_id=cid, reason="known good",
            workspace=WS, member=MEMBER, session="sess")
        first = await svc.decide(_cmd_req("bash deploy.sh --now"))
        assert first.decision == "allow"
        # The permit was consumed and the deploy has not SUCCEEDED yet, so the
        # same command is challenged again.
        second = await svc.decide(_cmd_req("bash deploy.sh --now"))
        assert second.decision == "rethink"
        assert _ack_advisory(second).evidence_event_id == cid

    @pytest.mark.asyncio
    async def test_a_different_command_cannot_spend_the_permit(self, r):
        """Different command => different hash => different challenge id: the
        permit stays where it is, unconsumed, and the new command gets its own
        challenge."""
        svc, cid = await self._challenged(r)
        await enforce.acknowledge(
            r, _Settings(), challenge_id=cid, reason="known good",
            workspace=WS, member=MEMBER, session="sess")
        other = await svc.decide(_cmd_req("bash deploy.sh --later"))
        assert other.decision == "rethink"
        other_cid = _ack_advisory(other).evidence_event_id
        assert other_cid != cid
        assert await r.get(f"proc:permit:{cid}") is not None  # untouched

    @pytest.mark.asyncio
    async def test_a_permit_is_session_bound(self, r):
        svc, cid = await self._challenged(r)
        await enforce.acknowledge(
            r, _Settings(), challenge_id=cid, reason="known good",
            workspace=WS, member=MEMBER, session="sess")
        other = await svc.decide(
            _cmd_req("bash deploy.sh --now", session="sess-2"))
        assert other.decision == "rethink"
        assert _ack_advisory(other).evidence_event_id != cid
        assert await r.get(f"proc:permit:{cid}") is not None  # untouched

    @pytest.mark.asyncio
    async def test_a_tuple_mismatched_permit_is_refused_and_destroyed(self, r):
        """The deterministic id is not the whole check: the bound tuple is
        verified after the GETDEL, and a mismatch fails toward re-challenge
        with the permit destroyed — the safe side."""
        await _seed_runbook(r)
        await store.set_mode(r, WS, "rb1", "require_ack", "human")
        svc = _service(r)
        bundle = await enforce.build_bundle(r, WS)
        chash = enforce.command_hash("bash deploy.sh --now")
        cid = enforce.challenge_id_for(WS, "sess", "rb1", "deploy", chash,
                                       bundle["version"], execution_no=1)
        await store.mint_permit(r, cid, {
            "workspace": WS, "member": "somebody-else", "session": "sess",
            "command_hash": chash, "skill": "rb1", "step_id": "deploy",
            "bundle_version": bundle["version"],
        })
        resp = await svc.decide(_cmd_req("bash deploy.sh --now"))
        assert resp.decision == "rethink"
        assert await r.get(f"proc:permit:{cid}") is None  # destroyed

    @pytest.mark.asyncio
    async def test_the_challenge_loop_never_yields_an_unacked_allow(self, r):
        """Retry-without-ack either re-challenges (same deterministic id) or
        escalates to block via the existing rethink limit — it NEVER allows.
        Then one ack + retry allows."""
        svc, cid = await self._challenged(r)
        for _ in range(6):
            resp = await svc.decide(_cmd_req("bash deploy.sh --now"))
            assert resp.decision in ("rethink", "block")
            acks = [a for a in resp.advisories
                    if a.code == "runbook_ack_required"]
            assert acks and acks[0].evidence_event_id == cid
        res = await enforce.acknowledge(
            r, _Settings(), challenge_id=cid, reason="restore tested today",
            workspace=WS, member=MEMBER, session="sess")
        assert res["ok"] is True
        assert (await svc.decide(
            _cmd_req("bash deploy.sh --now"))).decision == "allow"

    @pytest.mark.asyncio
    async def test_satisfied_evidence_needs_no_permit_at_all(self, r):
        await _seed_runbook(r)
        await store.set_mode(r, WS, "rb1", "require_ack", "human")
        svc = _service(r)
        backup = await svc.decide(_cmd_req("bash backup.sh --full"))
        assert backup.decision == "allow"
        await svc.record(_after(backup.action_id))
        resp = await svc.decide(_cmd_req("bash deploy.sh --now"))
        assert resp.decision == "allow"
        assert _human(resp) == []


class TestAcknowledgeRefusals:
    async def _challenge(self, r):
        await _seed_runbook(r)
        await store.set_mode(r, WS, "rb1", "require_ack", "human")
        svc = _service(r)
        resp = await svc.decide(_cmd_req("bash deploy.sh --now"))
        return _ack_advisory(resp).evidence_event_id

    @pytest.mark.asyncio
    async def test_an_unknown_challenge_is_refused(self, r):
        res = await enforce.acknowledge(
            r, _Settings(), challenge_id="rbc_nope", reason="x",
            workspace=WS, member=MEMBER, session="sess")
        assert res == {"ok": False, "error": "unknown_or_expired"}

    @pytest.mark.asyncio
    async def test_another_workspaces_challenge_is_refused_opaquely(self, r):
        cid = await self._challenge(r)
        res = await enforce.acknowledge(
            r, _Settings(), challenge_id=cid, reason="x",
            workspace="workspace-other", member="m", session="sess")
        # Same answer as not-existing: nothing leaks across the boundary.
        assert res == {"ok": False, "error": "unknown_or_expired"}
        assert await r.get(f"proc:permit:{cid}") is None

    @pytest.mark.asyncio
    async def test_the_wrong_session_is_refused(self, r):
        cid = await self._challenge(r)
        res = await enforce.acknowledge(
            r, _Settings(), challenge_id=cid, reason="x",
            workspace=WS, member=MEMBER, session="another-session")
        assert res == {"ok": False, "error": "session_mismatch"}
        assert await r.get(f"proc:permit:{cid}") is None

    @pytest.mark.asyncio
    async def test_an_empty_reason_is_refused(self, r):
        cid = await self._challenge(r)
        for reason in ("", "   "):
            res = await enforce.acknowledge(
                r, _Settings(), challenge_id=cid, reason=reason,
                workspace=WS, member=MEMBER, session="sess")
            assert res == {"ok": False, "error": "reason_required"}
        assert await r.get(f"proc:permit:{cid}") is None

    @pytest.mark.asyncio
    async def test_the_ack_reason_is_recorded_for_audit(self, r):
        cid = await self._challenge(r)
        await enforce.acknowledge(
            r, _Settings(), challenge_id=cid, reason="restore was verified",
            workspace=WS, member=MEMBER, session="sess")
        raw = await r.get(f"proc:ack:{cid}")
        assert raw
        rec = json.loads(raw)
        assert rec["reason"] == "restore was verified"
        assert rec["member"] == MEMBER
        assert rec["skill"] == "rb1"


class TestBlockMode:
    @pytest.mark.asyncio
    async def test_missing_load_bearing_predecessor_blocks(self, r):
        await _seed_runbook(r)
        await store.set_mode(r, WS, "rb1", "block", "human")
        svc = _service(r)
        resp = await svc.decide(_cmd_req("bash deploy.sh --now"))
        assert resp.decision == "block"
        codes = [a.code for a in resp.advisories]
        assert "runbook_blocked" in codes
        blocked = [a for a in resp.advisories if a.code == "runbook_blocked"][0]
        assert "run the backup" in blocked.message
        assert "vps deploy" in blocked.message

    @pytest.mark.asyncio
    async def test_successful_evidence_unblocks(self, r):
        await _seed_runbook(r)
        await store.set_mode(r, WS, "rb1", "block", "human")
        svc = _service(r)
        backup = await svc.decide(_cmd_req("bash backup.sh --full"))
        assert backup.decision == "allow"  # order 0: nothing precedes it
        await svc.record(_after(backup.action_id, exit_status=0))
        resp = await svc.decide(_cmd_req("bash deploy.sh --now"))
        assert resp.decision == "allow"
        assert _human(resp) == []

    @pytest.mark.asyncio
    async def test_a_permitted_but_failed_backup_does_not_unlock_the_deploy(
            self, r):
        """THE review-disposition-1 case, end to end: the backup was ALLOWED
        and ran, but exited nonzero — its evidence never commits, and block
        mode still refuses the deploy."""
        await _seed_runbook(r)
        await store.set_mode(r, WS, "rb1", "block", "human")
        svc = _service(r)
        backup = await svc.decide(_cmd_req("bash backup.sh --full"))
        assert backup.decision == "allow"
        await svc.record(_after(backup.action_id, exit_status=1))
        resp = await svc.decide(_cmd_req("bash deploy.sh --now"))
        assert resp.decision == "block"

    @pytest.mark.asyncio
    async def test_a_non_load_bearing_gap_never_blocks(self, r):
        """block refuses only over LOAD-BEARING predecessors — exactly round
        1's missing-load-bearing detection, not strict next-step order."""
        await _seed_runbook(r)
        # Make backup non-load-bearing.
        idx = json.loads(await r.get(store.INDEX_KEY))
        idx[0]["load_bearing"] = False
        await r.set(store.INDEX_KEY, json.dumps(idx))
        await store.set_mode(r, WS, "rb1", "block", "human")
        svc = _service(r)
        resp = await svc.decide(_cmd_req("bash deploy.sh --now"))
        assert resp.decision == "allow"

    @pytest.mark.asyncio
    async def test_evidence_from_the_previous_execution_does_not_unblock(
            self, r):
        """Evidence lookups are scoped to the CURRENT execution: after the
        terminal step closes execution 1, its backup no longer satisfies
        execution 2's deploy."""
        await _seed_runbook(r)
        await store.set_mode(r, WS, "rb1", "block", "human")
        svc = _service(r)
        b = await svc.decide(_cmd_req("bash backup.sh --full"))
        await svc.record(_after(b.action_id))
        d = await svc.decide(_cmd_req("bash deploy.sh --now"))
        assert d.decision == "allow"
        await svc.record(_after(d.action_id))  # terminal: closes execution 1
        again = await svc.decide(_cmd_req("bash deploy.sh --again"))
        assert again.decision == "block"


class TestModeStore:
    @pytest.mark.asyncio
    async def test_default_mode_is_advise(self, r):
        assert (await store.get_mode(r, WS, "rb1"))["mode"] == "advise"

    @pytest.mark.asyncio
    async def test_set_mode_round_trips_and_validates(self, r):
        rec = await store.set_mode(r, WS, "rb1", "block", "human")
        assert rec["mode"] == "block" and rec["set_by"] == "human"
        got = await store.get_mode(r, WS, "rb1")
        assert got["mode"] == "block"
        with pytest.raises(ValueError):
            await store.set_mode(r, WS, "rb1", "yolo", "human")

    @pytest.mark.asyncio
    async def test_modes_are_workspace_keyed(self, r):
        await store.set_mode(r, "workspace-other", "rb1", "block", "human")
        assert (await store.get_mode(r, WS, "rb1"))["mode"] == "advise"

    @pytest.mark.asyncio
    async def test_a_corrupt_mode_degrades_to_advise_never_raises(self, r):
        await r.set(store.mode_key(WS, "rb1"), "{not json")
        assert (await store.get_mode(r, WS, "rb1"))["mode"] == "advise"
        await r.set(store.mode_key(WS, "rb1"), json.dumps({"mode": "yolo"}))
        assert (await store.get_mode(r, WS, "rb1"))["mode"] == "advise"
