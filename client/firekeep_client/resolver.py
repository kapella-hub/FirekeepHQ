"""Profile resolver: reads ~/.firekeep/config and produces per-service endpoints.

The one place that knows profile shapes — the shim, hook cores, sidecar, and
`firekeep` CLI all call into here; no URL/auth string-building lives anywhere else.
Stdlib-only (SP1b import boundary).
"""
from __future__ import annotations

import configparser
import math
import os
import re
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
    headers: dict[str, str]  # X-Agent-Id always; X-API-Key if key set; X-Session-Id if given
    verify: bool | str       # False, a ca_path string, or OS_TRUST ("os") for OS-store TLS


class ConfigError(Exception):
    """Missing file / unknown profile / missing required key."""


def _config_path(path: Path | None = None) -> Path:
    if path is not None:
        return path
    env = os.environ.get("FIREKEEP_CONFIG")
    if env:
        return Path(env)
    return CONFIG_PATH


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
    if not cfg.read(p, encoding="utf-8"):
        raise ConfigError(f"firekeep config could not be read at {p}")
    return cfg


PIN_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def active_profile(cfg, override: str | None = None) -> str:
    # Precedence: explicit override > FIREKEEP_PROFILE env > [active] profile. The env
    # override mirrors FIREKEEP_AGENT_ID (agent_id() below); a pinned runtime's rendered
    # MCP entries set it, and the hooks dispatcher exports it for --profile. Section
    # existence is a UX/typo guard, not a security control — the environment is already
    # a full trust boundary via FIREKEEP_CONFIG (_config_path).
    source = "override"
    profile = (override or "").strip()
    if not profile:
        env = os.environ.get("FIREKEEP_PROFILE", "").strip()
        if env:
            profile, source = env, "FIREKEEP_PROFILE"
    if profile:
        if not cfg.has_section(profile):
            raise ConfigError(f"{source} profile '{profile}' has no [{profile}] section")
        return profile
    if not cfg.has_section("active"):
        raise ConfigError("config missing [active] section")
    if not cfg.has_option("active", "profile"):
        raise ConfigError("config [active] missing 'profile' key")
    profile = cfg.get("active", "profile").strip()
    if not profile:
        raise ConfigError("config [active] 'profile' is empty")
    if not cfg.has_section(profile):
        raise ConfigError(f"active profile '{profile}' has no [{profile}] section")
    return profile


def pinned_profile(cfg, runtime: str) -> str | None:
    """The profile pinned for `runtime` via [pins], or None. Malformed values (empty,
    charset-unsafe — they get rendered into bash hook command strings) are treated as
    ABSENT here; the pin CLI rejects them at write time and doctor warns on what it
    finds, so render never emits an unsafe token."""
    if not cfg.has_section("pins") or not cfg.has_option("pins", runtime):
        return None
    value = cfg.get("pins", runtime).strip()
    if not value or not PIN_NAME_RE.match(value):
        return None
    return value


def agent_id(cfg, profile: str) -> str:
    # FIREKEEP_AGENT_ID overrides the profile value (multi-agent workflow, start-agent.sh).
    env = os.environ.get("FIREKEEP_AGENT_ID")
    if env:
        return env
    if not cfg.has_section(profile):
        raise ConfigError(f"unknown profile '{profile}'")
    if not cfg.has_option(profile, "agent_id"):
        raise ConfigError(f"profile '{profile}' missing required key 'agent_id'")
    value = cfg.get(profile, "agent_id").strip()
    if not value:
        raise ConfigError(f"profile '{profile}' has empty 'agent_id'")
    return value


def _require(cfg, profile: str, key: str) -> str:
    if not cfg.has_option(profile, key):
        raise ConfigError(f"profile '{profile}' missing required key '{key}'")
    value = cfg.get(profile, key).strip()
    if not value:
        raise ConfigError(f"profile '{profile}' has empty '{key}'")
    return value


def _verify_for(cfg, profile: str, scheme: str) -> bool | str:
    try:
        verify_tls = cfg.getboolean(profile, "verify_tls", fallback=False)
    except ValueError:
        raise ConfigError(f"profile '{profile}' has non-boolean 'verify_tls'")

    if scheme == "https":
        # MITM protection: never speak https without verifying the peer.
        if not verify_tls:
            raise ConfigError(
                f"profile '{profile}': scheme=https with verify_tls=false is refused "
                f"(unverified TLS is a MITM hole — set verify_tls=true with a ca_path)"
            )
        ca_path = cfg.get(profile, "ca_path", fallback="").strip()
        if not ca_path:
            raise ConfigError(
                f"profile '{profile}': scheme=https requires 'ca_path' (internal CA cert, "
                f"or 'os' to verify against the operating-system trust store)"
            )
        if ca_path.lower() == OS_TRUST:
            # OS-trust sentinel: verify against the operating-system trust store
            # (transport/shim build a truststore SSLContext for it). This is the
            # MDM-managed-corporate-CA case — the CA lives in the OS keychain and
            # there is no PEM file to point at. Still verified TLS, never a bypass.
            return OS_TRUST
        return str(Path(ca_path).expanduser())

    # http: no TLS to verify. verify=False is ONLY legal here (personal plaintext).
    return False


def resolve(
    service: str,
    cfg=None,
    profile: str | None = None,
    session_id: str | None = None,
) -> Endpoint:
    # symdex is stdio-local and must NEVER be constructed as an HTTP endpoint.
    if service == "symdex":
        raise ValueError("symdex is stdio-local and is never resolved as an HTTP service")
    if service not in SERVICES:
        raise ValueError(f"unknown service '{service}' (expected one of {SERVICES})")

    if cfg is None:
        cfg = load_config()
    if profile is None:
        profile = active_profile(cfg)
    if not cfg.has_section(profile):
        raise ConfigError(f"unknown profile '{profile}'")

    kind = _require(cfg, profile, "kind").strip().lower()
    # Normalize scheme so the https TLS guard (_verify_for) can't be bypassed by
    # a case/whitespace typo (e.g. "HTTPS") that an HTTP client would still treat
    # as TLS. RFC 3986 schemes are case-insensitive.
    scheme = _require(cfg, profile, "scheme").strip().lower()
    verify = _verify_for(cfg, profile, scheme)

    headers = {"X-Agent-Id": agent_id(cfg, profile)}
    if cfg.has_option(profile, "api_key"):
        key = cfg.get(profile, "api_key").strip()
        if key:
            headers["X-API-Key"] = key
    if session_id:
        headers["X-Session-Id"] = session_id

    if kind == "ports":
        host = _require(cfg, profile, "host")
        mcp_url = f"{scheme}://{host}:{MCP_PORTS[service]}/mcp"
        rest_base = f"{scheme}://{host}:{REST_PORTS[service]}"
    elif kind == "paths":
        base_url = _require(cfg, profile, "base_url").rstrip("/")
        # MITM guard: `verify` above was computed from `scheme`, but the actual
        # URL is built from `base_url`. If they disagree (e.g. scheme=http with
        # a base_url that is actually https://...), verify would be False on a
        # real TLS endpoint -> unverified handshake. Refuse the mismatch outright
        # rather than silently trusting whichever of the two is more permissive.
        if not base_url.lower().startswith(f"{scheme}://"):
            raise ConfigError(
                f"profile '{profile}': scheme='{scheme}' does not match base_url "
                f"'{base_url}' (base_url must start with '{scheme}://') — refusing "
                f"a scheme/base_url mismatch that could bypass TLS verification"
            )
        mcp_url = f"{base_url}/mcp/{service}"
        rest_base = f"{base_url}/api/{service}"
    else:
        raise ConfigError(
            f"profile '{profile}' has unknown kind '{kind}' (expected 'ports' or 'paths')"
        )

    return Endpoint(mcp_url=mcp_url, rest_base=rest_base, headers=headers, verify=verify)
