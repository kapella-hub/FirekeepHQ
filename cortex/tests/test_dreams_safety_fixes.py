"""Each test here pins one audit finding. If a test fails, a rail is gone."""
import pytest

from app.db.vector import _projected_metadata


def _must_not_conditions(f, key):
    """Return every ``must_not`` FieldCondition on *f* with the given key.

    Structural, not textual: a filter that (incorrectly) put a condition in
    ``must`` instead of ``must_not`` — inverting "exclude X" into "require
    X" — must fail this, even though repr() would still contain the key
    name. That inversion is exactly what a `repr()`-substring assertion
    cannot catch (verified by hand: moving both conditions from must_not to
    must still satisfies `"confirmed_count" in repr(f)` and `"dream" in
    repr(f)`), which is the whole point of a filter that says "must be
    excluded" — it pins the *rail*, not just the vocabulary.
    """
    return [c for c in (f.must_not or []) if getattr(c, "key", None) == key]


def test_projected_metadata_exposes_memory_type():
    got = _projected_metadata({"memory_type": "procedural", "text": "x"}, "p1")
    assert got["memory_type"] == "procedural"


def test_projected_metadata_defaults_memory_type_when_absent():
    assert _projected_metadata({}, "p1")["memory_type"] == "episodic"


def test_projected_metadata_top_level_memory_type_wins_over_nested():
    """Top-level payload is authoritative (matches GC's read order and every
    other promoted key in this projection) — a stale nested copy must not
    override it."""
    payload = {
        "memory_type": "procedural",
        "metadata": {"memory_type": "episodic"},
    }
    assert _projected_metadata(payload, "p1")["memory_type"] == "procedural"


def test_projected_metadata_falls_back_to_nested_memory_type():
    """The third shape, and the one the first two could not catch (C1).

    With only both-absent and top-level-present covered, a literal
    ``payload.get("memory_type", "episodic")`` placed AFTER the
    ``**payload.get("metadata", {})`` spread passes both — and is still
    wrong: its default fires on the nested-only shape and OVERRIDES the value
    the spread supplied. That is a live recall regression on every
    deployment, independent of DREAM_ENABLED — a legacy ``reference`` memory
    (no age decay) starts recalling as ``episodic`` (90-day half-life) while
    GC keeps reading ``reference``, and legacy ``procedural`` degrades
    180d -> 90d the same way. GC's own read order (app/workers/gc.py:341-345)
    is the contract; this pins agreement with it on the shape where they can
    silently differ.
    """
    payload = {"metadata": {"memory_type": "reference"}}
    assert _projected_metadata(payload, "p1")["memory_type"] == "reference"


def test_projected_metadata_agrees_with_gc_on_every_memory_type_shape():
    """Same read, both sides, no shape excepted — the two are only safe if
    they cannot disagree at all, not if they happen to agree on three cases
    somebody thought of."""
    def _gc_read(payload):
        # Verbatim from app/workers/gc.py's scan loop.
        return (
            payload.get("memory_type")
            or (payload.get("metadata") or {}).get("memory_type")
            or "episodic"
        )

    shapes = [
        {},
        {"metadata": {}},
        {"metadata": None},
        {"memory_type": "procedural"},
        {"memory_type": "reference", "metadata": {"memory_type": "episodic"}},
        {"metadata": {"memory_type": "reference"}},
        {"metadata": {"memory_type": "procedural"}},
        {"memory_type": "", "metadata": {"memory_type": "transient"}},
        {"memory_type": None, "metadata": {"memory_type": "reference"}},
    ]
    for payload in shapes:
        assert _projected_metadata(payload, "p1")["memory_type"] == _gc_read(payload), payload


def test_find_similar_filter_excludes_confirmed_memories():
    from app.db import vector as v

    f = v._similarity_filter(namespace="default", domain="infra")
    matches = _must_not_conditions(f, "confirmed_count")
    assert len(matches) == 1, "must_not must exclude confirmed_count, structurally"
    assert matches[0].range is not None and matches[0].range.gt == 0


def _excluded_sources(f):
    return {
        c.match.value
        for c in _must_not_conditions(f, "source")
        if c.match is not None
    }


# Both dream-authored sources, on every rail that can supersede or rewrite a
# point. `dream_profile` was absent from BOTH shipped filters while the docs
# asserted it was present (final-review I1) — hence one parametrized test per
# rail over both values, so neither can be dropped without a named failure.
DREAM_SOURCES = ("dream", "dream_profile")


@pytest.mark.parametrize("source", DREAM_SOURCES)
def test_find_similar_filter_excludes_dream_authored_sources(source):
    """`find_similar`'s only caller is contradiction.py's
    detect_and_supersede, invoked from EVERY ordinary /memory/learn. A person
    profile is broad prose at domain="general" — a plausible >=0.85 match for
    a general-domain learn — so without this a routine write supersedes the
    profile and "one continuously-updated profile per human" is defeated by
    the most ordinary path in the system."""
    from app.db import vector as v

    f = v._similarity_filter(namespace="default", domain="infra")
    assert source in _excluded_sources(f), (
        f"must_not must exclude source=={source!r}, structurally"
    )


@pytest.mark.parametrize("source", DREAM_SOURCES)
def test_memory_agent_scope_filter_excludes_dream_authored_sources(source):
    """duplicate_detection_pass LLM-merges near-duplicate text and
    deep_contradiction_pass supersedes across domains. Both are actively
    wrong on a point that is REPLACED in place on every dream run."""
    from app.workers.memory_agent import _active_non_corpus_filter

    f = _active_non_corpus_filter()
    assert source in _excluded_sources(f), (
        f"must_not must exclude source=={source!r}, structurally"
    )


@pytest.mark.parametrize("source", DREAM_SOURCES)
def test_dream_activity_gate_ignores_dream_authored_writes(source):
    """The design spec's §Testing item, previously unwritten (final-review
    I6a). `_scope_filter` is the ONE filter both the activity gate and
    candidate selection scan, so it is the single point where dreaming could
    start feeding itself: dream output counted as "new memory activity" would
    hold the work-available gate open forever, and dream output entering the
    candidate pool would let the pass dream about its own dreams."""
    from app.dreams.task import _scope_filter

    assert source in _excluded_sources(_scope_filter()), (
        f"must_not must exclude source=={source!r}, structurally"
    )
