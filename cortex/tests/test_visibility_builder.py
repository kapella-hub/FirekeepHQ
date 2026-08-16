"""The one builder every member-principal egress consumes (spec §4.4).

Pure condition construction — no I/O. The shape tests pin the exact
Qdrant condition tree so an egress caller can trust 'apply the builder'
means the same thing everywhere.
"""
from qdrant_client.models import FieldCondition, IsEmptyCondition

from app.db.visibility import GENERATION_GUARD, visibility_should


def _match(cond):
    return (cond.key, cond.match.value)


def test_member_caller_gets_three_branches():
    conds = visibility_should("mem-alice")
    # absent visibility (legacy points), workspace, own-member
    assert any(isinstance(c, IsEmptyCondition) and c.is_empty.key == "visibility"
               for c in conds)
    flat = [_match(c) for c in conds if isinstance(c, FieldCondition)]
    assert ("visibility", "workspace") in flat
    # the member branch is a nested Filter: visibility==member AND member_id==caller
    nested = [c for c in conds if hasattr(c, "must")]
    assert len(nested) == 1
    pair = sorted(_match(c) for c in nested[0].must)
    assert pair == [("member_id", "mem-alice"), ("visibility", "member")]


def test_no_member_identity_fails_closed():
    conds = visibility_should(None)
    assert not [c for c in conds if hasattr(c, "must")], (
        "no member identity must mean NO private-chunk branch")
    conds_empty = visibility_should("")
    assert not [c for c in conds_empty if hasattr(c, "must")]


def test_generation_guard_excludes_uncommitted():
    assert GENERATION_GUARD.key == "committed"
    assert GENERATION_GUARD.match.value is False
