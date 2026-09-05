from firekeep_client import transport
from firekeep_hands import keep


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
                "relay.relay_task_post": {"status": "created", "task": {"id": "task-1"}},
                "relay.relay_task_list": {"tasks": [{"id": "task-1", "status": "completed", "result": "approve"}]},
                }.get(f"{service}.{tool}", {})
    monkeypatch.setattr(keep, "call_tool", fake)
    link = keep.KeepLink(agent_id="a", machine_id="m", offline=False)
    assert link.action_before(goal="g", task_id="t", apps=["X"]) == "A1"
    assert link.acquire_lease()["fencing_token"] == 7
    assert link.post_permit_task(challenge="c", title="Send", classes=("send",), task_id="t", step_index=2, expires_at="x") == "task-1"
    assert link.permit_task_state("c") == "approve"
    link.release_lease(); link.action_after("A1", "done", "ok")
    tools = [(s, t) for s, t, _ in seen]
    assert tools == [("cortex", "action_before"), ("relay", "relay_lease"), ("relay", "relay_task_post"),
                     ("relay", "relay_task_list"), ("relay", "relay_release"), ("cortex", "action_after")]
    assert seen[1][2]["resource_id"] == "hands:m" and seen[4][2]["fencing_token"] == 7
    assert seen[2][2]["title"] == "hands_permit:c" and seen[3][2]["title"] == "hands_permit:c"


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
