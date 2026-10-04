"""What a gateway backend child is started with.

The gateway runs wherever an agent runtime runs — a developer box, a CI runner,
the server-side ChatGPT tunnel (deploy/chatgpt-tunnel/) — and those processes
can carry server service credentials (FIREKEEP_INTERNAL_KEY and friends) in
their environment. Every backend child (the four shims, decision, and each
registered `mcp-stdio` dex/capability) used to inherit that environment whole,
and symdex's Cortex client read FIREKEEP_INTERNAL_KEY from it — so a dex child
could act with the deployment's service key instead of the member's enrolled
key. These tests pin the replacement: an allowlisted environment per child, the
member's enrolled Cortex connection passed explicitly to symdex, and a source
guard over LITERAL environment reads in the child packages. Names built from
variables (helper functions taking the name) escape the guard and are
classified in childenv.py by hand.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

from firekeep_client import childenv, dexes
from firekeep_client.gateway import Gateway

REPO = Path(__file__).resolve().parents[2]

# Server-side credentials a gateway process can plausibly hold. None of them may
# reach any backend child.
SERVER_SECRETS = {
    "FIREKEEP_INTERNAL_KEY": "nxs_server_internal",
    "FIREKEEP_BRIDGE_KEY": "nxs_bridge",
    "RELAY_INTERNAL_API_KEY": "nxs_relay",
    "DASHBOARD_API_KEY": "nxs_dashboard",
    "VAULT_KEY": "fernet-vault-key",
    "NEO4J_PASSWORD": "neo4j-pw",
    "NS_FIREKEEP_INTERNAL_KEY": "nxs_ns_internal",
    "FIREKEEP_SIGNING_KEY": "minisign-secret",
    "FIREKEEP_JOIN": "join-code-secret",
    "CONTROL_PLANE_API_KEY": "tunnel-control-plane",
    "AWS_SECRET_ACCESS_KEY": "aws-secret",
}

_ENV_ECHO_SERVER = r'''
import json, os, sys
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    if message["method"] == "initialize":
        result = {"protocolVersion":"2025-03-26","capabilities":{"tools":{}},"serverInfo":{"name":"env-echo","version":"1"}}
    elif message["method"] == "tools/list":
        result = {"tools":[{"name":"show_child_env","inputSchema":{"type":"object"}}]}
    else:
        result = {"content":[{"type":"text","text":json.dumps(dict(os.environ))}]}
    print(json.dumps({"jsonrpc":"2.0","id":message["id"],"result":result}), flush=True)
'''


def _write_server_config(path, *, agent_id, api_key=None, kind="ports", ca_path=None):
    if kind == "paths":
        server = (
            "kind = paths\n"
            "scheme = https\n"
            f"base_url = https://{agent_id}.firekeep.example\n"
            "verify_tls = true\n"
            f"ca_path = {ca_path or 'os'}\n"
        )
        expected_url = f"https://{agent_id}.firekeep.example/api/cortex"
    else:
        server = (
            "kind = ports\n"
            "scheme = http\n"
            f"host = {agent_id}.firekeep.example\n"
            "verify_tls = false\n"
        )
        expected_url = f"http://{agent_id}.firekeep.example:8100"
    if api_key:
        server += f"api_key = {api_key}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"[identity]\nagent_id = {agent_id}\n\n[server]\n{server}",
        encoding="utf-8",
    )
    return expected_url


def _backend(gateway, name):
    return next(item for item in gateway.backends if item.name == name)


def _child_env(backend) -> dict[str, str]:
    """Start the backend's REAL launch path with an env-echo program in place of
    the console script and return the environment the child actually saw."""
    backend.command = [sys.executable, "-u", "-c", _ENV_ECHO_SERVER]
    try:
        backend.discover("2025-03-26")
        assert backend.state == "ready (1 tools)", backend.state
        response = backend.request("tools/call", {"name": "show_child_env"})
        return json.loads(response["result"]["content"][0]["text"])
    finally:
        backend.close()


def _upper(environment) -> dict[str, str]:
    return {key.upper(): value for key, value in environment.items()}


@pytest.fixture
def enrolled(tmp_path, monkeypatch):
    config = Path(os.environ["FIREKEEP_CONFIG"])
    url = _write_server_config(config, agent_id="alice", api_key="nxs_alice")
    dexes.write_registry({"symdex": {"source": "bundled"}, "hands": {"source": "path"}})
    for name, value in SERVER_SECRETS.items():
        monkeypatch.setenv(name, value)
    return url


@pytest.mark.parametrize("name", ["cortex", "relay", "decision", "symdex", "hands"])
def test_no_backend_child_inherits_server_credentials(enrolled, name):
    gateway = Gateway()
    observed = _upper(_child_env(_backend(gateway, name)))

    leaked = sorted(set(SERVER_SECRETS) & set(observed))
    assert leaked == [], f"{name} child inherited {leaked}"
    # Building the child environment never scrubs the gateway's own process.
    for secret, value in SERVER_SECRETS.items():
        assert os.environ[secret] == value


@pytest.mark.parametrize("name", ["cortex", "decision", "symdex", "hands"])
def test_backend_child_keeps_what_it_needs_to_run(enrolled, monkeypatch, name):
    monkeypatch.setenv("FIREKEEP_RUNTIME", "claude")
    monkeypatch.setenv("NEXUS_AGENT_ID", "alice-laptop")
    monkeypatch.setenv("FIREKEEP_SESSION_ID", "sess-1")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp:3128")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    gateway = Gateway()
    observed = _upper(_child_env(_backend(gateway, name)))

    for required in (
        "PATH", "FIREKEEP_CONFIG", "FIREKEEP_CACHE_DIR", "FIREKEEP_LOG_DIR",
        "FIREKEEP_RUNTIME", "NEXUS_AGENT_ID", "FIREKEEP_SESSION_ID",
        "HTTPS_PROXY", "LC_ALL",
    ):
        assert required in observed, f"{name} child lost {required}"
    assert observed["FIREKEEP_CONFIG"] == os.environ["FIREKEEP_CONFIG"]
    if os.name == "nt":
        for required in ("SYSTEMROOT", "TEMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
            if required in _upper(os.environ):
                assert required in observed, f"{name} child lost {required}"


def test_symdex_child_receives_the_enrolled_member_connection(enrolled, monkeypatch):
    monkeypatch.setenv("FIREKEEP_CORTEX_URL", "https://stale.invalid")
    monkeypatch.setenv("FIREKEEP_CLIENT_API_KEY", "nxs_stale")
    gateway = Gateway()
    symdex = _backend(gateway, "symdex")
    assert "nxs_alice" not in repr(symdex)

    observed = _upper(_child_env(symdex))

    assert observed["FIREKEEP_CORTEX_URL"] == enrolled
    assert observed["FIREKEEP_CLIENT_API_KEY"] == "nxs_alice"
    assert "FIREKEEP_INTERNAL_KEY" not in observed
    assert os.environ["FIREKEEP_CORTEX_URL"] == "https://stale.invalid"
    assert os.environ["FIREKEEP_CLIENT_API_KEY"] == "nxs_stale"


def test_only_symdex_receives_the_injected_connection(enrolled):
    gateway = Gateway()
    for name in ("cortex", "decision", "hands"):
        observed = _upper(_child_env(_backend(gateway, name)))
        assert "FIREKEEP_CLIENT_API_KEY" not in observed, name
        assert "FIREKEEP_CORTEX_URL" not in observed, name


def test_symdex_paths_deployment_carries_its_trust_anchor(tmp_path, monkeypatch):
    ca = tmp_path / "internal-ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    config = Path(os.environ["FIREKEEP_CONFIG"])
    url = _write_server_config(
        config, agent_id="bob", api_key="nxs_bob", kind="paths", ca_path=str(ca)
    )
    dexes.write_registry({"symdex": {"source": "bundled"}})
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    environment = _backend(Gateway(), "symdex").env_factory()

    assert environment["FIREKEEP_CORTEX_URL"] == url
    assert environment["FIREKEEP_CLIENT_API_KEY"] == "nxs_bob"
    assert environment["FIREKEEP_CORTEX_CA"] == str(ca.resolve())
    # Never SSL_CERT_FILE: that would re-anchor symdex's OTHER https calls
    # (GitHub, LLM providers) on the internal CA and break them.
    assert "SSL_CERT_FILE" not in {key.upper() for key in environment}


def test_symdex_os_trust_deployment_says_so(monkeypatch):
    config = Path(os.environ["FIREKEEP_CONFIG"])
    _write_server_config(config, agent_id="carol", api_key="nxs_carol", kind="paths")
    dexes.write_registry({"symdex": {"source": "bundled"}})
    environment = _backend(Gateway(), "symdex").env_factory()
    assert environment["FIREKEEP_CORTEX_CA"] == "os"


def test_symdex_auth_off_deployment_gets_url_and_no_key(monkeypatch):
    config = Path(os.environ["FIREKEEP_CONFIG"])
    url = _write_server_config(config, agent_id="solo", api_key=None)
    dexes.write_registry({"symdex": {"source": "bundled"}})
    monkeypatch.setenv("FIREKEEP_CLIENT_API_KEY", "nxs_stale")
    monkeypatch.setenv("FIREKEEP_INTERNAL_KEY", "nxs_server_internal")
    environment = _upper(_backend(Gateway(), "symdex").env_factory())

    assert environment["FIREKEEP_CORTEX_URL"] == url
    assert "FIREKEEP_CLIENT_API_KEY" not in environment
    assert "FIREKEEP_INTERNAL_KEY" not in environment


def test_symdex_unenrolled_machine_gets_no_connection(monkeypatch):
    # The autouse fixture points FIREKEEP_CONFIG at a file that does not exist.
    dexes.write_registry({"symdex": {"source": "bundled"}})
    monkeypatch.setenv("FIREKEEP_CORTEX_URL", "https://stale.invalid")
    monkeypatch.setenv("FIREKEEP_CLIENT_API_KEY", "nxs_stale")
    environment = _upper(_backend(Gateway(), "symdex").env_factory())

    assert "FIREKEEP_CORTEX_URL" not in environment
    assert "FIREKEEP_CLIENT_API_KEY" not in environment


def test_symdex_gets_no_connection_while_bypassed(enrolled, monkeypatch):
    """Personal mode / FIREKEEP_BYPASS at spawn: the shims serve zero tools,
    and symdex must not quietly keep a live member connection to the Keep."""
    monkeypatch.setenv("FIREKEEP_BYPASS", "1")
    environment = _upper(_backend(Gateway(), "symdex").env_factory())
    assert "FIREKEEP_CORTEX_URL" not in environment
    assert "FIREKEEP_CLIENT_API_KEY" not in environment


def test_child_owned_secrets_reach_only_their_own_child(enrolled, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-user")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_user")
    monkeypatch.setenv("FIREKEEP_DECISION_NOTIFY_TOKEN", "notify-token")
    gateway = Gateway()
    environments = {
        name: _upper(_backend(gateway, name).env_factory())
        for name in ("cortex", "decision", "symdex", "hands")
    }

    assert environments["symdex"]["ANTHROPIC_API_KEY"] == "sk-ant-user"
    assert environments["symdex"]["GITHUB_TOKEN"] == "ghp_user"
    assert environments["decision"]["FIREKEEP_DECISION_NOTIFY_TOKEN"] == "notify-token"
    for name, environment in environments.items():
        if name != "symdex":
            assert "ANTHROPIC_API_KEY" not in environment, name
            assert "GITHUB_TOKEN" not in environment, name
        if name != "decision":
            assert "FIREKEEP_DECISION_NOTIFY_TOKEN" not in environment, name


def test_allowlist_matches_names_case_insensitively_and_keeps_their_spelling():
    source = {
        "Path": "C:\\bin",
        "http_proxy": "http://proxy:3128",
        "LocalAppData": "C:\\Users\\a\\AppData\\Local",
        "ProgramFiles(x86)": "C:\\Program Files (x86)",
        "lc_ctype": "en_US.UTF-8",
        "firekeep_internal_key": "nxs_lowercase_still_secret",
    }
    environment = childenv.child_environment("cortex", source=source)
    assert environment == {
        "Path": "C:\\bin",
        "http_proxy": "http://proxy:3128",
        "LocalAppData": "C:\\Users\\a\\AppData\\Local",
        "ProgramFiles(x86)": "C:\\Program Files (x86)",
        "lc_ctype": "en_US.UTF-8",
    }


def test_no_allowlisted_name_is_a_server_credential():
    forwarded = set(childenv.ALLOWED_NAMES)
    for extras in childenv.CHILD_EXTRAS.values():
        forwarded |= set(extras)
    assert forwarded.isdisjoint(SERVER_SECRETS)
    assert forwarded.isdisjoint(childenv.NOT_FORWARDED)
    # Shared allowlist names never look like credentials: a secret a child
    # legitimately owns is granted to THAT child via CHILD_EXTRAS only.
    secret_shaped = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?)$")
    assert not [name for name in childenv.ALLOWED_NAMES if secret_shaped.search(name)]


# --- source guard: the allowlist stays in step with what the children read ---

# Every package whose code runs inside a gateway backend child: the shim and
# decision server (client), each mcp-stdio dex/capability, and the ingest-client
# dexes (their code is reachable from the same venv, and a name they read that
# is not classified is how a new knob silently stops working).
_CHILD_SOURCES = (
    REPO / "client" / "firekeep_client",
    REPO / "symdex" / "src",
    REPO / "hands" / "src",
    REPO / "docdex" / "src",
    REPO / "maildex" / "src",
)
_READ_PATTERNS = (
    re.compile(r"""(?:os\.environ\.get|os\.getenv|environ\.get|getenv)\(\s*["']([A-Za-z_][A-Za-z0-9_()]*)["']"""),
    re.compile(r"""environ\[\s*["']([A-Za-z_][A-Za-z0-9_()]*)["']\s*\]"""),
    re.compile(r"""["']([A-Za-z_][A-Za-z0-9_()]*)["']\s+(?:not\s+)?in\s+os\.environ"""),
)


def _env_reads() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for root in _CHILD_SOURCES:
        for path in root.rglob("*.py"):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for pattern in _READ_PATTERNS:
                    for name in pattern.findall(line):
                        found.setdefault(name.upper(), set()).add(
                            f"{path.relative_to(REPO)}:{number}"
                        )
    return found


def test_source_guard_finds_the_reads_it_is_meant_to_police():
    reads = _env_reads()
    # Calibration: if these vanish the regexes broke, not the code.
    assert "FIREKEEP_CONFIG" in reads
    assert "FIREKEEP_SYMDEX_MAX_FILES" in reads
    assert "FIREKEEP_HANDS_OFFLINE" in reads


def test_every_env_name_a_child_reads_is_classified():
    classified = set(childenv.ALLOWED_NAMES) | set(childenv.NOT_FORWARDED)
    for extras in childenv.CHILD_EXTRAS.values():
        classified |= set(extras)
    unclassified = {
        name: sorted(where)
        for name, where in _env_reads().items()
        if name not in classified and not name.startswith(childenv.ALLOWED_PREFIXES)
    }
    assert unclassified == {}, (
        "classify each name in firekeep_client/childenv.py: ALLOWED_NAMES if every "
        "child may see it, CHILD_EXTRAS[<child>] if it is that child's own secret, "
        "NOT_FORWARDED if no gateway child needs it"
    )
