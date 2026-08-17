"""Text extraction for the four supported formats.

Two rules hold the whole module together:

* **It never raises.** A folder of documents is a folder of whatever a human
  happened to put there — a truncated PDF, a .docx that is really a renamed
  zip, a file that vanished between the walk and the read. Every one of those
  is a recorded per-file failure, never an exception that ends a sync over the
  other 4999 files.
* **An honest zero is a result, not a failure.** There is no OCR (a disclosed
  gap, I5): a scanned PDF yields no text. Reporting that as an error would put
  it in the retry set forever; reporting it as an empty extraction lets state
  record `seen_hash` and stop asking.
"""
from __future__ import annotations

import os
from pathlib import Path

SUPPORTED_SUFFIXES = frozenset({".md", ".txt", ".pdf", ".docx"})

DEFAULT_MAX_EXTRACT_KB = 400


def is_supported(path: str | Path) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_SUFFIXES


def max_extract_bytes() -> int:
    return _env_int("FIREKEEP_DOCDEX_MAX_EXTRACT_KB", DEFAULT_MAX_EXTRACT_KB) * 1024


def _env_int(name: str, default: int) -> int:
    """A cap read from the environment. Anything unparseable or non-positive
    falls back to the documented default — a typo in an env var must not
    silently disable a cap the docs promise."""
    raw = os.environ.get(name, "")
    try:
        value = int(raw.strip())
    except (AttributeError, ValueError):
        return default
    return value if value > 0 else default


def truncate(text: str) -> tuple[str, bool]:
    """Cut `text` to the extract cap, returning `(text, truncated)`.

    The cap is a byte budget, so the cut is made on the encoded form and then
    decoded back with `errors="ignore"` — landing mid-codepoint would produce a
    JSON body the server cannot decode.
    """
    limit = max_extract_bytes()
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    return encoded[:limit].decode("utf-8", "ignore"), True


def extract(path: str | Path) -> tuple[str, str | None]:
    """`(text, error)`. Exactly one of them is meaningful, except for the
    honest zero — `("", None)` — which is a valid, final result."""
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        return "", f"unsupported file type '{p.suffix or p.name}'"
    try:
        if suffix in (".md", ".txt"):
            return _text(p), None
        if suffix == ".pdf":
            return _pdf(p), None
        return _docx(p), None
    except Exception as e:  # noqa: BLE001 — per-file failures are DATA, never control flow
        return "", f"{type(e).__name__}: {e}"[:500]


def _text(p: Path) -> str:
    # errors="replace", not "strict": a stray Latin-1 byte in a notes file is a
    # document with one bad character, not an unreadable document.
    return p.read_bytes().decode("utf-8", "replace")


def _pdf(p: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(p))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 — one unreadable page must not lose the rest
            continue
    return "\n\n".join(part for part in pages if part.strip())


def _docx(p: Path) -> str:
    import docx

    document = docx.Document(str(p))
    parts = [para.text for para in document.paragraphs]
    # Tables are ordinary document text — a runbook whose steps live in a table
    # would otherwise extract as an honest zero and never be indexed.
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                parts.append(cell.text)
    return "\n".join(part for part in parts if part.strip())
