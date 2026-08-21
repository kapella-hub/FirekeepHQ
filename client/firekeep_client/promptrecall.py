"""Proactive recall — the Keep consults itself on every prompt.

Recall is PULL-based everywhere else in the kit: an agent has to decide to ask.
Fleet-wide that decision runs at 46% ("recall before you answer") while writes run
65-75%, so the Keep accumulates faster than it is consulted — and the one existing
push, the session-start briefing, only ever matches the session's ORIGINAL goal,
never what the user asked on turn 7. This module is the other push: every prompt
that carries signal is embedded against team memory, and the few genuinely
relevant, not-yet-seen memories are injected. Far more often, nothing is.

NOISE DISCIPLINE IS THE FEATURE, not a safety rail bolted on afterwards. It is
inherited directly from this hook's own history: before the 2026-07-14 rewrite the
prompt core re-injected the same five stale relay messages as raw JSON into context
on every single user message, and the field complaint that produced was about
exactly this channel. A pushed recall that fires on "ok" or "/commit", or that
re-injects the same memory every turn, would rebuild that failure with better
provenance. Hence, in order:

  * prompts under 24 characters (whitespace-collapsed) and slash commands are
    never queried at all — no signal to embed, and no server round-trip spent;
  * only sources above a real relevance floor are eligible (see `_relevance`);
  * a memory injected once is not injected again for 12h (per-agent scratch,
    the tasks-digest pattern);
  * at most 3, one collapsed line each, trimmed to 200 characters;
  * the whole thing is bounded at 2.5s and fails OPEN — any error injects
    nothing and leaves a hooklog line. The hook path budget is sacred.

Rendering states what it is — background evidence the agent may use, never an
instruction it must follow. It joins the prompt core's EXISTING systemMessage, so
the user sees exactly what the model was handed; no second channel is invented.

ON by default. `FIREKEEP_NO_RECALL_PUSH` (env) or `[recall] push = false` in
~/.firekeep/config turns it off; personal/bypass mode already suppresses the whole
prompt core at the dispatcher, so there is no third gate here.

Stdlib only (SP1b import boundary). Nothing in here raises.
"""
from __future__ import annotations

import hashlib
import json
import math
import os

from firekeep_client import hooklog, resolver, state, transport

_HOOK = "prompt"

# Below this, whitespace-collapsed, a prompt is an acknowledgement ("ok thanks",
# "yes do that", "run it") — there is nothing to embed and nothing memory could
# usefully match against.
MIN_PROMPT_CHARS = 24

DEFAULT_MIN_SCORE = 0.55
DEFAULT_TIMEOUT_SECONDS = 2.5
MAX_INJECTED = 3
MAX_LINE_CHARS = 200
TOP_K = 3
MAX_TASK_CHARS = 2000  # ContextQuery.task is max_length=2000 server-side.

# Per-agent, no session component — same shape as the tasks-suppression digest,
# and TTL'd for the same reason: without an expiry a memory injected once would
# stay suppressed on this machine forever, and only a NEW memory could ever break
# the silence. Twelve hours matches the session stash and personal-mode backstops.
SEEN_TTL_SECONDS = 12 * 3600
SEEN_CAP = 50

HEADER = ("[firekeep recall] team memory that may be relevant "
          "(verify before relying on it):")

_DISABLE = ("0", "false", "no", "off")   # explicit disable words (NOT blank)
_FALSEY = ("", "0", "false", "no", "off")


def is_enabled(cfg) -> bool:
    """Default ON. `FIREKEEP_NO_RECALL_PUSH` (env) wins over the config; `[recall]
    push = false` disables it persistently. A blank value (`push =`) means 'unset'
    -> the default (ON), not disabled — mirroring `autoupdate.is_enabled`, because
    a user who half-edits their config should get the documented default, not
    silence."""
    if os.environ.get("FIREKEEP_NO_RECALL_PUSH", "").strip().lower() not in _FALSEY:
        return False
    val = (cfg.get("recall", "push", fallback="true")
           if cfg.has_section("recall") else "true").strip().lower()
    return val not in _DISABLE


def _env_float(name: str, default: float) -> float:
    """A tunable read from the environment, falling back to `default` on anything
    unusable. `nan`/`inf` parse without error and would silently disable the
    comparison they feed (every `>=` against nan is False), so they are rejected
    too."""
    raw = os.environ.get(name, "")
    try:
        value = float(raw) if raw.strip() else default
    except (AttributeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def min_score() -> float:
    return _env_float("FIREKEEP_RECALL_PUSH_MIN_SCORE", DEFAULT_MIN_SCORE)


def timeout_seconds() -> float:
    value = _env_float("FIREKEEP_RECALL_PUSH_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
    return value if value > 0 else DEFAULT_TIMEOUT_SECONDS


def prompt_text(payload: dict) -> str:
    """The user's prompt, whitespace-collapsed. Claude Code's UserPromptSubmit and
    kiro's userPromptSubmit both deliver it as `prompt`; runtimes that deliver no
    prompt text (opencode maps `session.idle`) yield "" and are skipped by
    `carries_signal` — which is the honest coverage story, not a bug."""
    if not isinstance(payload, dict):
        return ""
    raw = payload.get("prompt")
    return " ".join(raw.split()) if isinstance(raw, str) else ""


def carries_signal(prompt: str) -> bool:
    """False for the two prompt shapes a recall could only add noise to: too short
    to embed meaningfully, and slash commands (whose text is a command name, not a
    description of work)."""
    if len(prompt) < MIN_PROMPT_CHARS:
        return False
    return not prompt.startswith("/")


def _relevance(source: dict) -> float | None:
    """The source's REAL relevance, or None when it has none to report.

    NOT `score`. `MemorySource.score` has been through the RAG engine's
    `_min_max_normalize`, which sets the best entry of the returned set to exactly
    1.0 and the worst to exactly 0.0 BY CONSTRUCTION — measured live 2026-08-06,
    "zzz nonsense query about knitting patterns" scored 1.0 just like a real
    question. A floor read off that number is not a relevance floor; it admits
    roughly the top half of whatever came back, on every prompt, forever, which is
    the raw-JSON-every-prompt failure wearing a score.

    `metadata.raw_score` is the pre-normalization value — cosine for vector
    entries, the jaccard/distance blend for graph ones — and the engine preserves
    it through normalization for exactly this kind of consumer (engine/rag.py,
    SP0 C4 defect #16; its own confidence band reads the same field for the same
    reason). Entries that never carried one (the resolution bonus, which is a
    sentinel 1.2) return None and are dropped rather than counted, so a sentinel
    cannot push itself into the user's context.

    Consequence, chosen deliberately: if the server ever stopped emitting
    `raw_score`, this feature goes DARK (injects nothing) rather than loud
    (injects everything). For a thing that writes into the user's context
    unasked, silence is the correct failure.
    """
    md = source.get("metadata")
    if isinstance(md, dict) and md.get("raw_score") is not None:
        try:
            value = float(md["raw_score"])
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None
    return None


def _source_id(source: dict) -> str:
    """A stable dedupe key. Vector sources carry `metadata.id` (the Qdrant point
    id, the same one /memory/recall stamps into its replay event); graph ones may
    carry `metadata.memory_ids`. Anything else falls back to a content hash, so a
    source with no id still dedupes against itself rather than re-injecting every
    turn."""
    md = source.get("metadata")
    if isinstance(md, dict):
        for key in ("id", "memory_id"):
            value = md.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                return str(value).strip()
        ids = md.get("memory_ids")
        if isinstance(ids, list) and ids and str(ids[0]).strip():
            return str(ids[0]).strip()
    content = source.get("content")
    text = content if isinstance(content, str) else ""
    return "sha:" + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def trim_line(text: object) -> str:
    """Collapse whitespace and cap at MAX_LINE_CHARS with a visible ellipsis.

    Exported because the prompt hook's two older neighbours — relay task titles
    and channel message bodies — render server-supplied strings into the SAME
    systemMessage and had no cap at all: a hostile fixture drove that hook to
    28,418 characters from one task and one message (measured 2026-08-21). They
    share this function rather than re-declaring 200, so the budget stays one
    number in one place.

    Callers keep identifiers (task id, sender) OUTSIDE the trim: the point is to
    hand over a pointer, not to lose the handle that fetches the full text.
    """
    if not isinstance(text, str):
        return ""
    collapsed = " ".join(text.split())
    if len(collapsed) > MAX_LINE_CHARS:
        return collapsed[:MAX_LINE_CHARS - 3] + "..."
    return collapsed


def _line(source: dict, score: float) -> str:
    """One collapsed, trimmed line. The score shown is the one that was actually
    thresholded (`_relevance`), never the normalized rank — a displayed number the
    filter did not use would misdescribe why the memory is on screen."""
    return f"- {trim_line(source.get('content'))} (score {score:.2f})"


def select(sources, *, seen, floor: float) -> list[tuple[str, str]]:
    """Pick what to inject: above the floor, not already seen this window, at most
    MAX_INJECTED, in server order (which is relevance order). Returns
    (id, rendered line) pairs so the caller can record exactly what it showed —
    recording ids it did NOT show would suppress memories the user never saw."""
    picked: list[tuple[str, str]] = []
    already = set(seen)
    for source in sources if isinstance(sources, list) else []:
        if len(picked) >= MAX_INJECTED:
            break
        if not isinstance(source, dict):
            continue
        score = _relevance(source)
        if score is None or score < floor:
            continue
        sid = _source_id(source)
        if sid in already:
            continue
        already.add(sid)
        picked.append((sid, _line(source, score)))
    return picked


def render(lines: list[str]) -> str:
    """The block, or "" for nothing to say. Header first so a reader knows in one
    line what this is and how much to trust it."""
    return "\n".join([HEADER, *lines]) if lines else ""


def _seen_key(agent: str) -> str:
    return f"recall_push_{agent}"


def read_seen(agent: str) -> list[str]:
    """Ids already injected inside the live window. Anything unreadable or
    malformed reads as EMPTY — a lost dedupe list costs one repeated memory, while
    treating a parse failure as 'everything was seen' would silently disable the
    feature."""
    raw = state.read_scratch(_seen_key(agent))
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(x) for x in data if isinstance(x, (str, int))] if isinstance(data, list) else []


def remember(agent: str, ids: list[str]) -> None:
    """Append the ids just injected, newest last, capped at SEEN_CAP. Rewriting the
    key refreshes its TTL, so the window rolls with an active session and lapses 12h
    after the last injection.

    Never raises. A failed write costs one future repeat of a memory the user has
    already seen; letting it escape would cost the injection itself, which is the
    thing the user actually wanted. Wrong direction to fail in, so it doesn't."""
    if not ids:
        return
    try:
        keep = (read_seen(agent) + list(ids))[-SEEN_CAP:]
        state.write_scratch(_seen_key(agent), json.dumps(keep),
                            ttl_seconds=SEEN_TTL_SECONDS)
    except Exception as e:  # noqa: BLE001
        hooklog.log_failure(_HOOK, f"proactive recall dedupe write failed: {e}")


def recall(cfg, prompt: str) -> list:
    """POST the prompt to cortex `/memory/recall` and return its `sources`.

    Three deliberate fields:

    `format: "raw"` — ContextQuery defaults to `"synthesized"`, which runs an LLM
    pass over the results before answering. On a 2.5s hook budget that is not a
    slower answer, it is NO answer on every prompt; Bridge's own proactive recall
    passes raw for the same reason (SP0 C6, defect #11). This path wants the rows,
    not prose about them.

    `top_k: 3` — the cap is 3, so asking for more would only be work thrown away.

    `trigger` is what keeps the measurement honest: a pushed recall IS a recall, so
    the fleet's "recall before you answer" number rises mechanically the day this
    ships. The server carries this field into the replay/eval record, so deliberate
    and pushed recall stay separable afterwards instead of confounding each other.

    No `namespace`: omitting it searches every namespace in the caller's workspace.
    Sending the literal `"default"` would scope to that one category and hide every
    memory filed under `infrastructure` and friends — 146 of them on the live store,
    which is the defect `docs/guides/cortex-design-decisions.md` records.

    Returns [] for a DEGRADED response (vector search down, results graph-only).
    Same call as Bridge makes: the floor below is calibrated on cosine, and a
    graph-only set is a different scale, so pushing it would be pushing noise the
    threshold cannot judge.
    """
    ep = resolver.resolve("cortex", cfg=cfg)
    body = {
        "task": prompt[:MAX_TASK_CHARS],
        "top_k": TOP_K,
        "format": "raw",
        "trigger": "prompt-hook",
    }
    data = transport.post_json(f"{ep.rest_base}/memory/recall", body,
                               headers=ep.headers, verify=ep.verify,
                               timeout=timeout_seconds())
    if not isinstance(data, dict) or data.get("degraded"):
        return []
    return data.get("sources", [])


def nudge(cfg, payload: dict) -> str:
    """The whole feature, as the prompt core sees it: a block to append, or "".

    Never raises and never blocks for longer than the configured timeout. Every
    failure mode — off, no signal, unreachable server, nothing above the floor,
    everything already seen — produces the same empty string, because the prompt
    hook's contract is that it costs nothing when it has nothing to add.
    """
    try:
        if not is_enabled(cfg):
            return ""
        prompt = prompt_text(payload)
        if not carries_signal(prompt):
            return ""
        agent = resolver.agent_id(cfg)
        picked = select(recall(cfg, prompt), seen=read_seen(agent), floor=min_score())
        if not picked:
            return ""
        remember(agent, [sid for sid, _ in picked])
        return render([line for _, line in picked])
    except Exception as e:  # noqa: BLE001 — fail open; the hook budget is sacred.
        hooklog.log_failure(_HOOK, f"proactive recall failed: {e}")
        return ""
