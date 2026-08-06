"""Shared helpers for the LongMemEval benchmark harness."""
from __future__ import annotations

import hashlib
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


_LABEL_STEM_MAX = 100
_LABEL_DIGEST_CHARS = 8


def label_digest(run_label: str) -> str:
    """Short stable digest of the RAW label (whitespace-stripped only).

    Stripping is the one normalisation applied before hashing: a trailing
    space is a shell artifact, not a different leg, and losing resume to one
    would be a nasty way to burn four hours. Everything else is significant,
    which is what makes `sanitize_label` injective.
    """
    raw = (run_label or "").strip()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_LABEL_DIGEST_CHARS]


def sanitize_label(run_label: str) -> str:
    """Map a --run-label to a single safe path segment, injectively.

    A label reaches the filesystem as a directory name, so anything that could
    escape `work/` (separators, `..`) or name nothing at all must be neutralised
    here rather than trusted from the command line.

    Neutralising alone is not enough, because it is many-to-one: `post dream`,
    `post/dream` and `post_dream` all cleaned to `post_dream`, as did any two
    labels agreeing on their first 100 characters. Two labels sharing a
    directory share their `recall_<config>.jsonl`, which restores exactly the
    cross-label resume leak scoping exists to prevent — the second leg skips
    every question and re-scores the first leg's rows. Appending a digest of
    the RAW label makes the map injective; the cleaned stem is kept in front
    of it so the directory is still readable at a glance.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", (run_label or "").strip())
    cleaned = cleaned.strip(".")
    stem = cleaned[:_LABEL_STEM_MAX] or "unlabelled"
    return f"{stem}-{label_digest(run_label)}"


def run_work_dir(run_label: str, *, work_dir: Path | None = None) -> Path:
    """Per-run-label directory for the artefacts a single benchmark LEG owns.

    Recall rows and their scores are label-scoped because resume is keyed by
    question id: unscoped, a second run under a different label reads the first
    label's `recall_<config>.jsonl`, skips all 500 recalls, re-scores the first
    run's rows and reports a bit-identical result — which is exactly what the
    Dreaming A/B comparator would read as "no regression". Scoping keeps resume
    working WITHIN a label (a 4-hour leg must survive an interruption) and makes
    it impossible ACROSS labels.

    `work/ingest_ledger.jsonl` is deliberately NOT scoped by this — see the
    comment at its construction site in `bench.run`.
    """
    return (work_dir or WORK_DIR) / sanitize_label(run_label)


# Artefacts that belonged to no label under the pre-scoping layout. They are
# never read (their owning label is unknowable, and adopting them is the bug
# scoping exists to prevent) but their presence is reported loudly.
_LEGACY_GLOBS = ("recall_*.jsonl", "scores_*.json", "qa_*.jsonl")


def legacy_unscoped_artefacts(work_dir: Path | None = None) -> list[Path]:
    """Pre-scoping run artefacts sitting directly in `work/`, if any."""
    root = work_dir or WORK_DIR
    if not root.is_dir():
        return []
    found: list[Path] = []
    for pattern in _LEGACY_GLOBS:
        found.extend(p for p in root.glob(pattern) if p.is_file())
    return sorted(found)


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
