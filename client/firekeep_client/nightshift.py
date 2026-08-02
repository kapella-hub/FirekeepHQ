"""Night Shift — the Fleet-as-GPU distill worker (client-side, stdlib-only).

Since SP1b the `stop` hook has enqueued a `distill_session` Relay task at every
session end — structural capture with, until now, nothing draining the queue
(the deliberately-built Fleet-as-GPU seam). Night Shift is the drain, run where
the free compute lives: the developer's own machine, against a LOCAL model served
by LM Studio (`:1234`) or Ollama (`:11434`). Both speak the OpenAI-compatible API,
so the request path is identical and choosing between them is pure detection: with
no `FIREKEEP_NIGHTSHIFT_LLM_BASE` set, they are probed in that order and the first
to answer wins.

Per task it:
  1. leases `distill.<task_id>` (fencing token — two workers can't double-drain);
  2. reconstructs the session's evidence: Cortex replay summary + auto-evals
     (best-effort) plus the workspace snapshot the stop hook stored in the task;
  3. asks the local model for a STRICT-JSON distillation: one consolidated
     memory, and optionally one skill;
  4. writes them through the EXISTING review surfaces — `memory_learn` and
     `skill_create(status="draft")` (drafts are invisible to recall until a
     human approves them in the dashboard) — attributed to the ORIGINAL
     session's agent and session_id, never to the worker;
  5. completes the task and releases the lease.

Posture: personal/bypass mode is a hard no-op (nothing is read or sent); a
`:cloud` model is refused (session content must not leave the machine — override
with FIREKEEP_NIGHTSHIFT_ALLOW_REMOTE=1) and no reachable backend aborts, both
BEFORE any task is touched; a malformed model
response is retried once and then the task is marked failed (visible in the
dashboard, no infinite retry); tasks predating the stop hook's session_id stamp
are completed with a note (backlog clears, nothing is invented). Import
boundary: stdlib + firekeep_client stdlib modules only (`hooks._mcp` for MCP,
`transport` for REST/LLM) — never `mcp`/`httpx`.
"""
from __future__ import annotations

import json
import os
import platform
from typing import Any, Callable

from firekeep_client import hooklog, resolver, transport
from firekeep_client.hooks import _mcp

# Supported local backends, probed in this order when no base is configured. Both
# speak the OpenAI-compatible API, so supporting both is DETECTION only — nothing
# in the request path differs.
_LLM_BACKENDS = (
    ("LM Studio", "http://127.0.0.1:1234/v1"),
    ("Ollama", "http://127.0.0.1:11434/v1"),
)
_DEFAULT_LLM_BASE = _LLM_BACKENDS[0][1]
_DEFAULT_LLM_MODEL = "qwen/qwen3.6-35b-a3b"
_DEFAULT_MAX_TASKS = 5
_TASK_TITLE = "distill_session"
_LLM_TIMEOUT = 300.0  # local 35B on a laptop: generous, not infinite

_SYSTEM_PROMPT = (
    "You distill an AI agent's completed work session into durable team "
    "knowledge. Reply with STRICT JSON only — no prose, no code fences — "
    "matching exactly:\n"
    '{"memory": {"action": "<what was done, 1-2 sentences>", '
    '"outcome": "<what resulted, incl. non-obvious learnings>", '
    '"resolution": "<how, if applicable>" | null, '
    '"tags": ["<lowercase-tag>", ...]}, '
    '"skill": {"trigger": "<one sentence: when this applies>", '
    '"symptoms": "<observable signals>", "steps": "<the procedure>", '
    '"gotchas": "<pitfalls>", "domain": "<one word>"} | null}\n'
    "Emit a skill ONLY for a hard-won fix, non-obvious root cause, or "
    "reusable technique — routine work gets \"skill\": null. Be concrete and "
    "specific; never invent details absent from the evidence."
)


def _llm_base() -> str:
    return (os.environ.get("FIREKEEP_NIGHTSHIFT_LLM_BASE") or _DEFAULT_LLM_BASE).rstrip("/")


def _llm_model() -> str:
    return os.environ.get("FIREKEEP_NIGHTSHIFT_LLM_MODEL") or _DEFAULT_LLM_MODEL


def _detect_llm_base(get_json: Callable[..., Any]) -> tuple[str, list[str]] | None:
    """Resolve the backend to use as `(base, model_ids)`, or None if none answered.

    An EXPLICIT base is probed and nothing else: a typo must fail loudly rather than
    silently fall through to a different engine than the operator named. With no
    config, probe the supported backends in order and take the first that answers.

    The model list comes back from the same probe rather than a second round trip.
    An unreadable shape degrades to `[]`, which callers must treat as "cannot tell"
    — never as "no models".
    """
    if os.environ.get("FIREKEEP_NIGHTSHIFT_LLM_BASE"):
        bases = [_llm_base()]
    else:
        bases = [b for _name, b in _LLM_BACKENDS]
    for base in bases:
        try:
            resp = get_json(f"{base}/models", headers={}, timeout=10.0)
        except Exception:  # noqa: BLE001 — any failure just means "not this one"
            continue
        try:
            models = [str(m["id"]) for m in (resp or {}).get("data") or [] if m.get("id")]
        except Exception:  # noqa: BLE001
            models = []
        return base, models
    return None


def _remote_model_refusal() -> str | None:
    """Night Shift's premise is that session content never leaves the machine. Ollama
    exposes `<name>:cloud` models that transparently route to a third party, which
    silently inverts that. Refused by default, overridable — the same shape as the
    bootstrap's SSL_CERT_FILE / FIREKEEP_KEEP_SSL_CERT_FILE opt-out.
    """
    model = _llm_model()
    if model.endswith(":cloud") and not os.environ.get("FIREKEEP_NIGHTSHIFT_ALLOW_REMOTE"):
        return (f"model '{model}' is a CLOUD model — Night Shift distills session "
                f"content, which would leave this machine. Pick a local model, or set "
                f"FIREKEEP_NIGHTSHIFT_ALLOW_REMOTE=1 if that is genuinely intended")
    return None


def _worker_id() -> str:
    return os.environ.get("FIREKEEP_NIGHTSHIFT_AGENT_ID") or "night-shift"


def _session_id_of(task: dict) -> str:
    """The stop hook stamps `session_id=<sid>` into the task description
    (0.1.23). Tasks from older clients have no stamp — they are 'legacy'."""
    desc = task.get("description") or ""
    for part in desc.replace(",", " ").split():
        if part.startswith("session_id="):
            return part.split("=", 1)[1].strip()
    return ""


def _extract_json(text: str) -> dict:
    """Parse the model's reply as JSON, tolerating fences/preamble by slicing
    from the first '{' to the last '}'. Raises ValueError when hopeless."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in model reply")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict) or not isinstance(data.get("memory"), dict):
        raise ValueError("model reply missing the memory object")
    mem = data["memory"]
    if not str(mem.get("action") or "").strip() or not str(mem.get("outcome") or "").strip():
        raise ValueError("memory.action/outcome empty — junk must not become a live memory")
    return data


def _evidence(sid: str, task: dict, get_json: Callable[..., Any]) -> str:
    """Assemble what the model sees. Replay/eval fetches are best-effort — a
    server hiccup degrades the evidence, never the run."""
    parts = [f"## Workspace snapshot at session end\n{task.get('context') or '(none)'}"]
    try:
        ep = resolver.resolve("cortex")
        summary = get_json(f"{ep.rest_base}/replay/sessions/{sid}/summary",
                           headers=ep.headers, verify=ep.verify)
        parts.append(f"## Replay summary\n{json.dumps(summary)[:4000]}")
    except Exception as e:  # noqa: BLE001
        hooklog.log_failure("nightshift", f"replay summary fetch failed: {e}")
    try:
        ep = resolver.resolve("cortex")
        evals = get_json(f"{ep.rest_base}/evals/sessions/{sid}",
                         headers=ep.headers, verify=ep.verify)
        parts.append(f"## Auto-eval scores\n{json.dumps(evals)[:2000]}")
    except Exception as e:  # noqa: BLE001
        hooklog.log_failure("nightshift", f"evals fetch failed: {e}")
    return "\n\n".join(parts)


def _synthesize(sid: str, assigner: str, evidence: str,
                post_json: Callable[..., Any], base: str) -> dict:
    """One LLM distillation with a single retry on malformed output."""
    body = {
        "model": _llm_model(),
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content":
                f"Session {sid} by agent {assigner}. Evidence:\n\n{evidence}"},
        ],
    }
    last_error: Exception | None = None
    for _attempt in (1, 2):
        resp = post_json(f"{base}/chat/completions", body,
                         headers={"Content-Type": "application/json"},
                         timeout=_LLM_TIMEOUT)
        try:
            text = resp["choices"][0]["message"]["content"]
            return _extract_json(text)
        except (KeyError, IndexError, TypeError, ValueError) as e:
            last_error = e
            hooklog.log_failure("nightshift", f"malformed LLM reply (attempt): {e!r}")
    raise ValueError(f"model never produced valid JSON: {last_error!r}")


def _relay_ok(resp: Any) -> bool:
    """Relay tools never raise — failures come back in-band as {"error": ...}
    dicts with HTTP 200 (wf_02954176 review). A write only counts if this holds."""
    return isinstance(resp, dict) and not resp.get("error")


def run(max_tasks: int = _DEFAULT_MAX_TASKS, dry_run: bool = False, *,
        call_tool: Callable[..., Any] = _mcp.call_tool,
        post_json: Callable[..., Any] = transport.post_json,
        get_json: Callable[..., Any] = transport.get_json) -> dict:
    """Drain up to `max_tasks` distill_session tasks. Returns a summary dict —
    {distilled, legacy, skipped, failed, duplicates, deferred} plus `error` on a
    run-level abort. Never raises — the CLI turns `error` into a nonzero exit.

    Counting is HONEST: distilled/legacy/duplicates increment only after the
    relay confirms the task update in-band (a completion that failed would mean
    re-distilling next run, so it is counted `failed`, not `distilled`).
    `deferred` = a TRANSIENT LLM failure; the task stays pending and the shift
    stops (the model is gone — failing every remaining task would be noise)."""
    out = {"distilled": 0, "legacy": 0, "skipped": 0, "failed": 0,
           "duplicates": 0, "deferred": 0}
    if max_tasks <= 0:
        return out

    # Personal mode: a distill reads session data and writes memories — exactly
    # what bypass promises never happens. Checked before ANY call.
    if resolver.is_bypassed():
        out["error"] = ("personal mode is on — Night Shift reads sessions and "
                        "writes team memory, so it stays fully dormant")
        return out

    # Cloud-model refusal and backend reachability — both BEFORE leasing anything.
    refusal = _remote_model_refusal()
    if refusal:
        out["error"] = refusal
        return out

    detected = _detect_llm_base(get_json)
    if detected is None:
        configured = os.environ.get("FIREKEEP_NIGHTSHIFT_LLM_BASE")
        where = configured or ", ".join(f"{n} {b}" for n, b in _LLM_BACKENDS)
        out["error"] = (
            f"no local OpenAI-compatible LLM reachable at {where}; start LM Studio "
            f"(`lms server start`, port 1234) or Ollama (`ollama serve`, port 11434) "
            f"and load {_llm_model()}, or point FIREKEEP_NIGHTSHIFT_LLM_BASE at it")
        return out

    # Finding the backend is not the same as having the model — the default model
    # name is an LM Studio identifier, so autodetecting Ollama and firing a chat call
    # would fail deep in the run with a bare 404. Lenient by design: an empty list
    # means the backend told us nothing readable, not that it has no models.
    base, models = detected
    if models and _llm_model() not in models:
        out["error"] = (f"model '{_llm_model()}' is not loaded at {base}; available: "
                        f"{', '.join(sorted(models))} — set FIREKEEP_NIGHTSHIFT_LLM_MODEL")
        return out

    cfg = resolver.load_config()
    worker = _worker_id()

    if not dry_run:
        try:
            call_tool("relay", "relay_register",
                      {"agent_id": worker, "goal": "night shift: distilling sessions",
                       "hostname": platform.node() or "unknown"}, cfg=cfg)
        except Exception as e:  # noqa: BLE001
            hooklog.log_failure("nightshift", f"relay_register failed: {e}")

    seen_sessions: set[str] = set()
    stop_shift = False
    try:
        listing = call_tool("relay", "relay_task_list",
                            {"status": "pending", "limit": 50}, cfg=cfg)
        tasks = [t for t in (listing.get("tasks") or [])
                 if t.get("title") == _TASK_TITLE][:max_tasks]

        for task in tasks:
            if stop_shift:  # a transient LLM loss ended the shift mid-loop
                break
            # Relay tasks carry "id" (relay/app/tasks.py) — a task_id read here
            # once made every update silently target nothing (wf_02954176).
            task_id = task.get("id") or ""
            sid = _session_id_of(task)
            assigner = task.get("assigner") or "unknown"

            if not task_id:
                out["skipped"] += 1
                continue

            if dry_run:
                if not sid:
                    out["legacy"] += 1
                    continue
                try:
                    _synthesize(sid, assigner, _evidence(sid, task, get_json),
                                post_json, base)
                    out["distilled"] += 1
                except transport.TransportError:
                    out["deferred"] += 1
                    break
                except Exception as e:  # noqa: BLE001
                    hooklog.log_failure("nightshift", f"dry-run synth failed: {e}")
                    out["failed"] += 1
                continue

            if not sid:
                # Pre-0.1.23 task: no session to reconstruct. Clear it honestly.
                try:
                    resp = call_tool("relay", "relay_task_update",
                                     {"task_id": task_id, "status": "completed",
                                      "result": ("night-shift: legacy task with no "
                                                 "session_id — nothing to distill")},
                                     cfg=cfg)
                    out["legacy" if _relay_ok(resp) else "failed"] += 1
                except Exception as e:  # noqa: BLE001
                    hooklog.log_failure("nightshift", f"legacy close failed: {e}")
                    out["failed"] += 1
                continue

            if sid in seen_sessions:
                # stop fires per assistant turn — one session enqueues N tasks.
                try:
                    resp = call_tool("relay", "relay_task_update",
                                     {"task_id": task_id, "status": "completed",
                                      "result": ("night-shift: duplicate — session "
                                                 "already distilled this run")}, cfg=cfg)
                    out["duplicates" if _relay_ok(resp) else "failed"] += 1
                except Exception as e:  # noqa: BLE001
                    hooklog.log_failure("nightshift", f"duplicate close failed: {e}")
                    out["failed"] += 1
                continue

            token = 0
            try:
                lease = call_tool("relay", "relay_lease",
                                  {"resource_id": f"distill.{task_id}",
                                   "agent_id": worker}, cfg=cfg)
                if not (isinstance(lease, dict) and lease.get("acquired")):
                    out["skipped"] += 1
                    continue
                token = int(lease.get("fencing_token") or 0)

                try:
                    synth = _synthesize(sid, assigner,
                                        _evidence(sid, task, get_json), post_json,
                                        base)
                except transport.TransportError as e:
                    # TRANSIENT (server restarting, model unloaded): leave the
                    # task pending for the next shift and stop this one.
                    hooklog.log_failure("nightshift", f"LLM transient, deferring: {e}")
                    out["deferred"] += 1
                    stop_shift = True
                    continue

                mem = synth["memory"]
                learn = {
                    "action": str(mem.get("action") or "")[:2000],
                    "outcome": str(mem.get("outcome") or "")[:4000],
                    "tags": [str(x) for x in (mem.get("tags") or [])][:8] + ["night-shift"],
                    "agent_id": assigner,
                    "session_id": sid,
                }
                if mem.get("resolution"):
                    learn["resolution"] = str(mem["resolution"])[:2000]
                stored = call_tool("cortex", "memory_learn", learn, cfg=cfg)
                if not (isinstance(stored, str) and stored.startswith("Stored memory")):
                    # In-band cortex failure (error STRING, HTTP 200) — the
                    # distillation would be silently lost if we completed here.
                    raise RuntimeError(f"memory_learn did not store: {stored!r}"[:300])

                skill = synth.get("skill")
                made_skill = False
                if isinstance(skill, dict) and str(skill.get("trigger") or "").strip():
                    created = call_tool("cortex", "skill_create", {
                        "trigger": str(skill.get("trigger") or "")[:1000],
                        "symptoms": str(skill.get("symptoms") or "")[:2000],
                        "steps": str(skill.get("steps") or "")[:4000],
                        "gotchas": str(skill.get("gotchas") or "")[:2000],
                        "domain": str(skill.get("domain") or "")[:100],
                        "status": "draft",
                        "agent_id": assigner,
                        "session_id": sid,
                    }, cfg=cfg)
                    made_skill = isinstance(created, str) and created.startswith("Skill created")
                    if not made_skill:
                        hooklog.log_failure("nightshift",
                                            f"skill_create not confirmed: {created!r}"[:300])

                resp = call_tool("relay", "relay_task_update",
                                 {"task_id": task_id, "status": "completed",
                                  "result": ("night-shift: distilled to memory"
                                             + (" + draft skill" if made_skill else ""))},
                                 cfg=cfg)
                if _relay_ok(resp):
                    seen_sessions.add(sid)
                    out["distilled"] += 1
                else:
                    # Memory IS stored but the task stays pending — next run
                    # would duplicate it, so this is a failure, loudly.
                    hooklog.log_failure(
                        "nightshift",
                        f"completion not confirmed for {task_id}: {resp!r}"[:300])
                    out["failed"] += 1
            except Exception as e:  # noqa: BLE001 — one bad task never stops the shift
                hooklog.log_failure("nightshift", f"task {task_id} failed: {e}")
                out["failed"] += 1
                try:
                    call_tool("relay", "relay_task_update",
                              {"task_id": task_id, "status": "failed",
                               "result": f"night-shift: {e}"[:500]}, cfg=cfg)
                except Exception as e2:  # noqa: BLE001
                    hooklog.log_failure("nightshift", f"task_update(failed) failed: {e2}")
            finally:
                if token:
                    try:
                        call_tool("relay", "relay_release",
                                  {"resource_id": f"distill.{task_id}",
                                   "agent_id": worker, "fencing_token": token},
                                  cfg=cfg)
                    except Exception as e:  # noqa: BLE001
                        hooklog.log_failure("nightshift", f"lease release failed: {e}")
    except Exception as e:  # noqa: BLE001 — the never-raises contract is real
        hooklog.log_failure("nightshift", f"run aborted: {e}")
        out["error"] = f"relay unreachable or run aborted: {e}"
    finally:
        if not dry_run:
            try:
                call_tool("relay", "relay_deregister", {"agent_id": worker}, cfg=cfg)
            except Exception as e:  # noqa: BLE001
                hooklog.log_failure("nightshift", f"relay_deregister failed: {e}")

    return out
