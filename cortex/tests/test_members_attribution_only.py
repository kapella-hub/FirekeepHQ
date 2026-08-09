"""Static invariant: membership is attribution, never runtime metering.

Survivor of the single-product conversion. The deleted
test_entitlement_gates_members_only.py carried three invariants: two guarded the
seat-gate placement and died with the entitlement system; this one — that the
members code never reads runtime identity or liveness — protects the property
the conversion itself declares ("membership is identity and attribution, not
metering", cortex/app/members/api.py) and must outlive it. Wiring presence or
agent identity into member issue/accept would quietly turn attribution back
into runtime metering, the exact thing that was just removed.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_member_code_never_reads_runtime_identity_or_liveness():
    member_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "cortex" / "app" / "members").glob("*.py")
    ).lower()
    for forbidden in ("x-agent-id", "agent_id", "nightshift", "presence"):
        assert forbidden not in member_source, (
            f"members/ reads {forbidden!r} — member records are attribution, "
            f"and runtime identity/liveness must never influence who may join"
        )


def test_no_entitlement_module_returns():
    """The signed-entitlement system stays gone: no module under auth/ or
    cortex/app/ may import a resurrected auth.entitlements."""
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if any(part in {"tests", ".venv", "venv", ".claude", "build"} for part in rel.parts):
            continue
        source = path.read_text(encoding="utf-8", errors="ignore")
        assert "auth.entitlements" not in source, (
            f"{rel.as_posix()} imports auth.entitlements — the entitlement "
            f"system was removed in the single-product conversion"
        )
