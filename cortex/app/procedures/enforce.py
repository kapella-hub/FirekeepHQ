"""Enforcement verdicts for command-kind runbook steps (round 2).

Spec: docs/superpowers/specs/2026-08-15-enforced-runbooks-design.md.

Deliberately NOT a policy rule, for the reason observe.py already documents:
PolicyContext carries no action type, so a rule cannot tell a file path from a
command before matching; ActionBeforeRequest can. It also keeps the blast
radius local — a failure here cannot take out the policy rule set.

Called from AgentGatewayService.decide() AFTER policy evaluation; the existing
precedence aggregates (block over rethink over allow). Everything here is
exception-tight: the pre-tool path cannot raise (I6), and a verdict that
cannot be computed degrades to `allow` with nothing recorded — the same
fail-open posture as round 1. The one fail-CLOSED branch the spec demands
(block-mode server unreachable) lives in the CLIENT hook, phase B; on the
server, block is only ever returned as an authenticated verdict.

Command matching is MISTAKE-CATCHING, NOT ADVERSARY-PROOF (see
match.match_command). Evidence is scored by SUCCESS, not permission: a command
observation is PENDING at decide() and commits only when the reconcile carries
a real exit status of 0 (review disposition 1 — a permitted-but-failed backup
must not unlock the deploy). File steps keep round-1 commit-at-allow,
documented as an approximation.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.agent_gateway.models import Advisory
from app.procedures import match, store

logger = logging.getLogger(__name__)

# What the client hook does when the escalation call itself fails, per mode.
# advise / require_ack fail OPEN; block fails CLOSED — the price the human
# accepted when choosing block, and only for block-mode patterns.
FAIL_POSTURE = {"advise": "open", "require_ack": "open", "block": "closed"}

# Cap on the receipt/audit copy of a command's identifying text.
_REASON_MAX_CHARS = 2000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def command_hash(normalized_command: str) -> str:
    """Identity of one whitespace-normalized command string."""
    return hashlib.sha256(normalized_command.encode("utf-8", "replace")).hexdigest()[:16]


def evaluated_marker() -> "Advisory":
    """The positive-evaluation receipt (review 2026-08-15, finding: a
    server-internal failure must not convert block mode into an authenticated
    allow). Attached ONLY on paths where runbook evaluation genuinely ran —
    index readable, session joinable, no internal exception. The client's
    block-mode branch lowers its exit code only when an allow carries this
    marker; a bare allow (degraded server) stays failed-closed. The empty
    message keeps it out of the human-facing advisory line."""
    return Advisory(code="runbook_evaluated", message="")


def challenge_id_for(workspace: str, session: str, skill_id: str, step_id: str,
                     chash: str, bundle_version: str,
                     execution_no: int = 0) -> str:
    """Deterministic challenge id over the bound tuple.

    Deterministic BY DESIGN: the retried command recomputes the same id, so it
    either consumes the permit minted for this exact (workspace, session,
    skill, step, command, bundle, EXECUTION) or lands back on the same
    challenge — loops are impossible by construction, and a different
    command, a changed bundle, or a fresh execution produces a different id,
    so nothing can be reused across them. execution_no joined the tuple by
    review (2026-08-15): without it, an acked-but-unspent permit minted
    during execution N remained consumable in execution N+1 within its TTL,
    waving the same command through a fresh evidence scope with no fresh ack.
    """
    raw = "\x1f".join([workspace, session, skill_id, step_id, chash,
                       bundle_version, str(int(execution_no))])
    return "rbc_" + hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:24]


def terminal_order(coverage_row: dict | None,
                   skill_entries: list[dict]) -> int | None:
    """The spec-list position of a skill's LAST step.

    From coverage (`spec_count` counts every kept spec, unobservable included)
    when available; falling back to the highest indexed order for a deployment
    that has not rebuilt coverage. None when nothing is known — an execution
    whose end cannot be located is never closed by guesswork.
    """
    if coverage_row:
        try:
            n = int(coverage_row.get("spec_count") or 0)
            if n > 0:
                return n - 1
        except (TypeError, ValueError):
            pass
    orders = [e.get("order", 0) for e in skill_entries
              if isinstance(e.get("order", 0), int)]
    return max(orders) if orders else None


def canonical_bundle_entries(index: list[dict], modes: dict[str, dict],
                             workspace: str) -> list[dict]:
    """The bundle's entry list, in canonical order, for one workspace.

    Command-kind steps of ALL runbooks, ALL modes — advise entries escalate
    too; the gated set is small and curated (spec: "Bundle").
    """
    entries: list[dict[str, Any]] = []
    for e in index:
        if not isinstance(e, dict):
            continue
        if (e.get("kind") or "file_glob") != "command":
            continue
        if not store.workspace_visible(e.get("workspace_id") or "", workspace):
            continue
        mode = (modes.get(e.get("skill_id") or "") or {}).get("mode") \
            or store.DEFAULT_MODE
        entries.append({
            "skill_id": e.get("skill_id") or "",
            "step_id": e.get("step_id") or "",
            "pattern": e.get("pattern") or "",
            # Forward-compat, pinned with the client (Phase B): the client
            # skips entries whose kind is present and not "command".
            "kind": "command",
            "mode": mode,
            "load_bearing": bool(e.get("load_bearing")),
            "fail_posture": FAIL_POSTURE.get(mode, "open"),
        })
    entries.sort(key=lambda x: (x["skill_id"], x["step_id"]))
    return entries


def bundle_version(entries: list[dict]) -> str:
    """sha256[:12] of the canonical entry list. Deterministic: sorted entries,
    sorted keys, no whitespace."""
    canon = json.dumps(entries, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:12]


async def build_bundle(redis_client, workspace: str) -> dict:
    """{version, workspace_id, entries} — what GET /procedures/bundle serves
    and what the permit protocol binds against."""
    index = await store.load_index(redis_client)
    modes: dict[str, dict] = {}
    for e in index:
        sid = e.get("skill_id") if isinstance(e, dict) else None
        if sid and sid not in modes:
            modes[sid] = await store.get_mode(redis_client, workspace, sid)
    entries = canonical_bundle_entries(index, modes, workspace)
    return {
        "version": bundle_version(entries),
        "workspace_id": workspace,
        "entries": entries,
    }


class CommandEnforcement:
    """One command's verdict, plus the pending evidence to write IF the
    decision settles on allow (two-phase, mirroring PendingObservation)."""

    __slots__ = ("decision", "advisories", "_redis", "_action_id", "_pending",
                 "_ttl")

    def __init__(self, decision: str = "allow", advisories=None, *,
                 redis_client=None, action_id: str = "",
                 pending: dict | None = None, ttl_seconds: int = 300):
        self.decision = decision
        self.advisories: list[Advisory] = list(advisories or [])
        self._redis = redis_client
        self._action_id = action_id
        self._pending = pending
        self._ttl = ttl_seconds

    async def commit(self) -> None:
        """The decision settled on allow: the command is about to run, so the
        pending observation may exist. Never raises."""
        if self._pending is None or self._redis is None or not self._action_id:
            return
        try:
            await store.write_pending(self._redis, self._action_id,
                                      self._pending, self._ttl)
        except Exception as exc:  # noqa: BLE001 — I6
            logger.debug("pending command observation not written: %s", exc)

    async def abort(self, decision: str = "") -> None:
        """The gateway refused the command: nothing will run, so nothing may
        pend. (No warn latches to give back — command advisories are not
        latched; each escalated command is a fresh deviation.)"""
        logger.debug("command enforcement pending dropped: decision=%s", decision)


async def evaluate(redis_client, settings, *, req, workspace: str, member: str,
                   action_id: str, index: list[dict],
                   index_ok: bool = True) -> CommandEnforcement:
    """Verdict for one run_command action. NEVER raises; degrades to allow —
    but a degraded allow carries NO evaluated marker, and the client's
    block-mode branch refuses to lower its exit code without one.

    advise      -> allow + advisory (the warning text reaches the agent)
    require_ack -> valid permit? consume (one-use, atomic GETDEL) -> allow
                   else -> rethink + challenge (advisory carries the id)
    block       -> a load-bearing predecessor lacks SUCCESSFUL evidence in the
                   CURRENT execution -> block; else allow
    """
    try:
        if getattr(req.action, "type", "") != "run_command":
            return CommandEnforcement()
        if not index_ok:
            # The index could not be READ (distinct from absent/empty): no
            # evaluation happened, so no marker — a block-mode client fails
            # closed rather than accepting a blind allow (review 2026-08-15).
            return CommandEnforcement()
        normalized = match.normalize_command(req.action.target)
        if not normalized:
            return CommandEnforcement("allow", [evaluated_marker()])
        visible = [e for e in index if isinstance(e, dict)
                   and store.workspace_visible(e.get("workspace_id") or "",
                                               workspace)]
        matched = match.match_command(visible, normalized)
        if not matched:
            # Evaluated: no runbook governs this command (the client's bundle
            # was stale, or the entry was retired server-side).
            return CommandEnforcement("allow", [evaluated_marker()])

        # A session that cannot be joined to an outcome supports no evidence
        # scope: nothing can pend and nothing can be verified against it —
        # the same rule the file path applies (observe._UNUSABLE_SESSIONS).
        # Deliberately NO marker: for a block-mode client this fails closed,
        # because enforcement without an evidence scope is not enforcement.
        session = (req.session_id or "").strip()
        if session.lower() in ("", "unknown", "none", "null"):
            return CommandEnforcement()

        chash = command_hash(normalized)
        ttl = int(getattr(settings, "AGENT_RECONCILE_DEADLINE_SECONDS", 300))

        decision = "allow"
        advisories: list[Advisory] = []
        matches_meta: list[dict] = []
        observed_by_skill: dict[str, set[str]] = {}
        exec_no_by_skill: dict[str, int] = {}
        bundle_ver = ""  # computed lazily, once, only if require_ack needs it

        for entry in matched:
            skill_id = entry["skill_id"]
            if skill_id not in observed_by_skill:
                record = await store.get_execution(
                    redis_client, session, skill_id, workspace)
                exec_no_by_skill[skill_id] = store.effective_execution_no(record)
                if record and record.get("closed_at"):
                    observed_by_skill[skill_id] = set()
                else:
                    observed_by_skill[skill_id] = set(
                        (record or {}).get("observed") or {})
            observed = observed_by_skill[skill_id]
            matches_meta.append({
                "skill": skill_id,
                "step_id": entry["step_id"],
                "execution_no": exec_no_by_skill[skill_id],
            })
            # A sibling step satisfied by THIS SAME command counts for the
            # verdict: the one command either succeeds for both steps or fails
            # for both, so challenging B over A here would challenge the
            # command for not yet having run itself.
            observed.add(entry["step_id"])

            missing = match.missing_load_bearing(
                visible, skill_id, entry.get("order", 0), observed)
            if not missing:
                continue

            mode = (await store.get_mode(redis_client, workspace,
                                         skill_id))["mode"]
            trigger = (entry.get("skill_trigger") or "this runbook").strip()
            reason = "; ".join(
                (m.get("step_text") or m.get("step_id") or "an earlier step")
                for m in missing)[:_REASON_MAX_CHARS]

            if mode == "advise":
                stats = await store.get_step_stats(redis_client, skill_id)
                for m in missing:
                    advisories.append(Advisory(
                        code="procedure_step_missing",
                        message=match.advisory_text(entry, m, stats),
                    ))
                continue

            if mode == "require_ack":
                if not bundle_ver:
                    bundle = await build_bundle(redis_client, workspace)
                    bundle_ver = bundle["version"]
                exec_no = exec_no_by_skill[skill_id]
                cid = challenge_id_for(workspace, session, skill_id,
                                       entry["step_id"], chash, bundle_ver,
                                       execution_no=exec_no)
                permit = await store.consume_permit(redis_client, cid)
                bound_ok = bool(permit) and (
                    permit.get("workspace") == workspace
                    and permit.get("member") == member
                    and permit.get("session") == session
                    and permit.get("command_hash") == chash
                    and permit.get("skill") == skill_id
                    and permit.get("step_id") == entry["step_id"]
                    and permit.get("bundle_version") == bundle_ver
                    # Review 2026-08-15: a permit minted during execution N
                    # must not survive into execution N+1's fresh scope.
                    and int(permit.get("execution_no", -1)) == int(exec_no)
                )
                if bound_ok:
                    continue  # the one-use permit authorises exactly this command
                await store.mint_challenge(redis_client, cid, {
                    "workspace": workspace, "member": member,
                    "session": session, "skill": skill_id,
                    "step_id": entry["step_id"], "command_hash": chash,
                    "bundle_version": bundle_ver, "execution_no": exec_no,
                    "missing": reason, "created": _now(),
                })
                if decision != "block":
                    decision = "rethink"
                advisories.append(Advisory(
                    code="runbook_ack_required",
                    message=(
                        f"Runbook \"{trigger}\" requires an acknowledgement: "
                        f"step(s) \"{reason}\" have no successful evidence in "
                        f"this execution. To proceed anyway, call "
                        f"runbook_ack(challenge_id=\"{cid}\", reason=<why>) "
                        f"and retry the same command."
                    ),
                    evidence_event_id=cid,
                ))
                continue

            if mode == "block":
                decision = "block"
                advisories.append(Advisory(
                    code="runbook_blocked",
                    message=(
                        f"Runbook \"{trigger}\" (block mode): load-bearing "
                        f"step(s) \"{reason}\" have no successful evidence in "
                        f"this execution. Complete them first — this command "
                        f"will not run until they have succeeded."
                    ),
                ))
                # Ledger (Phase C): a refused command is a deviation the
                # operator sees. Hash only, never the command text.
                await store.record_deviation(redis_client, settings, workspace, {
                    "at": _now(), "kind": "block", "skill_id": skill_id,
                    "step_id": entry["step_id"], "session": session,
                    "member": member, "agent": req.agent_id or "",
                    "command_hash": chash, "detail": "",
                })

        first = matches_meta[0]
        pending = {
            "workspace": workspace,
            "session": session,
            "skill": first["skill"],
            "step_id": first["step_id"],
            "execution_no": first["execution_no"],
            "command_hash": chash,
            "created": _now(),
            # Additive to the spec shape: observability labels, the audit-only
            # cwd the client hook reported, and the full match set when one
            # command satisfies several steps.
            "agent": req.agent_id,
            "adapter": req.adapter,
            "cwd": getattr(req.action, "cwd", None),
            "matches": matches_meta,
        }
        return CommandEnforcement(
            decision, [evaluated_marker()] + advisories,
            redis_client=redis_client,
            action_id=action_id, pending=pending, ttl_seconds=ttl,
        )
    except Exception as exc:  # noqa: BLE001 — I6: the pre-tool path cannot raise
        logger.debug("command enforcement skipped: %s", exc)
        return CommandEnforcement()


async def _attempt_deviations(redis_client, settings, workspace: str,
                              pending: dict, matches: list[dict],
                              detail: str) -> None:
    """One failed_attempt ledger row per pending match (Phase C). Recorded to
    the OWNING workspace — the operator whose runbook the attempt belonged to.
    Hash only, never the command text; pending carries no member id, so that
    field is genuinely empty here."""
    for m in matches:
        if not isinstance(m, dict):
            continue
        await store.record_deviation(redis_client, settings, workspace, {
            "at": _now(), "kind": "failed_attempt",
            "skill_id": m.get("skill") or "",
            "step_id": m.get("step_id") or "",
            "session": pending.get("session") or "",
            "member": "",
            "agent": pending.get("agent") or "",
            "command_hash": pending.get("command_hash") or "",
            "detail": detail,
        })


async def reconcile(redis_client, settings, *, action_id: str, success: bool,
                    exit_status: int | None,
                    caller_workspace: str = "") -> dict:
    """Settle one pending command observation. NEVER raises.

    exit_status == 0 AND success   -> the pending observation COMMITS
    nonzero / unknown / absent     -> attempt recorded; NOTHING satisfied
    no reconcile before TTL        -> pending already expired; nothing satisfied
    """
    try:
        pending = await store.take_pending(redis_client, action_id)
        if pending is None:
            return {"status": "none"}

        matches = pending.get("matches")
        if not isinstance(matches, list) or not matches:
            matches = [{"skill": pending.get("skill"),
                        "step_id": pending.get("step_id"),
                        "execution_no": pending.get("execution_no", 1)}]

        # Tenancy: a caller in another workspace cannot settle this evidence.
        # The pending is already consumed (GETDEL) — failing toward "satisfies
        # nothing" is the safe side.
        ws = pending.get("workspace") or ""
        if caller_workspace and ws and caller_workspace != ws:
            await store.record_attempt(redis_client, settings, action_id, {
                **pending, "settled": _now(),
                "outcome": {"success": bool(success), "exit_status": exit_status,
                            "refused": "workspace mismatch"},
            })
            await _attempt_deviations(redis_client, settings, ws, pending,
                                      matches, "workspace mismatch")
            return {"status": "attempt", "reason": "workspace mismatch"}

        real_zero = (exit_status == 0 and not isinstance(exit_status, bool))
        if not (success is True and real_zero):
            await store.record_attempt(redis_client, settings, action_id, {
                **pending, "settled": _now(),
                "outcome": {"success": bool(success), "exit_status": exit_status},
            })
            await _attempt_deviations(
                redis_client, settings, ws, pending, matches,
                "success=false" if success is not True
                else f"exit_status={exit_status}")
            return {"status": "attempt"}

        index = await store.load_index(redis_client)
        coverage = await store.load_coverage(redis_client)
        by_skill: dict[str, list[dict]] = {}
        for e in index:
            if isinstance(e, dict) and e.get("skill_id"):
                by_skill.setdefault(e["skill_id"], []).append(e)

        committed = 0
        for m in matches:
            skill_id = m.get("skill") or ""
            step_id = m.get("step_id") or ""
            if not skill_id or not step_id:
                continue
            entries = by_skill.get(skill_id) or []
            entry = next((e for e in entries if e.get("step_id") == step_id),
                         None)
            term = terminal_order(coverage.get(skill_id), entries)
            # Only a terminal COMMAND step closes an execution: "file_glob
            # semantics unchanged" is a round-1 keep, so file commits never
            # close, and an unobservable terminal step can never be seen to
            # commit — the execution then ages out on its TTL instead.
            closes = (entry is not None
                      and (entry.get("kind") or "file_glob") == "command"
                      and term is not None
                      and entry.get("order", -1) == term)
            try:
                expected = int(m.get("execution_no") or 1)
            except (TypeError, ValueError):
                expected = 1
            exec_id = await store.record_observation(
                redis_client, settings,
                session_id=pending.get("session") or "",
                skill_id=skill_id, step_id=step_id, action_id=action_id,
                target=f"command:{pending.get('command_hash') or ''}",
                agent_id=pending.get("agent") or "",
                adapter=pending.get("adapter") or "shell-hook",
                workspace_id=ws, expected_execution_no=expected,
                closes_execution=closes,
            )
            if exec_id:
                committed += 1
            else:
                logger.debug(
                    "stale command evidence dropped for %s (%s/%s exec %s)",
                    action_id, skill_id, step_id, expected)
        return {"status": "committed", "steps": committed}
    except Exception as exc:  # noqa: BLE001 — reconcile is best-effort
        logger.debug("command reconcile skipped for %s: %s", action_id, exc)
        return {"status": "error"}


async def acknowledge(redis_client, settings, *, challenge_id: str,
                      reason: str, workspace: str, member: str,
                      session: str) -> dict:
    """The ack half of the permit protocol (runbook_ack / POST /procedures/ack).

    Verifies the challenge belongs to the caller's verified workspace and the
    named session, records the acknowledged reason (audit + future ledger),
    and mints the one-use permit bound to (workspace, member, session,
    command_hash, skill, step, bundle_version), TTL 10 minutes.
    """
    reason = (reason or "").strip()[:_REASON_MAX_CHARS]
    if not reason:
        return {"ok": False, "error": "reason_required"}
    challenge = await store.get_challenge(redis_client, challenge_id)
    if challenge is None:
        return {"ok": False, "error": "unknown_or_expired"}
    if (challenge.get("workspace") or "") != workspace:
        # Do not reveal that another workspace's challenge exists.
        return {"ok": False, "error": "unknown_or_expired"}
    if (challenge.get("session") or "") != (session or ""):
        return {"ok": False, "error": "session_mismatch"}
    await store.record_ack(redis_client, settings, challenge_id, {
        "challenge_id": challenge_id, "reason": reason,
        "workspace": workspace, "member": member, "session": session,
        "skill": challenge.get("skill"), "step_id": challenge.get("step_id"),
        "command_hash": challenge.get("command_hash"),
        "missing": challenge.get("missing"), "acked_at": _now(),
    })
    # Ledger (Phase C): every accepted override is a deviation on the record.
    # No agent id on this path — the ack arrives over REST/MCP, not the
    # gateway's action stream.
    await store.record_deviation(redis_client, settings, workspace, {
        "at": _now(), "kind": "ack",
        "skill_id": challenge.get("skill") or "",
        "step_id": challenge.get("step_id") or "",
        "session": session, "member": member, "agent": "",
        "command_hash": challenge.get("command_hash") or "",
        "detail": reason,
    })
    await store.mint_permit(redis_client, challenge_id, {
        "workspace": workspace,
        "member": member,
        "session": session,
        "command_hash": challenge.get("command_hash") or "",
        "skill": challenge.get("skill") or "",
        "step_id": challenge.get("step_id") or "",
        "bundle_version": challenge.get("bundle_version") or "",
        "execution_no": challenge.get("execution_no", 0),
        "minted_at": _now(),
    })
    return {"ok": True, "challenge_id": challenge_id,
            "permit_expires_in_seconds": store.PERMIT_TTL_SECONDS}
