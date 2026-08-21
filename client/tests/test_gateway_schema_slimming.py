"""Schema slimming at the gateway — bytes off the wire, nothing off the meaning.

Pydantic renders an optional field ``X | None`` as
``{"anyOf": [{"type": "X"}, {"type": "null"}]}``. That is 43 characters where
JSON Schema's own type-array form, ``{"type": ["X", "null"]}``, is 24 — and the
two accept and reject exactly the same documents. Measured on the live gateway
(2026-08-21): 66 such fields across 98 tools.

The tempting version of this change is to drop the null branch entirely and emit
``{"type": "X"}``, which is shorter still. That one is NOT equivalent — it makes
an explicit ``null`` invalid where the server accepts it — so it is not done, and
``test_collapse_is_semantically_identical`` is the guard that keeps someone from
"optimizing" it later. The whole point of this file is that a token saving which
changes behaviour is not a saving.

Guarded properties:
  - every ``X | None`` field shrinks;
  - the slimmed schema accepts and rejects exactly what the original did,
    proven against a real validator rather than asserted;
  - nothing else in the tool dict is touched — name, description, and every
    other schema keyword survive byte-identical;
  - a schema with no ``anyOf`` is returned unchanged.
"""
from __future__ import annotations

import copy
import json

import pytest

# A REAL validator is the point of this file: the type-array collapse is only
# safe if it validates identically, and asserting that against a hand-rolled
# checker would prove nothing. jsonschema is not a client runtime dep, so the
# stdlib-only `client` job skips; `client-transport` installs it and runs it.
Draft202012Validator = pytest.importorskip("jsonschema").Draft202012Validator

from firekeep_client.gateway import _slim_schema  # noqa: E402


# A field shape lifted verbatim from the live gateway (memory_recall.namespace).
NULLABLE_STRING = {
    "anyOf": [{"type": "string"}, {"type": "null"}],
    "default": None,
    "description": "Optional CATEGORY filter.",
}
# memory_recall.tags — the nested case, where the non-null branch is compound.
NULLABLE_ARRAY = {
    "anyOf": [{"items": {"type": "string"}, "type": "array"}, {"type": "null"}],
    "default": None,
    "description": "Optional filter tags.",
}
# skill_create.step_specs — compound branch carrying its own keywords.
NULLABLE_OBJECT_ARRAY = {
    "anyOf": [
        {"items": {"additionalProperties": True, "type": "object"}, "type": "array"},
        {"type": "null"},
    ],
    "default": None,
}


def _schema(**props):
    return {"type": "object", "properties": props, "additionalProperties": False}


def _size(obj) -> int:
    return len(json.dumps(obj, separators=(",", ":")))


# --------------------------------------------------------------------------- #
# It shrinks                                                                   #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "field",
    [NULLABLE_STRING, NULLABLE_ARRAY, NULLABLE_OBJECT_ARRAY],
    ids=["string", "array", "object-array"],
)
def test_nullable_fields_shrink(field):
    original = _schema(f=field)
    slimmed = _slim_schema(original)
    assert _size(slimmed) < _size(original)
    # The collapsed form is the type-array, never a dropped null branch.
    assert "anyOf" not in slimmed["properties"]["f"]
    assert "null" in slimmed["properties"]["f"]["type"]


def test_measured_saving_is_real_not_rounding():
    """A 10-field schema of the common shape saves a meaningful slice."""
    original = _schema(**{f"f{i}": copy.deepcopy(NULLABLE_STRING) for i in range(10)})
    saved = _size(original) - _size(_slim_schema(original))
    assert saved >= 150, f"expected ~19 chars/field across 10 fields, got {saved}"


# --------------------------------------------------------------------------- #
# It means the same thing — proven, not asserted                               #
# --------------------------------------------------------------------------- #

DOCUMENTS = [
    {},
    {"f": None},
    {"f": "a string"},
    {"f": 42},
    {"f": []},
    {"f": ["a", "b"]},
    {"f": [1, 2]},
    {"f": {"k": "v"}},
    {"f": [{"k": "v"}]},
    {"f": True},
]


@pytest.mark.parametrize(
    "field",
    [NULLABLE_STRING, NULLABLE_ARRAY, NULLABLE_OBJECT_ARRAY],
    ids=["string", "array", "object-array"],
)
@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda d: json.dumps(d))
def test_collapse_is_semantically_identical(field, document):
    """The slimmed schema accepts exactly what the original accepted.

    This is the test that makes the change safe to ship. If someone later
    "optimizes" the collapse into dropping the null branch, `{"f": None}` starts
    failing here and the regression is caught at the point of change.
    """
    original = _schema(f=copy.deepcopy(field))
    slimmed = _slim_schema(copy.deepcopy(original))

    before = Draft202012Validator(original).is_valid(document)
    after = Draft202012Validator(slimmed).is_valid(document)
    assert before == after, (
        f"slimming changed validity of {document!r}: "
        f"original={before}, slimmed={after}"
    )


# --------------------------------------------------------------------------- #
# It touches nothing else                                                      #
# --------------------------------------------------------------------------- #

def test_sibling_keywords_survive():
    slimmed = _slim_schema(_schema(f=copy.deepcopy(NULLABLE_STRING)))["properties"]["f"]
    assert slimmed["default"] is None
    assert slimmed["description"] == "Optional CATEGORY filter."


def test_schema_without_anyof_is_returned_unchanged():
    original = _schema(
        task={"type": "string", "description": "What the agent is trying to do."},
        top_k={"type": "integer", "default": 3},
    )
    assert _slim_schema(copy.deepcopy(original)) == original


def test_multi_branch_anyof_is_left_alone():
    """Only the two-branch X|null shape collapses. A real union is untouched."""
    original = _schema(
        f={"anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}]}
    )
    assert _slim_schema(copy.deepcopy(original)) == original


def test_anyof_without_a_null_branch_is_left_alone():
    original = _schema(f={"anyOf": [{"type": "string"}, {"type": "integer"}]})
    assert _slim_schema(copy.deepcopy(original)) == original


def test_nested_properties_are_slimmed_too():
    """Collapse reaches nested object schemas, not just top-level properties."""
    original = {
        "type": "object",
        "properties": {
            "outer": {
                "type": "object",
                "properties": {"inner": copy.deepcopy(NULLABLE_STRING)},
            }
        },
    }
    slimmed = _slim_schema(original)
    inner = slimmed["properties"]["outer"]["properties"]["inner"]
    assert "anyOf" not in inner
    assert inner["type"] == ["string", "null"]


def test_slimming_never_raises_on_malformed_input():
    """A backend may serve anything. Slimming must degrade to a pass-through.

    The gateway's whole design is failure isolation; a schema it cannot parse
    must reach the model unmodified rather than take the surface down.
    """
    for junk in (None, [], "string", 42, {"properties": "not-a-dict"},
                 {"properties": {"f": {"anyOf": "not-a-list"}}},
                 {"properties": {"f": {"anyOf": [None, {"type": "null"}]}}}):
        _slim_schema(junk)  # must not raise
