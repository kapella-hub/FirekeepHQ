"""Dashboard Relay tab — Approve / Deny for `hands_permit:` tasks.

Firekeep Hands posts a relay task titled `hands_permit:<challenge>` when a
protected step needs a human's phone approval. The approval broker polls the
task: `completed` with a result starting `approve` means approve;
`cancelled`/`failed` (or `completed` with any other result) means deny. This
pins the two exact `relay_task_update` payloads the dashboard must send so a
future refactor can't silently swap the status/result pairing the broker
depends on.
"""
from pathlib import Path

HTML = Path(__file__).resolve().parents[1] / "dashboard" / "index.html"


def test_dashboard_offers_approve_and_deny_for_hands_permits():
    src = HTML.read_text(encoding="utf-8")
    assert "hands_permit:" in src
    assert "btn-hands-approve" in src and "btn-hands-deny" in src
    assert "result: 'approve'" in src and "status: 'completed'" in src
    assert "result: 'deny'" in src and "status: 'cancelled'" in src
    assert "function decideHandsPermit(" in src
