"""auth.principal.owns_session — the one session-ownership rule (THREAT-MODEL §5.16).

Bridge's ``session_owned_by`` and Cortex's session-owner resolver both call it;
these tests pin the rule itself so the two services cannot drift apart.
"""

from __future__ import annotations

import pytest

from auth.principal import owns_session

WS = "workspace-local"
OWNER = "member-owner"


@pytest.fixture(autouse=True)
def deployment_ids(monkeypatch):
    monkeypatch.setenv("FIREKEEP_WORKSPACE_ID", WS)
    monkeypatch.setenv("FIREKEEP_OWNER_MEMBER_ID", OWNER)


def test_bound_session_belongs_to_its_member_in_its_workspace():
    assert owns_session("member-alice", WS, member_id="member-alice", workspace_id=WS)
    assert not owns_session("member-alice", WS, member_id="member-bob", workspace_id=WS)
    assert not owns_session(
        "member-alice", WS, member_id="member-alice", workspace_id="workspace-other")


def test_session_bound_before_owner_workspace_matches_on_member_alone():
    assert owns_session("member-alice", "", member_id="member-alice", workspace_id="workspace-other")


def test_legacy_session_belongs_to_the_deployment_owner_only():
    assert owns_session("", "", member_id=OWNER, workspace_id=WS)
    assert not owns_session("", "", member_id="member-bob", workspace_id=WS)
    assert not owns_session("", "", member_id=OWNER, workspace_id="workspace-other")


def test_no_member_owns_nothing():
    assert not owns_session("member-alice", WS, member_id="", workspace_id=WS)
    assert not owns_session("", "", member_id=None, workspace_id=WS)
