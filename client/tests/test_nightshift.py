"""Night Shift — the Fleet-as-GPU distill worker (client-side, local-LLM).

The stop hook has enqueued `distill_session` Relay tasks since SP1b with nothing
draining them (the deliberate Fleet-as-GPU seam). Night Shift is the drain: it
leases each task, reconstructs the session's evidence (replay summary + evals +
workspace snapshot), asks the LOCAL model (LM Studio, OpenAI-compatible) to
distill it, and writes the results into the EXISTING review surfaces — a
consolidated memory (memory_learn) and an optional DRAFT skill (skill_create
status="draft", human-reviewed before recall sees it). All writes attribute to
the ORIGINAL session's agent, never to the worker.
"""
import json

import pytest

from firekeep_client import nightshift
from tests.conftest import DEFAULT_PERSONAL


@pytest.fixture
def cfg_env(write_config, monkeypatch):
    write_config(active="personal", personal=DEFAULT_PERSONAL)
    monkeypatch.delenv("FIREKEEP_NIGHTSHIFT_AGENT_ID", raising=False)
    monkeypatch.delenv("FIREKEEP_BYPASS", raising=False)


def _task(task_id="task-1", description="session_id=sess-42", assigner="mogan"):
    # Shape mirrors relay/app/tasks.py: the key is "id" — a "task_id" key here
    # masked the critical every-update-targeted-nothing bug (wf_02954176 review).
    return {"id": task_id, "title": "distill_session", "status": "pending",
            "description": description, "assigner": assigner,
            "context": "branch: main\n2 files changed"}


class _Recorder:
    """Scripted call_tool double: records every (service, tool, args) and returns
    canned responses keyed by tool name."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def __call__(self, service, tool, arguments, **kw):
        self.calls.append((service, tool, arguments))
        val = self.responses.get(tool)
        if callable(val):
            return val(arguments)
        if val is not None:
            return val
        # sensible defaults, mirroring the REAL tool result shapes
        if tool == "relay_task_list":
            return {"tasks": [], "count": 0}
        if tool == "relay_lease":
            return {"acquired": True, "fencing_token": 7}
        if tool == "memory_learn":
            return "Stored memory in domain 'general': ..."
        if tool == "skill_create":
            return "Skill created: abc123 — \"...\""
        if tool == "relay_task_update":
            return {"status": "updated"}
        return {}

    def named(self, tool):
        return [c for c in self.calls if c[1] == tool]


def _llm_ok(payload_obj):
    """An LM Studio chat-completions responder returning `payload_obj` as JSON.
    Signature mirrors transport.post_json (keyword-only headers) — the live run
    caught a probe call missing `headers` that permissive **kw doubles hid."""
    def post_json(url, body, *, headers, timeout=None, verify=True):
        return {"choices": [{"message": {"content": json.dumps(payload_obj)}}]}
    return post_json


_SYNTH = {
    "memory": {"action": "Fixed the flaky ingest test", "outcome": "suite green",
               "resolution": "pinned the clock", "tags": ["ci"]},
    "skill": {"trigger": "flaky time-dependent test", "symptoms": "intermittent CI red",
              "steps": "pin the clock with freezegun", "gotchas": "", "domain": "testing"},
}


def test_drain_happy_path_writes_memory_and_draft_skill(cfg_env):
    rec = _Recorder({"relay_task_list": {"tasks": [_task()], "count": 1}})
    out = nightshift.run(call_tool=rec, post_json=_llm_ok(_SYNTH),
                         get_json=lambda url, *, headers, timeout=None, verify=True: {"data": [{"id": "m"}]})

    assert out["distilled"] == 1
    # lease taken on the task, released with the fencing token
    lease = rec.named("relay_lease")[0][2]
    assert lease["resource_id"] == "distill.task-1"
    release = rec.named("relay_release")[0][2]
    assert release["fencing_token"] == 7
    # memory attributed to the ORIGINAL agent + session, tagged night-shift
    mem = rec.named("memory_learn")[0][2]
    assert mem["agent_id"] == "mogan"
    assert mem["session_id"] == "sess-42"
    assert "night-shift" in mem["tags"]
    # skill lands as a DRAFT (review queue), same attribution
    skill = rec.named("skill_create")[0][2]
    assert skill["status"] == "draft"
    assert skill["agent_id"] == "mogan"
    # task closed out
    upd = rec.named("relay_task_update")[0][2]
    assert upd["task_id"] == "task-1"
    assert upd["status"] == "completed"
    # worker presence registered and deregistered under its own identity
    assert rec.named("relay_register")[0][2]["agent_id"] == "night-shift"
    assert rec.named("relay_deregister")


def test_skill_null_means_memory_only(cfg_env):
    synth = {"memory": _SYNTH["memory"], "skill": None}
    rec = _Recorder({"relay_task_list": {"tasks": [_task()], "count": 1}})
    out = nightshift.run(call_tool=rec, post_json=_llm_ok(synth),
                         get_json=lambda url, *, headers, timeout=None, verify=True: {"data": []})
    assert out["distilled"] == 1
    assert rec.named("memory_learn")
    assert not rec.named("skill_create")


def test_legacy_task_without_session_id_completed_with_note_no_llm(cfg_env):
    rec = _Recorder({"relay_task_list": {"tasks": [_task(description="")], "count": 1}})

    def llm_must_not_run(url, body, **kw):
        raise AssertionError("no session_id -> nothing to distill -> no LLM call")

    out = nightshift.run(call_tool=rec, post_json=llm_must_not_run,
                         get_json=lambda url, *, headers, timeout=None, verify=True: {"data": []})
    assert out["legacy"] == 1
    upd = rec.named("relay_task_update")[0][2]
    assert upd["status"] == "completed"
    assert "no session_id" in upd["result"]
    assert not rec.named("memory_learn")


def test_lease_held_by_another_worker_skips_task(cfg_env):
    rec = _Recorder({
        "relay_task_list": {"tasks": [_task()], "count": 1},
        "relay_lease": {"acquired": False, "held": True, "holder_id": "other-shift"},
    })
    out = nightshift.run(call_tool=rec, post_json=_llm_ok(_SYNTH),
                         get_json=lambda url, *, headers, timeout=None, verify=True: {"data": []})
    assert out["skipped"] == 1
    assert not rec.named("relay_task_update")
    assert not rec.named("memory_learn")


def test_llm_unreachable_aborts_cleanly_before_touching_tasks(cfg_env):
    rec = _Recorder({"relay_task_list": {"tasks": [_task()], "count": 1}})

    def down(url, **kw):
        raise nightshift.transport.TransportError("connection refused")

    out = nightshift.run(call_tool=rec, post_json=_llm_ok(_SYNTH), get_json=down)
    assert out["error"].startswith("LM Studio")
    assert not rec.named("relay_lease")
    assert not rec.named("relay_task_update")


def test_malformed_llm_json_twice_marks_task_failed(cfg_env):
    rec = _Recorder({"relay_task_list": {"tasks": [_task()], "count": 1}})
    attempts = []

    def bad_json(url, body, **kw):
        attempts.append(1)
        return {"choices": [{"message": {"content": "not json {{"}}]}

    out = nightshift.run(call_tool=rec, post_json=bad_json,
                         get_json=lambda url, *, headers, timeout=None, verify=True: {"data": []})
    assert len(attempts) == 2  # one retry, then give up
    assert out["failed"] == 1
    upd = rec.named("relay_task_update")[0][2]
    assert upd["status"] == "failed"
    assert not rec.named("memory_learn")


def test_dry_run_reports_but_writes_nothing(cfg_env):
    rec = _Recorder({"relay_task_list": {"tasks": [_task()], "count": 1}})
    out = nightshift.run(dry_run=True, call_tool=rec, post_json=_llm_ok(_SYNTH),
                         get_json=lambda url, *, headers, timeout=None, verify=True: {"data": []})
    assert out["distilled"] == 1
    assert not rec.named("memory_learn")
    assert not rec.named("skill_create")
    assert not rec.named("relay_task_update")
    assert not rec.named("relay_lease")  # dry-run must not even take leases


def test_bypass_mode_is_a_noop(cfg_env, monkeypatch):
    monkeypatch.setenv("FIREKEEP_BYPASS", "1")
    rec = _Recorder()
    out = nightshift.run(call_tool=rec, post_json=_llm_ok(_SYNTH),
                         get_json=lambda url, *, headers, timeout=None, verify=True: {"data": []})
    assert "personal mode" in out["error"]
    assert rec.calls == []


def test_non_distill_pending_tasks_are_ignored(cfg_env):
    other = {"task_id": "task-9", "title": "review PR", "status": "pending",
             "description": "", "assigner": "mogan", "context": ""}
    rec = _Recorder({"relay_task_list": {"tasks": [other, _task()], "count": 2}})
    out = nightshift.run(call_tool=rec, post_json=_llm_ok(_SYNTH),
                         get_json=lambda url, *, headers, timeout=None, verify=True: {"data": []})
    assert out["distilled"] == 1
    touched = {c[2]["task_id"] for c in rec.named("relay_task_update")}
    assert touched == {"task-1"}


def test_max_tasks_caps_the_run(cfg_env):
    tasks = [_task(task_id=f"task-{i}", description=f"session_id=s{i}") for i in range(5)]
    rec = _Recorder({"relay_task_list": {"tasks": tasks, "count": 5}})
    out = nightshift.run(max_tasks=2, call_tool=rec, post_json=_llm_ok(_SYNTH),
                         get_json=lambda url, *, headers, timeout=None, verify=True: {"data": []})
    assert out["distilled"] == 2
    assert len(rec.named("relay_task_update")) == 2


# --------------------------------------------------------------------------- #
# Adversarial-review fixes (wf_02954176): the mock-invisible failure classes  #
# --------------------------------------------------------------------------- #


def _get_ok(url, *, headers, timeout=None, verify=True):
    return {"data": []}


def test_run_never_raises_when_relay_listing_fails(cfg_env):
    def rec(service, tool, arguments, **kw):
        if tool == "relay_task_list":
            raise nightshift.transport.TransportError("relay down")
        return {}

    out = nightshift.run(call_tool=rec, post_json=_llm_ok(_SYNTH), get_json=_get_ok)
    assert "error" in out  # clean error, not a traceback


def test_failed_memory_learn_marks_task_failed_not_completed(cfg_env):
    rec = _Recorder({
        "relay_task_list": {"tasks": [_task()], "count": 1},
        "memory_learn": "Error: Cannot reach Cortex at http://x — connection refused",
    })
    out = nightshift.run(call_tool=rec, post_json=_llm_ok(_SYNTH), get_json=_get_ok)
    assert out["failed"] == 1 and out["distilled"] == 0
    upd = rec.named("relay_task_update")[0][2]
    assert upd["status"] == "failed"
    assert not rec.named("skill_create")  # no skill without its memory


def test_inband_completion_error_is_not_counted_distilled(cfg_env):
    rec = _Recorder({
        "relay_task_list": {"tasks": [_task()], "count": 1},
        "relay_task_update": {"error": "Task task-1 not found", "status": "unavailable"},
    })
    out = nightshift.run(call_tool=rec, post_json=_llm_ok(_SYNTH), get_json=_get_ok)
    assert out["distilled"] == 0
    assert out["failed"] == 1


def test_same_session_duplicate_tasks_distill_once(cfg_env):
    """stop fires per assistant turn -> one session can enqueue N tasks. Only the
    first is distilled; the rest are completed as duplicates without an LLM call."""
    tasks = [_task(task_id=f"task-{i}", description="session_id=same-sess")
             for i in range(3)]
    rec = _Recorder({"relay_task_list": {"tasks": tasks, "count": 3}})
    llm_calls = []

    def llm(url, body, *, headers, timeout=None, verify=True):
        llm_calls.append(1)
        return {"choices": [{"message": {"content": json.dumps(_SYNTH)}}]}

    out = nightshift.run(call_tool=rec, post_json=llm, get_json=_get_ok)
    assert len(llm_calls) == 1
    assert out["distilled"] == 1 and out["duplicates"] == 2
    assert len(rec.named("memory_learn")) == 1
    statuses = [c[2]["status"] for c in rec.named("relay_task_update")]
    assert statuses.count("completed") == 3  # 1 distilled + 2 duplicate closures


def test_transient_llm_failure_defers_task_and_stops_shift(cfg_env):
    """A TransportError from the LLM is TRANSIENT (server restarting, model
    unloading) — the task must stay pending for the next run, never be marked
    failed, and the shift stops rather than failing every remaining task."""
    tasks = [_task(task_id=f"task-{i}", description=f"session_id=s{i}") for i in range(3)]
    rec = _Recorder({"relay_task_list": {"tasks": tasks, "count": 3}})

    def llm_dies(url, body, *, headers, timeout=None, verify=True):
        raise nightshift.transport.TransportError("LM Studio restarted")

    out = nightshift.run(call_tool=rec, post_json=llm_dies, get_json=_get_ok)
    assert out["deferred"] == 1
    assert out["failed"] == 0
    assert not rec.named("relay_task_update")  # nothing marked, nothing lost


def test_task_without_id_is_skipped_defensively(cfg_env):
    bad = {"title": "distill_session", "status": "pending",
           "description": "session_id=s1", "assigner": "mogan", "context": ""}
    rec = _Recorder({"relay_task_list": {"tasks": [bad], "count": 1}})
    out = nightshift.run(call_tool=rec, post_json=_llm_ok(_SYNTH), get_json=_get_ok)
    assert out["skipped"] == 1
    assert not rec.named("relay_lease")


def test_nonpositive_max_processes_nothing(cfg_env):
    rec = _Recorder({"relay_task_list": {"tasks": [_task()], "count": 1}})
    out = nightshift.run(max_tasks=-3, call_tool=rec, post_json=_llm_ok(_SYNTH),
                         get_json=_get_ok)
    assert out == {"distilled": 0, "legacy": 0, "skipped": 0, "failed": 0,
                   "duplicates": 0, "deferred": 0}


def test_dry_run_touches_no_relay_at_all_except_listing(cfg_env):
    rec = _Recorder({"relay_task_list": {"tasks": [_task()], "count": 1}})
    nightshift.run(dry_run=True, call_tool=rec, post_json=_llm_ok(_SYNTH),
                   get_json=_get_ok)
    tools = {c[1] for c in rec.calls}
    assert tools == {"relay_task_list"}  # no presence, no leases, no updates


def test_empty_llm_memory_fields_are_treated_as_malformed(cfg_env):
    junk = {"memory": {"action": "", "outcome": "", "tags": []}, "skill": None}
    rec = _Recorder({"relay_task_list": {"tasks": [_task()], "count": 1}})
    out = nightshift.run(call_tool=rec, post_json=_llm_ok(junk), get_json=_get_ok)
    assert out["failed"] == 1
    assert not rec.named("memory_learn")  # junk never becomes a live memory
