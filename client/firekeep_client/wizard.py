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
import shutil
import socket
import ssl
import sys
import urllib.parse
from typing import NamedTuple

PLACEHOLDER_AGENT_ID = "CHANGEME"

# --- the one question ---------------------------------------------------------
#
# What this replaced, and why. The install used to ask, unconditionally:
#
#     Server host (IP or hostname) [127.0.0.1]:
#     API key (blank if AUTH_ENABLED=false):
#
# at the one moment in the product's life when neither can exist. There was no
# "not yet" answer and no deferral, so a first-time user pressed Enter twice and
# got a syntactically valid config pointing at a server that was never there --
# and, because the config was valid, nothing downstream could tell that state
# apart from a deliberate localhost deployment.
#
# One question replaces both. It is the only prompt in the product that a machine
# genuinely cannot answer for you, because it is about your intent rather than
# your environment.
PROVISION_HERE = "provision"
JOIN_WITH_CODE = "join"
EXISTING_SERVER = "existing"
DECIDE_LATER = "later"

#: Written into [server] by the installer skeleton and removed by the first real
#: connection write. `config_write.upsert_server` replaces the whole [server]
#: section, so successfully joining or connecting clears this by construction --
#: there is no second place to remember to unset it.
UNCONFIGURED_MARKER = "configured"


class Plan(NamedTuple):
    """What the human said to do, for the caller to actually carry out.

    The wizard deliberately performs none of it: it owns questions, cli.py owns
    side effects. That split is what keeps the whole flow testable by handing
    `ask` a scripted list of answers.
    """

    cfg: configparser.ConfigParser
    action: str
    join_code: str = ""
    #: Rules/AGENTS.md path for an MCP client the kit ships no adapter for, or
    #: None when skipped. Carried, never acted on: cli.py persists it.
    generic_agents_md: str | None = None

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
        ctx = updater.dist_ssl_context()
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


def _already_connected(cfg) -> bool:
    """True when this config already points at a server someone configured.

    A re-run must not re-ask the routing question of a machine that is already
    enrolled -- `firekeep install --runtime claude` is a documented re-render
    path, not an invitation to repoint the machine.
    """
    if cfg.get("server", UNCONFIGURED_MARKER, fallback="").strip().lower() == "false":
        return False
    if cfg.get("server", "api_key", fallback="").strip():
        return True
    if cfg.get("server", "base_url", fallback="").strip():
        return True
    host = cfg.get("server", "host", fallback="").strip()
    return bool(host) and host != _SERVER_DEFAULTS["host"]


def _docker_available() -> bool:
    """Only whether the binary exists -- not whether the daemon is up.

    Deliberately shallow: this picks a DEFAULT for a menu the user can override
    in one keystroke, so being wrong is cheap, while shelling out to
    `docker info` on every install would cost seconds and can hang on a
    misconfigured DOCKER_HOST.
    """
    return shutil.which("docker") is not None


def ask_where_the_server_is(
    cfg, ask, *, docker=None, probe=_probe_os_trust, fetch_defaults=_fetch_org_defaults
) -> tuple[str, str]:
    """The single routing question. Returns (action, join_code)."""
    has_docker = _docker_available() if docker is None else docker
    # Default to what the machine is equipped for. A box with Docker is almost
    # certainly the box being set up; a laptop without it is almost certainly
    # joining something. Either way it is one keystroke to say otherwise.
    default = "1" if has_docker else "2"
    here = ("Set one up on this machine  (installs the server here with Docker)"
            if has_docker else
            "Set one up on this machine  (needs Docker, which was not found)")
    print(
        "\nWhere is your Firekeep server?\n"
        f"  1  {here}\n"
        "  2  I have a join code       (Dashboard -> Devices, or from a teammate)\n"
        "  3  It is already running    (you know its address and have a key)\n"
        "  4  Not yet                  (finish the client; `firekeep doctor` will\n"
        "                               tell you how to finish the job later)",
        file=sys.stderr,
    )
    answer = (ask("Choose", default) or default).strip().lower()

    if answer in ("2", "join", "code", "join code"):
        # Accepted here rather than deferred: a user who says "I have a code" has
        # it in their clipboard right now.
        code = ask("Paste your join code", "").strip()
        return (JOIN_WITH_CODE, code) if code else (DECIDE_LATER, "")
    if answer in ("3", "existing", "running", "host"):
        if cfg.get("server", "kind", fallback="ports").strip().lower() == "paths":
            _configure_paths(cfg, ask, probe=probe, fetch_defaults=fetch_defaults)
        else:
            _configure_ports(cfg, ask)
        return (EXISTING_SERVER, "")
    if answer in ("4", "later", "not yet", "no", "n", "skip"):
        return (DECIDE_LATER, "")
    # Anything else -- including "1", "here", or a fat-fingered answer while the
    # default is 1 -- provisions here. Falling through to the ACTION the default
    # advertises is safer than silently choosing "later" on a typo, because
    # provisioning is visible, interruptible and re-runnable, whereas "later"
    # looks exactly like success and is discovered hours afterwards.
    return (PROVISION_HERE, "")


def _ask_generic_agents_md(ask) -> str | None:
    """The last question, and the only discovery path for the generic runtime.

    Skippable by design — most people are on one of the four, and a question you
    must answer to get past is a worse tax than a tier you never learn about.
    Returns the raw answer; resolving and persisting it belongs to cli.py, since
    this module touches no filesystem."""
    answer = ask(
        "Also use an MCP client we don't ship an adapter for (Cursor, Windsurf, "
        "Gemini CLI, …)? Paste the path to its rules/AGENTS.md file, or press "
        "Enter to skip",
        "",
    ).strip()
    return answer or None


def prompt_config(
    cfg: configparser.ConfigParser,
    *,
    ask=console_ask,
    agent_id: str | None = None,
    host: str | None = None,
    probe=_probe_os_trust,
    fetch_defaults=_fetch_org_defaults,
    docker=None,
) -> Plan:
    """Walk the install prompts, mutating `cfg` and returning what to do next.

    `agent_id` / `host` are the CLI flags: each seeds its prompt's default
    rather than suppressing it, so `--host 10.0.0.4` interactively means 'suggest this',
    while the same flag under --non-interactive (which never calls here) means 'use this'.

    Wraps `_prompt_server_config` so the generic-client question is asked exactly
    once, LAST, on every path through it — that function has three early returns,
    and asking at each would be three chances to drift apart.
    """
    plan = _prompt_server_config(
        cfg, ask=ask, agent_id=agent_id, host=host, probe=probe,
        fetch_defaults=fetch_defaults, docker=docker,
    )
    plan = plan._replace(generic_agents_md=_ask_generic_agents_md(ask))
    # Field-failure consent (spec decision 1): asked once, only when unanswered.
    # ask_consent uses its own EOF-safe reader — NOT console_ask, whose
    # EOF-takes-the-default would silently enroll.
    from firekeep_client import report
    report.ask_consent(cfg)
    return plan


def _prompt_server_config(
    cfg: configparser.ConfigParser,
    *,
    ask=console_ask,
    agent_id: str | None = None,
    host: str | None = None,
    probe=_probe_os_trust,
    fetch_defaults=_fetch_org_defaults,
    docker=None,
) -> Plan:
    """Identity + "where is your server" — everything up to the generic question."""
    _ensure_section(cfg, "identity", {"agent_id": PLACEHOLDER_AGENT_ID})
    _ensure_section(cfg, "server", _SERVER_DEFAULTS)
    # Kept as a prompt when everything else was deleted, and that is a judgement
    # call worth recording: the OS username is a good DEFAULT but a poor ANSWER.
    # On the VPS that prompted this redesign it was `root`, and every memory,
    # session and replay event this machine ever wrote would have been attributed
    # to "root" with nothing downstream able to notice.
    identity = ask(
        "Agent identity (attributes every memory, session, and replay event)",
        agent_id or _default_agent_id(cfg),
    )
    cfg.set("identity", "agent_id", identity or PLACEHOLDER_AGENT_ID)

    if host:
        # An explicit --host IS the answer to the routing question.
        cfg.set("server", "kind", "ports")
        cfg.set("server", "scheme", "http")
        cfg.set("server", "verify_tls", "false")
        cfg.set("server", "host", host)
        # Switching a paths config to ports must DROP the paths keys, not leave
        # them alongside. A [server] holding both `host` and `base_url` is
        # ambiguous, and which one wins is a resolver implementation detail
        # rather than anything the user chose. `_configure_ports` has always
        # done this; the flag path has to as well.
        for stale in ("base_url", "ca_path"):
            cfg.remove_option("server", stale)
        _prompt_api_key(cfg, ask, "API key (blank if AUTH_ENABLED=false)")
        cfg.remove_option("server", UNCONFIGURED_MARKER)
        return Plan(cfg, EXISTING_SERVER)

    if _already_connected(cfg):
        # A machine that already HAS a server does not need to be asked where one
        # is -- it needs to be able to change the one it has. So it gets the
        # familiar edit-in-place prompts, prefilled with its current values,
        # exactly as before. The routing menu is for the state that had no
        # representation and no way out: a client with nothing to talk to.
        if cfg.get("server", "kind", fallback="ports").strip().lower() == "paths":
            _configure_paths(cfg, ask, probe=probe, fetch_defaults=fetch_defaults)
        else:
            _configure_ports(cfg, ask)
        cfg.remove_option("server", UNCONFIGURED_MARKER)
        return Plan(cfg, EXISTING_SERVER)

    action, code = ask_where_the_server_is(
        cfg, ask, docker=docker, probe=probe, fetch_defaults=fetch_defaults
    )
    if action in (PROVISION_HERE, EXISTING_SERVER, JOIN_WITH_CODE):
        # Each of these ends in a real connection being written -- by the server
        # install, by join, or by the prompts just answered -- so the config is
        # no longer "unconfigured" once the caller carries the plan out.
        cfg.remove_option("server", UNCONFIGURED_MARKER)
    else:
        cfg.set("server", UNCONFIGURED_MARKER, "false")
    return Plan(cfg, action, code)


def set_dist_base(cfg: configparser.ConfigParser, base: str) -> None:
    """Record where this kit was installed from, so `firekeep update` can find its way home.

    Never prompted for and never hardcoded: the bootstrap was fetched from this URL, so it
    is the one component that already knows it. A checkout install simply has no [dist]
    section, and `firekeep update` says so plainly."""
    if not cfg.has_section("dist"):
        cfg.add_section("dist")
    cfg.set("dist", "base_url", base.rstrip("/"))
