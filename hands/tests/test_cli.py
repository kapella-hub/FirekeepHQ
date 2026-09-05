import hashlib
import json

import pytest

from firekeep_hands import cli, paths
from firekeep_hands.config import Remembered, load_config, load_policy, save_policy
from firekeep_hands.evidence import Ledger


# -- status -----------------------------------------------------------------

def test_status_reports_not_running_when_no_broker(isolated_home, capsys):
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "not running" in out
    assert "last task: none" in out


def test_status_does_not_crash_when_backend_load_raises(isolated_home, monkeypatch, capsys):
    def _boom():
        raise RuntimeError("no accessibility API here")

    monkeypatch.setattr(cli.backends, "load_backend", _boom)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "backend: unavailable (no accessibility API here)" in out


def test_status_reports_broker_health_and_phone_off_hint(isolated_home, monkeypatch, capsys):
    class _FakeClient:
        def health(self):
            return {"ok": True, "chord": "ctrl+alt+y", "listeners": {"chord": "active", "phone": "off"}, "pending": 2}

    monkeypatch.setattr(cli.BrokerClient, "from_disk", classmethod(lambda cls, timeout=2.0: _FakeClient()))
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "chord ctrl+alt+y (active)" in out and "pending 2" in out
    assert "phone approvals are off" in out


def _running_broker(monkeypatch, pending_count=1):
    class _FakeClient:
        def health(self):
            return {"ok": True, "chord": "ctrl+alt+y",
                    "listeners": {"chord": "active", "phone": "off"}, "pending": pending_count}

    monkeypatch.setattr(cli.BrokerClient, "from_disk", classmethod(lambda cls, timeout=2.0: _FakeClient()))


def test_status_lists_what_is_waiting_for_a_chord(isolated_home, monkeypatch, capsys):
    """The chord approves the oldest pending permit, and until this line the
    only description of that step came from the runtime being gated."""
    from firekeep_hands.broker import pending
    from firekeep_hands.broker.permits import PermitStore

    store = PermitStore(ttl_s=60)
    store.request(challenge="a", title='Invoke "Send" in Mail', classes=("send",),
                  task_id="t", step_index=1)
    store.request(challenge="b", title="Empty the Trash", classes=("destroy",),
                  task_id="t", step_index=2)
    pending.write_pending(store, chord="ctrl+alt+y", deny_chord="ctrl+alt+n")

    _running_broker(monkeypatch, pending_count=2)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "pending permits: 2 — press ctrl+alt+y to approve the first" in out
    assert "ctrl+alt+n to deny it" in out
    assert 'Invoke "Send" in Mail (send)' in out
    assert "Empty the Trash (destroy)" in out
    # the oldest is marked, because that is the one the chord answers
    first, second = [line for line in out.splitlines() if "Mail" in line or "Trash" in line]
    assert first.strip().startswith("→") and not second.strip().startswith("→")


def test_status_says_nothing_about_permits_when_none_are_waiting(isolated_home, monkeypatch, capsys):
    _running_broker(monkeypatch, pending_count=0)
    assert cli.main(["status"]) == 0
    assert "pending permits:" not in capsys.readouterr().out


def test_status_does_not_read_a_stale_pending_file_when_the_broker_is_down(isolated_home, capsys):
    """Nothing removes pending.json when a broker is killed, so the list is
    only trustworthy once /health has answered."""
    from firekeep_hands.broker import pending
    from firekeep_hands.broker.permits import PermitStore

    store = PermitStore(ttl_s=60)
    store.request(challenge="a", title="Left over from a dead broker", classes=("send",),
                  task_id="t", step_index=0)
    pending.write_pending(store, chord="ctrl+alt+y", deny_chord="ctrl+alt+n")

    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "not running" in out and "Left over" not in out


def test_status_survives_a_corrupt_pending_file(isolated_home, monkeypatch, capsys):
    from firekeep_hands.broker import pending

    pending.pending_path().write_text("{not json", encoding="utf-8")
    _running_broker(monkeypatch)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "pending permits:" not in out and "policy:" in out


def test_status_shows_last_task(isolated_home, capsys):
    led = Ledger("t-status", goal="g", apps=["Notepad"], machine_id="m", session_id="s")
    led.record(step_index=0, action={"kind": "wait"}, route="none", classes=(), permit=None,
               before_png=None, after_png=None, outcome="ok", error=None)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "last task: t-status" in out and "1 steps" in out


def test_status_does_not_crash_when_broker_client_raises(isolated_home, monkeypatch, capsys):
    def _raise(cls, timeout=2.0):
        raise OSError("broker.json is corrupt")

    monkeypatch.setattr(cli.BrokerClient, "from_disk", classmethod(_raise))
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "broker: unavailable (broker.json is corrupt)" in out
    # status keeps going past the broken broker client
    assert "policy:" in out and "last task: none" in out


# -- allow --------------------------------------------------------------

def test_allow_domain_writes_policy_and_list_shows_it(isolated_home, capsys):
    assert cli.main(["allow", "domain", "example.com"]) == 0
    assert load_policy().domains == ["example.com"]
    capsys.readouterr()
    assert cli.main(["allow", "list"]) == 0
    out = capsys.readouterr().out
    assert "example.com" in out


def test_allow_app_writes_policy(isolated_home):
    assert cli.main(["allow", "app", "Notepad"]) == 0
    assert load_policy().apps == ["Notepad"]
    # adding the same app twice does not duplicate it
    assert cli.main(["allow", "app", "Notepad"]) == 0
    assert load_policy().apps == ["Notepad"]


def test_allow_with_no_subaction_defaults_to_list(isolated_home, capsys):
    cli.main(["allow", "domain", "example.com"])
    capsys.readouterr()
    assert cli.main(["allow"]) == 0
    assert "example.com" in capsys.readouterr().out


def test_allow_forget_removes_matching_remembered_entry(isolated_home):
    policy = load_policy()
    policy.remembered.append(Remembered(cls="send", app="Mail", match="Send", until="2099-01-01T00:00:00Z"))
    save_policy(policy)

    assert cli.main(["allow", "forget", "send", "Mail", "Send"]) == 0
    assert load_policy().remembered == []


def test_allow_forget_with_no_match_exits_1(isolated_home, capsys):
    assert cli.main(["allow", "forget", "send", "Mail", "Send"]) == 1
    assert capsys.readouterr().err


def test_allow_unknown_subaction_exits_2(isolated_home):
    assert cli.main(["allow", "remember", "send", "Mail", "Send"]) == 2


# -- chord --------------------------------------------------------------

def test_chord_prints_both_chords(isolated_home, capsys):
    assert cli.main(["chord"]) == 0
    out = capsys.readouterr().out
    assert "ctrl+alt+y" in out and "ctrl+alt+n" in out


def test_chord_set_persists(isolated_home):
    assert cli.main(["chord", "set", "ctrl+alt+u"]) == 0
    assert load_config().chord == "ctrl+alt+u"


def test_chord_set_bogus_exits_2(isolated_home, capsys):
    assert cli.main(["chord", "set", "bogus"]) == 2
    assert capsys.readouterr().err
    assert load_config().chord == "ctrl+alt+y"  # unchanged


def test_chord_set_deny_persists(isolated_home):
    assert cli.main(["chord", "set-deny", "ctrl+alt+d"]) == 0
    assert load_config().deny_chord == "ctrl+alt+d"


def test_chord_set_prints_restart_note(isolated_home, capsys):
    cli.main(["chord", "set", "ctrl+alt+u"])
    out = capsys.readouterr().out
    assert "firekeep-hands-broker run" in out and "log out and in" in out


# -- config -------------------------------------------------------------

def test_config_prints_json_with_sorted_keys(isolated_home, capsys):
    assert cli.main(["config"]) == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["chord"] == "ctrl+alt+y"
    assert list(json.loads(out).keys()) == sorted(data.keys())


def test_config_set_bool(isolated_home):
    assert cli.main(["config", "set", "phone_approvals", "true"]) == 0
    cfg = load_config()
    assert cfg.phone_approvals is True


def test_config_set_int(isolated_home):
    assert cli.main(["config", "set", "max_steps", "12"]) == 0
    cfg = load_config()
    assert cfg.max_steps == 12 and isinstance(cfg.max_steps, int)


def test_config_set_unknown_key_exits_2(isolated_home, capsys):
    assert cli.main(["config", "set", "nope", "1"]) == 2
    assert capsys.readouterr().err


def test_config_set_bad_int_exits_2(isolated_home, capsys):
    assert cli.main(["config", "set", "max_steps", "abc"]) == 2
    assert capsys.readouterr().err
    assert load_config().max_steps == 400  # unchanged


def test_config_set_bad_bool_exits_2(isolated_home, capsys):
    assert cli.main(["config", "set", "phone_approvals", "maybe"]) == 2
    assert capsys.readouterr().err


# -- evidence -------------------------------------------------------------

def test_evidence_lists_ledger_tasks(isolated_home, capsys):
    led = Ledger("t1", goal="write a note", apps=["Notepad"], machine_id="m", session_id="s")
    led.record(step_index=0, action={"kind": "wait"}, route="none", classes=(), permit=None,
               before_png=None, after_png=None, outcome="ok", error=None)
    led.close("done", "saved")

    assert cli.main(["evidence"]) == 0
    out = capsys.readouterr().out
    assert "t1" in out and "done" in out and "1 steps" in out


def test_evidence_shows_task_steps(isolated_home, capsys):
    led = Ledger("t2", goal="write a note", apps=["Notepad"], machine_id="m", session_id="s")
    led.record(step_index=0, action={"kind": "click"}, route="permit", classes=("send",),
               permit={"challenge": "c1", "verdict": "approve", "via": "chord"},
               before_png=b"\x89PNG1", after_png=b"\x89PNG2", outcome="ok", error=None)

    assert cli.main(["evidence", "t2"]) == 0
    out = capsys.readouterr().out
    assert "t2" in out
    before_sha8 = hashlib.sha256(b"\x89PNG1").hexdigest()[:8]
    after_sha8 = hashlib.sha256(b"\x89PNG2").hexdigest()[:8]
    assert f"#0 click [permit] ok classes=send permit=chord before={before_sha8} after={after_sha8}" in out


def test_evidence_unknown_task_exits_1(isolated_home, capsys):
    assert cli.main(["evidence", "does-not-exist"]) == 1
    assert capsys.readouterr().err


def test_evidence_malformed_step_line_reported(isolated_home, capsys):
    led = Ledger("t3", goal="g", apps=[], machine_id="m", session_id="s")
    with (led.dir / "steps.jsonl").open("a", encoding="utf-8") as fh:
        fh.write("not json at all\n")

    assert cli.main(["evidence", "t3"]) == 0
    out = capsys.readouterr().out
    assert "#? not json at all" in out


def test_evidence_rejects_dotdot_relative_task_id(isolated_home, capsys):
    root = paths.evidence_root()
    root.mkdir(parents=True, exist_ok=True)
    # A decoy directory OUTSIDE the evidence root that "../decoy" would reach
    # if `evidence <task_id>` built its path without checking containment.
    decoy = root.parent / "decoy-secret"
    decoy.mkdir(parents=True)
    (decoy / "task.json").write_text(json.dumps({"started": "2026-01-01T00:00:00Z", "goal": "top-secret"}))

    assert cli.main(["evidence", "../decoy-secret"]) == 1
    out, err = capsys.readouterr()
    assert "top-secret" not in out
    assert err


def test_evidence_rejects_absolute_path_task_id(isolated_home, tmp_path, capsys):
    decoy = tmp_path / "decoy-abs"
    decoy.mkdir(parents=True)
    (decoy / "task.json").write_text(json.dumps({"started": "2026-01-01T00:00:00Z", "goal": "top-secret"}))

    assert cli.main(["evidence", str(decoy)]) == 1
    out, err = capsys.readouterr()
    assert "top-secret" not in out
    assert err


# -- usage -------------------------------------------------------------

def test_main_with_no_args_exits_2(isolated_home):
    assert cli.main([]) == 2


def test_main_with_unknown_action_exits_2(isolated_home):
    assert cli.main(["bogus"]) == 2
