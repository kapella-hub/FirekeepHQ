"""Each test here pins one audit finding. If a test fails, a rail is gone."""
from app.db.vector import _projected_metadata


def _conditions_repr(f):
    return repr(f)


def test_projected_metadata_exposes_memory_type():
    got = _projected_metadata({"memory_type": "procedural", "text": "x"}, "p1")
    assert got["memory_type"] == "procedural"


def test_projected_metadata_defaults_memory_type_when_absent():
    assert _projected_metadata({}, "p1")["memory_type"] == "episodic"


def test_find_similar_filter_excludes_confirmed_and_dreams():
    # Build the filter the same way find_similar does and assert both guards are in it.
    from app.db import vector as v

    f = v._similarity_filter(namespace="default", domain="infra")
    text = _conditions_repr(f)
    assert "confirmed_count" in text, "confirmed memories must be excluded"
    assert "dream" in text, "dream points must be excluded"


def test_memory_agent_scope_filter_excludes_dreams():
    from app.workers.memory_agent import _active_non_corpus_filter

    assert "dream" in _conditions_repr(_active_non_corpus_filter())
