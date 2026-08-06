"""Living Procedures models. Pydantic only — no I/O, no app imports."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field, model_validator

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

    @model_validator(mode="after")
    def _check(self) -> "StepSpec":
        if not self.id:
            self.id = uuid.uuid4().hex[:12]
        if self.kind == "file_glob" and not self.pattern.strip():
            raise ValueError("kind='file_glob' requires a non-empty pattern")
        if self.kind != "file_glob" and self.pattern:
            self.pattern = ""
        return self
