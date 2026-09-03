"""One skill-payload builder, shaped exactly the way `app/skills/api.py`'s
create path stores a skill — shared by both ladder suites.

WHY THIS EXISTS. The ladder's completeness check read `payload["steps"]`, a key
no skill writer has ever stored: `create_skill` folds the steps into `content`
under a `## Steps` heading, and both synthesizer paths do the same. The bug
survived review because every ladder fixture invented the payload shape it
wanted — `test_ladder_rules.py`'s `_clean_payload` and `test_ladder_pass.py`'s
`_skill_point` both wrote the imaginary key — so the suite certified the bug
instead of catching it. Building fixtures from the real create path is what
makes "a draft created through `POST /skills` is admissible" a property the
tests can actually check.

Keep `skill_content` byte-identical to `create_skill`'s `full_content` template
(`app/skills/api.py`). If that template changes, this is the file that has to
change with it.
"""
from __future__ import annotations

#: Mirrors `create_skill`'s default timestamp shape; overridable per fixture.
DEFAULT_TIMESTAMP = "2026-09-01T00:00:00+00:00"


def skill_content(*, trigger: str, symptoms: str, domain: str, steps: str,
                  gotchas: str, verified_on: str = "2026-09-01") -> str:
    """The `content` string `POST /skills` stores, verbatim.

    Note the `## Steps` heading is emitted even when `steps` is empty — that is
    exactly why the heading alone cannot stand in for the body.
    """
    return (
        f"trigger: {trigger}\n"
        f"symptoms: {symptoms}\n"
        f"domain: {domain}\n"
        f"verified_on: {verified_on}\n"
        "---\n"
        f"## Steps\n{steps}\n\n"
        f"## Gotchas\n{gotchas}"
    )


def real_skill_payload(*, trigger: str = "the collector stops mid-sync",
                       symptoms: str = "no events arrive and the log is silent",
                       domain: str = "collectors",
                       steps: str = "1. restart the worker\n2. re-run the sync",
                       gotchas: str = "the state key survives a restart",
                       status: str = "draft",
                       **extra) -> dict:
    """A stored skill payload as `create_skill` writes it.

    `trigger`, `symptoms` and `domain` are real payload keys AND appear in the
    content preamble; `steps` and `gotchas` exist ONLY inside `content`. Any
    `**extra` is spread as raw payload keys (`ladder_since`, `duplicate_of`,
    `needs_rereview`, …) so a fixture can express ladder state without knowing
    how the content is built.
    """
    payload = {
        "memory_type": "skill",
        "skill_status": status,
        "trigger": trigger,
        "symptoms": symptoms,
        "content": skill_content(trigger=trigger, symptoms=symptoms, domain=domain,
                                 steps=steps, gotchas=gotchas),
        "domain": domain,
        "skill_score": 0.0,
        "source_session_id": None,
        "project": None,
        "agent_id": None,
        "namespace": "default",
        "timestamp": DEFAULT_TIMESTAMP,
        "source_type": "manual",
        "workspace_id": "ws-test",
        "member_id": "member-test",
    }
    payload.update(extra)
    return payload
