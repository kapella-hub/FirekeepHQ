
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
    """Stand-in for transport.post_json: records tools/call POSTs, can fail some."""

    def __init__(self, fail_on=()):
        self.calls = []  # (url, tool, arguments, headers)
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


def test_lifecycle_register_heartbeat_deregister(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch)
    # Race guard is exercised in the robustness task; isolate the sequence here.
    monkeypatch.setattr(sidecar, "should_deregister", lambda aid, profile="": True)
    monkeypatch.setattr(state, "resolve_session_id", lambda payload, cfg=None: "unknown")
    rec = _Recorder()

    sc = sidecar.Sidecar(interval=0, snapshot_every=5, post_json=rec)
    sc.run(max_iterations=1)  # loop-once / fake-clock (interval=0 => no real sleep)

    assert rec.tools == ["relay_register", "relay_heartbeat_presence", "relay_deregister"]
    # register goes to Relay's mcp_url and carries the profile identity header
    url0, tool0, args0, headers0 = rec.calls[0]
    assert url0 == "http://127.0.0.1:8050/mcp"
    assert args0 == {"agent_id": "tester", "goal": "Session started", "hostname": sc.hostname}
    assert headers0["X-Agent-Id"] == "tester"
    # heartbeat omits session_id when Bridge reports 'unknown'
    assert rec.calls[1][2] == {"agent_id": "tester", "goal": "Session started"}


def test_snapshot_posts_ctx_update_to_bridge(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch)
    monkeypatch.setattr(sidecar, "should_deregister", lambda aid, profile="": True)
    monkeypatch.setattr(state, "resolve_session_id", lambda payload, cfg=None: "sess-1")
    monkeypatch.setattr(sidecar.Sidecar, "_collect_snapshot", lambda self: "SNAP")
    rec = _Recorder()

    sc = sidecar.Sidecar(interval=0, snapshot_every=1, post_json=rec)
    sc.run(max_iterations=1)

    assert rec.tools == [
        "relay_register", "relay_heartbeat_presence", "ctx_update", "relay_deregister",
    ]
    ctx = [c for c in rec.calls if c[1] == "ctx_update"][0]
    assert ctx[0] == "http://127.0.0.1:8070/mcp"  # bridge, not relay
    assert ctx[2] == {
        "category": "scratch", "key": "workspace_snapshot",
        "content": "SNAP", "agent_id": "tester",
    }
    # heartbeat backfilled session_id when Bridge had an active session
    assert rec.calls[1][2]["session_id"] == "sess-1"


def test_singleton_lock_blocks_second_instance(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch)
    monkeypatch.setattr(sidecar, "_pid_alive", lambda pid: True)
    state.write_scratch("sidecar-tester-pid", "999999")  # a live foreign owner
    rec = _Recorder()

    sc = sidecar.Sidecar(interval=0, post_json=rec)
    sc.run(max_iterations=1)

    assert rec.calls == []  # never registered — another instance owns this identity
    log = (tmp_path / "logs" / "hooks.log").read_text(encoding="utf-8")
    assert "already running for tester" in log


def test_bypassed_sidecar_makes_zero_server_calls(tmp_path, monkeypatch):
    """Personal / FIREKEEP_BYPASS: the presence daemon must go fully dormant — no
    relay_register / heartbeat / snapshot / deregister reaches Relay or Bridge."""
    _write_config(tmp_path, monkeypatch)
    monkeypatch.setenv("FIREKEEP_BYPASS", "1")
    monkeypatch.setattr(sidecar, "should_deregister", lambda aid, profile="": True)
    monkeypatch.setattr(state, "resolve_session_id", lambda payload, cfg=None: "sess-1")
    monkeypatch.setattr(sidecar.Sidecar, "_collect_snapshot", lambda self: "SNAP")
    rec = _Recorder()

    sc = sidecar.Sidecar(interval=0, snapshot_every=1, post_json=rec)
    sc.run(max_iterations=2)

    assert rec.calls == []  # nothing reached the server while bypassed


def test_sidecar_resumes_calls_when_not_bypassed(tmp_path, monkeypatch):
    """Sanity: without bypass the daemon still registers/heartbeats (no regression)."""
    _write_config(tmp_path, monkeypatch)
    monkeypatch.delenv("FIREKEEP_BYPASS", raising=False)
    monkeypatch.setattr(sidecar, "should_deregister", lambda aid, profile="": True)
    monkeypatch.setattr(state, "resolve_session_id", lambda payload, cfg=None: "unknown")
    rec = _Recorder()

    sc = sidecar.Sidecar(interval=0, snapshot_every=5, post_json=rec)
    sc.run(max_iterations=1)

    assert "relay_register" in rec.tools
