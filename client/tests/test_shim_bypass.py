"""Hard bypass at the shim: under FIREKEEP_BYPASS, serve an inert 0-tool server and
never resolve config or proxy to the HTTP service. Non-bypassed runs are untouched.
"""
from __future__ import annotations


from firekeep_client import shim


def test_run_bypassed_serves_inert_without_resolving(monkeypatch):
    monkeypatch.setenv("FIREKEEP_BYPASS", "1")
    called = {}

    def fake_inert(service):
        called["svc"] = service
        return 0
    monkeypatch.setattr(shim, "_serve_inert", fake_inert)

    def _boom(*a, **k):
        raise AssertionError("resolve_connection must NOT run under bypass")
    monkeypatch.setattr(shim, "resolve_connection", _boom)

    rc = shim.run("cortex")

    assert rc == 0
    assert called["svc"] == "cortex"  # took the inert path with no config resolution


def test_serve_inert_builds_zero_tool_server(monkeypatch):
    """_serve_inert constructs a FastMCP with NO tools registered and runs it."""
    import mcp.server.fastmcp as fastmcp_mod

    ran = {}

    class FakeMCP:
        def __init__(self, name):
            ran["name"] = name
            ran["tools"] = 0  # nothing is ever registered on the inert server

        def run(self):
            ran["ran"] = True

    monkeypatch.setattr(fastmcp_mod, "FastMCP", FakeMCP)

    rc = shim._serve_inert("relay")

    assert rc == 0
    assert ran["ran"] is True
    assert "relay" in ran["name"]
    assert ran["tools"] == 0


def test_run_not_bypassed_reaches_resolve(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.write_text(
        "[identity]\nagent_id = t\n[server]\nkind = ports\nscheme = http\n"
        "host = 127.0.0.1\nverify_tls = false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FIREKEEP_CONFIG", str(cfg))
    monkeypatch.delenv("FIREKEEP_BYPASS", raising=False)

    monkeypatch.setattr(
        shim, "_serve_inert",
        lambda s: (_ for _ in ()).throw(AssertionError("inert despite team mode")),
    )
    reached = {}

    def fake_resolve(service):
        reached["svc"] = service
        raise shim.ConfigError("stop here")  # short-circuit before any real serve
    monkeypatch.setattr(shim, "resolve_connection", fake_resolve)

    rc = shim.run("cortex")

    assert reached["svc"] == "cortex"  # normal path reached resolve, not inert
    assert rc == 1  # ConfigError branch
