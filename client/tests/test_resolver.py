from pathlib import Path

import configparser
import textwrap

import pytest

from firekeep_client import resolver
from firekeep_client.resolver import ConfigError


def _write_config(tmp_path, monkeypatch, text: str):
    """Write an INI to tmp_path and point FIREKEEP_CONFIG at it. Never touches real ~."""
    cfg_file = tmp_path / "config"
    cfg_file.write_text(textwrap.dedent(text), encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(cfg_file))
    monkeypatch.delenv("FIREKEEP_AGENT_ID", raising=False)
    return cfg_file


PERSONAL = """\
    [active]
    profile = personal

    [personal]
    kind = ports
    scheme = http
    host = 198.51.100.7
    verify_tls = false
    agent_id = mogan
"""


def test_load_config_missing_file_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_CONFIG", str(tmp_path / "does-not-exist"))
    with pytest.raises(ConfigError):
        resolver.load_config()


def test_load_config_reads_env_path(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, PERSONAL)
    cfg = resolver.load_config()
    assert cfg.get("active", "profile") == "personal"


def test_load_config_explicit_path_wins_over_env(tmp_path, monkeypatch):
    good = tmp_path / "explicit"
    good.write_text(textwrap.dedent(PERSONAL), encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(tmp_path / "nope"))
    cfg = resolver.load_config(good)
    assert cfg.get("active", "profile") == "personal"


def test_active_profile_returns_pointer(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, PERSONAL)
    assert resolver.active_profile(resolver.load_config()) == "personal"


def test_active_profile_missing_active_section_raises(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, """\
        [personal]
        kind = ports
        agent_id = mogan
    """)
    with pytest.raises(ConfigError):
        resolver.active_profile(resolver.load_config())


def test_active_profile_dangling_pointer_raises(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, """\
        [active]
        profile = ghost
    """)
    with pytest.raises(ConfigError):
        resolver.active_profile(resolver.load_config())


@pytest.mark.parametrize("env_val, expected", [
    (None, "mogan"),                 # unset -> profile value is the default
    ("agent-beta", "agent-beta"),    # env override WINS
    ("", "mogan"),                   # empty env is not "set" -> falls through to profile
])
def test_agent_id_env_override(tmp_path, monkeypatch, env_val, expected):
    _write_config(tmp_path, monkeypatch, PERSONAL)
    cfg = resolver.load_config()
    if env_val is None:
        monkeypatch.delenv("FIREKEEP_AGENT_ID", raising=False)
    else:
        monkeypatch.setenv("FIREKEEP_AGENT_ID", env_val)
    assert resolver.agent_id(cfg, "personal") == expected


def test_agent_id_missing_key_raises(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, """\
        [active]
        profile = personal

        [personal]
        kind = ports
        scheme = http
        host = 1.2.3.4
        verify_tls = false
    """)
    cfg = resolver.load_config()
    monkeypatch.delenv("FIREKEEP_AGENT_ID", raising=False)
    with pytest.raises(ConfigError):
        resolver.agent_id(cfg, "personal")


# personal (kind=ports) + office (kind=paths) in one file; select via profile= arg.
BOTH = """\
    [active]
    profile = personal

    [personal]
    kind = ports
    scheme = http
    host = 198.51.100.7
    verify_tls = false
    agent_id = mogan

    [office]
    kind = paths
    scheme = https
    base_url = https://firekeep.office.example
    verify_tls = true
    ca_path = ~/.firekeep/firekeep-root-ca.crt
    api_key = nxs_secret_key_123
    agent_id = mogan
"""

PORTS_EXPECTED = {
    "cortex":   ("http://198.51.100.7:8080/mcp", "http://198.51.100.7:8100"),
    "bridge":   ("http://198.51.100.7:8070/mcp", "http://198.51.100.7:8070"),
    "sentinel": ("http://198.51.100.7:8060/mcp", "http://198.51.100.7:8060"),
    "relay":    ("http://198.51.100.7:8050/mcp", "http://198.51.100.7:8050"),
}
PATHS_EXPECTED = {
    "cortex":   ("https://firekeep.office.example/mcp/cortex", "https://firekeep.office.example/api/cortex"),
    "bridge":   ("https://firekeep.office.example/mcp/bridge", "https://firekeep.office.example/api/bridge"),
    "sentinel": ("https://firekeep.office.example/mcp/sentinel", "https://firekeep.office.example/api/sentinel"),
    "relay":    ("https://firekeep.office.example/mcp/relay", "https://firekeep.office.example/api/relay"),
}

ALL_SERVICES = ["cortex", "bridge", "sentinel", "relay"]


@pytest.mark.parametrize("service", ALL_SERVICES)
def test_resolve_ports_urls(tmp_path, monkeypatch, service):
    _write_config(tmp_path, monkeypatch, BOTH)
    ep = resolver.resolve(service, resolver.load_config(), profile="personal")
    mcp, rest = PORTS_EXPECTED[service]
    assert ep.mcp_url == mcp
    assert ep.rest_base == rest


@pytest.mark.parametrize("service", ALL_SERVICES)
def test_resolve_paths_urls(tmp_path, monkeypatch, service):
    _write_config(tmp_path, monkeypatch, BOTH)
    ep = resolver.resolve(service, resolver.load_config(), profile="office")
    mcp, rest = PATHS_EXPECTED[service]
    assert ep.mcp_url == mcp
    assert ep.rest_base == rest


def test_resolve_ports_rest_map_is_corrected(tmp_path, monkeypatch):
    # Regression pin: only cortex splits REST onto 8100; the others share MCP port.
    _write_config(tmp_path, monkeypatch, BOTH)
    cfg = resolver.load_config()
    assert resolver.resolve("cortex", cfg, profile="personal").rest_base.endswith(":8100")
    assert resolver.resolve("bridge", cfg, profile="personal").rest_base.endswith(":8070")
    assert resolver.resolve("sentinel", cfg, profile="personal").rest_base.endswith(":8060")
    assert resolver.resolve("relay", cfg, profile="personal").rest_base.endswith(":8050")
    assert resolver.MCP_PORTS != resolver.REST_PORTS  # cortex differs


def test_resolve_personal_headers_no_key_no_session(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, BOTH)
    ep = resolver.resolve("cortex", resolver.load_config(), profile="personal")
    assert ep.headers["X-Agent-Id"] == "mogan"
    assert "X-API-Key" not in ep.headers
    assert "X-Session-Id" not in ep.headers


def test_resolve_office_headers_include_key(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, BOTH)
    ep = resolver.resolve("relay", resolver.load_config(), profile="office")
    assert ep.headers["X-Agent-Id"] == "mogan"
    assert ep.headers["X-API-Key"] == "nxs_secret_key_123"


def test_resolve_session_id_header_when_given(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, BOTH)
    ep = resolver.resolve("cortex", resolver.load_config(), profile="personal", session_id="s-123")
    assert ep.headers["X-Session-Id"] == "s-123"


def test_resolve_agent_id_env_override_rides_headers(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, BOTH)
    cfg = resolver.load_config()
    monkeypatch.setenv("FIREKEEP_AGENT_ID", "agent-beta")
    ep = resolver.resolve("cortex", cfg, profile="personal")
    assert ep.headers["X-Agent-Id"] == "agent-beta"


def test_resolve_symdex_refused(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, BOTH)
    with pytest.raises(ValueError):
        resolver.resolve("symdex", resolver.load_config(), profile="office")


def test_resolve_unknown_service_refused(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, BOTH)
    with pytest.raises(ValueError):
        resolver.resolve("nope", resolver.load_config(), profile="personal")


def test_resolve_default_profile_uses_active_pointer(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, BOTH)  # [active] profile = personal
    ep = resolver.resolve("cortex", resolver.load_config())  # profile omitted
    assert ep.mcp_url == "http://198.51.100.7:8080/mcp"


def test_resolve_personal_verify_false(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, BOTH)
    assert resolver.resolve("cortex", resolver.load_config(), profile="personal").verify is False


def test_resolve_office_verify_is_ca_path(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, BOTH)
    ep = resolver.resolve("cortex", resolver.load_config(), profile="office")
    assert ep.verify == str(Path("~/.firekeep/firekeep-root-ca.crt").expanduser())


# https + verify_tls=false  -> MITM: must be refused.
MITM = """\
    [active]
    profile = office

    [office]
    kind = paths
    scheme = https
    base_url = https://firekeep.office.example
    verify_tls = false
    ca_path = ~/.firekeep/firekeep-root-ca.crt
    agent_id = mogan
"""

# https + verify_tls=true but NO ca_path -> must be refused.
NO_CA = """\
    [active]
    profile = office

    [office]
    kind = paths
    scheme = https
    base_url = https://firekeep.office.example
    verify_tls = true
    agent_id = mogan
"""


def test_resolve_https_verify_tls_false_is_refused(tmp_path, monkeypatch):
    # SECURITY GUARD: unverified TLS on an https profile is a MITM hole -> fail loud.
    _write_config(tmp_path, monkeypatch, MITM)
    with pytest.raises(ConfigError):
        resolver.resolve("cortex", resolver.load_config(), profile="office")


def test_resolve_https_missing_ca_path_is_refused(tmp_path, monkeypatch):
    # SECURITY GUARD: https requires the internal CA cert path.
    _write_config(tmp_path, monkeypatch, NO_CA)
    with pytest.raises(ConfigError):
        resolver.resolve("cortex", resolver.load_config(), profile="office")


def test_resolve_http_verify_is_false(tmp_path, monkeypatch):
    # http has no TLS to verify -> verify is False (regression from Task 4).
    _write_config(tmp_path, monkeypatch, BOTH)
    ep = resolver.resolve("bridge", resolver.load_config(), profile="personal")
    assert ep.verify is False


def test_resolve_https_verify_is_ca_path_string(tmp_path, monkeypatch):
    # https + verify_tls=true + ca_path -> verify is the expanduser'd ca_path string.
    _write_config(tmp_path, monkeypatch, BOTH)
    ep = resolver.resolve("relay", resolver.load_config(), profile="office")
    assert isinstance(ep.verify, str)
    assert ep.verify == str(Path("~/.firekeep/firekeep-root-ca.crt").expanduser())


# https written as "HTTPS" (case typo) + verify_tls=false -> guard must STILL fire.
MITM_UPPER = """\
    [active]
    profile = office

    [office]
    kind = paths
    scheme = HTTPS
    base_url = https://firekeep.office.example
    verify_tls = false
    ca_path = ~/.firekeep/firekeep-root-ca.crt
    agent_id = mogan
"""


def test_resolve_https_guard_is_case_insensitive(tmp_path, monkeypatch):
    """A scheme typo like 'HTTPS' must NOT bypass the verify guard (MITM)."""
    _write_config(tmp_path, monkeypatch, MITM_UPPER)
    with pytest.raises(ConfigError):
        resolver.resolve("cortex", resolver.load_config(), profile="office")


# The design spec's canonical example config (§4.1) uses inline `;` comments —
# it must parse (a user copying the spec's example must not get ConfigError).
SPEC_EXAMPLE = """\
    [active]
    profile = personal

    [personal]
    kind      = ports                 ; host:port/mcp URL style
    scheme    = http
    host      = 198.51.100.7
    verify_tls = false
    agent_id  = mogan
    ; api_key omitted
"""


def test_spec_canonical_example_parses(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, SPEC_EXAMPLE)
    ep = resolver.resolve("cortex", resolver.load_config(), profile="personal")
    assert ep.mcp_url == "http://198.51.100.7:8080/mcp"  # comment stripped from kind


# kind=paths scheme/base_url mismatch: verify is computed from `scheme` but the
# actual URL is built from `base_url`. If they disagree, a real https endpoint
# can end up with verify=False (unverified TLS handshake -> MITM hole).
SCHEME_HTTP_BASE_HTTPS = """\
    [active]
    profile = office

    [office]
    kind = paths
    scheme = http
    base_url = https://firekeep.office.example
    verify_tls = false
    agent_id = mogan
"""

SCHEME_HTTPS_BASE_HTTP = """\
    [active]
    profile = office

    [office]
    kind = paths
    scheme = https
    base_url = http://firekeep.office.example
    verify_tls = true
    ca_path = ~/.firekeep/firekeep-root-ca.crt
    agent_id = mogan
"""


def test_resolve_paths_scheme_http_base_https_is_refused(tmp_path, monkeypatch):
    # SECURITY GUARD: scheme=http would compute verify=False, but base_url is a
    # REAL https URL -> unverified TLS handshake on a genuine TLS endpoint (MITM).
    _write_config(tmp_path, monkeypatch, SCHEME_HTTP_BASE_HTTPS)
    with pytest.raises(ConfigError) as exc:
        resolver.resolve("cortex", resolver.load_config(), profile="office")
    assert "http" in str(exc.value) and "https://firekeep.office.example" in str(exc.value)


def test_resolve_paths_scheme_https_base_http_is_refused(tmp_path, monkeypatch):
    # Inverse mismatch must also be refused, not just the exploitable direction.
    _write_config(tmp_path, monkeypatch, SCHEME_HTTPS_BASE_HTTP)
    with pytest.raises(ConfigError) as exc:
        resolver.resolve("cortex", resolver.load_config(), profile="office")
    assert "https" in str(exc.value) and "http://firekeep.office.example" in str(exc.value)


def test_resolve_paths_scheme_base_url_match_passes(tmp_path, monkeypatch):
    # BOTH's office profile already has matching https scheme/base_url; pin that
    # the new guard doesn't break the happy path.
    _write_config(tmp_path, monkeypatch, BOTH)
    ep = resolver.resolve("cortex", resolver.load_config(), profile="office")
    assert ep.mcp_url == "https://firekeep.office.example/mcp/cortex"


# Task 4 tests: FIREKEEP_PROFILE override + pinned_profile helper


def _cfg(text: str) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
    cfg.read_string(text)
    return cfg


_BASE = """
[active]
profile = personal
[personal]
agent_id = tester
[office]
agent_id = tester
"""


def test_firekeep_profile_env_overrides_active(monkeypatch):
    monkeypatch.setenv("FIREKEEP_PROFILE", "office")
    assert resolver.active_profile(_cfg(_BASE)) == "office"


def test_empty_firekeep_profile_env_is_ignored(monkeypatch):
    monkeypatch.setenv("FIREKEEP_PROFILE", "   ")
    assert resolver.active_profile(_cfg(_BASE)) == "personal"


def test_firekeep_profile_env_unknown_section_raises_naming_the_source(monkeypatch):
    monkeypatch.setenv("FIREKEEP_PROFILE", "nope")
    with pytest.raises(resolver.ConfigError, match="FIREKEEP_PROFILE"):
        resolver.active_profile(_cfg(_BASE))


def test_explicit_override_beats_env(monkeypatch):
    monkeypatch.setenv("FIREKEEP_PROFILE", "office")
    assert resolver.active_profile(_cfg(_BASE), override="personal") == "personal"


def test_pinned_profile_reads_pins_section():
    cfg = _cfg(_BASE + "\n[pins]\nkiro = office\n")
    assert resolver.pinned_profile(cfg, "kiro") == "office"
    assert resolver.pinned_profile(cfg, "claude") is None


def test_pinned_profile_rejects_unsafe_names_silently():
    cfg = _cfg(_BASE + "\n[pins]\nkiro = my office\n")
    assert resolver.pinned_profile(cfg, "kiro") is None


def test_pinned_profile_absent_section():
    assert resolver.pinned_profile(_cfg(_BASE), "kiro") is None


# ca_path = os -> the OS-trust sentinel passes through verbatim (never path-expanded).
OS_TRUST_CFG = """\
    [active]
    profile = office

    [office]
    kind = paths
    scheme = https
    base_url = https://firekeep.office.example
    verify_tls = true
    ca_path = os
    agent_id = mogan
"""


def test_resolve_office_os_trust_sentinel(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, OS_TRUST_CFG)
    ep = resolver.resolve("cortex", resolver.load_config(), profile="office")
    assert ep.verify == resolver.OS_TRUST == "os"


def test_resolve_office_os_trust_is_case_insensitive(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, OS_TRUST_CFG.replace("ca_path = os", "ca_path = OS"))
    ep = resolver.resolve("cortex", resolver.load_config(), profile="office")
    assert ep.verify == "os"
