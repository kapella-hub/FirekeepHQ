"""The handoff graph leak: what is actually closed, and what is only mitigated.

THE ORIGINAL DEFECT. `query_related` / `query_related_multihop` take a namespace
and NOTHING else — no project, no workspace_id. So `POST /memory/handoff`, whose
whole output is one narrative asserting "this is project X's work", was answered
by a vector leg that honoured the project and a graph leg that ignored it.

THE FIRST FIX WAS LABELLED FIXED AND CLOSED ~1.5%. `_scope_verdict` can only
adjudicate a graph row that names vector `memory_ids`, and it admitted any row
that named none. MEASURED on the live Neo4j 2026-08-06:

    non-Namespace nodes : 3859   (Outcome 2608, Concept 1480, Action 826,
                                  Resolution 425, Domain 120, MemoryRef 36)
    carrying memory_ids :   27   (Action 12, Outcome 12, Resolution 3) = 0.7%

so 99.3% of graph rows were waved through verbatim, including the exact row from
the original probe. The short-circuit added alongside it ("no contributors and
no sources -> say so") could therefore never fire either: `sources` was never
empty.

WHY A CYPHER-SIDE FILTER IS NOT THE SMALLER FIX. Measured property census on the
live graph: `Action`, `Outcome`, `Resolution` and `Concept` nodes carry exactly
`id` and `description`. No project, no workspace_id, no namespace. Scoping the
query itself is a schema change plus a backfill of 5459 nodes, not a filter.

WHAT SHIPS INSTEAD. `unattributed` is a per-caller policy:

  * `/memory/recall` keeps `"admit"`. `workspace_id` is non-None on every
    authenticated recall, so a blanket deny would drop 99.3% of graph rows from
    every request in the system — that is deleting the graph leg, not scoping
    it. This is a STATED PARTIAL MITIGATION, with the residual measured above.
  * `/memory/handoff` uses `"deny"`. It emits one assertion about one project,
    not a ranked list, and an LLM handed an unattributable row folds it in
    indistinguishably. Refusing the unverifiable is the whole job here, and it
    costs only rows that could never have been attributed anyway.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.engine.rag import RAGEngine, _state_in_scope

SCOPE = {"project": "firekeep", "workspace_id": "ws-1", "namespace": None}


def _request():
    """The minimum `post_memory_handoff` touches: `.scope` and `.app.state`."""
    return SimpleNamespace(
        scope={}, headers={},
        app=SimpleNamespace(state=SimpleNamespace(vector_client=None)),
    )

STATES = {
    "in": {"project": "firekeep", "workspace_id": "ws-1", "namespace": "default"},
    "other-project": {"project": "timegrapher", "workspace_id": "ws-1", "namespace": "default"},
    "other-tenant": {"project": "firekeep", "workspace_id": "ws-2", "namespace": "default"},
}


def verdict(memory_ids, *, unattributed="admit", scope=SCOPE, states=STATES, legacy_unscoped=False):
    return RAGEngine._scope_verdict(
        states, memory_ids, scope, unattributed=unattributed,
        legacy_unscoped=legacy_unscoped,
    )


class TestSharedRules:
    def test_no_scope_declared_admits_everything(self):
        assert verdict([], scope=None) is True
        assert verdict([], scope={"project": None, "workspace_id": None, "namespace": None}) is True

    def test_a_qdrant_outage_admits_rather_than_emptying_recall(self):
        """Both policies. An outage is not evidence about scope, and the graph
        leg is the one leg that survives it."""
        assert verdict(["in"], states=None) is True
        assert verdict(["in"], states=None, unattributed="deny") is True
        assert verdict([], states=None, unattributed="deny") is True

    def test_an_out_of_project_row_is_denied_under_both_policies(self):
        assert verdict(["other-project"]) is False
        assert verdict(["other-project"], unattributed="deny") is False

    def test_a_cross_tenant_row_is_denied_under_both_policies(self):
        assert verdict(["other-tenant"]) is False
        assert verdict(["other-tenant"], unattributed="deny") is False

    def test_one_in_scope_memory_is_enough(self):
        """A shared entity like "docker" carries every memory that mentioned it;
        requiring all of them would drop it from every scoped recall."""
        assert verdict(["other-project", "in"]) is True
        assert verdict(["other-project", "in"], unattributed="deny") is True

    def test_a_dangling_link_is_denied_so_the_two_gates_agree(self):
        assert verdict(["nonexistent"]) is False


class TestTheResidualIsRealAndDeliberate:
    def test_recall_admits_an_unattributed_row(self):
        """This is the 99.3% — measured, stated, and NOT called fixed."""
        assert verdict([]) is True

    def test_handoff_denies_an_unattributed_row(self):
        assert verdict([], unattributed="deny") is False

    def test_deny_is_not_the_default(self):
        """Flipping the default would drop 99.3% of graph rows from every
        request in the system, because workspace_id is always declared."""
        sig = inspect.signature(RAGEngine._scope_verdict)
        assert sig.parameters["unattributed"].default == "admit"
        assert inspect.signature(RAGEngine.recall).parameters[
            "unattributed_graph"].default == "admit"


class TestHandoffWiring:
    """Behavioural, not textual — this is the load-bearing wiring."""

    @pytest.mark.asyncio
    async def test_the_endpoint_asks_recall_for_the_strict_policy(self):
        import app.main as main
        from app.models import HandoffRequest

        seen: dict = {}

        class _Engine:
            async def recall(self, query, **kw):
                seen.update(kw)
                seen["project"] = query.project
                seen["namespace"] = query.namespace
                return SimpleNamespace(sources=[], context_block="")

        request = _request()
        with patch.object(main, "get_memory_contributors",
                          new=AsyncMock(return_value=[])):
            out = await main.post_memory_handoff(
                request, HandoffRequest(project="firekeep", since_days=7), _Engine()
            )

        assert seen["unattributed_graph"] == "deny", (
            "a handoff must refuse graph rows it cannot attribute to the project"
        )
        assert "workspace_id" in seen, "the handoff must declare the caller's tenancy"
        assert seen["project"] == "firekeep"
        # And with nothing to hand off it says so rather than narrating.
        assert out["empty"] is True
        assert "No memories found" in out["summary"]

    @pytest.mark.asyncio
    async def test_the_empty_answer_is_reachable_only_because_of_deny(self):
        """With unattributed rows admitted, `sources` was never empty, so the
        short-circuit added alongside the first fix could never fire."""
        import app.main as main
        from app.models import HandoffRequest

        class _Engine:
            async def recall(self, query, **kw):
                # What the ADMIT policy would have produced: an unattributable
                # graph row surviving for a project that does not exist.
                return SimpleNamespace(
                    sources=[SimpleNamespace(store="graph", content="other work",
                                             score=1.0, metadata={})],
                    context_block="other work",
                )

        request = _request()
        with patch.object(main, "get_memory_contributors",
                          new=AsyncMock(return_value=[])),              patch.object(main, "synthesize_memories",
                          new=AsyncMock(return_value="a narrative")):
            out = await main.post_memory_handoff(
                request, HandoffRequest(project="__no_such_project", since_days=7),
                _Engine(),
            )
        assert out.get("empty") is not True, (
            "this documents the pre-fix behaviour the deny policy prevents"
        )


class TestLegacyUnscopedIsPermanentQuarantine:
    """Identity-v2 D4: the graph analogue of vector quarantine. Denied under
    BOTH policies — unlike an ordinary unattributed row, which recall admits
    and only handoff denies, `legacy_unscoped` is denied by both because the
    NODE's own identity is untrustworthy, not merely unattributed."""

    def test_denied_under_admit(self):
        assert verdict(["in"], legacy_unscoped=True) is False

    def test_denied_under_deny(self):
        assert verdict(["in"], unattributed="deny", legacy_unscoped=True) is False

    def test_denied_even_when_fully_in_scope(self):
        """Being backed by an in-scope memory does not rescue it, unlike an
        ordinary row (test_one_in_scope_memory_is_enough above)."""
        assert verdict(["in", "in"], legacy_unscoped=True) is False
        assert verdict(["in", "in"], unattributed="deny", legacy_unscoped=True) is False

    def test_denied_even_under_a_qdrant_outage(self):
        """Unlike an ordinary row (test_a_qdrant_outage_admits_rather_than_
        emptying_recall above), an outage does not rescue a legacy_unscoped
        row either — the denial is about the node's own identity, not about
        whether its backing memories could be resolved."""
        assert verdict(["in"], states=None, legacy_unscoped=True) is False


class TestStateInScope:
    def test_project_comparison_is_case_insensitive(self):
        assert _state_in_scope({"project": "FireKeep"}, {"project": "firekeep"}) is True

    def test_a_project_less_memory_matches_no_declared_project(self):
        assert _state_in_scope({"project": None}, {"project": "firekeep"}) is False

    def test_workspace_is_exact(self):
        assert _state_in_scope({"workspace_id": "ws-1"}, {"workspace_id": "ws-1"}) is True
        assert _state_in_scope({"workspace_id": "ws-2"}, {"workspace_id": "ws-1"}) is False

    def test_an_absent_namespace_counts_as_default(self):
        """Mirrors `namespace_condition`'s IsEmpty arm — legacy points predate
        the field and refusing them would trade a leak for data loss."""
        assert _state_in_scope({}, {"namespace": "default"}) is True
        assert _state_in_scope({}, {"namespace": "infrastructure"}) is False


class TestLifecycleFilterThreadsThePolicy:
    @pytest.mark.asyncio
    async def test_verify_graph_lifecycle_honours_deny(self):
        engine = RAGEngine.__new__(RAGEngine)

        async def _resolve(_ids):
            return STATES

        engine._resolve_lifecycle = _resolve  # type: ignore[method-assign]

        rows = [
            {"content": "linked, in scope", "metadata": {"memory_ids": ["in"]}},
            {"content": "unattributed", "metadata": {}},
        ]
        kept_admit = await engine._verify_graph_lifecycle(
            [dict(r, metadata=dict(r["metadata"])) for r in rows], scope=SCOPE
        )
        kept_deny = await engine._verify_graph_lifecycle(
            [dict(r, metadata=dict(r["metadata"])) for r in rows],
            scope=SCOPE, unattributed="deny",
        )
        assert {r["content"] for r in kept_admit} == {"linked, in scope", "unattributed"}
        assert {r["content"] for r in kept_deny} == {"linked, in scope"}

    @pytest.mark.asyncio
    async def test_the_streaming_gate_takes_the_same_policy(self):
        """Leaving it off the SSE path would make `format=stream` the
        documented bypass."""
        engine = RAGEngine.__new__(RAGEngine)

        async def _resolve(_ids):
            return STATES

        engine._resolve_lifecycle = _resolve  # type: ignore[method-assign]

        rows = [{"description": "unattributed"}]
        assert await engine._filter_graph_rows(rows, scope=SCOPE) == rows
        assert await engine._filter_graph_rows(
            rows, scope=SCOPE, unattributed="deny") == []
