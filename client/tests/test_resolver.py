from pathlib import Path
import textwrap

import pytest

from firekeep_client import resolver
from firekeep_client.resolver import ConfigError


def _write_config(tmp_path, monkeypatch, text: str):
    cfg_file = tmp_path / "config"
    cfg_file.write_text(textwrap.dedent(text), encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(cfg_file))
    monkeypatch.delenv("FIREKEEP_AGENT_ID", raising=False)
    return cfg_file


PORTS = """\
    [identity]
    agent_id = mogan

    [server]
    kind = ports
    scheme = http
    host = 198.51.100.7
    verify_tls = false
"""

PATHS = """\
    [identity]
    agent_id = mogan

    [server]
    kind = paths
    scheme = https
    base_url = https://firekeep.example
    verify_tls = true
    ca_path = ~/.firekeep/firekeep-root-ca.crt
    api_key = nxs_secret_key_123
"""

PORTS_EXPECTED = {
    "cortex": ("http://198.51.100.7:8080/mcp", "http://198.51.100.7:8100"),
    "bridge": ("http://198.51.100.7:8070/mcp", "http://198.51.100.7:8070"),
    "sentinel": ("http://198.51.100.7:8060/mcp", "http://198.51.100.7:8060"),
    "relay": ("http://198.51.100.7:8050/mcp", "http://198.51.100.7:8050"),
}

PATHS_EXPECTED = {
    service: (f"https://firekeep.example/mcp/{service}",
              f"https://firekeep.example/api/{service}")
    for service in resolver.SERVICES
}


def test_load_config_missing_file_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_CONFIG", str(tmp_path / "does-not-exist"))
    with pytest.raises(ConfigError):
        resolver.load_config()


def test_malformed_config_is_path_named_without_echoing_contents(tmp_path, monkeypatch):
    path = _write_config(tmp_path, monkeypatch, "not ini\nsecret = nxs_do_not_echo\n")
    with pytest.raises(ConfigError) as caught:
        resolver.load_config()
    assert str(path.resolve()) in str(caught.value)
    assert "nxs_do_not_echo" not in str(caught.value)


def test_load_config_reads_env_path(tmp_path, monkeypatch):
    path = _write_config(tmp_path, monkeypatch, PORTS)
    cfg = resolver.load_config()
    assert cfg.get("server", "host") == "198.51.100.7"
    assert cfg._firekeep_path == path


def test_explicit_path_wins_over_env(tmp_path, monkeypatch):
    good = tmp_path / "explicit"
    good.write_text(textwrap.dedent(PORTS), encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(tmp_path / "nope"))
    assert resolver.load_config(good).get("identity", "agent_id") == "mogan"


@pytest.mark.parametrize("env_val,expected", [
    (None, "mogan"),
    ("agent-beta", "agent-beta"),
    ("", "mogan"),
])
def test_agent_id_env_override(tmp_path, monkeypatch, env_val, expected):
    _write_config(tmp_path, monkeypatch, PORTS)
    cfg = resolver.load_config()
    if env_val is None:
        monkeypatch.delenv("FIREKEEP_AGENT_ID", raising=False)
    else:
        monkeypatch.setenv("FIREKEEP_AGENT_ID", env_val)
    assert resolver.agent_id(cfg) == expected


def test_agent_id_missing_key_raises(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, PORTS.replace("agent_id = mogan\n", ""))
    with pytest.raises(ConfigError, match="agent_id"):
        resolver.agent_id(resolver.load_config())


@pytest.mark.parametrize("service", resolver.SERVICES)
def test_resolve_ports_urls(tmp_path, monkeypatch, service):
    _write_config(tmp_path, monkeypatch, PORTS)
    ep = resolver.resolve(service)
    assert (ep.mcp_url, ep.rest_base) == PORTS_EXPECTED[service]
    assert ep.verify is False


@pytest.mark.parametrize("service", resolver.SERVICES)
def test_resolve_paths_urls(tmp_path, monkeypatch, service):
    _write_config(tmp_path, monkeypatch, PATHS)
    ep = resolver.resolve(service)
    assert (ep.mcp_url, ep.rest_base) == PATHS_EXPECTED[service]
    assert ep.verify == str(Path("~/.firekeep/firekeep-root-ca.crt").expanduser().resolve())


def test_headers_include_identity_key_and_optional_session(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, PATHS)
    ep = resolver.resolve("relay", session_id="s-123")
    assert ep.headers == {
        "X-Agent-Id": "mogan",
        "X-API-Key": "nxs_secret_key_123",
        "X-Session-Id": "s-123",
    }


def test_plain_http_without_key_has_only_identity_header(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, PORTS)
    assert resolver.resolve("cortex").headers == {"X-Agent-Id": "mogan"}


@pytest.mark.parametrize("service", ["symdex", "nope"])
def test_non_http_service_is_refused(tmp_path, monkeypatch, service):
    _write_config(tmp_path, monkeypatch, PORTS)
    with pytest.raises(ValueError):
        resolver.resolve(service)


@pytest.mark.parametrize("text,fragment", [
    (PATHS.replace("verify_tls = true", "verify_tls = false"), "verify_tls=false"),
    (PATHS.replace("    ca_path = ~/.firekeep/firekeep-root-ca.crt\n", ""), "requires 'ca_path'"),
    (PATHS.replace("scheme = https", "scheme = http"), "does not match base_url"),
    (PATHS.replace("base_url = https://", "base_url = http://"), "does not match base_url"),
])
def test_tls_and_scheme_guards(tmp_path, monkeypatch, text, fragment):
    path = _write_config(tmp_path, monkeypatch, text)
    with pytest.raises(ConfigError) as caught:
        resolver.resolve("cortex")
    assert fragment in str(caught.value)
    assert str(path) in str(caught.value)


def test_https_guard_is_case_insensitive(tmp_path, monkeypatch):
    text = PATHS.replace("scheme = https", "scheme = HTTPS").replace(
        "verify_tls = true", "verify_tls = false"
    )
    _write_config(tmp_path, monkeypatch, text)
    with pytest.raises(ConfigError, match="verify_tls=false"):
        resolver.resolve("cortex")


@pytest.mark.parametrize("sentinel", ["os", "OS"])
def test_os_trust_sentinel_is_case_insensitive(tmp_path, monkeypatch, sentinel):
    text = PATHS.replace("~/.firekeep/firekeep-root-ca.crt", sentinel)
    _write_config(tmp_path, monkeypatch, text)
    assert resolver.resolve("cortex").verify == resolver.OS_TRUST


def test_inline_comments_parse(tmp_path, monkeypatch):
    text = PORTS.replace("kind = ports", "kind = ports ; fixed service ports")
    _write_config(tmp_path, monkeypatch, text)
    assert resolver.resolve("cortex").mcp_url == "http://198.51.100.7:8080/mcp"


def test_firekeep_profile_is_ignored(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, PORTS)
    monkeypatch.setenv("FIREKEEP_PROFILE", "office")
    assert resolver.resolve("cortex").mcp_url == "http://198.51.100.7:8080/mcp"
