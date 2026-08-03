"""Customer-facing membership and offline licence management surfaces."""

from pathlib import Path


DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard" / "index.html"


def test_members_tab_names_the_seat_boundary_and_join_flow():
    source = DASHBOARD.read_text(encoding="utf-8")
    panel = source.split('id="panel-members"', 1)[1].split("</section>", 1)[0]
    assert "People consume seats" in panel
    assert "devices, terminals and agent runtimes do not" in panel
    assert "Invite a person" in panel
    assert "macOS / Linux" in panel and "Windows PowerShell" in panel
    assert "/members/invites" in source
    assert "firekeep join " in source


def test_licence_tab_explains_offline_verification_and_manages_document():
    source = DASHBOARD.read_text(encoding="utf-8")
    panel = source.split('id="panel-licence"', 1)[1].split("</section>", 1)[0]
    assert "Offline, server-authoritative" in panel
    assert "does not phone home" in panel
    assert "Signed document" in panel
    assert "Verify &amp; apply" in panel
    assert "CONFIG.CORTEX_API + '/licence'" in source


def test_devices_tab_remains_plan_and_seat_agnostic():
    source = DASHBOARD.read_text(encoding="utf-8")
    panel = source.split('id="panel-devices"', 1)[1].split("</section>", 1)[0]
    lowered = panel.lower()
    assert "licence" not in lowered and "plan" not in lowered and "seat" not in lowered
