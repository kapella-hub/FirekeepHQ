import configparser
import inspect
from pathlib import Path

import pytest

from firekeep_client import resolver


def _write_server(path: Path, *, server: dict | None = None, agent_id: str = "alice") -> Path:
    cfg = configparser.ConfigParser()
    cfg["identity"] = {"agent_id": agent_id}
    cfg["server"] = server or {
        "kind": "ports",
        "scheme": "http",
        "host": "198.51.100.7",
        "verify_tls": "false",
        "api_key": "nxs_test",
    }
    with path.open("w", encoding="utf-8") as handle:
        cfg.write(handle)
    return path


def test_resolve_reads_the_single_server_section(firekeep_env):
    _write_server(firekeep_env["config_path"])

    endpoint = resolver.resolve("cortex")

    assert endpoint.mcp_url == "http://198.51.100.7:8080/mcp"
    assert endpoint.rest_base == "http://198.51.100.7:8100"
    assert endpoint.headers == {"X-Agent-Id": "alice", "X-API-Key": "nxs_test"}
    assert "profile" not in inspect.signature(resolver.resolve).parameters


def test_identity_env_override_is_preserved(firekeep_env, monkeypatch):
    _write_server(firekeep_env["config_path"], agent_id="from-file")
    monkeypatch.setenv("FIREKEEP_AGENT_ID", "from-env")

    assert resolver.agent_id(resolver.load_config()) == "from-env"
    assert resolver.resolve("bridge").headers["X-Agent-Id"] == "from-env"


@pytest.mark.parametrize(
    ("server", "message"),
    [
        ({
            "kind": "paths", "scheme": "https", "base_url": "https://fk.example",
            "verify_tls": "false", "ca_path": "os",
        }, "verify_tls=false"),
        ({
            "kind": "paths", "scheme": "https", "base_url": "https://fk.example",
            "verify_tls": "true",
        }, "requires 'ca_path'"),
        ({
            "kind": "paths", "scheme": "http", "base_url": "https://fk.example",
            "verify_tls": "false",
        }, "does not match base_url"),
    ],
)
def test_tls_invariants_name_the_resolved_config(firekeep_env, server, message):
    path = _write_server(firekeep_env["config_path"], server=server)

    with pytest.raises(resolver.ConfigError) as caught:
        resolver.resolve("cortex")

    assert message in str(caught.value)
    assert str(path) in str(caught.value)
    assert "profile" not in str(caught.value).lower()


def test_os_trust_sentinel_survives_the_new_shape(firekeep_env):
    _write_server(firekeep_env["config_path"], server={
        "kind": "paths", "scheme": "https", "base_url": "https://fk.example",
        "verify_tls": "true", "ca_path": "os", "api_key": "nxs_test",
    })

    endpoint = resolver.resolve("relay")

    assert endpoint.verify == resolver.OS_TRUST
    assert endpoint.mcp_url == "https://fk.example/mcp/relay"


def test_no_server_and_no_legacy_active_fails_with_the_real_path(firekeep_env):
    firekeep_env["config_path"].write_text("[dist]\nbase_url = https://dist.example\n")

    with pytest.raises(resolver.ConfigMigrationConflict) as caught:
        resolver.load_config()

    assert str(firekeep_env["config_path"]) in str(caught.value)
    assert "no [server] section" in str(caught.value)
