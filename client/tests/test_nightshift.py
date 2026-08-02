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
    # Match the model id the get_json fakes report from /models, so the suite's world
    # is self-consistent now that a configured-but-absent model aborts the run. Also
    # insulates these tests from the real default model name changing.
    monkeypatch.setenv("FIREKEEP_NIGHTSHIFT_LLM_MODEL", "m")


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
    # This test owns ONE behaviour: abort before anything is leased or updated. The
    # message's content is asserted by test_unreachable_error_names_both_supported_
    # backends — it used to be checked here as startswith("LM Studio"), which broke
    # when the abort stopped naming a single hardcoded backend.
    assert out["error"]
    assert not rec.named("relay_lease")
    assert not rec.named("relay_task_update")


def test_autodetects_ollama_when_lm_studio_is_down(cfg_env, monkeypatch):
    """LM Studio and Ollama are both OpenAI-compatible, so supporting both is a
    DETECTION problem, not a protocol one — nothing in the request path changes. With
    no base configured, a down LM Studio must fall through to Ollama's :11434 rather
    than aborting a shift the machine could actually run."""
    monkeypatch.delenv("FIREKEEP_NIGHTSHIFT_LLM_BASE", raising=False)
    rec = _Recorder({"relay_task_list": {"tasks": [_task()], "count": 1}})

    def get_json(url, **kw):
        if ":1234" in url:
            raise nightshift.transport.TransportError("connection refused")
        return {"data": []}

    inner, posted = _llm_ok(_SYNTH), []

    def post_json(url, *a, **kw):
        posted.append(url)
        return inner(url, *a, **kw)

    nightshift.run(call_tool=rec, post_json=post_json, get_json=get_json)
    assert posted and all(":11434" in u for u in posted)


def test_unreachable_error_names_both_supported_backends(cfg_env, monkeypatch):
    """The abort message hardcoded LM Studio, so an Ollama user who mistyped a port was
    told to run `lms server start` — advice for software they do not have."""
    monkeypatch.delenv("FIREKEEP_NIGHTSHIFT_LLM_BASE", raising=False)
    rec = _Recorder({"relay_task_list": {"tasks": [_task()], "count": 1}})

    def down(url, **kw):
        raise nightshift.transport.TransportError("connection refused")

    out = nightshift.run(call_tool=rec, post_json=_llm_ok(_SYNTH), get_json=down)
    assert "LM Studio" in out["error"] and "Ollama" in out["error"]
    assert "1234" in out["error"] and "11434" in out["error"]


def test_missing_model_fails_early_with_the_available_list(cfg_env, monkeypatch):
    """Detecting the BACKEND is not the same as having the MODEL. The default model is
    an LM Studio identifier, so auto-detecting Ollama and then firing a chat call fails
    deep in the run with a bare 404. Fail before leasing, and say what IS loaded — the
    operator cannot guess. Deliberately lenient: an empty or unparseable model list is
    'cannot tell', which proceeds rather than blocking a runnable shift."""
    monkeypatch.delenv("FIREKEEP_NIGHTSHIFT_LLM_BASE", raising=False)
    monkeypatch.setenv("FIREKEEP_NIGHTSHIFT_LLM_MODEL", "not-installed")
    rec = _Recorder({"relay_task_list": {"tasks": [_task()], "count": 1}})
    models = {"data": [{"id": "qwen3:30b"}, {"id": "llama3:latest"}]}
    out = nightshift.run(call_tool=rec, post_json=_llm_ok(_SYNTH),
                         get_json=lambda *a, **k: models)
    assert "not-installed" in out["error"]
    assert "qwen3:30b" in out["error"]
    assert not rec.named("relay_lease")


def test_empty_model_list_does_not_block_the_shift(cfg_env, monkeypatch):
    """The lenient half of the rule above: a backend that reports no models (or a shape
    we cannot read) must not veto a shift that would otherwise run."""
    monkeypatch.delenv("FIREKEEP_NIGHTSHIFT_LLM_BASE", raising=False)
    rec = _Recorder({"relay_task_list": {"tasks": [_task()], "count": 1}})
    out = nightshift.run(call_tool=rec, post_json=_llm_ok(_SYNTH),
                         get_json=lambda *a, **k: {"data": []})
    assert not out.get("error")


def test_explicit_base_is_probed_alone_and_never_falls_through(cfg_env, monkeypatch):
    """The behaviour 95300b2 documented most emphatically had NO coverage: an explicit
    FIREKEEP_NIGHTSHIFT_LLM_BASE must be probed alone, so a typo fails loudly instead of
    silently landing on a different engine than the operator named. Deleting that branch
    left the whole file green, because every autodetect test runs with no base
    configured and so never exercises it. Verified to catch that mutation."""
    monkeypatch.setenv("FIREKEEP_NIGHTSHIFT_LLM_BASE", "http://127.0.0.1:9999/v1")
    rec = _Recorder({"relay_task_list": {"tasks": [_task()], "count": 1}})

    def get_json(url, **kw):
        if ":9999" in url:
            raise nightshift.transport.TransportError("connection refused")
        return {"data": [{"id": "m"}]}      # a healthy backend on a default port

    out = nightshift.run(call_tool=rec, post_json=_llm_ok(_SYNTH), get_json=get_json)
    assert out["error"], "a typo'd explicit base must abort, not silently use another engine"
    assert not rec.named("relay_lease")


def test_untagged_model_matches_ollamas_tagged_id(cfg_env, monkeypatch):
    """Ollama's /v1/models reports fully-tagged ids ("llama3:latest") while its chat API
    accepts and resolves the BARE name ("llama3"). Exact-equality matching therefore
    false-vetoes a model that works perfectly — and the veto is silent in the worst way:
    the shift aborts before leasing, so distillation simply never happens and the queue
    grows. A guard against a bare 404 must not itself become the thing that stops the
    feature."""
    monkeypatch.delenv("FIREKEEP_NIGHTSHIFT_LLM_BASE", raising=False)
    monkeypatch.setenv("FIREKEEP_NIGHTSHIFT_LLM_MODEL", "llama3")
    rec = _Recorder({"relay_task_list": {"tasks": [_task()], "count": 1}})
    models = {"data": [{"id": "llama3:latest"}, {"id": "qwen3:30b"}]}
    out = nightshift.run(call_tool=rec, post_json=_llm_ok(_SYNTH),
                         get_json=lambda *a, **k: models)
    assert not out.get("error")
    assert rec.named("relay_lease")


def test_size_tagged_cloud_model_is_also_refused(cfg_env, monkeypatch):
    """Ollama's cloud tags are commonly `<size>-cloud` — `gpt-oss:120b-cloud` is the
    spelling its own cloud documentation leads with. A guard matching only a literal
    ":cloud" suffix lets the documented form straight through, which is precisely the
    egress it exists to prevent."""
    monkeypatch.setenv("FIREKEEP_NIGHTSHIFT_LLM_MODEL", "gpt-oss:120b-cloud")
    rec = _Recorder({"relay_task_list": {"tasks": [_task()], "count": 1}})
    out = nightshift.run(call_tool=rec, post_json=_llm_ok(_SYNTH),
                         get_json=lambda *a, **k: {"data": []})
    assert "cloud" in out["error"].lower()
    assert "FIREKEEP_NIGHTSHIFT_ALLOW_REMOTE" in out["error"]
    assert not rec.named("relay_lease")


def test_cloud_model_is_refused_before_touching_anything(cfg_env, monkeypatch):
    """Night Shift's whole premise is that session content never leaves the machine.
    An Ollama `:cloud` model silently routes it to a third party, inverting that — so
    it is refused by default with a named opt-out (the SSL_CERT_FILE /
    FIREKEEP_KEEP_SSL_CERT_FILE precedent)."""
    monkeypatch.setenv("FIREKEEP_NIGHTSHIFT_LLM_MODEL", "minimax-m2:cloud")
    rec = _Recorder({"relay_task_list": {"tasks": [_task()], "count": 1}})
    out = nightshift.run(call_tool=rec, post_json=_llm_ok(_SYNTH),
                         get_json=lambda *a, **k: {"data": []})
    assert "cloud" in out["error"].lower()
    assert "FIREKEEP_NIGHTSHIFT_ALLOW_REMOTE" in out["error"]
    assert not rec.named("relay_lease")


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
