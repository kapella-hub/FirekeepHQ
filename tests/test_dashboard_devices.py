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


def test_add_device_asks_the_server_which_address_it_publishes_on():
    """It used to hardcode a tunnel onto 127.0.0.1 whatever the deployment.

    That address then travelled into the joining machine's config, which is how
    a device enrolled from this form ended up permanently pointed at its own
    loopback instead of the server.
    """
    source = DASHBOARD.read_text(encoding="utf-8")
    assert "/enroll/defaults" in source
    panel = source.split('id="panel-devices"', 1)[1].split("</section>", 1)[0]
    assert 'id="deviceInviteHost"' in panel
    assert "body.transport = 'tunnel'; body.kind = 'ports'; body.host = '127.0.0.1';" not in source
    member_form = source.split("memberInviteForm')", 1)[1].split("</script>", 1)[0]
    assert "transport: 'tunnel'" not in member_form
