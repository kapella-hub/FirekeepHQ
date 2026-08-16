"""Connection resolver: reads ~/.firekeep/config and produces service endpoints.

The one place that knows the two server shapes — the shim, hook cores, sidecar,
and `firekeep` CLI all call into here; no URL/auth string-building lives elsewhere.
Stdlib-only (SP1b import boundary).
"""
from __future__ import annotations

import configparser
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path

CONFIG_PATH = Path.home() / ".firekeep" / "config"  # override via env FIREKEEP_CONFIG (tests)

# Personal / bypass mode ------------------------------------------------------
# A transient MARKER FILE (NOT the config) that, while present, makes Firekeep
# go dormant for "personal work": hooks no-op, the shim serves 0 tools, the
# decision board suppresses itself. It is deliberately separate from ~/.firekeep/config
# so toggling it never rewrites config. The `stop` hook removes it at session end
# (the user's chosen auto-clear semantics); DEFAULT_PERSONAL_TTL_HOURS is the
# crash backstop for a session that never fired `stop`.
DEFAULT_PERSONAL_TTL_HOURS = 12.0

SERVICES = ("cortex", "bridge", "sentinel", "relay")
MCP_PORTS = {"cortex": 8080, "bridge": 8070, "sentinel": 8060, "relay": 8050}

# `ca_path = os` sentinel: verify TLS against the operating-system trust store
# instead of a CA file. transport._build_ssl_context / shim.build_client turn it
# into a truststore SSLContext.
OS_TRUST = "os"
# REST-port correction: bridge/sentinel/relay serve REST on the SAME app/port as MCP
# (they are @mcp.custom_route handlers). Only cortex splits REST onto 8100.
REST_PORTS = {"cortex": 8100, "bridge": 8070, "sentinel": 8060, "relay": 8050}


@dataclass(frozen=True)
class Endpoint:
    mcp_url: str
    rest_base: str
    headers: dict[str, str]  # X-Agent-Id always; X-API-Key if key set; X-Session-Id if
                             # given; X-Firekeep-* attribution when FIREKEEP_RUNTIME is set
    verify: bool | str       # False, a ca_path string, or OS_TRUST ("os") for OS-store TLS


class ConfigError(Exception):
    """Missing file or invalid single-connection configuration."""


class ConfigMigrationConflict(ConfigError):
    """A legacy profile config cannot be collapsed without choosing for the user."""


def _config_path(path: Path | None = None) -> Path:
    if path is not None:
        return Path(path).expanduser().resolve()
    env = os.environ.get("FIREKEEP_CONFIG")
    if env:
        return Path(env).expanduser().resolve()
    return CONFIG_PATH.expanduser().resolve()


def _path_for(cfg: configparser.ConfigParser, path: Path | None = None) -> Path:
    """Return the source path attached by load_config, or the current resolved path."""
    if path is not None:
        return Path(path)
    attached = getattr(cfg, "_firekeep_path", None)
    return Path(attached) if attached is not None else _config_path()


def _env_truthy(value: str | None) -> bool:
    """A permissive truthiness for env flags: anything other than empty/0/false/no/off."""
    if value is None:
        return False
    return value.strip().lower() not in ("", "0", "false", "no", "off")


def personal_marker_path(path: Path | None = None) -> Path:
    """The personal-mode marker, alongside the config it shadows (~/.firekeep/personal).
    Basing it on _config_path() means FIREKEEP_CONFIG isolates it in tests exactly as it
    isolates the config."""
    return _config_path(path).parent / "personal"


def _personal_ttl_seconds() -> float:
    raw = os.environ.get("FIREKEEP_PERSONAL_TTL_HOURS", "")
    try:
        hours = float(raw) if raw.strip() else DEFAULT_PERSONAL_TTL_HOURS
    except (ValueError, AttributeError):
        hours = DEFAULT_PERSONAL_TTL_HOURS
    # `nan`/`inf` (incl. overflow like `1e999`) parse without error and slip past a
    # bare `<= 0` guard (nan<=0 and inf<=0 are both False), which would make the
    # staleness comparison always-False and silently disable the crash backstop.
    if not math.isfinite(hours) or hours <= 0:
        hours = DEFAULT_PERSONAL_TTL_HOURS
    return hours * 3600.0


def is_personal(path: Path | None = None) -> bool:
    """True when the personal-mode marker exists AND is fresh (age < TTL). A stale
    marker is treated as OFF and best-effort removed (crash recovery — a session that
    never fired `stop` can't strand personal mode past the TTL). Never raises."""
    try:
        marker = personal_marker_path(path)
        if not marker.exists():
            return False
        if time.time() - marker.stat().st_mtime > _personal_ttl_seconds():
            try:
                marker.unlink()
            except OSError:
                pass
            return False
        return True
    except Exception:  # noqa: BLE001 — the gate must never raise into a hook/shim.
        return False


def set_personal(on: bool, path: Path | None = None) -> bool:
    """Create (on) or remove (off) the personal-mode marker. Returns the OBSERVED
    resulting state (whether the marker actually exists afterward) — NOT the intended
    one. This matters on the off-path: if `unlink` fails (Windows PermissionError, a
    lock, a read-only ~/.firekeep), the marker survives and personal mode is still ON, so
    we must report True, never a false 'off' that would let team logging silently stop.
    Never raises."""
    try:
        marker = personal_marker_path(path)
        if on:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(f"personal mode set at {time.time()}\n", encoding="utf-8")
        else:
            try:
                marker.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                # Could not remove it — do NOT claim 'off'. Fall through to report the
                # TRUE (still-on) state below.
                pass
        return marker.exists()
    except Exception:  # noqa: BLE001
        # Couldn't even resolve/stat the marker. Report the SAFE-direction state: an
        # ON request that failed -> not-on (don't claim false privacy); an OFF request
        # that failed -> still-on (don't claim false team mode).
        return not on


def is_bypassed(path: Path | None = None) -> bool:
    """THE bypass gate — the one function the hooks, shim, decision server, and
    sidecar consult.

    True when personal mode is on (the live marker) OR the FIREKEEP_BYPASS env var is
    truthy (the startup-scoped hard bypass). Fails toward NOT bypassed on any error,
    so a bug in this path can never SILENTLY stop team logging."""
    try:
        if _env_truthy(os.environ.get("FIREKEEP_BYPASS")):
            return True
        return is_personal(path)
    except Exception:  # noqa: BLE001
        return False


def _raw_config(path: Path | None = None) -> configparser.ConfigParser:
    """The kit config read RAW: no migration, no rewrite, no raise.

    Deliberately NOT load_config(), which raises on a missing file and — when
    `[server]` is absent — MIGRATES: backs up, atomically rewrites, prints to
    stderr, and can raise ConfigMigrationConflict. Merely asking "is generic
    configured?" must never have a side effect on the user's config, and it runs
    on every install and every uninstall.

    Returns an empty parser on anything unreadable: callers treat "cannot tell"
    as "not configured", which is the four's existing behaviour."""
    cfg = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
    try:
        cfg.read(_config_path(path), encoding="utf-8")
    except (configparser.Error, OSError, UnicodeError):
        return configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
    return cfg


def generic_agents_md(path: Path | None = None) -> Path | None:
    """The rules/AGENTS.md file the generic adapter manages, or None when the
    user never opted in. Absolute and resolved; the presence of this value is
    also what makes `generic` join the `"all"` install/uninstall fan-out."""
    value = _raw_config(path).get("generic", "agents_md", fallback="").strip()
    return Path(value).expanduser().resolve() if value else None


def load_config(path: Path | None = None) -> configparser.ConfigParser:
    p = _config_path(path)
    if not p.exists():
        raise ConfigError(f"firekeep config not found at {p}")
    # interpolation=None: api_key / base_url may contain '%' which BasicInterpolation
    # would choke on. We never use ${...} interpolation in this INI.
    # inline_comment_prefixes: the spec's canonical example config annotates values
    # (`kind = ports    ; host:port/mcp URL style`); without this, the comment text
    # becomes part of the VALUE and resolve() fails on the spec's own example.
    # Safe: nxs_ keys/URLs/agent ids never legitimately contain ' ;' or ' #'
    # (configparser only strips when the prefix follows whitespace).
    cfg = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
    try:
        loaded = cfg.read(p, encoding="utf-8")
    except (configparser.Error, OSError, UnicodeError) as exc:
        raise ConfigError(
            f"firekeep config at {p} is not valid INI ({type(exc).__name__})"
        ) from exc
    if not loaded:
        raise ConfigError(f"firekeep config could not be read at {p}")
    cfg._firekeep_path = p
    if not cfg.has_section("server"):
        # Lazy import avoids a module cycle: migrate reuses the resolver's exact
        # TLS and endpoint-shape validation while resolver owns ConfigError.
        from firekeep_client import migrate
        cfg = migrate.migrate_config(p)
        cfg._firekeep_path = p
    return cfg


def agent_id(cfg: configparser.ConfigParser) -> str:
    # FIREKEEP_AGENT_ID overrides the machine identity (multi-agent workflow).
    env = os.environ.get("FIREKEEP_AGENT_ID")
    if env:
        return env
    path = _path_for(cfg)
    if not cfg.has_section("identity"):
        raise ConfigError(f"firekeep config {path} missing [identity] section")
    if not cfg.has_option("identity", "agent_id"):
        raise ConfigError(f"firekeep config {path} [identity] missing required key 'agent_id'")
    value = cfg.get("identity", "agent_id").strip()
    if not value:
        raise ConfigError(f"firekeep config {path} [identity] has empty 'agent_id'")
    return value


def _require(cfg: configparser.ConfigParser, key: str, *, section: str = "server",
             path: Path | None = None) -> str:
    source = _path_for(cfg, path)
    if not cfg.has_section(section):
        raise ConfigError(f"firekeep config {source} missing [{section}] section")
    if not cfg.has_option(section, key):
        raise ConfigError(
            f"firekeep config {source} [{section}] missing required key '{key}'"
        )
    value = cfg.get(section, key).strip()
    if not value:
        raise ConfigError(f"firekeep config {source} [{section}] has empty '{key}'")
    return value


def _verify_for(cfg: configparser.ConfigParser, scheme: str, *, section: str = "server",
                path: Path | None = None) -> bool | str:
    source = _path_for(cfg, path)
    try:
        verify_tls = cfg.getboolean(section, "verify_tls", fallback=False)
    except ValueError:
        raise ConfigError(
            f"firekeep config {source} [{section}] has non-boolean 'verify_tls'"
        )

    if scheme == "https":
        # MITM protection: never speak https without verifying the peer.
        if not verify_tls:
            raise ConfigError(
                f"firekeep config {source} [{section}]: scheme=https with "
                f"verify_tls=false is refused "
                f"(unverified TLS is a MITM hole — set verify_tls=true with a ca_path)"
            )
        ca_path = cfg.get(section, "ca_path", fallback="").strip()
        if not ca_path:
            raise ConfigError(
                f"firekeep config {source} [{section}]: scheme=https requires 'ca_path' "
                f"(internal CA cert, "
                f"or 'os' to verify against the operating-system trust store)"
            )
        if ca_path.lower() == OS_TRUST:
            # OS-trust sentinel: verify against the operating-system trust store
            # (transport/shim build a truststore SSLContext for it). This is the
            # MDM-managed-corporate-CA case — the CA lives in the OS keychain and
            # there is no PEM file to point at. Still verified TLS, never a bypass.
            return OS_TRUST
        return str(Path(ca_path).expanduser().resolve())

    # http: no TLS to verify. verify=False is ONLY legal for plain HTTP.
    return False


# --- runtime attribution (round-2 measurement contract) ----------------------
# A process that knows which runtime launched it (`firekeep gateway --runtime
# <name>` exports FIREKEEP_RUNTIME, inherited by the shim children that make the
# actual HTTP calls; the hook dispatcher's --runtime flag does the same for the
# hook cores) attaches five X-Firekeep-* headers to every server call. Computed
# once per process (first resolve) and cached — the on-disk re-hash is a
# process-start snapshot, not a per-request stat. Trust level is exactly
# X-Agent-Id's: an untrusted observability label, never a gate.
_ATTRIBUTION_CACHE: dict[str, dict[str, str]] = {}


def _runtime_attribution() -> dict[str, str]:
    """The X-Firekeep-* attribution headers, or {} when this process has no
    runtime identity (FIREKEEP_RUNTIME unset — old rendered configs). Never
    raises: attribution must not be able to break a server call."""
    runtime = os.environ.get("FIREKEEP_RUNTIME", "").strip()
    if not runtime:
        return {}
    cached = _ATTRIBUTION_CACHE.get(runtime)
    if cached is None:
        from firekeep_client import __version__
        from firekeep_client.adapters import base as _adapters_base
        try:
            rendered = _adapters_base.read_rendered_instructions_hash(runtime)
        except Exception:  # noqa: BLE001 — a hash failure is 'absent', not an outage
            rendered = None
        cached = {
            "X-Firekeep-Runtime": runtime,
            "X-Firekeep-Client": __version__,
            # The client re-hashes what is actually on disk rather than trusting
            # its own stamp — a hand-edited block reports its true hash.
            "X-Firekeep-Instr-Rendered": rendered if rendered is not None else "absent",
            "X-Firekeep-Instr-Expected": _adapters_base.RENDERED_INSTRUCTIONS_HASH,
            "X-Firekeep-Instr-Gateway": _adapters_base.GATEWAY_INSTRUCTIONS_HASH,
        }
        _ATTRIBUTION_CACHE[runtime] = cached
    return dict(cached)


def resolve(
    service: str,
    cfg=None,
    session_id: str | None = None,
) -> Endpoint:
    # symdex is stdio-local and must NEVER be constructed as an HTTP endpoint.
    if service == "symdex":
        raise ValueError("symdex is stdio-local and is never resolved as an HTTP service")
    if service not in SERVICES:
        raise ValueError(f"unknown service '{service}' (expected one of {SERVICES})")

    if cfg is None:
        cfg = load_config()
    path = _path_for(cfg)
    if not cfg.has_section("server"):
        raise ConfigError(f"firekeep config {path} missing [server] section")

    kind = _require(cfg, "kind").strip().lower()
    # Normalize scheme so the https TLS guard (_verify_for) can't be bypassed by
    # a case/whitespace typo (e.g. "HTTPS") that an HTTP client would still treat
    # as TLS. RFC 3986 schemes are case-insensitive.
    scheme = _require(cfg, "scheme").strip().lower()
    verify = _verify_for(cfg, scheme)

    headers = {"X-Agent-Id": agent_id(cfg)}
    headers.update(_runtime_attribution())  # X-Firekeep-* when the runtime is known
    if cfg.has_option("server", "api_key"):
        key = cfg.get("server", "api_key").strip()
        if key:
            headers["X-API-Key"] = key
    if session_id:
        headers["X-Session-Id"] = session_id

    if kind == "ports":
        host = _require(cfg, "host")
        mcp_url = f"{scheme}://{host}:{MCP_PORTS[service]}/mcp"
        rest_base = f"{scheme}://{host}:{REST_PORTS[service]}"
    elif kind == "paths":
        base_url = _require(cfg, "base_url").rstrip("/")
        # MITM guard: `verify` above was computed from `scheme`, but the actual
        # URL is built from `base_url`. If they disagree (e.g. scheme=http with
        # a base_url that is actually https://...), verify would be False on a
        # real TLS endpoint -> unverified handshake. Refuse the mismatch outright
        # rather than silently trusting whichever of the two is more permissive.
        if not base_url.lower().startswith(f"{scheme}://"):
            raise ConfigError(
                f"firekeep config {path} [server]: scheme='{scheme}' does not match base_url "
                f"'{base_url}' (base_url must start with '{scheme}://') — refusing "
                f"a scheme/base_url mismatch that could bypass TLS verification"
            )
        mcp_url = f"{base_url}/mcp/{service}"
        rest_base = f"{base_url}/api/{service}"
    else:
        raise ConfigError(
            f"firekeep config {path} [server] has unknown kind '{kind}' "
            f"(expected 'ports' or 'paths')"
        )

    return Endpoint(mcp_url=mcp_url, rest_base=rest_base, headers=headers, verify=verify)
