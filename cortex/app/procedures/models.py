"""Living Procedures models. Pydantic only — no I/O, no app imports."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field, PrivateAttr, model_validator

StepKind = Literal["file_glob", "unobservable"]

# Bounds the pre-edit match loop and the denormalised index. A pattern longer
# than this is not a glob anyone wrote on purpose.
MAX_PATTERN_CHARS = 200
MAX_TEXT_CHARS = 500


class StepSpec(BaseModel):
    """One step of a procedure, made addressable.

    SELF-CONTAINED by design: it carries its own `text` rather than an index
    into the skill's `## Steps` markdown, because that markdown is one blob
    (skills/api.py folds `steps` into `content`; parse_skill_content returns
    `body` undivided). An index would desync the moment a human PATCHes
    `content`, silently and undetectably.
    """

    id: str = ""
    text: str = Field(max_length=MAX_TEXT_CHARS)
    kind: StepKind = "unobservable"
    pattern: str = Field(default="", max_length=MAX_PATTERN_CHARS)
    load_bearing: bool = False

    # Whether the CALLER named this step, as opposed to the id being minted a
    # microsecond ago. `merge_step_specs` needs the distinction and
    # `model_fields_set` cannot supply it — assigning `self.id` below marks the
    # field as set either way (measured, pydantic 2.12). A PrivateAttr keeps it
    # out of `model_dump()`, which is what writes the Qdrant payload.
    _id_authored: bool = PrivateAttr(default=False)

    @model_validator(mode="after")
    def _check(self) -> "StepSpec":
        if not self.id:
            self.id = uuid.uuid4().hex[:12]
        else:
            self._id_authored = True
        if self.kind == "file_glob" and not self.pattern.strip():
            raise ValueError("kind='file_glob' requires a non-empty pattern")
        if self.kind != "file_glob" and self.pattern:
            self.pattern = ""
        return self


def _flat(text: str) -> str:
    return " ".join((text or "").split()).strip().lower()


def merge_step_specs(new_specs: list[StepSpec],
                     old_specs: list | None) -> list[dict]:
    """Carry each step's id forward across a spec rewrite, matched on TEXT.

    The design pins `id` as "minted server-side if absent, stable thereafter",
    and the id is the key an execution's evidence is filed under. Nothing on the
    agent path can hold ids still: `skill_add_step_specs`' documented entry shape
    has no `id`, no MCP surface ever RETURNS one, and its own docstring tells the
    caller to resend the whole list to add a single step. So a wholesale replace
    re-keyed every step on every wording fix, orphaning the procedure's entire
    recorded history — and the nightly pass then read each stored execution as a
    skip of every current step.

    Text is the only join available, and it is the right one: a step whose text
    changed IS a different step, and its evidence should not follow. An id the
    caller named always wins — if you name it, we honour it — and each old id is
    adopted at most once, so two steps that share a text resolve positionally
    rather than collapsing onto one identity.
    """
    out: list[dict] = []
    available: dict[str, list[str]] = {}
    for old in old_specs or []:
        if not isinstance(old, dict):
            continue
        oid = old.get("id")
        if oid:
            available.setdefault(_flat(old.get("text") or ""), []).append(str(oid))
    for spec in new_specs:
        data = spec.model_dump()
        if not spec._id_authored:
            candidates = available.get(_flat(spec.text)) or []
            if candidates:
                data["id"] = candidates.pop(0)
        out.append(data)
    return out
