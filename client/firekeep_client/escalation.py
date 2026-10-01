"""Escalation — telling a cheap main model when to consult a stronger one.

The problem this exists to solve is not detection, it is that the decision is
never made. Claude Code ships an advisor tool, but it is a SERVER-SIDE tool
(`{type: "advisor_20260301"}`): Anthropic's own infrastructure runs the
consultation, so it cannot reach a non-Anthropic upstream — pointed at
api.deepseek.com the request 400s on the unknown tool type. It is also gated on
the base model having an `advisor_rank` in Anthropic's model catalog, which no
third-party model has. Measured 2026-09-13 on this fleet: with the advisor
unavailable and nothing else prompting it, a subagent handed a genuinely hard
distributed-concurrency design problem made ZERO tool calls and never once
considered escalating. The escalation has to be stated in words, by the client.

TWO HALVES, because there are two different reasons to escalate and only one of
them can be counted:

  * `record_outcome` / `nudge` — EVIDENCE. Consecutive tool failures, and one
    file edited repeatedly without a clean pass. Both are arithmetic on things
    the post_tool hook already computes; neither is a guess about difficulty,
    and both match the trigger the escalation agent documents for itself
    ("debugging that has failed twice").
  * `standing_policy` — JUDGEMENT. Whether a piece of work is an architectural
    decision is not detectable by counting, and a keyword matcher for it is
    worse than useless: it fires on any sentence that mentions "design" or
    "plan", which is most of a normal working day. So that half is not a
    classifier at all — it is a standing instruction injected once at session
    start, and the model applies its own reading of the work in front of it.

RESTRAINT IS THE FEATURE. This writes into the user's context unasked, and the
prompt core carries the scar of getting that wrong once already (raw JSON, five
stale messages, every single prompt, 2026-07-14). So: it speaks only on counted
evidence, it speaks at most ONCE per distinct situation, and the moment there is
nothing to report it clears its own suppression so the NEXT trip can speak
again. Every failure path — corrupt state, unwritable disk, unresolvable
session — is silent, because a missed nudge costs one Fable call the user never
got, while a loud failure costs them the context window they are working in.

Advisory only, deliberately. Nothing here blocks a tool call; the model is told
what the evidence is and left to act on it.

TWO GATES, and they answer different questions. `is_enabled` is the user's switch:
`FIREKEEP_NO_ESCALATION` (env) or `[escalation] nudge = false` in
~/.firekeep/config. `should_escalate` is whether the feature is USEFUL AT ALL here —
an allowlist of main models (`[escalation] for_models`, default `deepseek`) AND a
check that the session is not talking to Anthropic directly. Both checks are
needed: pointing this at Claude inverts it (a strong model told to consult a peer
is circular when the main model is itself Fable) and spends real money where
DeepSeek tokens cost almost nothing, and the model NAME alone is not enough to
tell — see `_direct_to_anthropic` for the `claude-fable` case that reads a
deepseek model name off the global settings while running Fable. Between them,
`_active` is what the rest of the module asks.

Stdlib only (SP1b import boundary). Nothing in here raises.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from firekeep_client import hooklog, state
from firekeep_client.promptrecall import trim_line

_HOOK = "escalation"

# Model-name prefixes whose main model BENEFITS from this. See `should_escalate`.
DEFAULT_FOR_MODELS = ("deepseek",)

# Two, because one failed command is ordinary work and three is late. This is the
# same bar the escalation agent documents for itself: "debugging that has failed
# twice".
FAIL_STREAK_THRESHOLD = 2

# Three, because the first edit is the attempt, the second is the adjustment, and
# the third is going in circles. Counted per file and cleared by a passing
# command, so this is "without a clean pass", not a lifetime edit tally.
EDIT_THRASH_THRESHOLD = 3

# At most this many thrashed files are named — the nudge is a pointer to the
# problem, not an inventory of the session.
MAX_FILES_NAMED = 2

# Per-session, and TTL'd for the same reason the recall dedupe list is: without
# an expiry a streak could outlive the session that earned it and fire at a
# session that never failed anything.
STATE_TTL_SECONDS = 12 * 3600

STANDING_POLICY = (
    "\n\n[firekeep] Escalation: architecture decisions, system design, planning, "
    "and migrations go to the `fable-advisor` subagent BEFORE you commit to an "
    "approach — not after it fails."
)

_HEADER = "[escalation]"
_INSTRUCTION = (
    "Before continuing, spawn the `fable-advisor` subagent with the failing "
    "command, its output, and the file — do not attempt another fix unaided."
)

_EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit"})

_DISABLE = ("0", "false", "no", "off")   # explicit disable words (NOT blank)
_FALSEY = ("", "0", "false", "no", "off")


def is_enabled(cfg) -> bool:
    """Default ON. `FIREKEEP_NO_ESCALATION` (env) wins over the config; `[escalation]
    nudge = false` disables it persistently. A blank value (`nudge =`) means 'unset'
    -> the default (ON), not disabled — mirroring `promptrecall.is_enabled`, because
    a user who half-edits their config should get the documented default, not
    silence."""
    if os.environ.get("FIREKEEP_NO_ESCALATION", "").strip().lower() not in _FALSEY:
        return False
    val = (cfg.get("escalation", "nudge", fallback="true")
           if cfg.has_section("escalation") else "true").strip().lower()
    return val not in _DISABLE


def state_key(session_id: str) -> str:
    return f"escalation_{session_id}"


def _settings_paths() -> tuple[Path, ...]:
    """Where the harness keeps the CONFIGURED main model, most specific first.

    A module-level function so a test can point it at a file it controls. Without
    that, a test would read the machine's real ~/.claude/settings.json and its
    result would depend on whoever happened to run it — which is the same class of
    mistake as feeding a hook a payload you invented.
    """
    cwd = Path.cwd()
    home = Path.home()
    return (
        cwd / ".claude" / "settings.local.json",
        cwd / ".claude" / "settings.json",
        home / ".claude" / "settings.local.json",
        home / ".claude" / "settings.json",
    )


def configured_model() -> str:
    """The main model as CONFIGURED, or "" when it cannot be determined.

    `ANTHROPIC_MODEL` wins when set — the harness's own per-session override, and
    the one source a hook process is guaranteed to inherit. Otherwise the
    harness's settings files, most specific first.

    What this CANNOT see, deliberately documented rather than hidden: a mid-session
    `/model` switch. That is session-scoped and never written back to settings, so
    a session that overrides the model after start is still judged by the
    configured default. The hook payload carries no model field — checked — so
    there is no better source available to a hook today.
    """
    env = os.environ.get("ANTHROPIC_MODEL", "").strip()
    if env:
        return env
    for path in _settings_paths():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if isinstance(data, dict):
            model = data.get("model")
            if isinstance(model, str) and model.strip():
                return model.strip()
    return ""


def for_models(cfg) -> tuple[str, ...]:
    """Model-name prefixes that BENEFIT from escalation. `[escalation] for_models`
    is a comma-separated list, for anyone running a different cheap model behind
    the same kind of proxy. A blank value means the documented default rather than
    an empty allowlist — the same rule `is_enabled` follows, because a half-edited
    config should not silently disable the feature."""
    try:
        raw = (cfg.get("escalation", "for_models", fallback="")
               if cfg.has_section("escalation") else "")
    except Exception:  # noqa: BLE001
        return DEFAULT_FOR_MODELS
    prefixes = tuple(p.strip().lower() for p in (raw or "").split(",") if p.strip())
    return prefixes or DEFAULT_FOR_MODELS


def _direct_to_anthropic() -> bool:
    """True when this session talks to Anthropic's API directly.

    This is the signal that survives what a hook cannot otherwise see. The
    `claude-fable` escape hatch launches Claude Code with its own settings file
    (`--settings ~/.claude/anthropic-only.settings.json`) whose `model` is Fable —
    invisible to a hook, and the global settings it falls through to say
    deepseek-flash. Reading only the model name therefore nudges a Fable session,
    exactly backwards. But that file's `env` block sets ANTHROPIC_BASE_URL, and
    session env IS inherited by hook processes, so the endpoint gives the answer
    the model name cannot.

    An UNSET variable means Claude Code's own default, which is Anthropic.
    Deliberately not a port check: any endpoint that is not Anthropic's is the
    signal, so a user on some other router still works.
    """
    base = os.environ.get("ANTHROPIC_BASE_URL", "").strip().lower()
    return (not base) or ("api.anthropic.com" in base)


def should_escalate(cfg) -> bool:
    """Whether THIS session is one escalation can actually help.

    Escalation pays when the main model is the weak link: it upgrades a
    cheaper-but-good-enough answer. Point it at Claude and the same line inverts —
    it tells a strong model to consult a peer (circular when the main model is
    itself Fable) and spends real money doing it, where DeepSeek tokens cost
    almost nothing. So this is an ALLOWLIST of models that benefit, and it fails
    QUIET: a session that cannot be identified as cheap gets no nudge. That is the
    direction the rest of this module fails in, and for the same reason — a thing
    that writes into the user's context unasked should go dark, not loud. The cost
    of a wrong guess is asymmetric: a missed nudge costs one escalation the user
    never got, a wrong one spends money and gives bad advice.

    Two independent checks, because either alone is wrong: the model name (what
    the session is configured to run) and the endpoint (where those requests
    actually go).
    """
    if _direct_to_anthropic():
        return False
    model = configured_model().lower()
    if not model:
        return False
    return any(model.startswith(p) for p in for_models(cfg))


def _active(cfg) -> bool:
    """Both gates: the user has not switched it off, AND this model benefits."""
    try:
        return bool(is_enabled(cfg)) and should_escalate(cfg)
    except Exception:  # noqa: BLE001
        return False


def _empty() -> dict:
    return {"fail_streak": 0, "edits": {}, "notified": ""}


def read_state(session_id: str) -> dict:
    """The counters, or a clean slate. Anything unreadable or malformed reads as
    NO EVIDENCE — a lost counter costs a missed nudge, while treating a parse
    failure as 'stuck' would spend a Fable call on a session that never earned
    one. Same direction promptrecall's dedupe list fails in, for the same reason:
    a thing that writes into the context unasked should fail dark, not loud."""
    raw = state.read_scratch(state_key(session_id))
    if not raw:
        return _empty()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()

    # bool is an int in Python; -5 and True are both "not a count".
    streak = data.get("fail_streak")
    if not isinstance(streak, int) or isinstance(streak, bool) or streak < 0:
        streak = 0

    edits: dict[str, int] = {}
    raw_edits = data.get("edits")
    if isinstance(raw_edits, dict):
        for path, count in raw_edits.items():
            if (isinstance(path, str) and path
                    and isinstance(count, int) and not isinstance(count, bool)
                    and count > 0):
                edits[path] = count

    notified = data.get("notified")
    return {"fail_streak": streak, "edits": edits,
            "notified": notified if isinstance(notified, str) else ""}


def write_state(session_id: str, data: dict) -> None:
    """Never raises. A failed write costs at most one repeated nudge; letting it
    escape would cost the hook, which is the thing the user wanted."""
    try:
        state.write_scratch(state_key(session_id), json.dumps(data),
                            ttl_seconds=STATE_TTL_SECONDS)
    except Exception as e:  # noqa: BLE001
        hooklog.log_failure(_HOOK, f"escalation state write failed: {e}")


def record_outcome(session_id: str, *, tool_name: str, file_path: str,
                   success: bool) -> None:
    """Fold one tool result into the counters. Called from post_tool for every
    Bash/Edit/Write call, whether or not pre_tool queued an action for it.

    The rules, and why each is the way it is:

      * a success resets the failure streak — progress means the model is not
        stuck, and 'fail, succeed, fail' is not 'failed twice';
      * an edit bumps that file's counter whether or not it reported success —
        a write that lands but does not fix the problem is still a lap around
        the same circle, and that is the failure mode a failure count misses;
      * a PASSING COMMAND clears every file counter — a green command is
        demonstrated progress, which is exactly what 'without a clean pass'
        is measured against.
    """
    try:
        st = read_state(session_id)
        st["fail_streak"] = 0 if success else st["fail_streak"] + 1
        if tool_name in _EDIT_TOOLS and file_path:
            st["edits"][file_path] = st["edits"].get(file_path, 0) + 1
        if tool_name == "Bash" and success:
            st["edits"] = {}
        write_state(session_id, st)
    except Exception as e:  # noqa: BLE001 — counting must never break a hook
        hooklog.log_failure(_HOOK, f"escalation counting failed: {e}")


def evidence(st: dict) -> list[str]:
    """The counted reasons to escalate, as short phrases. Empty when there are none."""
    out: list[str] = []
    streak = st.get("fail_streak", 0)
    if streak >= FAIL_STREAK_THRESHOLD:
        out.append(f"{streak} consecutive failed commands")
    edits = st.get("edits") or {}
    thrashed = sorted(p for p, c in edits.items() if c >= EDIT_THRASH_THRESHOLD)
    for path in thrashed[:MAX_FILES_NAMED]:
        out.append(f"{trim_line(path)} edited {edits[path]}x without a clean pass")
    return out


def render(lines: list[str]) -> str:
    """The block, or '' for nothing to say. Label first so a reader knows in one
    line what this is."""
    if not lines:
        return ""
    return f"{_HEADER} {'; '.join(lines)}.\n{_INSTRUCTION}"


def nudge(cfg, payload: dict) -> str:
    """The whole feature, as the prompt core sees it: a block to append, or "".

    Never raises. Every failure mode — off, no evidence, unreadable state,
    unresolvable session, this exact situation already reported — produces the
    same empty string, because the prompt hook's contract is that it costs
    nothing when it has nothing to add.
    """
    try:
        if not _active(cfg):
            return ""
        if not isinstance(payload, dict):
            payload = {}
        session_id = state.resolve_session_id(payload, cfg)
        st = read_state(session_id)
        lines = evidence(st)
        if not lines:
            # Nothing to report. Clear the suppression so the NEXT trip speaks:
            # without this, a session that fails twice, recovers, and fails twice
            # again would stay silent forever on an identical digest.
            if st.get("notified"):
                st["notified"] = ""
                write_state(session_id, st)
            return ""
        digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:16]
        if st.get("notified") == digest:
            return ""
        st["notified"] = digest
        write_state(session_id, st)
        return render(lines)
    except Exception as e:  # noqa: BLE001 — fail open; the hook budget is sacred.
        hooklog.log_failure(_HOOK, f"escalation nudge failed: {e}")
        return ""


def standing_policy(cfg) -> str:
    """The judgement half: one line, once, at session start. '' when the user has
    switched it off OR the main model is not one that benefits.

    It is prose for the model, not for the human — the dispatcher promotes a
    core's systemMessage to `additionalContext` only for session_start and
    prompt, which is the channel that actually reaches the context window."""
    try:
        return STANDING_POLICY if _active(cfg) else ""
    except Exception as e:  # noqa: BLE001
        hooklog.log_failure(_HOOK, f"standing policy failed: {e}")
        return ""
