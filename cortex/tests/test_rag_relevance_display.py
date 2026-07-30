"""The score an agent reads must mean relevance, not rank.

`_min_max_normalize` rescales every result set so the best entry is exactly 1.0
and the worst exactly 0.0. That is fine for ordering and wrong for display:

  - the confidence band read `max(e["score"])`, which min-max GUARANTEES is 1.0,
    so `confidence: high` was unconditional. Observed live: a recall whose
    weakest result rendered as [0%] still announced high confidence.
  - `[0%]` does not mean irrelevant. RECALL_SCORE_FLOOR (0.35) filters on raw
    cosine before this, so a 0% entry cleared the floor — it is merely last.
  - two results with cosines 0.775 and 0.609 both displayed as 1.0 (observed
    live against a populated instance).

An agent that reads "0%" reasonably ignores a relevant memory, and one that reads
"confidence: high" on noise learns to distrust recall. Both push toward the low
recall-usage this instance actually shows.
"""

from __future__ import annotations

from app.engine.rag import RAGEngine, _min_max_normalize

_format_markdown = RAGEngine._format_markdown


def _entry(content, score, raw, store="vector"):
    return {"content": content, "score": score, "store": store,
            "metadata": {"raw_score": raw}}


def test_min_max_still_pins_the_top_to_one():
    """The premise. If this ever stops being true, the rest is unnecessary."""
    entries = [{"score": 0.61}, {"score": 0.42}, {"score": 0.36}]
    _min_max_normalize(entries)
    assert entries[0]["score"] == 1.0
    assert entries[-1]["score"] == 0.0


def test_confidence_can_be_low():
    """The band must be able to come out low — it never could before."""
    out = _format_markdown([_entry("weak", 1.0, 0.31), _entry("weaker", 0.0, 0.28)])
    assert "confidence: low" in out, out.splitlines()[0]


def test_confidence_can_be_medium():
    out = _format_markdown([_entry("mid", 1.0, 0.55), _entry("lower", 0.0, 0.41)])
    assert "confidence: medium" in out, out.splitlines()[0]


def test_confidence_is_high_only_when_relevance_is():
    out = _format_markdown([_entry("strong", 1.0, 0.88), _entry("lower", 0.0, 0.42)])
    assert "confidence: high" in out, out.splitlines()[0]


def test_displayed_percentage_is_relevance_not_rank():
    """The bottom entry cleared the score floor; it must not render as 0%."""
    out = _format_markdown([_entry("top", 1.0, 0.78), _entry("bottom", 0.0, 0.61)])
    assert "[78%]" in out, out
    assert "[61%]" in out, out
    assert "[0%]" not in out, out
    assert "[100%]" not in out, out


def test_entries_without_a_raw_score_fall_back_to_the_normalized_value():
    """Resolution bonuses carry a sentinel 1.2 and no real relevance.

    They must not crash the formatter, and must not be counted toward the band —
    a sentinel propping up `confidence` would reintroduce the original defect.
    """
    entries = [
        {"content": "resolution", "score": 1.0, "store": "graph",
         "metadata": {"name": "resolution", "label": "Resolution"}},
        _entry("real but weak", 0.0, 0.30),
    ]
    out = _format_markdown(entries)
    assert "confidence: low" in out, out.splitlines()[0]
    assert "[100%]" in out, "the fallback should still render the normalized value"
