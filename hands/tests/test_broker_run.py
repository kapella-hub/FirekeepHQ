"""What `run()` wires up before it binds a socket, and what the CLI refuses.

`build_runtime` exists so this is testable without signal handling or a real
OS hook: `run()` is that helper plus a socket plus a wait loop.
"""
import pytest

from firekeep_hands.config import HandsConfig
from firekeep_hands.broker import server
from firekeep_hands.broker.__main__ import main


class FakeLink:
    def __init__(self, offline): self.offline = offline; self.posted = []
    def post_permit_task(self, **kw): self.posted.append(kw); return "task-" + kw["challenge"]
    def permit_task_state(self, challenge): return "pending"
    def close_permit_task(self, task_id, result): pass


@pytest.fixture(autouse=True)
def no_real_hook(monkeypatch):
    """Never install a live keyboard hook from a test. The listener modules
    have their own tests; this file is about the wiring around them."""
    installed = []

    def fake_listener(cfg, store, listeners):
        installed.append(cfg.chord)
        listeners["chord"] = "active"
        return None

    monkeypatch.setattr(server, "_chord_listener", fake_listener)
    return installed


def test_phone_approvals_are_off_by_default(no_real_hook):
    """Relay records no actor on a task update, so a completed permit task
    proves only that someone holding the workspace key completed it — the
    driving agent included. Opt-in until relay names the approver."""
    store, listeners, bridge = server.build_runtime(HandsConfig(), FakeLink(offline=False))
    assert HandsConfig().phone_approvals is False
    assert listeners["phone"] == "off"
    assert bridge is None
    assert listeners["chord"] == "active" and no_real_hook == ["ctrl+alt+y"]


def test_phone_is_active_only_when_opted_in_and_connected():
    cfg = HandsConfig(phone_approvals=True)
    store, listeners, bridge = server.build_runtime(cfg, FakeLink(offline=False))
    try:
        assert listeners["phone"] == "active" and bridge is not None and bridge.is_alive()
    finally:
        if bridge is not None:
            bridge.stop(); bridge.join(timeout=3)


def test_opted_in_but_no_keep_reports_offline_and_starts_nothing():
    """The third state matters to the human reading the doctor row: they
    turned the phone path on and it still is not there."""
    cfg = HandsConfig(phone_approvals=True)
    store, listeners, bridge = server.build_runtime(cfg, FakeLink(offline=True))
    assert listeners["phone"] == "offline" and bridge is None


def test_the_store_takes_its_ttl_from_the_config():
    store, _listeners, bridge = server.build_runtime(HandsConfig(permit_ttl_s=5), FakeLink(offline=True))
    assert store.ttl_s == 5
    assert bridge is None


def test_a_chord_listener_that_cannot_install_leaves_the_health_row_honest(monkeypatch):
    """`build_runtime` must not paper over an unavailable listener: with no
    chord and no phone, every protected step is refused and the human has to
    be told which half is missing."""
    monkeypatch.setattr(server, "_chord_listener", lambda cfg, store, listeners: None)
    _store, listeners, _bridge = server.build_runtime(HandsConfig(), FakeLink(offline=True))
    assert listeners == {"chord": "unavailable", "phone": "off"}


def test_there_is_no_approve_sub_command():
    """A guarded rule, not an oversight: a command anything with shell access
    could run would defeat the point of watching for real keystrokes."""
    for argv in (["approve"], ["decide", "approve"], ["permit", "approve"]):
        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code != 0


def test_main_run_dispatches_to_the_server(monkeypatch):
    seen = []
    monkeypatch.setattr("firekeep_hands.broker.__main__.run_broker", lambda argv: seen.append(argv) or 0)
    assert main(["run"]) == 0 and seen == [["run"]]


def test_main_status_reports_a_broker_that_is_not_running(capsys):
    assert main(["status"]) == 1
    assert "not running" in capsys.readouterr().out
