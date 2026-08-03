"""The customer-facing Devices tab must expose the whole enrollment lifecycle."""

from pathlib import Path


DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard" / "index.html"


def test_devices_tab_has_issue_manage_and_cancel_surfaces():
    source = DASHBOARD.read_text(encoding="utf-8")
    panel = source.split('id="panel-devices"', 1)[1].split("</section>", 1)[0]
    assert "Add device" in panel
    assert "Enrolled devices" in panel
    assert "Outstanding invites" in panel
    assert "macOS / Linux" in panel and "Windows PowerShell" in panel
    assert "Licence" not in panel and "plan" not in panel.lower() and "seat" not in panel.lower()


def test_devices_javascript_uses_the_admin_routes():
    source = DASHBOARD.read_text(encoding="utf-8")
    assert "groupDeviceCredentials" in source
    for route in (
        "/auth/keys", "/enroll/invite", "/enroll/invites/",
    ):
        assert route in source
    for action in ("renameDevice", "revokeDevice", "regenerateDevice", "cancelDeviceInvite"):
        assert f"function {action}" in source
