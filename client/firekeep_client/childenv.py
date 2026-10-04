"""The environment a gateway backend child is started with.

The gateway runs wherever an agent runtime runs: a developer shell, a CI
runner, the server-side ChatGPT tunnel (``deploy/chatgpt-tunnel/``). Any of
those can hold server service credentials — ``FIREKEEP_INTERNAL_KEY``,
``RELAY_INTERNAL_API_KEY``, ``VAULT_KEY`` and the rest of a deployment's
``.env``. Backend children (the four shims, the decision server, every
``mcp-stdio`` dex or capability in the registry) used to inherit that
environment whole, and symdex's Cortex client read ``FIREKEEP_INTERNAL_KEY``
from it — so a dex child could act with the deployment's service key instead
of the member's enrolled key.

A child now gets an ALLOWLISTED environment: what a process needs to run (OS,
locale, proxy and TLS, the desktop session), the kit's own non-secret
``FIREKEEP_*`` knobs, and — only for the child that owns it — a credential that
child is documented to read (symdex's LLM provider keys, the decision server's
notify token). Anything else is dropped. Symdex additionally receives the
member's enrolled Cortex connection, resolved from ``~/.firekeep/config`` and
passed explicitly (``FIREKEEP_CORTEX_URL`` / ``FIREKEEP_CLIENT_API_KEY`` /
``FIREKEEP_CORTEX_CA``); the parent's copies of those names are never
forwarded, so the config, not ambient environment, decides who symdex is.

``client/tests/test_gateway_child_env.py`` scans every child package for the
environment names it reads and fails on any name not classified here — the
allowlist cannot silently fall behind a new knob.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping

# Names match case-insensitively (Windows environment names are; POSIX proxies
# are conventionally lower-case) and keep their original spelling in the child.

_OS_WINDOWS = frozenset({
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
    "PROGRAMDATA", "ALLUSERSPROFILE", "PUBLIC", "PROGRAMFILES",
    "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES",
    "COMMONPROGRAMFILES(X86)", "COMMONPROGRAMW6432", "USERNAME", "USERDOMAIN",
    "COMPUTERNAME", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS",
})
_OS_POSIX = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LANGUAGE",
    "TZ", "TERM", "__CF_USER_TEXT_ENCODING",
    # The desktop session: the decision server opens a browser and Hands drives
    # the desktop, both from the child's environment.
    "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS",
})
_NETWORK = frozenset({
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
})
_PYTHON = frozenset({"VIRTUAL_ENV"})

# The kit's own non-secret knobs, as the children (and the client code they
# import) read them. Identity labels such as NEXUS_AGENT_ID are labels, not
# credentials — the server derives identity from the key.
_KIT = frozenset({
    # resolver / state / logging / reporting — every shim and Hands use these
    "FIREKEEP_CONFIG", "FIREKEEP_CACHE_DIR", "FIREKEEP_LOG_DIR",
    "FIREKEEP_AGENT_ID", "FIREKEEP_RUNTIME", "FIREKEEP_BYPASS",
    "FIREKEEP_PERSONAL_TTL_HOURS", "FIREKEEP_SESSION_STASH_TTL_HOURS",
    "FIREKEEP_SHIM_DEBUG", "FIREKEEP_NO_JOB_OBJECT",
    "FIREKEEP_REPORT_CONSENT", "FIREKEEP_REPORT_DIR",
    "FIREKEEP_FAILURE_REPORT", "FIREKEEP_NO_FAILURE_REPORT",
    "FIREKEEP_SESSION_ID", "NEXUS_AGENT_ID",
    # hook-side knobs the client modules a child imports may consult
    "FIREKEEP_AGENT_GOAL", "FIREKEEP_BRIEFING", "FIREKEEP_NO_AUTO_UPDATE",
    "FIREKEEP_NO_AUTO_SYNC", "FIREKEEP_NO_AUTO_INDEX", "FIREKEEP_NO_AUTO_NIGHTSHIFT",
    "FIREKEEP_NO_RECALL_PUSH", "FIREKEEP_RECALL_PUSH_MIN_SCORE",
    "FIREKEEP_RECALL_PUSH_TIMEOUT_SECONDS", "FIREKEEP_SIDECAR_INTERVAL",
    "FIREKEEP_SNAPSHOT_DIR", "FIREKEEP_SNAPSHOT_KEEP", "FIREKEEP_SNAPSHOT_MAX_BYTES",
    "FIREKEEP_NIGHTSHIFT_AGENT_ID", "FIREKEEP_NIGHTSHIFT_ALLOW_REMOTE",
    "FIREKEEP_NIGHTSHIFT_LLM_BASE", "FIREKEEP_NIGHTSHIFT_LLM_MODEL",
    "FIREKEEP_NIGHTSHIFT_DRAIN_INTERVAL_HOURS",
    # decision server
    "FIREKEEP_DECISION_HEADLESS", "FIREKEEP_DECISION_SURFACE",
    "FIREKEEP_DECISION_NOTIFY_URL", "DECISION_INGEST_CLIENT_TIMEOUT_SECONDS",
    # symdex
    "CODE_INDEX_PATH", "SYMDEX_ANALYTICS_ENABLED", "FIREKEEP_SYMDEX_HOST",
    "FIREKEEP_SYMDEX_PORT", "FIREKEEP_SYMDEX_MODE", "FIREKEEP_SYMDEX_MAX_FILES",
    "FIREKEEP_SYMDEX_MAX_RESULT_TOKENS", "FIREKEEP_SYMDEX_SHARE_STATS",
    # hands
    "FIREKEEP_HANDS_OFFLINE", "FIREKEEP_HANDS_LOG", "FIREKEEP_HANDS_TRACE_KEYS",
    # docdex / maildex (ingest clients: no gateway child today, classified so a
    # future mcp-stdio entry inherits a working environment)
    "FIREKEEP_DOCDEX_MAX_EXTRACT_KB", "FIREKEEP_DOCDEX_MAX_FILES",
    "FIREKEEP_DOCDEX_MAX_FILE_MB", "FIREKEEP_DOCDEX_INGEST_TIMEOUT_SECONDS",
    "FIREKEEP_DOCDEX_SYNC_INTERVAL_HOURS",
    "FIREKEEP_MAILDEX_BACKFILL_DAYS", "FIREKEEP_MAILDEX_MAX_MESSAGE_KB",
    "FIREKEEP_MAILDEX_INGEST_TIMEOUT_SECONDS", "FIREKEEP_MAILDEX_MAX_PER_SYNC",
    "FIREKEEP_MAILDEX_SYNC_INTERVAL_HOURS",
})

ALLOWED_NAMES: frozenset[str] = _OS_WINDOWS | _OS_POSIX | _NETWORK | _PYTHON | _KIT
ALLOWED_PREFIXES: tuple[str, ...] = ("LC_", "XDG_", "PYTHON")

# A credential a child is documented to read reaches THAT child only.
CHILD_EXTRAS: dict[str, frozenset[str]] = {
    # symdex's own features: AI symbol summaries / scaffolding and GitHub
    # repository indexing (symdex/README.md).
    "symdex": frozenset({
        "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY",
        "OPENAI_API_BASE", "OPENAI_MODEL", "OPENAI_TIMEOUT", "GITHUB_TOKEN",
    }),
    # the bearer for the decision board's optional notify webhook
    "decision": frozenset({"FIREKEEP_DECISION_NOTIFY_TOKEN"}),
}

# Classified and deliberately NOT forwarded to any child.
NOT_FORWARDED: frozenset[str] = frozenset({
    # Server service credentials. The allowlist already drops them; they are
    # named here so the intent is greppable and the tests can assert it.
    "FIREKEEP_INTERNAL_KEY", "FIREKEEP_BRIDGE_KEY", "NS_FIREKEEP_INTERNAL_KEY",
    "RELAY_INTERNAL_API_KEY", "DASHBOARD_API_KEY", "VAULT_KEY", "NEO4J_PASSWORD",
    "FIREKEEP_SIGNING_KEY",
    # An enrolment code is a credential; only `firekeep join` reads it.
    "FIREKEEP_JOIN",
    # Set by the gateway from ~/.firekeep/config for symdex alone; a parent's
    # copy (stale shell export, another member's key) is never authoritative.
    "FIREKEEP_CORTEX_URL", "FIREKEEP_CLIENT_API_KEY", "FIREKEEP_CORTEX_CA",
    # The gateway's own surface: a child must not be able to re-read or widen it.
    "FIREKEEP_TOOLSET", "FIREKEEP_TOOLS_ALLOW",
    # Install/update trust path: no gateway child installs or updates the kit,
    # and a trust-anchor override has no business reaching one.
    "FIREKEEP_DIST_BASE", "FIREKEEP_SERVER_DIST_BASE", "FIREKEEP_SIGNING_PUB",
    "FIREKEEP_SUMS_FILE", "FIREKEEP_VERSION", "FIREKEEP_INSTALL_TIMEOUT",
    "FIREKEEP_SERVER_INSTALL_TIMEOUT", "FIREKEEP_NO_MODIFY_PATH", "FIREKEEP_PROFILE",
    # Adapter render-time only.
    "PI_CODING_AGENT_CONFIG_DIR",
})


def child_environment(
    name: str, *, source: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The allowlisted subset of ``source`` (default: this process's
    environment) that backend ``name`` may see. Never mutates ``source``."""
    environ = os.environ if source is None else source
    extras = CHILD_EXTRAS.get(name, frozenset())
    kept: dict[str, str] = {}
    for key, value in environ.items():
        upper = key.upper()
        if upper in NOT_FORWARDED:
            continue
        if upper in ALLOWED_NAMES or upper in extras or upper.startswith(ALLOWED_PREFIXES):
            kept[key] = value
    return kept


def cortex_connection() -> dict[str, str]:
    """The member's enrolled Cortex REST connection, for symdex.

    Empty — symdex's Cortex tools then report "not configured", exactly as
    before this connection was passed — when the machine is not enrolled, the
    config is unreadable, or the kit is bypassed (personal mode /
    ``FIREKEEP_BYPASS``) at spawn: the shims serve zero tools then, and symdex
    must not keep a live member connection to the Keep either. Never raises: a
    broken config must cost symdex its Cortex tools, not its mount."""
    from firekeep_client import resolver

    try:
        if resolver.is_bypassed():
            return {}
        endpoint = resolver.resolve("cortex", resolver.load_config())
    except Exception:  # noqa: BLE001 - see docstring
        return {}
    connection = {"FIREKEEP_CORTEX_URL": endpoint.rest_base}
    api_key = endpoint.headers.get("X-API-Key")
    if api_key:
        connection["FIREKEEP_CLIENT_API_KEY"] = api_key
    if isinstance(endpoint.verify, str):
        # A CA file path, or resolver.OS_TRUST ("os").
        connection["FIREKEEP_CORTEX_CA"] = endpoint.verify
    return connection


# Backends that receive something beyond the allowlist, computed at spawn.
_INJECTED: dict[str, Callable[[], dict[str, str]]] = {"symdex": cortex_connection}


def backend_environment(name: str) -> dict[str, str]:
    """Everything backend ``name`` is started with. Computed when the child is
    (re)started, not when the gateway is built, so an enrolment or a personal
    toggle between restarts is honoured."""
    environment = child_environment(name)
    inject = _INJECTED.get(name)
    if inject is not None:
        environment.update(inject())
    return environment
