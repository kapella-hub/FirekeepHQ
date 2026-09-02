"""Night Shift — the Fleet-as-GPU job catalog worker (client-side, stdlib-only).

Night Shift drains the fleet job catalog: `distill_session` (the `stop` hook has
enqueued one of these at every session end since SP1b — structural capture with,
until this module, nothing draining the queue), and, since the fleet catalog,
`reauthor_stale_skill` and `propose_contested_verdict` — both enqueued nightly by
cortex's `fleet_enqueue_pass` for a human to eventually review. All three run where
the free compute lives: the developer's own machine, against a LOCAL model served
by LM Studio (`:1234`) or Ollama (`:11434`). Both speak the OpenAI-compatible API,
so the request path is identical and choosing between them is pure detection: with
no `FIREKEEP_NIGHTSHIFT_LLM_BASE` set, they are probed in that order and the first
to answer wins.

Per distill_session task it:
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

Job catalog (`JOB_TITLES`, listed and drained FIFO-by-title under ONE
`max_tasks` budget — distill_session first, then the two fleet jobs — each
fleet task leases `fleet.<task_id>`):
  - `reauthor_stale_skill`: input is a stale skill's full context (trigger,
    symptoms, content, staleness/efficacy stats). The model returns a verdict —
    "rewrite" (materially improvable: output is a COMPLETE replacement skill,
    written as `skill_create(status="draft", origin_job="reauthor_stale_skill",
    reauthor_of=<original skill_id>)` — the original is never touched), "still_valid"
    or "retire" (both are a NO-OP: nothing is written; the verdict rides the
    task's completion for a human to act on — spec decision 9).
  - `propose_contested_verdict`: input is a contradicting pair of unconfirmed
    memories. The model proposes "supersede" (one wins) or "coexist" (both stand),
    posted as a draft to `POST /memory/contested/propose` for a human to confirm —
    NOTHING here activates a skill or resolves a pair; the human dashboard does.

Posture: personal/bypass mode is a hard no-op (nothing is read or sent); a
`:cloud` model is refused (session content must not leave the machine — override
with FIREKEEP_NIGHTSHIFT_ALLOW_REMOTE=1) and no reachable backend aborts, both
BEFORE any task is touched; a malformed model
response is retried once and then the task is marked failed (visible in the
dashboard, no infinite retry); tasks predating the stop hook's session_id stamp
are completed with a note (backlog clears, nothing is invented). An unconfirmed
skill_create or an unconfirmed/rejected propose POST fails that ONE task, never
the shift; a TRANSIENT LLM TransportError defers and stops the whole shift, same
as the distill path. Import boundary: stdlib + firekeep_client stdlib modules
only (`hooks._mcp` for MCP, `transport` for REST/LLM) — never `mcp`/`httpx`.
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
JOB_DISTILL = "distill_session"
JOB_REAUTHOR = "reauthor_stale_skill"
JOB_VERDICT = "propose_contested_verdict"
# Listed in THIS order: distill first (the queue that existed before the
# catalog), then the fleet jobs cortex enqueues nightly. One max_tasks budget.
JOB_TITLES = (JOB_DISTILL, JOB_REAUTHOR, JOB_VERDICT)
_TASK_TITLE = JOB_DISTILL  # legacy alias — existing tests and messages use it
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

_REAUTHOR_PROMPT = (
    "You review a team skill (a 'what to do when X happens' playbook) that nobody "
    "has recalled for a long time. Decide whether it is still worth keeping as "
    "written. Reply with STRICT JSON only — no prose, no code fences — matching:\n"
    '{"verdict": "rewrite" | "still_valid" | "retire", "reason": "<one sentence>", '
    '"skill": {"trigger": "<one sentence: when this applies>", '
    '"symptoms": "<observable signals>", "steps": "<the procedure>", '
    '"gotchas": "<pitfalls>", "domain": "<one word>"} | null}\n'
    "Use \"rewrite\" ONLY when you can make the skill materially clearer, more "
    "specific or more correct from the evidence given — then fill \"skill\" with the "
    "COMPLETE rewritten playbook (never a diff). \"still_valid\" = keep as is; "
    "\"retire\" = obsolete. For those two, \"skill\" must be null. Never invent "
    "commands, paths or facts absent from the evidence."
)

_VERDICT_PROMPT = (
    "Two memories in a team knowledge base contradict each other and neither has "
    "been confirmed by a human. Propose a verdict for a human to review. Reply with "
    "STRICT JSON only — no prose, no code fences — matching:\n"
    '{"action": "supersede" | "coexist", "winner_id": "<id of the memory to KEEP>" | null, '
    '"rationale": "<1-3 sentences citing the evidence>"}\n'
    "\"supersede\" = one is wrong or outdated: winner_id MUST be exactly one of the "
    "two ids given. \"coexist\" = both are true in their own contexts: winner_id must "
    "be null. Prefer the more specific, more recent, more confirmed memory. Never "
    "invent facts absent from the two texts."
)


def _llm_base() -> str:
    return (os.environ.get("FIREKEEP_NIGHTSHIFT_LLM_BASE") or _DEFAULT_LLM_BASE).rstrip("/")


def _llm_model() -> str:
    return os.environ.get("FIREKEEP_NIGHTSHIFT_LLM_MODEL") or _DEFAULT_LLM_MODEL


def _detect_llm_base(
    get_json: Callable[..., Any],
) -> tuple[str, list[str], str | None] | None:
    """Resolve the backend as `(base, model_ids, ollama_native_root)`, or None if
    none answered.

    An EXPLICIT base is probed and nothing else: a typo must fail loudly rather than
    silently fall through to a different engine than the operator named. With no
    config, probe the supported backends in order and take the first that answers.

    The model list comes back from the same probe rather than a second round trip.
    An unreadable shape degrades to `[]`, which callers must treat as "cannot tell"
    — never as "no models".

    `ollama_native_root` is the Ollama native API root when the base turns out to be
    an Ollama server (else None) — see `_ollama_native_root` for why that path exists.
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
        return base, models, _ollama_native_root(base, get_json)
    return None


def _ollama_native_root(base: str, get_json: Callable[..., Any]) -> str | None:
    """If `base` is an Ollama server, return its native API root (the base with a
    trailing `/v1` stripped); else None.

    This exists because of a measured, load-bearing asymmetry: Ollama's
    OpenAI-compatible `/v1` endpoint honours NO thinking control — not the `/no_think`
    soft switch, not a top-level `think`, not `chat_template_kwargs.enable_thinking`.
    A qwen3-class reasoning model asked for JSON there thinks until it exhausts the
    token budget and returns empty content (measured >4min, then nothing — the same
    silent whitespace-burn the cortex dream path hit). Its NATIVE `/api/chat` DOES
    honour `think: false`, which turns that same call into 34s of clean JSON. Since
    the shipped default model is a qwen3, Night Shift is broken-by-default on Ollama
    without this.

    Detection is a probe of the Ollama-only `/api/version`: LM Studio 404s it and so
    stays on `/v1`, where its own reasoning handling applies.
    """
    root = base[:-len("/v1")].rstrip("/") if base.rstrip("/").endswith("/v1") else base.rstrip("/")
    try:
        get_json(f"{root}/api/version", headers={}, timeout=5.0)
    except Exception:  # noqa: BLE001 — not Ollama (LM Studio 404s) or unreachable
        return None
    return root


def _model_available(model: str, models: list[str]) -> bool:
    """Whether `model` names something in `models`, tolerating Ollama's tag forms.

    Ollama's /v1/models reports fully-tagged ids (`llama3:latest`) while its chat API
    accepts and resolves the bare name (`llama3`). Exact equality therefore rejects a
    model that works — and because the check runs before leasing, that false veto is
    silent: the shift aborts and distillation simply stops. Compared both directions so
    neither spelling is penalised.
    """
    if model in models:
        return True
    if ":" not in model and f"{model}:latest" in models:
        return True
    if model.endswith(":latest") and model[: -len(":latest")] in models:
        return True
    return False


def _remote_model_refusal() -> str | None:
    """Night Shift's premise is that session content never leaves the machine. Ollama
    exposes `<name>:cloud` models that transparently route to a third party, which
    silently inverts that. Refused by default, overridable — the same shape as the
    bootstrap's SSL_CERT_FILE / FIREKEEP_KEEP_SSL_CERT_FILE opt-out.
    """
    model = _llm_model()
    # Match the TAG, not a literal ":cloud" suffix. Ollama's cloud models are commonly
    # tagged `<name>:<size>-cloud` (gpt-oss:120b-cloud, qwen3-coder:480b-cloud) — the
    # form its own cloud docs lead with — so a suffix test on ":cloud" alone lets the
    # documented spelling straight through.
    tag = model.rsplit(":", 1)[-1] if ":" in model else ""
    if (tag == "cloud" or tag.endswith("-cloud")) and not os.environ.get(
            "FIREKEEP_NIGHTSHIFT_ALLOW_REMOTE"):
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


def _content_of(resp: Any) -> str:
    """Pull the assistant text from either chat response shape: OpenAI `/v1`
    (`choices[0].message.content`) or Ollama native `/api/chat`
    (`message.content`). Raises so the caller's malformed-reply retry fires."""
    if isinstance(resp, dict):
        choices = resp.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            msg = choices[0].get("message")
            if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                return msg["content"]
        msg = resp.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            return msg["content"]
    raise ValueError("no assistant content in LLM response")


def _json_object(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in model reply")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("model reply is not a JSON object")
    return data


def _chat_json(messages: list[dict], post_json: Callable[..., Any], base: str,
               native: str | None, validate: Callable[[str], dict]) -> dict:
    """One local-model call, `validate`d, with a single retry on malformed output.

    Raises transport.TransportError on a TRANSIENT loss (caller defers the shift)
    and ValueError when the model never produced valid output (caller fails the
    task). The Ollama native/`think:false` handling is the one documented in
    `_ollama_native_root`; a non-thinking model that rejects `think` is retried
    without it rather than deferring the shift.
    """
    if native:
        url = f"{native}/api/chat"
        body: dict[str, Any] = {"model": _llm_model(), "messages": messages,
                                "stream": False, "think": False, "format": "json",
                                "options": {"temperature": 0.2}}
    else:
        url = f"{base}/chat/completions"
        body = {"model": _llm_model(), "temperature": 0.2, "messages": messages}
    last_error: Exception | None = None
    for _attempt in (1, 2):
        try:
            resp = post_json(url, body, headers={"Content-Type": "application/json"},
                             timeout=_LLM_TIMEOUT)
        except transport.TransportError as e:
            if native and body.get("think") is not None and "think" in str(e).lower():
                body = {k: v for k, v in body.items() if k != "think"}
                hooklog.log_failure(
                    "nightshift", f"model rejects think:false, retrying without it: {e}")
                continue
            raise
        try:
            return validate(_content_of(resp))
        except (KeyError, IndexError, TypeError, ValueError) as e:
            last_error = e
            hooklog.log_failure("nightshift", f"malformed LLM reply (attempt): {e!r}")
    raise ValueError(f"model never produced valid JSON: {last_error!r}")


def _synthesize(sid: str, assigner: str, evidence: str,
                post_json: Callable[..., Any], base: str,
                native: str | None = None) -> dict:
    """Distill one session (see module docstring). Kept as the named entry point
    the existing tests exercise; the mechanics live in `_chat_json`."""
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content":
            f"Session {sid} by agent {assigner}. Evidence:\n\n{evidence}"},
    ]
    return _chat_json(messages, post_json, base, native, _extract_json)


def _validate_reauthor(text: str) -> dict:
    data = _json_object(text)
    verdict = str(data.get("verdict") or "")
    if verdict not in ("rewrite", "still_valid", "retire"):
        raise ValueError(f"reauthor verdict must be rewrite|still_valid|retire, got {verdict!r}")
    skill = data.get("skill")
    if verdict == "rewrite":
        if not isinstance(skill, dict) or not str(skill.get("trigger") or "").strip() \
                or not str(skill.get("steps") or "").strip():
            raise ValueError("rewrite verdict without a complete skill")
    return data


def _validate_proposal(text: str, pair: set[str]) -> dict:
    data = _json_object(text)
    action = str(data.get("action") or "")
    winner = data.get("winner_id")
    if action == "supersede":
        if winner not in pair:
            raise ValueError(f"supersede winner_id {winner!r} is not one of the pair")
    elif action == "coexist":
        data["winner_id"] = None
    else:
        raise ValueError(f"action must be supersede|coexist, got {action!r}")
    if not str(data.get("rationale") or "").strip():
        raise ValueError("proposal without a rationale")
    return data


def _relay_ok(resp: Any) -> bool:
    """Relay tools never raise — failures come back in-band as {"error": ...}
    dicts with HTTP 200 (wf_02954176 review). A write only counts if this holds."""
    return isinstance(resp, dict) and not resp.get("error")


def _task_created_at(task: dict) -> float:
    """Stable fallback ordering for pre-upgrade Relay servers."""
    try:
        return float(task.get("created_at"))
    except (TypeError, ValueError):
        return float("inf")


def _task_context(task: dict) -> dict:
    try:
        ctx = json.loads(task.get("context") or "")
    except (TypeError, ValueError) as e:
        raise ValueError(f"task context is not JSON: {e}") from e
    if not isinstance(ctx, dict):
        raise ValueError("task context is not a JSON object")
    return ctx


def _handle_reauthor(ctx: dict, *, call_tool, cfg, post_json, base, native,
                     worker: str, dry_run: bool) -> tuple[str, str]:
    """Returns (summary_counter, relay result text). Raises to fail the task."""
    skill_id = str(ctx.get("skill_id") or "")
    if not skill_id:
        raise ValueError("task context has no skill_id")
    evidence = json.dumps({k: ctx.get(k) for k in (
        "trigger", "symptoms", "content", "domain", "project", "timestamp",
        "last_recalled_at", "stale_detected_at", "access_count",
        "skill_efficacy", "skill_efficacy_n")}, indent=1)
    messages = [{"role": "system", "content": _REAUTHOR_PROMPT},
                {"role": "user", "content": f"Stale skill {skill_id}:\n\n{evidence}"}]
    data = _chat_json(messages, post_json, base, native, _validate_reauthor)
    verdict, reason = data["verdict"], str(data.get("reason") or "")[:300]
    if verdict != "rewrite":
        # No artifact is written for these two — the stale flag stays in the
        # inbox for the human; the verdict rides the task (spec decision 9).
        return "noop", f"night-shift: {verdict} — {reason}"
    if dry_run:
        return "reauthored", "night-shift (dry run): would draft a re-authored skill"
    skill = data["skill"]
    created = call_tool("cortex", "skill_create", {
        "trigger": str(skill.get("trigger") or "")[:1000],
        "symptoms": str(skill.get("symptoms") or "")[:2000],
        "steps": str(skill.get("steps") or "")[:4000],
        "gotchas": str(skill.get("gotchas") or "")[:2000],
        "domain": str(skill.get("domain") or ctx.get("domain") or "")[:100],
        "status": "draft",
        "origin_job": JOB_REAUTHOR,
        "reauthor_of": skill_id,
        "agent_id": worker,
    }, cfg=cfg)
    if not (isinstance(created, str) and created.startswith("Skill created")):
        # Older server (unknown argument), cross-workspace 404, in-band error:
        # never complete a task whose draft was not confirmed.
        raise RuntimeError(f"skill_create did not confirm: {created!r}"[:300])
    return "reauthored", f"night-shift: re-authored draft awaiting review — {reason}"


def _handle_propose(ctx: dict, *, post_json, base, native, worker: str,
                    dry_run: bool) -> tuple[str, str]:
    a, b = ctx.get("a") or {}, ctx.get("b") or {}
    ids = {str(a.get("id") or ""), str(b.get("id") or "")} - {""}
    if len(ids) != 2:
        raise ValueError("task context does not describe a pair")
    evidence = json.dumps({"a": a, "b": b, "contested_at": ctx.get("contested_at")}, indent=1)
    messages = [{"role": "system", "content": _VERDICT_PROMPT},
                {"role": "user", "content": f"Contested pair:\n\n{evidence}"}]
    data = _chat_json(messages, post_json, base, native,
                      lambda text: _validate_proposal(text, ids))
    action = data["action"]
    if action == "supersede":
        winner = str(data["winner_id"])
        loser = next(i for i in ids if i != winner)
    else:
        winner, loser = str(a.get("id")), str(b.get("id"))
    body = {"winner_id": winner, "loser_id": loser, "action": action,
            "rationale": str(data.get("rationale") or "")[:1000]}
    if dry_run:
        return "proposed", f"night-shift (dry run): would propose {action}"
    ep = resolver.resolve("cortex")
    headers = dict(ep.headers)
    headers.update({"Content-Type": "application/json", "X-Agent-Id": worker})
    try:
        resp = post_json(f"{ep.rest_base}/memory/contested/propose", body,
                         headers=headers, verify=ep.verify)
    except transport.TransportError as e:
        # A cortex 404/409/5xx here is THIS task's failure, not the model going
        # away — it must not defer the whole shift.
        raise RuntimeError(f"propose rejected by cortex: {e}"[:300]) from e
    if not (isinstance(resp, dict) and resp.get("status") == "proposed"):
        raise RuntimeError(f"propose not confirmed: {resp!r}"[:300])
    return "proposed", (f"night-shift: proposed {action}"
                        + (f" (keep {winner})" if action == "supersede" else "")
                        + f" — {body['rationale'][:200]}")


def _run_fleet_task(task: dict, title: str, *, out: dict, call_tool, cfg, post_json,
                    base, native, worker: str, dry_run: bool) -> None:
    """One catalog task: lease, dispatch, write through review surfaces, complete.

    Mirrors the distill branch's contract — honest counting, one bad task never
    stops the shift, a TRANSIENT model loss defers and stops it (`out["_stop"]`).
    """
    task_id = task.get("id") or ""
    if dry_run:
        try:
            ctx = _task_context(task)
            if title == JOB_REAUTHOR:
                counter, _ = _handle_reauthor(ctx, call_tool=call_tool, cfg=cfg, post_json=post_json,
                                              base=base, native=native, worker=worker, dry_run=True)
            elif title == JOB_VERDICT:
                counter, _ = _handle_propose(ctx, post_json=post_json, base=base, native=native,
                                             worker=worker, dry_run=True)
            else:
                raise ValueError(f"unknown fleet job {title!r}")
            out[counter] += 1
        except transport.TransportError:
            out["deferred"] += 1
            out["_stop"] = True
        except Exception as e:  # noqa: BLE001
            hooklog.log_failure("nightshift", f"dry-run {title} failed: {e}")
            out["failed"] += 1
        return

    token = 0
    try:
        lease = call_tool("relay", "relay_lease",
                          {"resource_id": f"fleet.{task_id}", "agent_id": worker}, cfg=cfg)
        if not (isinstance(lease, dict) and lease.get("acquired")):
            out["skipped"] += 1
            return
        token = int(lease.get("fencing_token") or 0)
        ctx = _task_context(task)
        try:
            if title == JOB_REAUTHOR:
                counter, result = _handle_reauthor(
                    ctx, call_tool=call_tool, cfg=cfg, post_json=post_json, base=base,
                    native=native, worker=worker, dry_run=False)
            elif title == JOB_VERDICT:
                counter, result = _handle_propose(
                    ctx, post_json=post_json, base=base, native=native, worker=worker,
                    dry_run=False)
            else:
                raise ValueError(f"unknown fleet job {title!r}")
        except transport.TransportError as e:
            hooklog.log_failure("nightshift", f"LLM transient, deferring: {e}")
            out["deferred"] += 1
            out["_stop"] = True
            return
        resp = call_tool("relay", "relay_task_update",
                         {"task_id": task_id, "status": "completed", "result": result[:500]},
                         cfg=cfg)
        if _relay_ok(resp):
            out[counter] += 1
            if counter == "reauthored":
                out["draft_skills"] += 1
        else:
            hooklog.log_failure("nightshift",
                                f"completion not confirmed for {task_id}: {resp!r}"[:300])
            out["failed"] += 1
    except Exception as e:  # noqa: BLE001 — one bad task never stops the shift
        hooklog.log_failure("nightshift", f"task {task_id} ({title}) failed: {e}")
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
                          {"resource_id": f"fleet.{task_id}", "agent_id": worker,
                           "fencing_token": token}, cfg=cfg)
            except Exception as e:  # noqa: BLE001
                hooklog.log_failure("nightshift", f"lease release failed: {e}")


def run(max_tasks: int = _DEFAULT_MAX_TASKS, dry_run: bool = False, *,
        call_tool: Callable[..., Any] = _mcp.call_tool,
        post_json: Callable[..., Any] = transport.post_json,
        get_json: Callable[..., Any] = transport.get_json) -> dict:
    """Drain up to `max_tasks` tasks across the job catalog (`JOB_TITLES`).
    Returns a summary dict — {distilled, legacy, skipped, failed, duplicates,
    deferred, reauthored, proposed, noop, draft_skills} plus `error` on a
    run-level abort. Never raises — the CLI turns `error` into a nonzero exit.

    Counting is HONEST: distilled/legacy/duplicates/reauthored/proposed increment
    only after the relay confirms the task update in-band (a completion that
    failed would mean re-processing next run, so it is counted `failed`, not the
    success bucket). `noop` = a reauthor verdict of still_valid/retire (nothing
    written, by design). `draft_skills` = every draft skill_create this run
    confirmed, from EITHER job (distill or reauthor). `deferred` = a TRANSIENT
    LLM failure; the task stays pending and the shift stops (the model is gone —
    failing every remaining task would be noise)."""
    out = {"distilled": 0, "legacy": 0, "skipped": 0, "failed": 0,
           "duplicates": 0, "deferred": 0, "reauthored": 0, "proposed": 0,
           "noop": 0, "draft_skills": 0}
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
    base, models, native = detected
    if models and not _model_available(_llm_model(), models):
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
        def _list(title: str) -> list[dict]:
            args = {"status": "pending", "title": title, "oldest_first": True, "limit": 50}
            try:
                listing = call_tool("relay", "relay_task_list", args, cfg=cfg)
            except transport.TransportError:
                listing = None
            if not isinstance(listing, dict) or "tasks" not in listing:
                if title != JOB_DISTILL:
                    return []  # a pre-catalog relay has no such tasks
                # Rolling upgrades: older Relay schemas reject title/oldest_first
                # (an isError tool RESULT unwraps to a STRING, not a dict — see
                # hooks/_mcp.py). Sort the compatibility page locally.
                listing = call_tool("relay", "relay_task_list",
                                    {"status": "pending", "limit": 50}, cfg=cfg)
            return sorted(
                (t for t in ((listing or {}).get("tasks") or []) if t.get("title") == title),
                key=_task_created_at,
            )

        tasks: list[dict] = []
        for title in JOB_TITLES:
            tasks.extend(_list(title))
        tasks = tasks[:max_tasks]

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

            title = task.get("title") or ""
            if title != JOB_DISTILL:
                _run_fleet_task(task, title, out=out, call_tool=call_tool, cfg=cfg,
                                post_json=post_json, base=base, native=native,
                                worker=worker, dry_run=dry_run)
                if out.get("_stop"):
                    stop_shift = True
                    out.pop("_stop", None)
                continue

            if dry_run:
                if not sid:
                    out["legacy"] += 1
                    continue
                try:
                    _synthesize(sid, assigner, _evidence(sid, task, get_json),
                                post_json, base, native)
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
                                        base, native)
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
                        "origin_job": JOB_DISTILL,
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
                    if made_skill:
                        out["draft_skills"] += 1
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
