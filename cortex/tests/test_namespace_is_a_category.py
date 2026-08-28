"""Namespace is a CATEGORY, not a tenant — and the choice is pinned here.

THE DEFECT CHAIN, in order, because each fix created the next problem:

1. `db/vector.py` read `if namespace != "default":` before appending its
   namespace clause, so a recall scoped to `default` applied NO filter and
   matched every namespace while the response echoed `namespace: "default"`.
   Real, and worth fixing.
2. The fix made the clause unconditional while the client kit still sent the
   literal `"default"` on every recall. MEASURED on the live store
   (2026-08-06): recallable memories in the owner's workspace went 4340 -> 4194.
   146 memories — 129 of them `status=active`, several written that week —
   became invisible to every recall the product actually makes.
3. The guidance that CREATES those memories ships unchanged: the root
   `CLAUDE.md` and `mcp_server.vault_store`'s docstring both tell agents to
   store operational facts under `namespace="infrastructure"`.

THE CHOICE. `workspace_id` is the tenancy boundary: applied as a hard `must` in
the same filter and DERIVED FROM THE VERIFIED PRINCIPAL. `namespace` is a free
string on the request body that any caller may set to anything, so it isolates
nothing and never did. Live evidence: all 4347 points carry ONE `workspace_id`,
while `namespace` holds 18 values that read as topics (`infrastructure`,
`engineering`, `product`, `research`, `team`, `architecture`, `strategy`,
`release_operations`) plus historical service names. So: category.

THE CONTRACT these tests pin:
  * unspecified (`None`) -> no namespace clause; every category in the workspace
  * any string, INCLUDING `"default"` -> exactly that category
  * `"default"` additionally matches points with NO namespace field (legacy)
  * no caller sends the literal `"default"` as a stand-in for "unspecified"

LIVE VERIFICATION of the whole thing, run against the owner's real 4347-memory
store, filters transcribed from `search()`:

    old shipped   (namespace='default' -> wildcard): 4340
    fix round 1   (namespace='default' -> equality): 4194   <- 146 lost
    NEW unscoped  (namespace omitted -> no clause) : 4340   <- parity restored
    NEW explicit  (namespace='default')            : 4194
    NEW explicit  (namespace='infrastructure')     :   14

and end-to-end with the deployment's own embedding model, query
"where is my VPS deployed and how do I reach it":

    fix round 1 : rank 1 = 0.6524 ns='default'  (a tangential install memory);
                  the VPS access-details memory does not appear at all
    NEW unscoped: rank 1 = 0.6886 ns='infrastructure'
                  (the memory recording the owner's VPS access details)
"""

from __future__ import annotations

import inspect

from qdrant_client.models import FieldCondition, Filter

from app.db.vector import namespace_condition
from app.models import ContextQuery, RecallResponse


def _keys(clause) -> set[str]:
    """Namespace values a clause matches, flattening the should-arm."""
    if isinstance(clause, Filter):
        return {c.match.value for c in (clause.should or [])
                if isinstance(c, FieldCondition) and c.match is not None}
    return {clause.match.value}


class TestNamespaceCondition:
    def test_none_applies_no_clause(self):
        """Unspecified means every namespace in the workspace, not one of them."""
        assert namespace_condition(None) is None

    def test_default_is_scoped_like_any_other_namespace(self):
        """The original defect: `"default"` must NOT be a wildcard."""
        clause = namespace_condition("default")
        assert clause is not None
        assert "default" in _keys(clause)

    def test_default_also_matches_points_with_no_namespace_field(self):
        """Legacy points predate the field; Qdrant will not match a missing key
        against MatchValue("default"), so the should-arm carries an IsEmpty."""
        clause = namespace_condition("default")
        assert isinstance(clause, Filter)
        assert clause.should is not None and len(clause.should) == 2
        kinds = {type(c).__name__ for c in clause.should}
        assert "IsEmptyCondition" in kinds

    def test_a_named_namespace_is_an_exact_match_and_nothing_else(self):
        clause = namespace_condition("infrastructure")
        assert isinstance(clause, FieldCondition)
        assert clause.key == "namespace"
        assert clause.match.value == "infrastructure"

    def test_default_and_named_are_different_clauses(self):
        """If these ever collapse, either `default` is a wildcard again or a
        named namespace silently picks up field-less points."""
        assert namespace_condition("default") != namespace_condition("engineering")


class TestRecallDefaultsToUnscoped:
    def test_context_query_namespace_defaults_to_none(self):
        assert ContextQuery(task="t").namespace is None

    def test_an_explicit_default_survives_validation(self):
        """Scoping to the default CATEGORY has to remain expressible."""
        assert ContextQuery(task="t", namespace="default").namespace == "default"

    def test_recall_response_does_not_invent_default(self):
        assert RecallResponse(context_block="", sources=[], score=0.0).namespace is None


class TestNoCallerSendsDefaultAsAStandIn:
    """The model change alone is not the fix — it only helps if callers stop
    sending the literal string. Each of these sent `"default"` and would have
    kept the 146 hidden on its own path."""

    def test_mcp_memory_recall_defaults_to_none_and_omits_the_key(self):
        from app import mcp_server

        sig = inspect.signature(mcp_server.memory_recall)
        assert sig.parameters["namespace"].default is None

        src = inspect.getsource(mcp_server.memory_recall)
        assert '"namespace": namespace' not in src, (
            "memory_recall must not put namespace in the body unconditionally"
        )
        assert "if namespace is not None:" in src

    def test_mcp_handoff_helper_sends_no_namespace(self):
        from app import mcp_server

        src = inspect.getsource(mcp_server.memory_handoff)
        assert '"namespace": "default"' not in src

    def test_rag_reads_namespace_with_a_none_fallback(self):
        """`getattr(query, "namespace", "default")` would re-scope every recall
        from a query object that predates the field."""
        import app.engine.rag as rag

        src = inspect.getsource(rag)
        assert 'getattr(query, "namespace", "default")' not in src
        assert 'getattr(query, "namespace", None)' in src


class TestGraphLegAcceptsNone:
    """The graph leg's own clause is `if namespace and namespace != "default"`.

    `None` must not fall into the named-namespace arm — that would build a
    Cypher filter for a `Namespace {name: null}` node and empty the graph leg on
    every recall. `"default"` deliberately stays unscoped there; see
    `Neo4jClient.query_related`'s docstring for the measurement (118 of 5459
    graph nodes are reachable from ANY Namespace node).
    """

    def test_none_and_default_both_skip_the_cypher_clause(self):
        import app.db.graph as graph

        src = inspect.getsource(graph)
        assert 'if namespace != "default":' not in src, (
            "a bare != check treats None as a named namespace"
        )
        assert src.count('if namespace and namespace != "default":') == 4


class TestWritePathIsScopedBothWays:
    def test_similarity_filter_scopes_default_too(self):
        """A `default` write used to be able to supersede an `infrastructure`
        memory while an `infrastructure` write could not see a `default`
        near-duplicate. Scoping both ways only narrows supersession."""
        from app.db.vector import _similarity_filter

        f = _similarity_filter("default", None, workspace_id="ws-1")
        assert any(isinstance(c, Filter) for c in f.must), (
            "the default namespace must contribute a clause on the write path"
        )
