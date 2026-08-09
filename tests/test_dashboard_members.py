"""Customer-facing membership surfaces — one product, no plan or seat UI.

Replaces test_dashboard_members_licence.py. The Solo/Team split and the
signed-entitlement system were removed: the BUSL-1.1 LICENSE file is the only
boundary (legal, not technical), so the old licence-tab test flips into a
guard that no licence/plan/seat surface comes back. Member identity — invites,
enrollment, attribution — is auth, not metering, and stays.
"""

from pathlib import Path


DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard" / "index.html"


def test_members_tab_names_attribution_and_join_flow():
    source = DASHBOARD.read_text(encoding="utf-8")
    panel = source.split('id="panel-members"', 1)[1].split("</section>", 1)[0]
    # Membership is identity and attribution, not seat metering.
    assert "Members are people, not devices" in panel
    assert "attributed to the person who made them" in panel
    assert "member identity" in panel
    assert "enrolls their first device" in panel
    assert "Invite a person" in panel
    assert "macOS / Linux" in panel and "Windows PowerShell" in panel
    assert "/members/invites" in source
    assert "firekeep join " in source
    # The panel carries no plan/seat/licence language of its own.
    lowered = panel.lower()
    assert "seat" not in lowered and "plan" not in lowered and "licence" not in lowered


def test_no_licence_or_entitlement_ui_remains():
    # Flipped invariant: the predecessor test asserted the Licence tab existed
    # and managed a signed document; this one asserts every trace stays gone —
    # nav button, icon symbol, panel, tab dispatch, JS handlers and the
    # /licence API route. Substrings are targeted rather than a blanket
    # word-ban because "licence" legitimately survives in an icon-set
    # licensing comment and in the WHY comment recording this removal.
    source = DASHBOARD.read_text(encoding="utf-8")
    assert 'data-tab="licence"' not in source
    assert "panel-licence" not in source
    assert "i-licence" not in source
    assert "loadLicence" not in source
    assert "'/licence'" not in source
    assert "name === 'licence'" not in source
    # GET /members no longer carries an entitlement key; nothing may read it,
    # and the seat-summary card that displayed it must not resurface.
    assert ".entitlement" not in source
    assert "memberPlanSummary" not in source
    assert "max_members" not in source


def test_devices_tab_remains_plan_and_seat_agnostic():
    source = DASHBOARD.read_text(encoding="utf-8")
    panel = source.split('id="panel-devices"', 1)[1].split("</section>", 1)[0]
    lowered = panel.lower()
    assert "licence" not in lowered and "plan" not in lowered and "seat" not in lowered
