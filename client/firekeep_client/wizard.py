"""Interactive config prompts for `firekeep install` (stdlib-only).

Lives outside cli.py so the whole flow is testable without a TTY: `ask` is injected, so a
test drives it with a scripted list of answers. The wizard never touches the filesystem —
it takes a ConfigParser and returns it mutated; cli.py owns reading, writing, and chmod.

Re-running install must be safe: non-secret prompts are prefilled with the CURRENT value,
and a blank API-key answer keeps the existing secret without printing it into the prompt.
"""
from __future__ import annotations

import configparser
import getpass
import os
import socket
import ssl
import sys
import urllib.parse

PLACEHOLDER_AGENT_ID = "CHANGEME"

# Default single-server shape: fixed service ports on localhost. Existing
# path-routed configs keep their shape when the installer is re-run.
_SERVER_DEFAULTS = {
    "kind": "ports",
    "scheme": "http",
    "host": "127.0.0.1",
    "verify_tls": "false",
}
_PATHS_DEFAULTS = {
    "kind": "paths",
    "scheme": "https",
    "base_url": "https://firekeep.example",
    "verify_tls": "true",
    "ca_path": "~/.firekeep/firekeep-root-ca.crt",
    "api_key": "",
}


def is_interactive(stream=None) -> bool:
    """Prompt only when there is a human on the other end. A piped/CI/scripted install
    (`./install < /dev/null`) must never block waiting on stdin."""
    stream = sys.stdin if stream is None else stream
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):  # closed or non-tty-like stream
        return False


def console_ask(prompt: str, default: str = "") -> str:
    """Default `ask`: print `prompt [default]: `, return the typed value or the default.

    EOF (^D, or stdin closing mid-install) is not an error — it means 'take the defaults
    and stop asking', which is exactly what the default value already is."""
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        print()
        return default
    return answer or default


def _default_agent_id(cfg: configparser.ConfigParser) -> str:
    """Best guess at who this is, in descending order of how much the user meant it:
    an already-configured agent_id, then the OS username. CHANGEME never wins — it is the
    placeholder we are here to eliminate."""
    current = cfg.get("identity", "agent_id", fallback="").strip()
    if current and current != PLACEHOLDER_AGENT_ID:
        return current
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - no OS username (rare container case); ask blind
        return ""


def _ensure_section(cfg: configparser.ConfigParser, name: str, defaults: dict) -> None:
    if not cfg.has_section(name):
        cfg.add_section(name)
    for key, value in defaults.items():
        if not cfg.has_option(name, key):
            cfg.set(name, key, value)


def _prompt_api_key(cfg, ask, fresh_prompt="API key") -> None:
    """Prompt without ever rendering the existing credential as a console default."""
    current = cfg.get("server", "api_key", fallback="")
    has_existing = bool(current.strip())
    prompt = "API key (Enter keeps existing)" if has_existing else fresh_prompt
    replacement = ask(prompt, "")
    if replacement:
        cfg.set("server", "api_key", replacement)
    elif not has_existing and cfg.has_option("server", "api_key"):
        cfg.remove_option("server", "api_key")


def _configure_ports(cfg, ask) -> None:
    _ensure_section(cfg, "server", _SERVER_DEFAULTS)
    for key in ("base_url", "ca_path"):
        cfg.remove_option("server", key)
    host = ask("Server host (IP or hostname)",
               cfg.get("server", "host", fallback="127.0.0.1"))
    cfg.set("server", "host", host)
    _prompt_api_key(cfg, ask, "API key (blank if AUTH_ENABLED=false)")


def _fetch_org_defaults(cfg, timeout: float = 3.0) -> dict:
    """Fetch <dist base>/latest/org-defaults.json — org-published prefills for the
    server connection (published only via the organization registry, never public
    GitHub Pages). Best-effort: any failure (no [dist] section — checkout
    installs, network, bad JSON) returns {} and the wizard prompts as before.
    """
    try:
        import json
        import urllib.request

        from firekeep_client import updater

        base = os.environ.get("FIREKEEP_DIST_BASE", "").rstrip("/")
        if not base:
            base = updater.dist_base(cfg)  # raises when no [dist] section
        ctx = updater._dist_ssl_context()
        req = urllib.request.Request(f"{base}/latest/org-defaults.json")
        kwargs = {"timeout": timeout}
        if ctx is not None:
            kwargs["context"] = ctx
        with urllib.request.urlopen(req, **kwargs) as resp:  # noqa: S310 — https release host
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — prefill sugar must never block an install
        return {}


def _probe_os_trust(base_url: str, timeout: float = 5.0) -> bool:
    """True if base_url's TLS certificate verifies against the OS trust store.

    Read-only handshake, best-effort: ANY failure (bad URL, unreachable host,
    unverifiable chain, no truststore) returns False and the wizard falls back
    to the ca_path file prompt — this probe can only ever improve the default,
    never block the install.
    """
    try:
        parts = urllib.parse.urlparse(base_url)
        if parts.scheme != "https" or not parts.hostname:
            return False
        try:
            import truststore
            ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except ImportError:
            ctx = ssl.create_default_context()
        with socket.create_connection((parts.hostname, parts.port or 443), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=parts.hostname):
                return True
    except Exception:
        return False


def _configure_paths(cfg, ask, probe=_probe_os_trust, fetch_defaults=_fetch_org_defaults) -> None:
    _ensure_section(cfg, "server", _PATHS_DEFAULTS)
    cfg.remove_option("server", "host")
    # Org-published prefills — only fill prompts whose
    # current value is empty/skeleton; a configured machine is never overridden.
    org = {}
    current_base = cfg.get("server", "base_url", fallback="")
    if current_base in ("", _PATHS_DEFAULTS["base_url"]):
        defaults = fetch_defaults(cfg)
        org = defaults.get("server", {}) or defaults.get("office", {}) or {}
        if org.get("base_url"):
            current_base = str(org["base_url"])
            print("firekeep: server connection prefilled from your org's published defaults",
                  file=sys.stderr)
    base = ask("Server base URL", current_base)
    cfg.set("server", "base_url", base)
    if org.get("ca_path") and cfg.get("server", "ca_path", fallback="") in (
            "", _PATHS_DEFAULTS["ca_path"]):
        cfg.set("server", "ca_path", str(org["ca_path"]))
    # Default ca_path to the OS-trust sentinel when the server's cert already
    # verifies against the OS store (corporate CA managed by MDM) — but never
    # override a ca_path the user has deliberately configured to something else.
    current_ca = cfg.get("server", "ca_path", fallback=_PATHS_DEFAULTS["ca_path"])
    default_ca = current_ca
    if current_ca in ("", "os", _PATHS_DEFAULTS["ca_path"]) and probe(base):
        if current_ca != "os":
            print("firekeep: server certificate verifies against the OS trust store — "
                  "no CA file needed (Enter accepts 'os')", file=sys.stderr)
        default_ca = "os"
    cfg.set("server", "ca_path",
            ask("Internal CA cert path ('os' = use the OS trust store)",
                default_ca))
    _prompt_api_key(cfg, ask)


def prompt_config(
    cfg: configparser.ConfigParser,
    *,
    ask=console_ask,
    agent_id: str | None = None,
    host: str | None = None,
    probe=_probe_os_trust,
    fetch_defaults=_fetch_org_defaults,
) -> configparser.ConfigParser:
    """Walk the install prompts, mutating and returning `cfg`.

    `agent_id` / `host` are the CLI flags: each seeds its prompt's default
    rather than suppressing it, so `--host 10.0.0.4` interactively means 'suggest this',
    while the same flag under --non-interactive (which never calls here) means 'use this'.
    """
    _ensure_section(cfg, "identity", {"agent_id": PLACEHOLDER_AGENT_ID})
    _ensure_section(cfg, "server", _SERVER_DEFAULTS)
    identity = ask(
        "Agent identity (attributes every memory, session, and replay event)",
        agent_id or _default_agent_id(cfg),
    )
    cfg.set("identity", "agent_id", identity or PLACEHOLDER_AGENT_ID)

    if host:
        cfg.set("server", "kind", "ports")
        cfg.set("server", "scheme", "http")
        cfg.set("server", "verify_tls", "false")
        cfg.set("server", "host", host)
    if cfg.get("server", "kind", fallback="ports").strip().lower() == "paths":
        _configure_paths(cfg, ask, probe=probe, fetch_defaults=fetch_defaults)
    else:
        _configure_ports(cfg, ask)
    return cfg


def set_dist_base(cfg: configparser.ConfigParser, base: str) -> None:
    """Record where this kit was installed from, so `firekeep update` can find its way home.

    Never prompted for and never hardcoded: the bootstrap was fetched from this URL, so it
    is the one component that already knows it. A checkout install simply has no [dist]
    section, and `firekeep update` says so plainly."""
    if not cfg.has_section("dist"):
        cfg.add_section("dist")
    cfg.set("dist", "base_url", base.rstrip("/"))
