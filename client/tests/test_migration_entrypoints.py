import asyncio
import io
import json
import sys

import pytest

from firekeep_client import cli, connect as connect_module, resolver, sidecar
from firekeep_client.hooks import __main__ as dispatcher


CONFLICT = """\
[active]
profile = personal

[personal]
kind = ports
scheme = http
host = 198.51.100.7
verify_tls = false
api_key = first
agent_id = alice

[office]
kind = ports
scheme = http
host = 203.0.113.9
verify_tls = false
api_key = second
agent_id = alice

[pins]
kiro = office
"""


def _write_conflict(tmp_path, monkeypatch):
    path = tmp_path / "config"
    path.write_text(CONFLICT, encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(path))
    return path


def test_cli_surfaces_migration_conflict_as_exit_3(tmp_path, monkeypatch, capsys):
    path = _write_conflict(tmp_path, monkeypatch)
    before = path.read_bytes()

    assert cli.main(["doctor"]) == 3

    assert "migration refused" in capsys.readouterr().err
    assert path.read_bytes() == before


def test_shim_surfaces_migration_conflict_as_startup_failure(tmp_path, monkeypatch, capsys):
    for dependency in ("anyio", "httpx", "mcp"):
        pytest.importorskip(dependency)
    from firekeep_client import shim

    path = _write_conflict(tmp_path, monkeypatch)

    assert shim.run("cortex") == 3

    err = capsys.readouterr().err
    assert "migration blocked" in err
    assert str(path.resolve()) in err


def test_hook_renders_migration_conflict_as_system_message(monkeypatch, capsys):
    message = "firekeep config migration refused: C:/tmp/config defines two servers"

    class Core:
        @staticmethod
        def run(_payload):
            raise resolver.ConfigMigrationConflict(message)

    monkeypatch.setitem(dispatcher._CORE_MODULES, "prompt", Core)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))

    assert dispatcher.main(["prompt"]) == 0
    assert json.loads(capsys.readouterr().out)["systemMessage"] == message


def test_connect_refuses_conflict_before_touching_the_remote(tmp_path, monkeypatch, capsys):
    path = _write_conflict(tmp_path, monkeypatch)
    before = path.read_bytes()

    def must_not_probe(*_args, **_kwargs):
        raise AssertionError("remote access must not start before migration preflight")

    monkeypatch.setattr(connect_module, "_probe_server", must_not_probe)

    assert cli.main(["connect", "root@example.test"]) == 3
    assert "migration refused" in capsys.readouterr().err
    assert path.read_bytes() == before


def test_connect_does_not_overwrite_a_malformed_existing_config(
        tmp_path, monkeypatch, capsys):
    path = tmp_path / "config"
    original = b"api_key = secret-without-an-ini-section\n"
    path.write_bytes(original)
    monkeypatch.setenv("FIREKEEP_CONFIG", str(path))

    def must_not_probe(*_args, **_kwargs):
        raise AssertionError("remote access must not start with malformed config")

    monkeypatch.setattr(connect_module, "_probe_server", must_not_probe)

    assert cli.main(["connect", "root@example.test"]) == 1
    err = capsys.readouterr().err
    assert "cannot read existing Firekeep config" in err
    assert "secret-without" not in err
    assert path.read_bytes() == original


def test_decision_server_refuses_conflict_before_mcp_start(tmp_path, monkeypatch, capsys):
    pytest.importorskip("anyio")
    from firekeep_client.decision import server as decision_server

    path = _write_conflict(tmp_path, monkeypatch)

    assert decision_server.main() == 3

    err = capsys.readouterr().err
    assert "config migration blocked" in err
    assert str(path.resolve()) in err


def test_decision_tool_does_not_degrade_a_migration_conflict(monkeypatch):
    pytest.importorskip("anyio")
    from firekeep_client.decision import server as decision_server

    message = "ambiguous config"
    monkeypatch.setattr(decision_server, "_is_headless", lambda: False)

    def raise_conflict(_service):
        raise resolver.ConfigMigrationConflict(message)

    monkeypatch.setattr(decision_server.resolver, "resolve", raise_conflict)

    with pytest.raises(resolver.ConfigMigrationConflict, match=message):
        asyncio.run(decision_server._run_decision_board("ctx", []))


def test_sidecar_surfaces_migration_conflict_as_exit_3(tmp_path, monkeypatch, capsys):
    path = _write_conflict(tmp_path, monkeypatch)
    monkeypatch.setattr(sidecar, "_install_signal_handlers", lambda _sc: None)

    assert sidecar.main([]) == 3

    err = capsys.readouterr().err
    assert "config migration blocked" in err
    assert str(path.resolve()) in err
