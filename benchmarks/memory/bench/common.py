"""Shared helpers for the LongMemEval benchmark harness."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
WORK_DIR = ROOT / "work"
RESULTS_DIR = ROOT / "results"

_SESSION_TAG_PREFIX = "lm_session:"
_DATE_TAG_PREFIX = "lm_date:"

# Keys every LongMemEval-S row must carry. Verified at download AND load time
# so a dataset-format drift fails loudly, never as a zero-score run.
REQUIRED_KEYS = frozenset({
    "question_id", "question_type", "question", "answer", "question_date",
    "haystack_dates", "haystack_session_ids", "haystack_sessions",
    "answer_session_ids",
})


def sanitize_namespace(question_id: str) -> str:
    """Map a question id to a server-legal namespace (^[a-zA-Z0-9_-]+$)."""
    cleaned = re.sub(r"[^a-z0-9_]", "_", question_id.lower())
    return f"lm_{cleaned}"[:200]


def session_tag(session_id: str) -> str:
    return f"{_SESSION_TAG_PREFIX}{session_id}"[:100]


def date_tag(date: str) -> str:
    return f"{_DATE_TAG_PREFIX}{date}"[:100]


def parse_session_tag(tags: list[str]) -> str | None:
    for tag in tags:
        if isinstance(tag, str) and tag.startswith(_SESSION_TAG_PREFIX):
            return tag[len(_SESSION_TAG_PREFIX):]
    return None


def parse_date_tag(tags: list[str]) -> str | None:
    for tag in tags:
        if isinstance(tag, str) and tag.startswith(_DATE_TAG_PREFIX):
            return tag[len(_DATE_TAG_PREFIX):]
    return None


def is_abstention(question_id: str) -> bool:
    return question_id.endswith("_abs")


def verify_dataset(rows: list[dict]) -> None:
    if not rows:
        raise ValueError("dataset is empty")
    for i, row in enumerate(rows):
        missing = REQUIRED_KEYS - row.keys()
        if missing:
            raise ValueError(
                f"row {i} missing keys: {sorted(missing)} — dataset format drift?"
            )


def load_dataset(path: Path) -> list[dict]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    verify_dataset(rows)
    return rows
