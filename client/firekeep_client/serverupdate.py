"""Server update visibility — detect-and-tell only.

Spec: docs/superpowers/specs/2026-08-25-server-update-visibility-design.md.
Never raises; never applies updates (spec decision 4: server updates can
carry irreversible store migrations — the tell always routes through
`bash update.sh --to vY`, which backs up first). Cortex /version is read
LIVE on every call; only the dist-manifest fetch is day-cached (decision 5 —
a cached verdict lied to the operator's post-update doctor run).
"""
from __future__ import annotations

import datetime
import json
import urllib.request
from dataclasses import dataclass

from firekeep_client import resolver, state, updater
from firekeep_client.transport import get_json

_TIMEOUT = 3.0
_CACHE_KEY = "server_update_check"


@dataclass
class ServerUpdateStatus:
    running: str
    latest: str | None
    relation: str  # "behind"|"current"|"ahead"|"unjudged"|"unprovenanced"
    ack: bool


# A build that cannot say what it is. `provenance._FALLBACK_VERSION` is the
# server-side source of truth, but it lives in a different distribution unit
# (the server repo, not the client wheel), so the value is restated here and
# tied to it by tests/test_provenance.py's build-plumbing guard.
#
# WHY THIS SET IS NOT JUST THE CURRENT SENTINEL: `0.6.0` is what every image
# built before the fix stamps, and those images are already in the field. The
# client is the only half of the pair that reaches them, so it carries the
# legacy value permanently. No `v0.6.0` server release has ever existed (the
# series runs v0.1.0..v0.4.7, then v1.0.0+), so this shadows nothing real.
_UNPROVENANCED = frozenset({"0.0.0-unprovenanced", "0.6.0"})


def is_unprovenanced(version: str) -> bool:
    """True when the server reported a build-time placeholder, not a version."""
    return version.strip().removeprefix("v") in _UNPROVENANCED


def _fetch_running(cfg) -> str | None:
    """Live cortex /version — the _check_versions fetch pattern, 3s."""
    try:
        ep = resolver.resolve("cortex", cfg=cfg)
        data = get_json(f"{ep.rest_base}/version", headers=ep.headers,
                        timeout=_TIMEOUT, verify=ep.verify)
        running = str((data or {}).get("version", "")).strip()
        return running or None
    except Exception:  # noqa: BLE001 — no answer means nothing to say
        return None


def _fetch_latest_uncached(cfg) -> str | None:
    """server/latest/server.json's version — updater.fetch_manifest's shape,
    the promoted dist_ssl_context (same trust story as the client manifest)."""
    base = updater.dist_base(cfg)  # raises UpdateError on checkout installs
    req = urllib.request.Request(f"{base}/server/latest/server.json")
    kwargs = {"timeout": _TIMEOUT}
    ctx = updater.dist_ssl_context()
    if ctx is not None:
        kwargs["context"] = ctx
    with urllib.request.urlopen(req, **kwargs) as resp:  # noqa: S310 — https dist host
        data = json.loads(resp.read().decode("utf-8"))
    version = data.get("version") if isinstance(data, dict) else None
    if not isinstance(version, str) or not version.strip():
        raise updater.UpdateError("malformed server manifest")
    return version.strip()


def _fetch_latest(cfg) -> str | None:
    """Day-cached (negatives too): one 3s cost per day, not per session."""
    try:
        today = datetime.date.today().isoformat()
        cached = state.read_scratch(_CACHE_KEY) or ""
        if cached.startswith(today + "|"):
            return cached.split("|", 1)[1] or None
        latest = ""
        try:
            latest = _fetch_latest_uncached(cfg) or ""
        finally:
            state.write_scratch(_CACHE_KEY, f"{today}|{latest}")
        return latest or None
    except Exception:  # noqa: BLE001
        return None


def _relation(running: str, latest: str | None) -> str:
    # Checked BEFORE the manifest: provenance is a property of the server, not
    # of dist-host reachability, so this verdict must never degrade to
    # "unjudged" merely because the manifest could not be fetched.
    if is_unprovenanced(running):
        return "unprovenanced"
    if latest is None:
        return "unjudged"
    try:
        run = running.strip().removeprefix("v")
        lat = latest.strip().removeprefix("v")
        if updater.is_newer(lat, run):
            return "behind"
        if updater.is_newer(run, lat):
            return "ahead"
        return "current"
    except updater.UpdateError:
        return "unjudged"


def _acked(cfg, latest: str | None) -> bool:
    try:
        if latest is None:
            return False
        return cfg.get("dist", "server_update_ack", fallback="").strip() == latest
    except Exception:  # noqa: BLE001
        return False


def check(cfg) -> ServerUpdateStatus | None:
    """None ONLY when cortex /version did not answer. Never raises."""
    try:
        running = _fetch_running(cfg)
        if running is None:
            return None
        latest = _fetch_latest(cfg)
        return ServerUpdateStatus(
            running=running, latest=latest,
            relation=_relation(running, latest),
            ack=_acked(cfg, latest),
        )
    except Exception:  # noqa: BLE001 — visibility must never cost a command
        return None


def is_clean_release(version: str) -> bool:
    """True for a clean vX.Y.Z; False for a git-describe suffix, an
    unprovenanced placeholder, or anything else malformed.

    Mirrors `_relation` step for step — the same `_UNPROVENANCED` membership
    test, then the same parse `_relation` applies via its `except UpdateError`
    — never a shape regex, so this and `_relation` can never disagree on an
    input. Both gained the sentinel check together for exactly that reason."""
    if is_unprovenanced(version):
        return False  # parses, but means "unknown" -- see _UNPROVENANCED
    try:
        updater.parse_version(version.strip().removeprefix("v"))
        return True
    except updater.UpdateError:
        return False


def nudge_line(status: ServerUpdateStatus | None) -> str:
    """The briefing line, or '' — behind + unacked only."""
    try:
        if status is None or status.relation != "behind" or status.ack:
            return ""
        return (f"\n\n[firekeep] server update available: {status.running} -> "
                f"{status.latest} — run `bash update.sh --to {status.latest}` "
                f"on the server host")
    except Exception:  # noqa: BLE001
        return ""
