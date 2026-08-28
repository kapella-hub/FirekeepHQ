"""Scope must be enforced on the GRAPH leg too, not just the vector leg.

WHY THESE EXIST. `RAGEngine._dual_retrieve` runs two legs. The vector leg
applies `project` and `workspace_id` as hard Qdrant `must` filters. The graph
leg calls `query_related` / `query_related_multihop`, which take a namespace and
NOTHING ELSE — no project, no workspace_id — so a scoped recall was answered by
one leg that honoured the scope and one that ignored it, and every leaked row
arrived tagged `(graph)`.

Proven live through `POST /memory/handoff`: a handoff for
`__no_such_project_xyz` returned HTTP 200 describing ANOTHER project's work
("All 303 Karma tests pass...", "All live validations passed on the corporate
work macOS machine..."), and a handoff for a real project returned 8 results of
which 3 were unrelated. The same hole is a TENANCY leak for `workspace_id`.

Scope is enforced against Qdrant rather than in Cypher because Qdrant is
already the authority the graph leg is checked against for LIFECYCLE (see
`_lifecycle_verdict`), the payload fields are already fetched for that check,
and graph nodes carry no project property to filter on in Cypher at all.
"""

from __future__ import annotations

import pytest

from app.engine.rag import RAGEngine


def _states(**by_id):
    """id -> payload projection, as `get_lifecycle_states` returns it."""
    return {
        mid: {"id": mid, "status": "active", **fields}
        for mid, fields in by_id.items()
    }


class TestScopeVerdict:
    def test_admits_when_nothing_is_scoped(self):
        """An unscoped recall must behave exactly as before this change."""
        assert RAGEngine._scope_verdict(_states(m1={"project": "other"}), ["m1"], None)
        assert RAGEngine._scope_verdict(
            _states(m1={"project": "other"}), ["m1"],
            {"project": None, "workspace_id": None, "namespace": None},
        )

    def test_refuses_a_row_backed_only_by_another_project(self):
        """The live handoff leak, at the unit that decides it."""
        assert not RAGEngine._scope_verdict(
            _states(m1={"project": "otherproj"}), ["m1"], {"project": "myproj"}
        )

    def test_admits_a_row_backed_by_the_declared_project(self):
        assert RAGEngine._scope_verdict(
            _states(m1={"project": "myproj"}), ["m1"], {"project": "myproj"}
        )

    def test_project_comparison_is_case_insensitive(self):
        """`project` is lowercased on write; a caller's casing must not matter,
        or the gate would silently drop that project's own rows."""
        assert RAGEngine._scope_verdict(
            _states(m1={"project": "myproj"}), ["m1"], {"project": "MyProj"}
        )

    def test_one_in_scope_memory_is_enough(self):
        """A graph node carries the ids of EVERY memory that mentioned it.

        Requiring all of them would drop shared entities ("docker", "redis")
        from every scoped recall — the node's description is legitimately in
        scope if any backing memory is.
        """
        assert RAGEngine._scope_verdict(
            _states(m1={"project": "other"}, m2={"project": "myproj"}),
            ["m1", "m2"], {"project": "myproj"},
        )

    def test_workspace_mismatch_is_refused(self):
        """Tenancy: the vector leg treats workspace_id as a hard `must`."""
        assert not RAGEngine._scope_verdict(
            _states(m1={"workspace_id": "workspace-b"}), ["m1"],
            {"workspace_id": "workspace-a"},
        )

    def test_missing_namespace_counts_as_default(self):
        """Mirrors vector.namespace_condition's IsEmpty arm — points written
        before the field existed belong to the default namespace, and refusing
        them would trade a leak for data loss."""
        assert RAGEngine._scope_verdict(
            _states(m1={}), ["m1"], {"namespace": "default"}
        )
        assert not RAGEngine._scope_verdict(
            _states(m1={}), ["m1"], {"namespace": "tenant-b"}
        )

    def test_unlinked_rows_stay_recallable(self):
        """Sleep-cycle and legacy graph-owned knowledge has no vector record.

        This is the honest limit of the gate and it matches the lifecycle
        contract exactly, so the two gates cannot disagree about what
        "unverifiable" means.
        """
        assert RAGEngine._scope_verdict(_states(), [], {"project": "myproj"})

    def test_lookup_unavailable_admits(self):
        """The graph leg is the one leg that still works when Qdrant is down;
        an outage must not silently empty recall."""
        assert RAGEngine._scope_verdict(None, ["m1"], {"project": "myproj"})

    def test_dangling_link_is_refused(self):
        """Agrees with `_lifecycle_verdict`, which already refuses this."""
        assert not RAGEngine._scope_verdict(
            _states(other={"project": "myproj"}), ["gone"], {"project": "myproj"}
        )

    def test_legacy_unscoped_row_is_denied_even_when_scope_matches(self):
        """Identity-v2 D4: a chain node the migration flagged
        legacy_unscoped MERGEd across workspaces before scoping existed, so
        coincidental agreement with the declared scope does not rescue it —
        unlike an ordinary in-scope row, which is admitted."""
        assert not RAGEngine._scope_verdict(
            _states(m1={"project": "myproj"}), ["m1"], {"project": "myproj"},
            legacy_unscoped=True,
        )

    def test_legacy_unscoped_row_is_denied_even_when_nothing_is_scoped(self):
        """Permanent quarantine, not a scope-conditional gate: an unscoped
        recall (the branch that admits everything else) must not wave it
        through either."""
        assert not RAGEngine._scope_verdict(
            _states(m1={"project": "myproj"}), ["m1"], None,
            legacy_unscoped=True,
        )
        assert not RAGEngine._scope_verdict(_states(), [], None, legacy_unscoped=True)

    def test_legacy_unscoped_defaults_false_and_does_not_affect_ordinary_rows(self):
        """Every pre-existing call site is unaffected — the parameter is
        opt-in and only the migration will ever set it True."""
        assert RAGEngine._scope_verdict(
            _states(m1={"project": "myproj"}), ["m1"], {"project": "myproj"},
        )


class TestScopeIsApplied:
    @pytest.mark.asyncio
    async def test_verify_graph_lifecycle_drops_out_of_project_rows(self):
        """The integration point: a scoped recall must not render the row."""

        class _Vector:
            async def get_lifecycle_states(self, ids):
                return _states(
                    mine={"project": "myproj"}, theirs={"project": "otherproj"}
                )

        engine = RAGEngine.__new__(RAGEngine)
        engine._vector = _Vector()

        entries = [
            {"content": "mine", "metadata": {"memory_ids": ["mine"]}},
            {"content": "theirs", "metadata": {"memory_ids": ["theirs"]}},
        ]
        kept = await engine._verify_graph_lifecycle(
            entries, include_archived=False, scope={"project": "myproj"},
        )
        assert [e["content"] for e in kept] == ["mine"]

    @pytest.mark.asyncio
    async def test_streaming_path_applies_the_same_gate(self):
        """SSE recall must not be a way around the scope.

        The streaming path already re-applies the lifecycle gate for exactly
        this reason; leaving scope off it would have made `format=stream` the
        documented bypass.
        """

        class _Vector:
            async def get_lifecycle_states(self, ids):
                return _states(
                    mine={"workspace_id": "ws-a"}, theirs={"workspace_id": "ws-b"}
                )

        engine = RAGEngine.__new__(RAGEngine)
        engine._vector = _Vector()

        rows = [
            {"name": "a", "description": "mine", "memory_ids": ["mine"]},
            {"name": "b", "description": "theirs", "memory_ids": ["theirs"]},
        ]
        kept = await engine._filter_graph_rows(
            rows, include_archived=False, scope={"workspace_id": "ws-a"},
        )
        assert [r["name"] for r in kept] == ["a"]

    @pytest.mark.asyncio
    async def test_verify_graph_lifecycle_drops_legacy_unscoped_rows(self):
        """Identity-v2 D4, the integration point: a row whose backing chain
        node was migration-stamped legacy_unscoped must not render, even
        though its memory_ids resolve perfectly in-scope."""

        class _Vector:
            async def get_lifecycle_states(self, ids):
                return _states(mine={"project": "myproj"})

        engine = RAGEngine.__new__(RAGEngine)
        engine._vector = _Vector()

        entries = [
            {"content": "mine", "metadata": {"memory_ids": ["mine"]}},
            {
                "content": "collided",
                "metadata": {"memory_ids": ["mine"], "legacy_unscoped": True},
            },
        ]
        kept = await engine._verify_graph_lifecycle(
            entries, include_archived=False, scope={"project": "myproj"},
        )
        assert [e["content"] for e in kept] == ["mine"]

    @pytest.mark.asyncio
    async def test_streaming_path_drops_legacy_unscoped_rows(self):
        """Same gate, same reason, on the SSE path — leaving it off would
        make `format=stream` a documented bypass around D4's quarantine."""

        class _Vector:
            async def get_lifecycle_states(self, ids):
                return _states(mine={"workspace_id": "ws-a"})

        engine = RAGEngine.__new__(RAGEngine)
        engine._vector = _Vector()

        rows = [
            {"name": "a", "description": "mine", "memory_ids": ["mine"]},
            {
                "name": "b",
                "description": "collided",
                "memory_ids": ["mine"],
                "legacy_unscoped": True,
            },
        ]
        kept = await engine._filter_graph_rows(
            rows, include_archived=False, scope={"workspace_id": "ws-a"},
        )
        assert [r["name"] for r in kept] == ["a"]
