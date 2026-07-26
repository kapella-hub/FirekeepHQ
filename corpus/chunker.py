"""Source-aware document chunking.

Splits documents into chunks appropriate for LLM extraction,
respecting document structure (headers, paragraphs, endpoints).
"""

from __future__ import annotations

import re


def chunk_content(
    content: str,
    source_type: str = "text",
    chunk_size: int = 1500,
    overlap: int = 200,
) -> list[str]:
    """Chunk content based on source type.

    Args:
        content: Raw document text.
        source_type: One of 'text', 'wiki', 'jira', 'api-doc'.
        chunk_size: Target chunk size in characters.
        overlap: Overlap between adjacent paragraph chunks.

    Returns:
        List of text chunks.
    """
    stripped = content.strip()
    if not stripped:
        return []

    if source_type == "wiki":
        return _chunk_wiki(stripped, chunk_size, overlap)
    if source_type == "jira":
        return _chunk_jira(stripped, chunk_size, overlap)
    if source_type == "api-doc":
        return _chunk_api_doc(stripped, chunk_size, overlap)
    return _chunk_paragraphs(stripped, chunk_size, overlap)


# ---------------------------------------------------------------------------
# Strategy: paragraphs (default for plain text)
# ---------------------------------------------------------------------------

def _chunk_paragraphs(
    text: str, chunk_size: int, overlap: int,
) -> list[str]:
    """Split on double-newlines, merge small paragraphs, split large ones."""
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    return _merge_segments(paragraphs, chunk_size, overlap)


# ---------------------------------------------------------------------------
# Strategy: wiki (split on markdown headers)
# ---------------------------------------------------------------------------

_HEADER_RE = re.compile(r"^(#{1,4})\s", re.MULTILINE)


def _chunk_wiki(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Split on ## / ### headers, keeping the header with its section."""
    sections: list[str] = []
    positions = [m.start() for m in _HEADER_RE.finditer(text)]

    if not positions:
        return _chunk_paragraphs(text, chunk_size, overlap)

    # If content exists before the first header, include it
    if positions[0] > 0:
        preamble = text[: positions[0]].strip()
        if preamble:
            sections.append(preamble)

    for i, pos in enumerate(positions):
        end = positions[i + 1] if i + 1 < len(positions) else len(text)
        section = text[pos:end].strip()
        if section:
            sections.append(section)

    # Sections that exceed chunk_size get sub-chunked by paragraph
    result: list[str] = []
    for section in sections:
        if len(section) <= chunk_size:
            result.append(section)
        else:
            # Keep the header line, sub-chunk the body
            lines = section.split("\n", 1)
            header = lines[0]
            body = lines[1] if len(lines) > 1 else ""
            effective_size = max(chunk_size - len(header) - 1, 50)
            sub_chunks = _chunk_paragraphs(body, effective_size, overlap)
            if sub_chunks:
                sub_chunks[0] = f"{header}\n{sub_chunks[0]}"
                result.extend(sub_chunks)
            else:
                result.append(header)

    return result


# ---------------------------------------------------------------------------
# Strategy: jira (issue as unit, split if too large)
# ---------------------------------------------------------------------------

def _chunk_jira(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Keep entire issue as one chunk when possible."""
    if len(text) <= chunk_size:
        return [text]
    return _chunk_paragraphs(text, chunk_size, overlap)


# ---------------------------------------------------------------------------
# Strategy: api-doc (split on endpoint headers)
# ---------------------------------------------------------------------------

_ENDPOINT_RE = re.compile(
    r"^#{1,4}\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+",
    re.MULTILINE,
)


def _chunk_api_doc(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Split on endpoint definitions (### GET /path)."""
    positions = [m.start() for m in _ENDPOINT_RE.finditer(text)]

    if not positions:
        return _chunk_wiki(text, chunk_size, overlap)

    sections: list[str] = []
    if positions[0] > 0:
        preamble = text[: positions[0]].strip()
        if preamble:
            sections.append(preamble)

    for i, pos in enumerate(positions):
        end = positions[i + 1] if i + 1 < len(positions) else len(text)
        section = text[pos:end].strip()
        if section:
            sections.append(section)

    result: list[str] = []
    for section in sections:
        if len(section) <= chunk_size:
            result.append(section)
        else:
            sub = _chunk_paragraphs(section, chunk_size, overlap)
            result.extend(sub)

    return result


# ---------------------------------------------------------------------------
# Shared: merge small segments into chunks up to chunk_size
# ---------------------------------------------------------------------------

def _split_large_segment(text: str, chunk_size: int) -> list[str]:
    """Split an oversized segment on sentence boundaries."""
    # Try splitting on sentence-ending punctuation followed by space
    sentences = re.split(r"(?<=[.!?])\s+", text)
    if len(sentences) <= 1:
        # No sentence boundaries — hard split on word boundaries
        result = []
        while len(text) > chunk_size:
            split_at = text.rfind(" ", 0, chunk_size)
            if split_at <= 0:
                split_at = chunk_size
            result.append(text[:split_at].strip())
            text = text[split_at:].strip()
        if text:
            result.append(text)
        return result

    # Merge sentences up to chunk_size
    result: list[str] = []
    current = sentences[0]
    for s in sentences[1:]:
        combined = f"{current} {s}"
        if len(combined) <= chunk_size:
            current = combined
        else:
            result.append(current)
            current = s
    if current.strip():
        result.append(current)
    return result


def _merge_segments(
    segments: list[str], chunk_size: int, overlap: int,
) -> list[str]:
    """Merge small segments, split oversized ones."""
    if not segments:
        return []

    # Pre-split any oversized segments
    expanded: list[str] = []
    for seg in segments:
        if len(seg) > chunk_size:
            expanded.extend(_split_large_segment(seg, chunk_size))
        else:
            expanded.append(seg)

    if not expanded:
        return []

    chunks: list[str] = []
    current = expanded[0]

    for seg in expanded[1:]:
        combined = f"{current}\n\n{seg}"
        if len(combined) <= chunk_size:
            current = combined
        else:
            chunks.append(current)
            # Overlap: carry tail of previous chunk into next
            if overlap > 0 and len(current) > overlap:
                tail = current[-overlap:]
                # Try to break at a word boundary
                space = tail.find(" ")
                if space != -1:
                    tail = tail[space + 1 :]
                current = f"{tail}\n\n{seg}" if tail.strip() else seg
            else:
                current = seg

    if current.strip():
        chunks.append(current)

    return chunks
