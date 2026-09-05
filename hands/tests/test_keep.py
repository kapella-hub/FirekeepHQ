import pytest

from firekeep_client import resolver, transport
from firekeep_hands import keep


def test_a_machine_with_no_keep_is_offline_without_being_told(monkeypatch):
    """An unconfigured machine that thinks it is online advertises a phone
    approval path it does not have and retries a doomed post every few
    seconds per permit. `resolver.resolve` answers from the config file with
    no network I/O, so asking is nearly free."""
    monkeypatch.delenv("FIREKEEP_HANDS_OFFLINE", raising=False)
    monkeypatch.setattr(resolver, "resolve", lambda *a, **k: (_ for _ in ()).throw(
        resolver.ConfigError("firekeep config not found")))
    assert keep.KeepLink(agent_id="a", machine_id="m").offline is True


def test_a_configured_machine_is_online(monkeypatch):
    monkeypatch.delenv("FIREKEEP_HANDS_OFFLINE", raising=False)
    monkeypatch.setattr(resolver, "resolve", lambda *a, **k: object())
    assert keep.KeepLink(agent_id="a", machine_id="m").offline is False


def test_any_resolver_failure_counts_as_no_keep(monkeypatch):
    monkeypatch.delenv("FIREKEEP_HANDS_OFFLINE", raising=False)
    monkeypatch.setattr(resolver, "resolve", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("odd")))
    assert keep.KeepLink(agent_id="a", machine_id="m").offline is True


def test_an_explicit_offline_argument_wins_over_both(monkeypatch):
    monkeypatch.delenv("FIREKEEP_HANDS_OFFLINE", raising=False)
    monkeypatch.setattr(resolver, "resolve", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("odd")))
    assert keep.KeepLink(agent_id="a", machine_id="m", offline=False).offline is False
    monkeypatch.setattr(resolver, "resolve", lambda *a, **k: object())
    monkeypatch.setenv("FIREKEEP_HANDS_OFFLINE", "1")
    assert keep.KeepLink(agent_id="a", machine_id="m", offline=False).offline is False


def test_the_env_switch_still_forces_offline_on_a_configured_machine(monkeypatch):
    monkeypatch.setenv("FIREKEEP_HANDS_OFFLINE", "1")
    monkeypatch.setattr(resolver, "resolve", lambda *a, **k: object())
    assert keep.KeepLink(agent_id="a", machine_id="m").offline is True


def test_offline_makes_every_call_a_noop(monkeypatch):
    called = []
    monkeypatch.setattr(keep, "call_tool", lambda *a, **k: called.append(a))
    link = keep.KeepLink(agent_id="a", machine_id="m", offline=True)
    assert link.action_before(goal="g", task_id="t", apps=[]) is None
    assert link.acquire_lease() is None and link.permit_task_state("c") is None
    assert called == []


def test_online_calls_map_to_the_right_tools(monkeypatch):
    seen = []
    def fake(service, tool, args, **kw):
        seen.append((service, tool, args))
        return {"cortex.action_before": {"action_id": "A1"},
                "relay.relay_lease": {"status": "acquired", "fencing_token": 7},
                "relay.relay_heartbeat": {"status": "extended"},
                "relay.relay_task_post": {"status": "created", "task": {"id": "task-1"}},
                "relay.relay_task_list": {"tasks": [{"id": "task-1", "status": "completed", "result": "approve"}]},
                }.get(f"{service}.{tool}", {})
    monkeypatch.setattr(keep, "call_tool", fake)
    link = keep.KeepLink(agent_id="a", machine_id="m", offline=False)
    assert link.action_before(goal="g", task_id="t", apps=["X"]) == "A1"
    assert link.acquire_lease()["fencing_token"] == 7
    link.renew_lease()
    assert link.post_permit_task(challenge="c", title="Send", classes=("send",), task_id="t", step_index=2, expires_at="x") == "task-1"
    assert link.permit_task_state("c") == "approve"
    link.release_lease(); link.action_after("A1", "done", "ok")
    tools = [(s, t) for s, t, _ in seen]
    assert tools == [("cortex", "action_before"), ("relay", "relay_lease"), ("relay", "relay_heartbeat"),
                     ("relay", "relay_task_post"), ("relay", "relay_task_list"), ("relay", "relay_release"),
                     ("cortex", "action_after")]
    assert seen[1][2]["resource_id"] == "hands:m" and seen[5][2]["fencing_token"] == 7
    assert seen[2][2]["resource_id"] == "hands:m" and seen[2][2]["fencing_token"] == 7 and seen[2][2]["agent_id"] == "a"
    assert seen[3][2]["title"] == "hands_permit:c" and seen[4][2]["title"] == "hands_permit:c"


def test_the_keeps_decision_is_kept_alongside_the_action_id(monkeypatch):
    """cortex answers `action_before` with a whole `ActionBeforeResponse`,
    not just an id. The decision half lands on `last_decision` so a caller
    can honour a block; the reason comes from the advisories, which is the
    only human-readable text in that response."""
    monkeypatch.setattr(keep, "call_tool", lambda *a, **k: {
        "decision": "block",
        "action_id": "A9",
        "tier": "full",
        "advisories": [{"code": "contested", "message": "this contradicts Tuesday's runbook"},
                       {"code": "contested", "message": "and it deletes the backup"}],
    })
    link = keep.KeepLink(agent_id="a", machine_id="m", offline=False)
    assert link.action_before(goal="g", task_id="t", apps=[]) == "A9"
    assert link.last_decision.blocked is True
    assert link.last_decision.reason == (
        "this contradicts Tuesday's runbook; and it deletes the backup")


@pytest.mark.parametrize("reply,expected", [
    ({"decision": "allow", "action_id": "A1"}, "allow"),
    ({"decision": "rethink", "action_id": "A1", "advisories": []}, "rethink"),
    ({"action_id": "A1"}, None),          # a reply with no decision at all
    ({}, None),
    (None, None),                          # offline, or the call failed
    ("not a dict", None),
])
def test_only_an_explicit_block_reads_as_blocked(monkeypatch, reply, expected):
    monkeypatch.setattr(keep, "call_tool", lambda *a, **k: reply)
    link = keep.KeepLink(agent_id="a", machine_id="m", offline=False)
    link.action_before(goal="g", task_id="t", apps=[])
    assert link.last_decision.decision == expected
    assert link.last_decision.blocked is False


def test_an_offline_link_never_claims_the_keep_decided(monkeypatch):
    monkeypatch.setattr(keep, "call_tool", lambda *a, **k: pytest.fail("called while offline"))
    link = keep.KeepLink(agent_id="a", machine_id="m", offline=True)
    link.action_before(goal="g", task_id="t", apps=[])
    assert link.last_decision == keep.KeepDecision(None, "")


def _lease_replies(monkeypatch, replies):
    """Answers `relay_lease` from `replies` in order and records every call."""
    seen = []

    def fake(service, tool, args, **kw):
        seen.append((service, tool, args))
        if tool == "relay_lease":
            return replies.pop(0)
        return {}

    monkeypatch.setattr(keep, "call_tool", fake)
    return seen


def test_our_own_stranded_lease_is_released_and_retaken(monkeypatch):
    """A server that exited without hands_task_end leaves its lease held for
    the full TTL. The next run on the same machine, same agent, must not be
    locked out for half an hour by its own dead predecessor."""
    seen = _lease_replies(monkeypatch, [
        {"acquired": False, "held_by": "a", "fencing_token": 4, "expires_in": 1700},
        {"acquired": True, "fencing_token": 5},
    ])
    link = keep.KeepLink(agent_id="a", machine_id="m", offline=False)
    result = link.acquire_lease()

    assert result["acquired"] is True
    assert [t for _s, t, _a in seen] == ["relay_lease", "relay_release", "relay_lease"]
    assert seen[1][2] == {"resource_id": "hands:m", "agent_id": "a", "fencing_token": 4}
    assert link._fencing_token == 5      # ours, from the successful retake


def test_another_agents_lease_is_never_taken_and_its_token_never_adopted(monkeypatch):
    seen = _lease_replies(monkeypatch, [
        {"acquired": False, "held_by": "someone-else", "fencing_token": 9, "expires_in": 1700},
    ])
    link = keep.KeepLink(agent_id="a", machine_id="m", offline=False)
    result = link.acquire_lease()

    assert result["acquired"] is False and result["held_by"] == "someone-else"
    assert [t for _s, t, _a in seen] == ["relay_lease"]   # no release, no retry
    assert link._fencing_token == 0
    link.release_lease()
    assert [t for _s, t, _a in seen] == ["relay_lease"]   # and nothing sent afterwards


def test_the_reclaim_happens_at_most_once_per_link(monkeypatch):
    """A live peer sharing this agent id must not be robbed over and over by
    a caller that keeps retrying."""
    seen = _lease_replies(monkeypatch, [
        {"acquired": False, "held_by": "a", "fencing_token": 4},
        {"acquired": False, "held_by": "a", "fencing_token": 6},
        {"acquired": False, "held_by": "a", "fencing_token": 6},
    ])
    link = keep.KeepLink(agent_id="a", machine_id="m", offline=False)
    link.acquire_lease()
    link.acquire_lease()
    assert [t for _s, t, _a in seen].count("relay_release") == 1


def test_reclaim_can_be_switched_off(monkeypatch):
    seen = _lease_replies(monkeypatch, [
        {"acquired": False, "held_by": "a", "fencing_token": 4},
    ])
    link = keep.KeepLink(agent_id="a", machine_id="m", offline=False)
    assert link.acquire_lease(reclaim_own=False)["acquired"] is False
    assert [t for _s, t, _a in seen] == ["relay_lease"]


def test_transport_errors_are_swallowed(monkeypatch):
    def boom(*a, **k): raise transport.TransportError("down")
    monkeypatch.setattr(keep, "call_tool", boom)
    link = keep.KeepLink(agent_id="a", machine_id="m", offline=False)
    assert link.action_before(goal="g", task_id="t", apps=[]) is None
    assert link.permit_task_state("c") is None


def test_arbitrary_exceptions_are_also_swallowed(monkeypatch):
    def boom(*a, **k): raise ValueError("weird")
    monkeypatch.setattr(keep, "call_tool", boom)
    link = keep.KeepLink(agent_id="a", machine_id="m", offline=False)
    assert link.action_before(goal="g", task_id="t", apps=[]) is None
    assert link.acquire_lease() is None
    assert link.post_permit_task(challenge="c", title="t", classes=(), task_id="t", step_index=0, expires_at="x") is None
    assert link.permit_task_state("c") is None
    link.release_lease()
    link.renew_lease()
    link.action_after("A1", "done", "ok")
    link.close_permit_task("task-1", "approve")


def test_permit_task_state_maps_every_relay_status(monkeypatch):
    def make_fake(tasks):
        def fake(service, tool, args, **kw):
            if (service, tool) == ("relay", "relay_task_list"):
                return {"tasks": tasks}
            return {}
        return fake

    link = keep.KeepLink(agent_id="a", machine_id="m", offline=False)

    monkeypatch.setattr(keep, "call_tool", make_fake([{"id": "1", "status": "completed", "result": "approve: go ahead"}]))
    assert link.permit_task_state("c") == "approve"

    monkeypatch.setattr(keep, "call_tool", make_fake([{"id": "1", "status": "completed", "result": "no"}]))
    assert link.permit_task_state("c") == "deny"

    monkeypatch.setattr(keep, "call_tool", make_fake([{"id": "1", "status": "cancelled", "result": None}]))
    assert link.permit_task_state("c") == "deny"

    monkeypatch.setattr(keep, "call_tool", make_fake([{"id": "1", "status": "failed", "result": None}]))
    assert link.permit_task_state("c") == "deny"

    monkeypatch.setattr(keep, "call_tool", make_fake([{"id": "1", "status": "pending", "result": None}]))
    assert link.permit_task_state("c") == "pending"

    monkeypatch.setattr(keep, "call_tool", make_fake([{"id": "1", "status": "in-progress", "result": None}]))
    assert link.permit_task_state("c") == "pending"

    monkeypatch.setattr(keep, "call_tool", make_fake([]))
    assert link.permit_task_state("c") is None


def test_close_permit_task_calls_relay_task_update(monkeypatch):
    seen = []
    def fake(service, tool, args, **kw):
        seen.append((service, tool, args))
        return {}
    monkeypatch.setattr(keep, "call_tool", fake)
    link = keep.KeepLink(agent_id="a", machine_id="m", offline=False)
    link.close_permit_task("task-1", "approve")
    assert seen == [("relay", "relay_task_update", {"task_id": "task-1", "status": "cancelled", "result": "approve"})]


def test_action_before_sends_cortexs_real_argument_shape(monkeypatch):
    """Pins the exact arg dict against cortex/app/mcp_server.py:1542's real
    `action_before(session_id, agent_id, action_type, target, preview="",
    intent="", expected_changes=None, success_criteria: list[str]|None=None,
    confidence=None)` — a future drift back to the simplified shorthand
    (no session_id/agent_id, success_criteria as a string) should fail here,
    not silently no-op against the real server."""
    seen = []
    def fake(service, tool, args, **kw):
        seen.append((service, tool, args))
        return {"action_id": "A1"}
    monkeypatch.setattr(keep, "call_tool", fake)
    link = keep.KeepLink(agent_id="a", machine_id="m", offline=False, session_id="s1")
    assert link.action_before(goal="g", task_id="t", apps=["Notepad", "Mail"]) == "A1"
    service, tool, args = seen[0]
    assert (service, tool) == ("cortex", "action_before")
    assert args == {
        "session_id": "s1",
        "agent_id": "a",
        "action_type": "hands_task",
        "target": "desktop:m",
        "preview": "apps: Notepad, Mail",
        "intent": "g",
        "success_criteria": ["task ends with outcome=done"],
        "confidence": 0.6,
    }
    assert isinstance(args["success_criteria"], list)
    assert isinstance(args["session_id"], str) and isinstance(args["agent_id"], str)
    assert isinstance(args["confidence"], float)


def test_action_before_falls_back_to_task_id_when_no_session_id(monkeypatch):
    seen = []
    def fake(service, tool, args, **kw):
        seen.append(args)
        return {"action_id": "A1"}
    monkeypatch.setattr(keep, "call_tool", fake)
    link = keep.KeepLink(agent_id="a", machine_id="m", offline=False)  # no session_id given
    link.action_before(goal="g", task_id="t1", apps=[])
    assert seen[0]["session_id"] == "t1"
    assert seen[0]["preview"] == ""


def test_action_after_sends_cortexs_real_argument_shape(monkeypatch):
    """Pins the exact arg dict against cortex/app/mcp_server.py:1599's real
    `action_after(action_id, success: bool, actual_changes=None,
    observed_criteria_met=None, deviation_notes="", exit_status=None)` — it
    has no `outcome`/`summary` fields at all."""
    seen = []
    def fake(service, tool, args, **kw):
        seen.append((service, tool, args))
        return {}
    monkeypatch.setattr(keep, "call_tool", fake)
    link = keep.KeepLink(agent_id="a", machine_id="m", offline=False)

    link.action_after("A1", "done", "saved the file")
    assert seen == [("cortex", "action_after", {
        "action_id": "A1",
        "success": True,
        "deviation_notes": "done: saved the file",
    })]
    assert isinstance(seen[0][2]["success"], bool)

    seen.clear()
    link.action_after("A1", "failed", "could not click Send")
    assert seen[0][2]["success"] is False
    assert seen[0][2]["deviation_notes"] == "failed: could not click Send"


def test_action_after_truncates_deviation_notes_to_500_chars(monkeypatch):
    seen = []
    def fake(service, tool, args, **kw):
        seen.append(args)
        return {}
    monkeypatch.setattr(keep, "call_tool", fake)
    link = keep.KeepLink(agent_id="a", machine_id="m", offline=False)
    link.action_after("A1", "done", "x" * 600)
    assert len(seen[0]["deviation_notes"]) == 500


def test_acquire_lease_lost_race_does_not_hold_and_release_sends_nothing(monkeypatch):
    seen = []
    def fake(service, tool, args, **kw):
        seen.append((service, tool, args))
        if (service, tool) == ("relay", "relay_lease"):
            return {"acquired": False, "held_by": "x"}
        return {}
    monkeypatch.setattr(keep, "call_tool", fake)
    link = keep.KeepLink(agent_id="a", machine_id="m", offline=False)

    result = link.acquire_lease()
    assert result == {"acquired": False, "held_by": "x"}

    seen.clear()
    link.release_lease()
    assert seen == []  # lost the race: never send relay_release at all


def test_renew_lease_calls_relay_heartbeat_not_relay_lease(monkeypatch):
    """relay_heartbeat is relay's actual TTL-extension primitive
    (relay/app/mcp_server.py:434) — re-calling relay_lease while already
    holding it only reports the holder, it does not extend the TTL."""
    seen = []
    def fake(service, tool, args, **kw):
        seen.append((service, tool, args))
        if (service, tool) == ("relay", "relay_lease"):
            return {"fencing_token": 9}
        return {}
    monkeypatch.setattr(keep, "call_tool", fake)
    link = keep.KeepLink(agent_id="a", machine_id="m", offline=False)
    link.acquire_lease()

    seen.clear()
    link.renew_lease()
    assert seen == [("relay", "relay_heartbeat", {"resource_id": "hands:m", "fencing_token": 9, "agent_id": "a"})]


def test_renew_lease_is_a_noop_without_a_held_lease(monkeypatch):
    seen = []
    def fake(service, tool, args, **kw):
        seen.append((service, tool, args))
        return {}
    monkeypatch.setattr(keep, "call_tool", fake)
    link = keep.KeepLink(agent_id="a", machine_id="m", offline=False)  # never acquired a lease
    link.renew_lease()
    assert seen == []


def test_action_before_and_post_permit_task_never_raise_on_non_str_apps_or_classes(monkeypatch):
    """apps/classes elements that aren't strings must not raise TypeError out
    of ', '.join(...) — that construction has to happen inside the same
    guarded region as the call itself, not in the caller's own frame before
    _call is ever reached."""
    def fake(service, tool, args, **kw):
        return {"action_id": "A1", "task": {"id": "task-1"}}
    monkeypatch.setattr(keep, "call_tool", fake)
    link = keep.KeepLink(agent_id="a", machine_id="m", offline=False)
    assert link.action_before(goal="g", task_id="t", apps=[1, None]) == "A1"
    assert link.post_permit_task(
        challenge="c", title="t", classes=(1,), task_id="t", step_index=0, expires_at="x"
    ) == "task-1"
