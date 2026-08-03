"""Static invariant: entitlement checks may meter only member creation."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_device_enrollment_has_no_entitlement_or_seat_check():
    enrollment_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "cortex" / "app" / "enroll").glob("*.py")
    ).lower()
    assert "load_entitlement" not in enrollment_source
    assert "max_members" not in enrollment_source
    assert "seatlimit" not in enrollment_source


def test_member_seat_code_never_reads_runtime_identity_or_liveness():
    member_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "cortex" / "app" / "members").glob("*.py")
    ).lower()
    for forbidden in ("x-agent-id", "agent_id", "nightshift", "presence"):
        assert forbidden not in member_source


def test_entitlement_imports_are_confined_to_membership_and_read_only_surfaces():
    consumers = []
    for path in ROOT.rglob("*.py"):
        # `.claude` covers worktrees — nested checkouts carry their own copies
        # of every consumer and would double-count them on any machine with an
        # active worktree.
        if any(part in {"tests", ".venv", "venv", ".claude"} for part in path.parts):
            continue
        source = path.read_text(encoding="utf-8", errors="ignore")
        if "auth.entitlements" in source:
            consumers.append(path.relative_to(ROOT).as_posix())
    assert set(consumers) == {
        "cortex/app/briefing/api.py",
        "cortex/app/members/api.py",
        "cortex/app/members/store.py",
    }
