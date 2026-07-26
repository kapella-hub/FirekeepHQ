"""The shipped defaults must be safe on a machine nobody has hardened yet.

Why this file exists
--------------------
Two defaults combined to publish this stack's own admin surface to the public
internet: every app port was published on `0.0.0.0`, and `AUTH_ENABLED`
defaulted to `false`, which makes every caller anonymous. `GET /vault/secrets`
and `POST /auth/keys` therefore answered anyone who could reach the port. Twelve
real secrets left a VPS that way — behind an *active* ufw, because Docker
publishes a port by writing its own iptables DOCKER chain and that chain is
evaluated before ufw's INPUT chain.

Nothing caught it. Both values were syntactically fine, documented, and
deliberate at the time. The only durable guard is an assertion on the shipped
default itself, so this file pins the four properties that were wrong:

  * every published port names an explicit host interface, and it is never
    all-interfaces
  * app ports follow ${BIND_ADDR}, which defaults to loopback
  * datastore ports do NOT follow BIND_ADDR — widening app access must never
    widen database access
  * every AUTH_ENABLED default is true, and .env.example agrees

Two traps this file is deliberately built to avoid:

  * It enumerates ports out of the parsed YAML rather than grepping for
    "0.0.0.0". A grep would both miss a future service that publishes a bare
    "9000:9000" (no interface = all interfaces) and fire on `MCP_HOST:
    "0.0.0.0"`, which is the IN-CONTAINER bind and must stay as it is —
    test_in_container_binds_are_not_narrowed guards that from the other side.
  * The parity check against .env.example covers AUTH_ENABLED and BIND_ADDR
    only. A general every-var-must-match sweep flags legitimate divergence and
    gets deleted by the next person who hits one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
BASE = REPO / "docker-compose.yml"
OFFICE = REPO / "docker-compose.office.yml"
TEST_COMPOSE = REPO / "docker-compose.test.yml"
ENV_EXAMPLE = REPO / ".env.example"

COMPOSE_FILES = (BASE, OFFICE, TEST_COMPOSE)

# The published app surface. Keyed by service so a deleted `ports:` block is a
# failure rather than a silently-skipped loop.
APP_PORTS = {
    "cortex-api": "8100",
    "cortex-mcp": "8080",
    "bridge": "8070",
    "sentinel": "8060",
    "relay": "8050",
    "dashboard": "8040",
}

# Datastores. Loopback-only, always — not parameterised by BIND_ADDR.
DATASTORE_SERVICES = ("neo4j", "qdrant", "redis", "ollama")

# Every service whose process enforces X-API-Key auth.
AUTH_SERVICES = ("cortex-api", "cortex-mcp", "bridge", "sentinel", "relay")

BIND_ADDR_EXPR = "${BIND_ADDR:-127.0.0.1}"
LOOPBACK = "127.0.0.1"

# The one deliberate all-interfaces binding in the repo: Caddy in the OFFICE
# overlay is the single network-reachable surface of that deployment, it
# terminates TLS with the internal CA, and the overlay only applies when an
# operator explicitly passes `-f docker-compose.office.yml`. Exempted by name
# in one named file so a new all-interfaces binding anywhere else still fails.
ALL_INTERFACES_EXEMPT = {(OFFICE.name, "caddy")}

# Host-IP values that mean "every interface on this host".
ALL_INTERFACES = {"0.0.0.0", "::", "[::]", "*", ""}


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader that tolerates compose's merge-control tags.

    `ports: !override` is how docker-compose.office.yml replaces the base list
    instead of appending to it. PyYAML has no constructor for that tag and
    raises, so register a pass-through: the tag controls compose's merge, and
    the node underneath it is ordinary YAML.
    """


class _TaggedList(list):
    """A sequence that remembers the compose merge tag it was written with."""

    tag: str | None = None


def _passthrough(loader, node):
    if isinstance(node, yaml.SequenceNode):
        # Keep the tag on the value. Reading it back off the parsed structure
        # beats re-finding `ports: !override` in the raw text: the service
        # blocks it would have to slice up are whitespace-delimited, and the
        # first attempt at that broke on the last block in the file.
        out = _TaggedList(loader.construct_sequence(node, deep=True))
        out.tag = node.tag
        return out
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return loader.construct_scalar(node)


for _tag in ("!override", "!reset"):
    _ComposeLoader.add_constructor(_tag, _passthrough)


def _load(path: Path) -> dict:
    # yaml.load with an explicit SafeLoader SUBCLASS, not unsafe_load: the only
    # constructors added above are pass-throughs for two compose merge tags, so
    # !!python/object is still unconstructable here.
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_ComposeLoader) or {}


def _code_only(text: str) -> str:
    """Strip YAML comments before any raw-text scan.

    Two failure modes, and this file hit the first one while being written:
    explaining `${BIND_ADDR}` in a comment tripped the check that the default
    is always spelled out, and the `--host 0.0.0.0` mentioned in that same
    comment would have made test_in_container_binds_are_not_narrowed pass even
    if the real bind were narrowed — a vacuous assertion, which is worse than
    none. Same idiom and same reason as test_install_health_probe.py.

    `(?<!\\S)#` matches a `#` only at line start or after whitespace, which is
    exactly when YAML begins a comment outside a quoted scalar.
    """
    return "\n".join(re.sub(r"(?<!\S)#.*$", "", ln) for ln in text.splitlines())


def _host_ip(spec) -> str:
    """The host interface a published-port entry binds to.

    Returns "" when the entry names none, which is NOT the same as harmless:
    docker publishes an interface-less mapping on every interface, so the
    all-interfaces assertion has to treat it exactly like an explicit 0.0.0.0.
    """
    if isinstance(spec, dict):  # long syntax
        return str(spec.get("host_ip", ""))
    spec = str(spec)
    # "${BIND_ADDR:-127.0.0.1}:8100:8000" — the interpolation contains a colon
    # of its own, so peel the whole ${...} off before splitting the remainder.
    interpolated = re.match(r"^(\$\{[^}]*\})(?::|$)", spec)
    if interpolated:
        return interpolated.group(1)
    parts = spec.split(":")
    return parts[0] if len(parts) == 3 else ""


def _published(path: Path):
    """(service, port-entry) for every published port in one compose file."""
    for service, body in (_load(path).get("services") or {}).items():
        for entry in (body or {}).get("ports") or []:
            yield service, entry


def _ports_of(path: Path, service: str) -> list:
    body = (_load(path).get("services") or {}).get(service) or {}
    return list(body.get("ports") or [])


# --------------------------------------------------------------------------
# Published ports
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_no_published_port_binds_every_interface(path: Path) -> None:
    """The exposure itself. Also fails on an interface-less "8100:8000"."""
    for service, entry in _published(path):
        if (path.name, service) in ALL_INTERFACES_EXEMPT:
            continue
        host_ip = _host_ip(entry)
        assert host_ip not in ALL_INTERFACES, (
            f"{path.name}: {service} publishes {entry!r} on every interface. "
            f"Bind it to {BIND_ADDR_EXPR} (app service) or {LOOPBACK} "
            f"(datastore). A host firewall will not contain this: Docker's own "
            f"iptables chain is evaluated before ufw's INPUT chain."
        )


@pytest.mark.parametrize("service,port", sorted(APP_PORTS.items()))
def test_app_ports_follow_bind_addr(service: str, port: str) -> None:
    """One knob, defaulting to loopback, for the whole published app surface."""
    entries = _ports_of(BASE, service)
    assert entries, f"{service} publishes no ports — has the mapping been dropped?"
    assert any(str(e).startswith(f"{BIND_ADDR_EXPR}:{port}:") for e in entries), (
        f"{service} does not publish host port {port} via {BIND_ADDR_EXPR}; got {entries!r}"
    )


@pytest.mark.parametrize("service", DATASTORE_SERVICES)
def test_datastore_ports_are_literal_loopback(service: str) -> None:
    """BIND_ADDR must not reach the datastores.

    Setting BIND_ADDR=0.0.0.0 is a decision about the app surface. Redis here
    has no password at all and Qdrant holds every memory in plaintext; if that
    one knob also published them, the fix would have re-created the leak with
    a nicer variable name.
    """
    for entry in _ports_of(BASE, service):
        assert _host_ip(entry) == LOOPBACK, (
            f"{service} publishes {entry!r}; datastore ports must stay a literal "
            f"{LOOPBACK} and must not follow BIND_ADDR"
        )


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_bind_addr_default_is_loopback(path: Path) -> None:
    """Every BIND_ADDR interpolation, wherever it appears, defaults to loopback.

    `${BIND_ADDR}` with no default expands to empty, which docker reads as all
    interfaces — the original bug with an extra step.
    """
    code = _code_only(path.read_text(encoding="utf-8"))
    for found in re.findall(r"\$\{BIND_ADDR[^}]*\}", code):
        assert found == BIND_ADDR_EXPR, (
            f"{path.name}: {found} — BIND_ADDR must carry the :-{LOOPBACK} default"
        )


def test_in_container_binds_are_not_narrowed() -> None:
    """The other half: 0.0.0.0 is correct INSIDE the container.

    uvicorn's --host and the three MCP_HOST vars are what let the compose
    network reach the process at all. Narrowing them to 127.0.0.1 while
    chasing the string "0.0.0.0" would make every service unreachable —
    including from the published mapping itself.
    """
    text = _code_only(BASE.read_text(encoding="utf-8"))
    assert "--host 0.0.0.0" in text, "cortex-api's in-container uvicorn bind was narrowed"
    for var in ("MCP_HOST", "NB_MCP_HOST", "NS_MCP_HOST", "NR_MCP_HOST"):
        assert f'{var}: "0.0.0.0"' in text, f"{var} was narrowed; the service becomes unreachable"


# --------------------------------------------------------------------------
# Office overlay
# --------------------------------------------------------------------------


def test_office_port_overrides_replace_rather_than_merge() -> None:
    """Compose MERGES list-valued keys by appending unless tagged `!override`.

    Without the tag the office overlay's 127.0.0.1 binding is added to the
    base binding rather than replacing it, and `up` dies with "address already
    in use" — which has already happened in this repo.
    """
    overriding = 0
    for service, body in (_load(OFFICE).get("services") or {}).items():
        ports = (body or {}).get("ports")
        if ports is None or service == "caddy":
            continue  # caddy is defined here, not overridden — nothing to merge with
        assert getattr(ports, "tag", None) in ("!override", "!reset"), (
            f"office overlay: {service} redefines ports without !override — the "
            f"lists merge by appending and the host port is bound twice"
        )
        overriding += 1
    assert overriding == len(APP_PORTS), (
        f"expected all {len(APP_PORTS)} app services to be rebound by the office "
        f"overlay, found {overriding} — a service left on the base binding is "
        f"published outside Caddy"
    )


@pytest.mark.parametrize("service", sorted(APP_PORTS))
def test_office_pins_app_services_to_literal_loopback(service: str) -> None:
    """The overlay must not inherit BIND_ADDR.

    Caddy is the office deployment's only reachable surface. An operator who
    sets BIND_ADDR=0.0.0.0 for an unrelated reason must not thereby publish
    the app services next to it.
    """
    for entry in _ports_of(OFFICE, service):
        assert _host_ip(entry) == LOOPBACK, (
            f"office overlay: {service} publishes {entry!r}; it must pin a literal "
            f"{LOOPBACK}, not follow BIND_ADDR"
        )


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


def _auth_default(service: str) -> str:
    env = ((_load(BASE).get("services") or {}).get(service) or {}).get("environment") or {}
    assert "AUTH_ENABLED" in env, (
        f"{service} no longer declares AUTH_ENABLED — it would fall back to the "
        f"application default, which is not what this file is asserting"
    )
    return str(env["AUTH_ENABLED"])


@pytest.mark.parametrize("service", AUTH_SERVICES)
def test_auth_enabled_defaults_to_true(service: str) -> None:
    """Auth off hands every caller `scopes: ["*"]`, which opens the vault."""
    assert _auth_default(service) == "${AUTH_ENABLED:-true}", (
        f"{service}: AUTH_ENABLED default is {_auth_default(service)!r}. Off, "
        f"GET /vault/secrets and POST /auth/keys answer any caller who reaches "
        f"the port."
    )


def test_every_service_that_enforces_auth_declares_it() -> None:
    """A service that stops declaring AUTH_ENABLED silently opts itself out.

    Asserted as a set rather than a count so both a dropped service and a new
    undeclared one are visible in the failure message.
    """
    declaring = {
        name
        for name, body in (_load(BASE).get("services") or {}).items()
        if "AUTH_ENABLED" in ((body or {}).get("environment") or {})
    }
    assert declaring == set(AUTH_SERVICES), (
        f"services declaring AUTH_ENABLED changed: {sorted(declaring)} != "
        f"{sorted(AUTH_SERVICES)}"
    )


# --------------------------------------------------------------------------
# .env.example must agree with the compose defaults
# --------------------------------------------------------------------------


def _env_example_value(key: str) -> str | None:
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return None


def test_env_example_enables_auth() -> None:
    """install.sh copies this file to .env, so it is the effective default.

    A `false` here silently beats the compose `:-true` — compose interpolates
    from .env — which would make the flip cosmetic on exactly the fresh
    installs it is meant to protect.
    """
    assert _env_example_value("AUTH_ENABLED") == "true", (
        f"AUTH_ENABLED={_env_example_value('AUTH_ENABLED')} in .env.example "
        f"overrides the compose default for every fresh install"
    )


def test_env_example_bind_addr_matches_compose_default() -> None:
    assert _env_example_value("BIND_ADDR") == LOOPBACK, (
        f"BIND_ADDR={_env_example_value('BIND_ADDR')} in .env.example disagrees "
        f"with the compose default {BIND_ADDR_EXPR}"
    )


def test_env_example_warns_that_a_host_firewall_does_not_contain_docker() -> None:
    """The single fact that made the real leak survive an active ufw.

    An operator setting BIND_ADDR=0.0.0.0 will reason "it's fine, ufw is on".
    It is not fine, and the file has to say so *where they are making the
    change* — so this reads only the block an operator has in front of them
    while editing the value, not the whole file. Searching the whole file
    would keep passing once the warning drifted into some unrelated comment,
    which is the failure mode a documentation assertion is most prone to.
    """
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    start = text.index("# --- Network exposure ---")
    block = text[start : text.index("\nBIND_ADDR=", start)].lower()
    assert "ufw" in block and "iptables" in block, (
        "the BIND_ADDR block must explain that Docker's iptables chain is "
        "evaluated before ufw's INPUT chain, so a host firewall does not "
        "contain a published port"
    )
