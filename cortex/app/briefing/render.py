"""Rendered pre-flight briefing text + instruction-priority builder.

All string assembly is pure Python (f-strings / join) over already-decoded
JSON values — there is NO shell/python interpolation of upstream data (the
structural elimination of briefing.sh defects S2/S3). The endpoint returns a
dict; FastAPI's single json encode is the only escaping in the whole path.
"""
from __future__ import annotations

from typing import Any

Section = dict[str, Any]


def build_instructions(sections: dict[str, Section], agent_id: str, briefing_id: str) -> str:
    """Priority: resume-nudge (strong) > agent-aware ctx_start > plain ctx_start.

    `briefing_id` (the id minted by GET /briefing) is rendered into every
    branch's ctx_start_session(...) call so an agent that follows the printed
    instruction supplies it. Bridge stores it on the session hash, and
    GET /patterns/effectiveness later reconciles it back to the session_id to
    close the strategy-tip A/B feedback loop (see compute_tip_effectiveness /
    _build_briefing_map). Without rendering the id here, no caller ever passes
    it and the reconciliation map stays permanently empty.
    """
    resumable = sections.get("resumable_sessions", {})
    if resumable.get("status") == "ok":
        rec = (resumable.get("data") or {}).get("recommended")
        if rec and rec.get("strong_nudge"):
            goal = (rec.get("goal") or "")[:80]
            return (
                f"You have unfinished work: '{goal}'. Resume with "
                f"ctx_resume_session(session_id='{rec.get('session_id')}') "
                f"or start fresh with ctx_start_session(goal=..., briefing_id='{briefing_id}')."
            )
    if agent_id and agent_id != "default":
        return (
            f"Call ctx_start_session(goal=..., briefing_id='{briefing_id}') as {agent_id}, "
            f"then memory_recall to load relevant past experience."
        )
    return (
        f"Call ctx_start_session(goal=..., briefing_id='{briefing_id}'), then memory_recall "
        f"to load relevant past experience."
    )


def _marker(name: str, section: Section) -> str | None:
    """Inline degradation marker for an unavailable section, else None."""
    if section.get("status") == "unavailable":
        return f"[{name.upper()} unavailable: {section.get('error')}]"
    return None


def render_briefing(*, agent_id: str, goal: str, sections: dict[str, Section],
                    instructions: str) -> str:
    """Full pre-flight briefing text. Section order per investigation §2 line 160.

    Pure string assembly over decoded values — no eval/exec/shell/format-injection
    surface exists, so the S1/S2/S3 defect classes cannot recur.
    """
    lines: list[str] = ["=== PRE-FLIGHT BRIEFING ==="]
    if agent_id or goal:
        lines.append(f"You are {agent_id}. Goal: {goal[:80]}.")

    def emit(name: str, body_fn) -> None:
        sec = sections.get(name, {})
        mark = _marker(name, sec)
        if mark:
            lines.append(mark)
            return
        body_fn(sec.get("data") or {})

    # 0. profile (Dreaming Task 8) — who this session is working with, rendered
    # first so it's the first thing read: work -> memories -> nightly dream ->
    # next session opens already knowing you. Absent on every fresh install
    # (no dream has run yet) -> section is "empty" -> nothing rendered, not a
    # placeholder line.
    def _profile(d):
        text = d.get("text")
        if text:
            lines.append(f"PROFILE: {text}")
    emit("profile", _profile)

    # 1. environment
    def _env(d):
        if d.get("summary") or d.get("event_count"):
            lines.append(f"ENVIRONMENT: {d.get('summary', '')}")
    emit("environment", _env)

    # 2. errors (from environment.recent_errors — no standalone section)
    env = sections.get("environment", {})
    if env.get("status") == "ok":
        errs = (env.get("data") or {}).get("recent_errors") or []
        if errs:
            summarised = "; ".join((e.get("summary", "")[:60]) for e in errs)
            lines.append(f"ERRORS: {len(errs)} recent error(s): {summarised}")

    # 3. tasks
    def _tasks(d):
        tasks = d.get("tasks") or []
        if tasks:
            lines.append(f"TASKS: {len(tasks)} pending task(s):")
            for t in tasks:
                lines.append(f"- {t.get('title', '')} [{t.get('priority', '')}] from {t.get('assigner', '')}")
    emit("tasks", _tasks)

    # 4. bulletins
    def _bull(d):
        for p in d.get("posts") or []:
            lines.append(f"BULLETINS: {p.get('author', '')}: {p.get('content', '')[:60]}")
    emit("bulletins", _bull)

    # 5. quality
    def _quality(d):
        if d.get("total_sessions"):
            insights = "; ".join(d.get("insights") or [])
            lines.append(f"QUALITY: From {d['total_sessions']} recent session(s): {insights}")
    emit("quality", _quality)

    # 6. strategy tips (only when shown — control group withholds, D6)
    def _tips(d):
        if d.get("shown"):
            lines.append("STRATEGY TIPS:")
            for p in d.get("patterns") or []:
                cat = (p.get("category") or "")[:4].upper()
                lines.append(f"- [{cat} {int(p.get('confidence', 0) * 100)}%] {p.get('recommendation', '')}")
    emit("strategy_tips", _tips)

    # 6b. observed patterns (N=1 surface — descriptive, UNVALIDATED, provenance-tagged).
    # Deliberately labelled distinctly from STRATEGY TIPS so an observed candidate is
    # never read as a promoted (trial+) strategy card.
    def _observed(d):
        items = d.get("items") or []
        if items:
            lines.append("FROM YOUR RECENT SESSIONS (observed, unvalidated):")
            for it in items:
                conf = int((it.get("confidence") or 0) * 100)
                lines.append(
                    f"- {it.get('recommendation', '')}  "
                    f"[{conf}% — from {it.get('provenance', '')}]"
                )
    emit("observed", _observed)

    # 7. cross-agent
    def _cross(d):
        pats = d.get("patterns") or []
        if pats:
            lines.append("CROSS-AGENT LEARNINGS:")
            for p in pats:
                cat = (p.get("category") or "")[:4].upper()
                lines.append(f"- [{cat} {int(p.get('confidence', 0) * 100)}%, from {p.get('source_agent', '')}] {p.get('recommendation', '')}")
    emit("cross_agent", _cross)

    # 8. skills
    def _skills(d):
        skills = d.get("skills") or []
        if skills:
            lines.append("RELEVANT SKILLS:")
            for s in skills:
                lines.append(f"  * {s.get('trigger', '')}")
                if s.get("symptoms"):
                    lines.append(f"    Symptoms: {s.get('symptoms', '')[:60]}")
    emit("skills", _skills)

    # 9. vault
    def _vault(d):
        secrets = d.get("secrets") or []
        if secrets:
            listed = ", ".join(f"{s.get('key')} [{s.get('category')}]" for s in secrets)
            lines.append(f"VAULT: {d.get('count', len(secrets))} secret(s) available: {listed}")
    emit("vault", _vault)

    # 10. resumable sessions
    def _resume(d):
        sessions = d.get("sessions") or []
        if sessions:
            # Section header, emitted once. (The sibling _bull label above is a
            # per-item prefix by design — do not "fix" that one to match.)
            lines.append("RESUMABLE SESSIONS:")
        for s in sessions:
            goal_t = (s.get("goal") or "")[:80]
            age = s.get("age_hours")
            age_s = f"{age}h ago" if age is not None else "unknown age"
            lines.append(f"- [{s.get('session_id')}] \"{goal_t}\" ({s.get('reason')} {age_s})")
    emit("resumable_sessions", _resume)

    # 11. instructions (always present)
    lines.append(instructions)

    # 12. discipline
    def _disc(d):
        if d.get("untagged_total"):
            lines.append(f"⚠️ Discipline: {d['untagged_total']} memory call(s) had no session_id.")
    emit("discipline", _disc)

    # 13. dlq
    def _dlq(d):
        for w in d.get("warnings") or []:
            lines.append(f"⚠️ {w}")
    emit("dlq", _dlq)

    # PR5 D2/D3: the grading-nudge treatment section, emitted LAST — it is an
    # instruction about how the session should END, so it is the final thing
    # read. Verbatim: the text is the registered intervention.
    def _nudge(d):
        if d.get("shown") and d.get("text"):
            lines.append(d["text"])
    emit("grading_nudge", _nudge)

    lines.append("=== END BRIEFING ===")
    return "\n".join(lines)
