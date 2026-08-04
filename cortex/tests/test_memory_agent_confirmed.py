"""Human confirmation must protect a memory from the memory-agent passes.

`POST /memory/confirm` is the strongest signal a user can give that a memory
is correct. Two 6-hourly maintenance passes ignored it:

  * `deep_contradiction_pass` read `confirmed_count` only through the ranking
    ratio `(1 + confirmed) / (1 + contradicted)`, so a memory confirmed ONCE
    still lost to one confirmed three times and was written `status=
    superseded` — a permanent 0.5 recall multiplier.
  * `duplicate_detection_pass` could fold a confirmed memory into an
    LLM-synthesized merge: the confirmed original was superseded, its wording
    replaced, and `_merge_lifecycle`'s `confirmed_count` max fold carried the
    human's confirmation onto text nobody had confirmed.

The two fixes are deliberately different shapes — exclusion from scope for
dedup, refusal at the write for contradictions — and the tests below pin BOTH
that a confirmed memory is never buried and that it can still win. See
`_dedup_scope_filter` / `_active_non_corpus_filter` docstrings for why.

Precedent for the guard itself: `gc.py::_scan_candidates` (skips
`confirmed_count > 0` first) and `vector.py::_similarity_filter` (the same
`must_not`, for learn-time contradiction detection).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from app.workers.memory_agent import (
    _active_non_corpus_filter,
    _cosine_similarity,
    _dedup_scope_filter,
    deep_contradiction_pass,
    duplicate_detection_pass,
)

CONFIRMED_TEXT = "Release artifacts are published from the tagged CI pipeline only"

# `deep_contradiction_pass` acts only on 0.85 <= similarity <= 0.95. These two
# unit vectors sit at exactly 0.90. Getting this wrong is not a near miss: an
# out-of-window pair makes the pass a no-op, so every "nothing was superseded"
# assertion below would pass without the guard ever being reached. The first
# draft of this file did exactly that. `test_fixture_vectors_are_inside_the_
# contradiction_window` pins it.
VEC_A = [1.0, 0.0, 0.0]
VEC_B = [0.9, 0.4358898943540674, 0.0]


# ---------------------------------------------------------------------------
# A Qdrant double that APPLIES the filter it is handed
# ---------------------------------------------------------------------------
#
# Every pre-existing fake in this suite is a MagicMock whose scroll /
# query_points ignore `scroll_filter` / `query_filter` outright, so a test
# written against one can only inspect the filter OBJECT that was passed. That
# is how a repr()-shaped assertion can pass against a catastrophically
# inverted filter. The double below returns only the points a filter admits,
# so dropping or inverting a condition changes which memories the pass WRITES
# to, and the assertions on those writes fail.


def _cond_matches(payload: dict, cond) -> bool:
    """Evaluate one FieldCondition against a payload, Qdrant's semantics."""
    value = payload.get(cond.key)
    match = getattr(cond, "match", None)
    if match is not None:
        return value == match.value
    rng = getattr(cond, "range", None)
    if rng is not None:
        # A missing or non-numeric field never satisfies a range — which is
        # what keeps legacy points that predate `confirmed_count` eligible.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        if rng.gt is not None and not value > rng.gt:
            return False
        if rng.gte is not None and not value >= rng.gte:
            return False
        if rng.lt is not None and not value < rng.lt:
            return False
        if rng.lte is not None and not value <= rng.lte:
            return False
        return True
    raise AssertionError(f"double cannot evaluate condition: {cond!r}")


def _filter_admits(payload: dict, flt) -> bool:
    if flt is None:
        return True
    for cond in flt.must or []:
        if not _cond_matches(payload, cond):
            return False
    for cond in flt.must_not or []:
        if _cond_matches(payload, cond):
            return False
    return True


def _point(pid, payload, vector, score=None):
    p = MagicMock()
    p.id = pid
    p.payload = payload
    p.vector = vector
    if score is not None:
        p.score = score
    return p


def _mem(pid, text, vector, *, confirmed=0, contradicted=0, domain="infra",
         timestamp="2026-01-01T00:00:00+00:00"):
    return _point(pid, {
        "text": text,
        "status": "active",
        "domain": domain,
        "tags": [],
        "confirmed_count": confirmed,
        "contradicted_count": contradicted,
        "timestamp": timestamp,
    }, vector)


class _FilteringQdrant:
    """Honours must/must_not; records every write for assertion."""

    def __init__(self, points):
        self._points = list(points)
        self.upserts: list = []
        self.payload_writes: list[tuple[dict, list[str]]] = []

    def scroll(self, collection_name, scroll_filter=None, limit=100, offset=None,
               with_payload=True, with_vectors=False):
        admitted = [p for p in self._points if _filter_admits(p.payload, scroll_filter)]
        return admitted[:limit], None

    def query_points(self, collection_name, query=None, query_filter=None, limit=10,
                     with_payload=True):
        hits = [
            _point(p.id, p.payload, p.vector, score=_cosine_similarity(query, p.vector))
            for p in self._points
            if _filter_admits(p.payload, query_filter)
        ]
        hits.sort(key=lambda h: h.score, reverse=True)
        result = MagicMock()
        result.points = hits[:limit]
        return result

    def upsert(self, collection_name, points):
        self.upserts.append(points)

    def set_payload(self, collection_name, payload, points):
        self.payload_writes.append((payload, list(points)))

    def close(self):
        pass

    @property
    def superseded_ids(self) -> set[str]:
        return {
            pid
            for payload, ids in self.payload_writes
            if payload.get("status") == "superseded"
            for pid in ids
        }


def _settings(**overrides):
    defaults = {
        "QDRANT_HOST": "localhost",
        "QDRANT_PORT": 6333,
        "QDRANT_COLLECTION": "firekeep_memories",
        "REDIS_URL": "redis://localhost:6379",
        "LLM_BASE_URL": "http://localhost:11434/v1",
        "LLM_MODEL": "test-model",
        "LLM_API_KEY": "test-key",
        "AGENT_ENABLED": True,
        "AGENT_SCHEDULE_HOURS": 6,
        "DEDUP_SIMILARITY_THRESHOLD": 0.78,
        "DEDUP_ENABLED": True,
        "EMBEDDING_MODEL": "test-embed",
        "AGENT_BATCH_LIMIT": 100,
        "GC_PURGE_ENABLED": False,
    }
    defaults.update(overrides)
    return MagicMock(**defaults)


def _neo4j_noop(mock_neo4j):
    session = MagicMock()
    mock_neo4j.return_value.session.return_value.__enter__ = MagicMock(return_value=session)
    mock_neo4j.return_value.session.return_value.__exit__ = MagicMock(return_value=False)


# ---------------------------------------------------------------------------
# Filter semantics — evaluated, not repr()'d
# ---------------------------------------------------------------------------


class TestScopeFilters:
    def test_dedup_filter_excludes_confirmed_memories(self):
        flt = _dedup_scope_filter()
        assert not _filter_admits({"status": "active", "confirmed_count": 1}, flt)
        assert not _filter_admits({"status": "active", "confirmed_count": 99}, flt)

    def test_dedup_filter_still_admits_unconfirmed_and_legacy_points(self):
        """`confirmed_count: 0` and a point predating the field must both stay
        eligible — the guard protects confirmed memories, it does not switch
        dedup off."""
        flt = _dedup_scope_filter()
        assert _filter_admits({"status": "active", "confirmed_count": 0}, flt)
        assert _filter_admits({"status": "active"}, flt)

    def test_dedup_filter_is_a_strict_superset_of_the_shared_scope(self):
        """Derived from `_active_non_corpus_filter`, so the corpus/dream/
        dream_profile guards cannot drift away from it."""
        shared = _active_non_corpus_filter()
        derived = _dedup_scope_filter()
        for cond in shared.must:
            assert cond in derived.must
        for cond in shared.must_not:
            assert cond in derived.must_not
        assert len(derived.must_not) == len(shared.must_not) + 1

    def test_shared_scope_still_admits_confirmed_memories(self):
        """The over-fix tripwire. `_active_non_corpus_filter` scopes the
        contradiction pass's similarity QUERY and the coherence pass's
        centroid input as well as their scrolls. Excluding confirmed memories
        there would stop one being FOUND — it could no longer supersede a
        stale rival, and it would drop out of its domain centroid, changing
        outlier detection for the unconfirmed memories around it."""
        assert _filter_admits({"status": "active", "confirmed_count": 5},
                              _active_non_corpus_filter())


# ---------------------------------------------------------------------------
# duplicate_detection_pass — confirmed memories are out of scope
# ---------------------------------------------------------------------------


class TestDedupProtectsConfirmed:
    @patch("app.workers.memory_agent._fire_webhook_sync")
    @patch("app.workers.memory_agent._get_neo4j_driver")
    @patch("app.workers.memory_agent._get_qdrant_client")
    @patch("app.workers.memory_agent.get_settings")
    def test_confirmed_memory_is_not_merged_while_its_duplicates_still_are(
        self, mock_settings, mock_qdrant, mock_neo4j, mock_webhook
    ):
        """Three same-domain near-duplicates, one of them human-confirmed.

        The two unconfirmed ones must still merge — this asserts the confirmed
        memory was excluded SPECIFICALLY, not that the pass was disabled. The
        confirmed memory must not be superseded, its text must not be
        replaced, its confirmation must not be inherited by the synthesized
        point, and its content must never even reach the merge LLM.
        """
        mock_settings.return_value = _settings()
        client = _FilteringQdrant([
            _mem("human-confirmed", CONFIRMED_TEXT, [0.90, 0.10, 0.00], confirmed=2),
            _mem("dupe-a", "artifacts come from the tagged pipeline", [0.89, 0.11, 0.01]),
            _mem("dupe-b", "tagged pipeline publishes the artifacts", [0.88, 0.12, 0.00]),
        ])
        mock_qdrant.return_value = client
        _neo4j_noop(mock_neo4j)

        merged_text = "Artifacts are published by the tagged CI pipeline"
        with patch("app.workers.memory_agent.httpx.post") as mock_httpx:
            llm = MagicMock()
            llm.raise_for_status = MagicMock()
            llm.json.return_value = {"choices": [{"message": {"content": json.dumps(
                {"text": merged_text, "domain": "infra", "tags": []}
            )}}]}
            embed = MagicMock()
            embed.raise_for_status = MagicMock()
            embed.json.return_value = {"data": [{"embedding": [0.5] * 3}]}
            mock_httpx.side_effect = [llm, embed]

            result = duplicate_detection_pass()

            llm_request = json.dumps(mock_httpx.call_args_list[0].kwargs["json"])

        # The pass still does its job on the unconfirmed pair.
        assert result["merged"] == 1
        assert client.superseded_ids == {"dupe-a", "dupe-b"}

        # ...and the confirmed memory is untouched by every write path.
        assert "human-confirmed" not in client.superseded_ids
        for payload, ids in client.payload_writes:
            assert "human-confirmed" not in ids

        # No laundering: the synthesized survivor carries no confirmation and
        # is not the confirmed memory's text.
        assert len(client.upserts) == 1
        survivor = client.upserts[0][0].payload
        assert survivor["text"] == merged_text
        assert survivor["text"] != CONFIRMED_TEXT
        assert (survivor.get("confirmed_count") or 0) == 0

        # The confirmed wording was never sent to the merge model at all.
        assert CONFIRMED_TEXT not in llm_request

    @patch("app.workers.memory_agent._fire_webhook_sync")
    @patch("app.workers.memory_agent._get_neo4j_driver")
    @patch("app.workers.memory_agent._get_qdrant_client")
    @patch("app.workers.memory_agent.get_settings")
    def test_confirmed_memory_is_not_superseded_on_the_llm_fallback_path(
        self, mock_settings, mock_qdrant, mock_neo4j, mock_webhook
    ):
        """LLM down. The fallback keeps the keeper's text, so nothing is
        re-embedded — but the losers are still superseded outright, and a
        confirmed memory that is not the top-confidence member was one of
        them. With the LLM unavailable there is no cluster left here at all."""
        mock_settings.return_value = _settings()
        client = _FilteringQdrant([
            _mem("human-confirmed", CONFIRMED_TEXT, [0.90, 0.10, 0.00], confirmed=1),
            _mem("rival", "confirmed more often", [0.89, 0.11, 0.01], confirmed=3),
        ])
        mock_qdrant.return_value = client
        _neo4j_noop(mock_neo4j)

        with patch("app.workers.memory_agent.httpx.post") as mock_httpx:
            mock_httpx.side_effect = Exception("LLM down")
            result = duplicate_detection_pass()

        assert result["merged"] == 0
        assert client.payload_writes == []
        assert client.upserts == []


# ---------------------------------------------------------------------------
# deep_contradiction_pass — confirmed memories stay in scope, are never buried
# ---------------------------------------------------------------------------


class TestContradictionProtectsConfirmed:
    def test_fixture_vectors_are_inside_the_contradiction_window(self):
        """Guard the guard. Outside 0.85-0.95 the pass never evaluates a pair,
        so the "nothing was superseded" tests below would pass on a no-op."""
        assert 0.85 <= _cosine_similarity(VEC_A, VEC_B) <= 0.95

    @patch("app.workers.memory_agent._fire_webhook_sync")
    @patch("app.workers.memory_agent._has_supersedes_link", return_value=False)
    @patch("app.workers.memory_agent._get_neo4j_driver")
    @patch("app.workers.memory_agent._get_qdrant_client")
    @patch("app.workers.memory_agent.get_settings")
    def test_confirmed_once_is_not_buried_by_confirmed_three_times(
        self, mock_settings, mock_qdrant, mock_neo4j, mock_link, mock_webhook
    ):
        """(1+1)/(1+0)=2.0 vs (1+3)/(1+0)=4.0 — the ranking ratio picks the
        confirmed-once memory as stale. Confirmation is not a tiebreak input:
        the pair is skipped and nothing is superseded."""
        mock_settings.return_value = _settings()
        client = _FilteringQdrant([
            _mem("human-confirmed", CONFIRMED_TEXT, VEC_A, confirmed=1),
            _mem("rival", "a competing claim", VEC_B, confirmed=3,
                 timestamp="2026-02-01T00:00:00+00:00"),
        ])
        mock_qdrant.return_value = client
        _neo4j_noop(mock_neo4j)

        result = deep_contradiction_pass()

        assert result["contradictions_found"] == 0
        assert client.payload_writes == []
        mock_webhook.assert_not_called()

    @patch("app.workers.memory_agent._fire_webhook_sync")
    @patch("app.workers.memory_agent._has_supersedes_link", return_value=False)
    @patch("app.workers.memory_agent._get_neo4j_driver")
    @patch("app.workers.memory_agent._get_qdrant_client")
    @patch("app.workers.memory_agent.get_settings")
    def test_confirmed_but_contradicted_is_not_buried_by_an_unconfirmed_memory(
        self, mock_settings, mock_qdrant, mock_neo4j, mock_link, mock_webhook
    ):
        """The second route into the same burial, and the one a
        `confirmed > other.confirmed` comparison would miss: (1+1)/(1+2)=0.67
        loses to a never-confirmed, never-contradicted memory at 1.0."""
        mock_settings.return_value = _settings()
        client = _FilteringQdrant([
            _mem("human-confirmed", CONFIRMED_TEXT, VEC_A,
                 confirmed=1, contradicted=2),
            _mem("rival", "a competing claim", VEC_B,
                 timestamp="2026-02-01T00:00:00+00:00"),
        ])
        mock_qdrant.return_value = client
        _neo4j_noop(mock_neo4j)

        result = deep_contradiction_pass()

        assert result["contradictions_found"] == 0
        assert client.payload_writes == []

    @patch("app.workers.memory_agent._fire_webhook_sync")
    @patch("app.workers.memory_agent._has_supersedes_link", return_value=False)
    @patch("app.workers.memory_agent._get_neo4j_driver")
    @patch("app.workers.memory_agent._get_qdrant_client")
    @patch("app.workers.memory_agent.get_settings")
    def test_a_confirmed_memory_can_still_supersede_an_unconfirmed_rival(
        self, mock_settings, mock_qdrant, mock_neo4j, mock_link, mock_webhook
    ):
        """The not-over-fixed guard, and the reason the shared scope filter
        must keep admitting confirmed memories.

        A fix that excluded `confirmed_count > 0` from
        `_active_non_corpus_filter` would remove the confirmed memory from the
        similarity QUERY too, so the stale rival would never be matched
        against it and would survive. This double honours the filter, so that
        fix fails here rather than merely reading differently."""
        mock_settings.return_value = _settings()
        client = _FilteringQdrant([
            _mem("human-confirmed", CONFIRMED_TEXT, VEC_A, confirmed=2,
                 timestamp="2026-02-01T00:00:00+00:00"),
            _mem("stale", "the outdated claim", VEC_B),
        ])
        mock_qdrant.return_value = client
        _neo4j_noop(mock_neo4j)

        result = deep_contradiction_pass()

        assert result["contradictions_found"] == 1
        assert result["details"][0]["kept"] == "human-confirmed"
        assert result["details"][0]["superseded"] == "stale"
        assert client.payload_writes == [
            ({"status": "superseded", "superseded_by": "human-confirmed"}, ["stale"]),
        ]

    @patch("app.workers.memory_agent._fire_webhook_sync")
    @patch("app.workers.memory_agent._has_supersedes_link", return_value=False)
    @patch("app.workers.memory_agent._get_neo4j_driver")
    @patch("app.workers.memory_agent._get_qdrant_client")
    @patch("app.workers.memory_agent.get_settings")
    def test_unconfirmed_contradictions_are_unaffected(
        self, mock_settings, mock_qdrant, mock_neo4j, mock_link, mock_webhook
    ):
        """Neither side confirmed: the pass behaves exactly as before, older
        loses on the equal-confidence tiebreak."""
        mock_settings.return_value = _settings()
        client = _FilteringQdrant([
            _mem("older", "the older claim", VEC_A,
                 timestamp="2026-01-01T00:00:00+00:00"),
            _mem("newer", "the newer claim", VEC_B,
                 timestamp="2026-02-01T00:00:00+00:00"),
        ])
        mock_qdrant.return_value = client
        _neo4j_noop(mock_neo4j)

        result = deep_contradiction_pass()

        assert result["contradictions_found"] == 1
        assert result["details"][0]["superseded"] == "older"
        assert client.superseded_ids == {"older"}
