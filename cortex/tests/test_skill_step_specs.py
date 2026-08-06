"""Step specs round-trip through create, PATCH and the response projection.

Load-bearing detail: SkillRequest has no model_config, so pydantic's default
extra='ignore' silently DROPS an unknown field. A spec sent to a server without
this task's change is accepted with a 201 and lost. These tests are what make
that impossible to ship twice.

Fixtures are IMPORTED from test_skill_api rather than rebuilt: `_make_app` is
the app the existing skills surface is tested against, and a second, subtly
different app would let this file pass while the real router regressed.
"""
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.procedures.models import StepSpec
from tests.test_skill_api import (  # noqa: F401 — pytest fixtures, reused not redefined
    _make_app,
    _make_mock_point,
    mock_settings,
    mock_vector,
)


def test_a_file_glob_spec_requires_a_pattern():
    with pytest.raises(ValueError):
        StepSpec(text="regen the lock", kind="file_glob", pattern="")


def test_an_unobservable_spec_needs_no_pattern():
    s = StepSpec(text="ask the customer to confirm")
    assert s.kind == "unobservable"
    assert s.pattern == ""


def test_a_blank_id_is_minted_and_a_supplied_id_is_kept():
    minted = StepSpec(text="a")
    assert minted.id and len(minted.id) >= 8
    kept = StepSpec(id="fixed-id", text="a")
    assert kept.id == "fixed-id"


def test_create_persists_specs_and_the_response_echoes_them(mock_vector, mock_settings):
    mock_vector._client.upsert = AsyncMock()
    client = TestClient(_make_app(mock_vector, mock_settings))
    body = {
        "trigger": "publishing a client release",
        "symptoms": "teammates get a stale wheel",
        "steps": "1. bump the version\n2. bump the bundled symdex wheel",
        "step_specs": [
            {"text": "bump the version", "kind": "file_glob",
             "pattern": "client/pyproject.toml", "load_bearing": False},
            {"text": "bump the bundled symdex wheel", "kind": "file_glob",
             "pattern": "client/bootstrap/*", "load_bearing": True},
        ],
    }
    resp = client.post("/skills", json=body)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert len(data["step_specs"]) == 2
    assert all(s["id"] for s in data["step_specs"])

    written = mock_vector._client.upsert.call_args.kwargs["points"][0]
    assert len(written.payload["step_specs"]) == 2
    assert written.payload["step_specs"][1]["load_bearing"] is True


def test_create_without_specs_writes_no_spec_key(mock_vector, mock_settings):
    """Absent specs must not become an empty list on the point: `step_specs`
    present-and-empty and absent are the same to a reader, but only one of them
    is what the author said."""
    mock_vector._client.upsert = AsyncMock()
    client = TestClient(_make_app(mock_vector, mock_settings))
    resp = client.post("/skills", json={
        "trigger": "t", "symptoms": "s", "steps": "x", "gotchas": "",
    })
    assert resp.status_code == 201
    written = mock_vector._client.upsert.call_args.kwargs["points"][0]
    assert "step_specs" not in written.payload
    assert resp.json()["step_specs"] is None


def _patchable(mock_vector, point):
    """retrieve + a set_payload that actually mutates, so the PATCH response
    (which re-fetches the point) reflects the write instead of echoing the
    pre-PATCH payload back."""
    mock_vector._client.retrieve = AsyncMock(return_value=[point])

    async def _set_payload(*, collection_name, payload, points):
        point.payload.update(payload)

    mock_vector._client.set_payload = AsyncMock(side_effect=_set_payload)
    return mock_vector


def test_patch_replaces_the_spec_list_wholesale(mock_vector, mock_settings):
    point = _make_mock_point(skill_id="skill-1")
    point.payload["step_specs"] = [
        {"id": "old-1", "text": "one", "kind": "file_glob",
         "pattern": "*.py", "load_bearing": True},
        {"id": "old-2", "text": "two", "kind": "unobservable",
         "pattern": "", "load_bearing": False},
    ]
    _patchable(mock_vector, point)
    client = TestClient(_make_app(mock_vector, mock_settings))

    resp = client.patch(
        "/skills/skill-1",
        json={"step_specs": [{"text": "only step", "kind": "unobservable"}]},
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["step_specs"]) == 1
    assert resp.json()["step_specs"][0]["text"] == "only step"


def test_a_spec_edit_keeps_the_ids_of_the_steps_it_did_not_change(
        mock_vector, mock_settings):
    """The design pins `id` as "minted server-side if absent, STABLE thereafter",
    and every id is the key an execution's evidence is filed under.

    Nothing on the agent path can hold ids still: `skill_add_step_specs`'
    documented entry shape has no `id`, no MCP surface ever RETURNS one, and the
    same docstring tells the caller to resend the whole list to add one step. So
    every wording fix re-keyed all five steps, orphaning the procedure's entire
    recorded history — and the nightly pass then read each stored execution as a
    skip of every current step.
    """
    point = _make_mock_point(skill_id="skill-1")
    point.payload["step_specs"] = [
        {"id": "keep-me", "text": "bump the version", "kind": "file_glob",
         "pattern": "client/pyproject.toml", "load_bearing": True},
        {"id": "retired", "text": "tag the release", "kind": "unobservable",
         "pattern": "", "load_bearing": False},
    ]
    _patchable(mock_vector, point)
    client = TestClient(_make_app(mock_vector, mock_settings))

    resp = client.patch("/skills/skill-1", json={"step_specs": [
        {"text": "bump the version", "kind": "file_glob",
         "pattern": "client/pyproject.toml", "load_bearing": True},
        {"text": "push the signed tag", "kind": "unobservable"},
    ]})
    assert resp.status_code == 200, resp.text
    specs = resp.json()["step_specs"]
    assert specs[0]["id"] == "keep-me"
    # A step whose TEXT changed is a different step: its evidence should not
    # follow, and it must not inherit the retired step's id either.
    assert specs[1]["id"] not in {"keep-me", "retired"}


def test_an_explicit_id_still_wins_over_the_text_match(mock_vector, mock_settings):
    point = _make_mock_point(skill_id="skill-1")
    point.payload["step_specs"] = [
        {"id": "old", "text": "one", "kind": "unobservable", "pattern": "",
         "load_bearing": False},
    ]
    _patchable(mock_vector, point)
    client = TestClient(_make_app(mock_vector, mock_settings))
    resp = client.patch("/skills/skill-1", json={
        "step_specs": [{"id": "chosen", "text": "one", "kind": "unobservable"}],
    })
    assert resp.json()["step_specs"][0]["id"] == "chosen"


def test_specs_are_not_a_semantic_field(mock_vector, mock_settings):
    """Changing specs must NOT re-embed: specs are metadata about the steps,
    not the skill's meaning, and a needless embed on every spec edit puts an
    embedding-backend outage in the write path."""
    point = _make_mock_point(skill_id="skill-1")
    _patchable(mock_vector, point)
    mock_vector._client.upsert = AsyncMock()
    client = TestClient(_make_app(mock_vector, mock_settings))

    resp = client.patch(
        "/skills/skill-1",
        json={"step_specs": [{"text": "s", "kind": "unobservable"}]},
    )
    assert resp.status_code == 200, resp.text
    mock_vector._embed.assert_not_called()
    mock_vector._client.upsert.assert_not_awaited()
    mock_vector._client.set_payload.assert_awaited_once()


def test_more_than_the_cap_is_rejected(mock_vector, mock_settings):
    client = TestClient(_make_app(mock_vector, mock_settings))
    specs = [{"text": f"step {i}", "kind": "unobservable"} for i in range(51)]
    resp = client.post("/skills", json={
        "trigger": "t", "symptoms": "s", "steps": "x", "step_specs": specs,
    })
    assert resp.status_code == 422


def test_more_than_the_cap_is_rejected_on_patch_too(mock_vector, mock_settings):
    point = _make_mock_point(skill_id="skill-1")
    _patchable(mock_vector, point)
    client = TestClient(_make_app(mock_vector, mock_settings))
    specs = [{"text": f"step {i}", "kind": "unobservable"} for i in range(51)]
    resp = client.patch("/skills/skill-1", json={"step_specs": specs})
    assert resp.status_code == 422
