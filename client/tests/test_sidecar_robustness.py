"""SP1b Task 19 — sidecar robustness (signal handling, per-tick failure
isolation) + the controller-mandated seam reconciliation (Part B): the
sidecar's registration-race guard and the hooks' guard (session_start.py /
stop.py) now share ONE canonical scratch key via firekeep_client.state, closing
the T18-review repro where the sidecar deregistered a fresh hook-registered
presence because the two guards read/wrote different keys.
"""
import signal
import threading
import time

from firekeep_client import sidecar, state, transport


def _write_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.write_text(
        "[active]\nprofile = personal\n\n"
        "[personal]\nkind = ports\nscheme = http\nhost = 127.0.0.1\n"
        "verify_tls = false\nagent_id = tester\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FIREKEEP_CONFIG", str(cfg))
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("FIREKEEP_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("FIREKEEP_AGENT_ID", raising=False)
    monkeypatch.delenv("FIREKEEP_AGENT_GOAL", raising=False)
    return cfg


class _Recorder:
    def __init__(self, fail_on=()):
        self.calls = []
        self.fail_on = set(fail_on)

    def __call__(self, url, body, *, headers, timeout, verify):
        tool = body["params"]["name"]
        self.calls.append((url, tool, body["params"]["arguments"], headers))
        if tool in self.fail_on:
            raise transport.TransportError(f"boom {tool}", status=503)
        return {"jsonrpc": "2.0", "id": body["id"], "result": {"ok": True}}

    @property
    def tools(self):
        return [c[1] for c in self.calls]


def test_heartbeat_transport_failure_logged_not_fatal(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch)
    monkeypatch.setattr(sidecar, "should_deregister", lambda aid: True)
    monkeypatch.setattr(state, "resolve_session_id", lambda payload, cfg=None: "unknown")
    rec = _Recorder(fail_on={"relay_heartbeat_presence"})

    sc = sidecar.Sidecar(interval=0, snapshot_every=5, post_json=rec)
    sc.run(max_iterations=1)

    # heartbeat raised TransportError, but the daemon still deregistered cleanly
    assert rec.tools == ["relay_register", "relay_heartbeat_presence", "relay_deregister"]
    log = (tmp_path / "logs" / "hooks.log").read_text(encoding="utf-8")
    assert "relay_heartbeat_presence failed" in log


def test_unexpected_cycle_exception_isolated(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch)
    monkeypatch.setattr(sidecar, "should_deregister", lambda aid: True)

    def _boom(self):
        raise ValueError("kaboom")

    monkeypatch.setattr(sidecar.Sidecar, "heartbeat", _boom)
    rec = _Recorder()

    sc = sidecar.Sidecar(interval=0, snapshot_every=5, post_json=rec)
    sc.run(max_iterations=1)

    # a non-Transport exception in a tick is isolated; deregister still runs
    assert rec.tools == ["relay_register", "relay_deregister"]
    log = (tmp_path / "logs" / "hooks.log").read_text(encoding="utf-8")
    assert "heartbeat/snapshot cycle error" in log


def test_signal_handler_sets_stop(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch)
    sc = sidecar.Sidecar()
    prev = signal.getsignal(signal.SIGINT)
    try:
        sidecar._install_signal_handlers(sc)
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler)
        handler(signal.SIGINT, None)  # simulate delivery (Windows-safe)
        assert sc._stop.is_set()
    finally:
        signal.signal(signal.SIGINT, prev)


def test_stop_triggers_clean_deregister(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch)
    monkeypatch.setattr(sidecar, "should_deregister", lambda aid: True)
    rec = _Recorder()
    ev = threading.Event()
    ev.set()  # already stopped before the loop body runs

    sc = sidecar.Sidecar(interval=999, post_json=rec, stop_event=ev)
    sc.run()

    # stop pre-set: register, then straight to finally-deregister, no heartbeat
    assert rec.tools == ["relay_register", "relay_deregister"]


def test_should_deregister_race_guard(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch)
    # no record -> safe to deregister
    assert sidecar.should_deregister("tester") is True
    # fresh registration inside the window -> skip (a newer session likely took over)
    sidecar.mark_registered("tester")
    assert sidecar.should_deregister("tester") is False
    # an old registration (older than the window) -> deregister. Canonical key
    # (SP1b Task 19 Part B): "presence_registered_{agent_id}" -- the SAME key
    # firekeep_client.state.mark_registered / hooks/session_start.py write to.
    old = int(time.time()) - (sidecar.REGISTRATION_RACE_WINDOW + 5)
    state.write_scratch("presence_registered_tester", str(old))
    assert sidecar.should_deregister("tester") is True


# --- Part B: controller-mandated seam reconciliation cross-composition test --


def test_hook_style_registration_guards_sidecar_deregister(tmp_path, monkeypatch):
    """The T18-review repro, now guarded: a hook-style registration mark (what
    session_start.py writes via state.mark_registered after relay_register)
    must be visible to the SIDECAR's independent should_deregister check --
    they are different processes/compositions sharing one scratch key via
    firekeep_client.state, the single keying authority. Deliberately does NOT
    monkeypatch should_deregister: that would make this test exercise nothing.
    """
    _write_config(tmp_path, monkeypatch)
    rec = _Recorder()

    # Simulate a hook-style fresh registration (session_start.py's real call,
    # using the same agent-only key as the sidecar).
    state.mark_registered("tester")

    sc = sidecar.Sidecar(post_json=rec)
    sc.deregister()

    # Real should_deregister reads the canonical key and sees a fresh mark ->
    # skips the relay call entirely (no clobbering a session that just started).
    assert "relay_deregister" not in rec.tools
    assert rec.tools == []


def test_register_survives_raw_ssl_error(firekeep_env, write_config, monkeypatch):
    """A malformed office ca_path raises RAW ssl.SSLError (not TransportError)
    from _build_ssl_context — the sidecar must degrade + hooklog, not crash
    before its try/finally even starts (T27 review follow-up)."""
    import ssl

    from firekeep_client import sidecar as sc_mod

    from tests.conftest import DEFAULT_PERSONAL

    write_config(personal=DEFAULT_PERSONAL)

    def boom(*a, **kw):
        raise ssl.SSLError("bad ca")

    sc = sc_mod.Sidecar(interval=0, post_json=boom)
    sc.register()  # must not raise
