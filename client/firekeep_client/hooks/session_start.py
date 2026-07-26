"""SessionStart core — thin GET /briefing fetch + presence register.

Supersedes scripts/briefing.sh's 610-line assembly: Cortex GET /briefing already
aggregates every section server-side and returns an authoritative `rendered`
text. Fetch happens BEFORE register so the server's crash-detection reads
pre-registration presence (design §6.1; parity with briefing.sh §2, which
snapshots presence before self-registering).

Also appends a one-line client-update nudge (board 2026-07-14: "briefing
nudge", not auto-update): at most one latest.json fetch per calendar day
(scratch-cached, including negative results), 3s timeout, silent on ANY
failure — the nudge can only ever add a line, never delay or break a session.
"""
from __future__ import annotations

import datetime
import os
import platform
import urllib.parse

from firekeep_client import autoupdate, hooklog, resolver, state, transport, updater
from firekeep_client.hooks import _mcp, never_raise

_HOOK = "session_start"
_FALLBACK = (
    "Firekeep MCP servers are available. Call ctx_start_session with your "
    "goal, then memory_recall for relevant context."
)
_UPDATE_CHECK_TIMEOUT = 3.0


def _update_nudge(cfg) -> str:
    """One line when a newer client exists, '' otherwise. When auto-update is on
    (the default) this ALSO fires a detached background `firekeep update`; when it's
    opted out, it falls back to the manual 'run: firekeep update' nudge. Never raises."""
    try:
        from firekeep_client import __version__
        today = datetime.date.today().isoformat()
        cache_key = "update_check"
        cached = state.read_scratch(cache_key) or ""
        if cached.startswith(today + "|"):
            latest = cached.split("|", 1)[1]
        else:
            latest = ""
            try:
                base = updater.dist_base(cfg)  # raises on checkout installs (no [dist])
                latest = updater.fetch_manifest(base, timeout=_UPDATE_CHECK_TIMEOUT).version
            finally:
                # Cache failures as an empty version too — an unreachable dist
                # host must cost at most ONE 3s timeout per day, not one per
                # session start.
                state.write_scratch(cache_key, f"{today}|{latest}")
        if latest and updater.is_newer(latest, __version__):
            # Only claim "updating in background" when a background update is actually
            # in flight (maybe_spawn True — spawned now or already claimed today).
            # If it's disabled, or the launcher is missing / the spawn failed, fall
            # back to the honest manual nudge instead of lying about an update.
            if autoupdate.is_enabled(cfg) and autoupdate.maybe_spawn(cfg, latest, today):
                return (f"\n\n[firekeep] updating client in background: "
                        f"{__version__} -> {latest} (applies next session; "
                        f"disable with `firekeep update --auto off`)")
            return (f"\n\n[firekeep] client update available: {__version__} -> {latest} — "
                    f"run: firekeep update")
    except Exception:  # noqa: BLE001 — the nudge must never cost a session
        pass
    return ""


@never_raise({})
def run(payload: dict) -> dict:
    cfg = resolver.load_config()
    profile = resolver.active_profile(cfg)
    agent = resolver.agent_id(cfg, profile)
    goal = payload.get("goal") or os.environ.get("FIREKEEP_AGENT_GOAL", "")

    # A NEW session must never inherit a previous (possibly crashed) session's
    # stashed Bridge id — clear UNCONDITIONALLY, before the briefing fetch, so a
    # briefing failure or a version-skewed server (no briefing_id) can't leave a
    # stale id riding this session's proxied calls.
    try:
        state.clear_session_stash(agent, profile)
    except Exception as e:  # noqa: BLE001
        hooklog.log_failure(_HOOK, f"session stash clear failed: {e}")

    # Briefing suppression: FIREKEEP_BRIEFING=off skips the server fetch entirely,
    # returning the minimal fallback. Useful when the briefing text is too verbose
    # for the user's workflow (e.g. large context windows, fast iteration).
    _briefing_off = os.environ.get("FIREKEEP_BRIEFING", "").strip().lower() == "off"

    # 1. Fetch server-rendered briefing (REST) BEFORE registering presence.
    rendered = _FALLBACK
    if not _briefing_off:
        try:
            ep = resolver.resolve("cortex", cfg=cfg)
            qs = urllib.parse.urlencode({"agent_id": agent, "goal": goal})
            data = transport.get_json(
                f"{ep.rest_base}/briefing?{qs}", headers=ep.headers, verify=ep.verify
            )
            if isinstance(data, dict) and data.get("rendered"):
                rendered = data["rendered"]
            # Stash the server-minted briefing_id so the bridge shim can inject it
            # into a ctx_start_session call the agent makes without it — closing the
            # briefing_id -> session_id A/B join mechanically. (The stale-id clear
            # already ran unconditionally above.)
            if isinstance(data, dict) and data.get("briefing_id"):
                try:
                    state.write_session_stash(agent, profile, briefing_id=data["briefing_id"])
                except Exception as e:  # noqa: BLE001
                    hooklog.log_failure(_HOOK, f"session stash write failed: {e}")
        except Exception as e:  # noqa: BLE001 — availability over enforcement
            hooklog.log_failure(_HOOK, f"GET /briefing failed: {e}")

    # 2. Register presence with Relay (best-effort).
    try:
        args = {"agent_id": agent, "goal": goal, "hostname": platform.node() or "unknown"}
        sid = payload.get("session_id")
        if sid:
            args["session_id"] = sid
        _mcp.call_tool("relay", "relay_register", args, cfg=cfg)
    except Exception as e:  # noqa: BLE001
        hooklog.log_failure(_HOOK, f"relay_register failed: {e}")

    # 3. Pin the registration epoch for stop.py's <5s race guard (unconditional,
    #    mirrors briefing.sh writing the reg timestamp on every start). Shared
    #    keying authority: firekeep_client.state.mark_registered/should_deregister
    #    (SP1b Task 19 seam reconciliation) -- the SAME scratch key the
    #    sidecar's independent registration guard reads, so a mixed
    #    hooks+sidecar composition for one agent_id can't clobber each other.
    try:
        state.mark_registered(agent, profile=profile)
    except Exception as e:  # noqa: BLE001
        hooklog.log_failure(_HOOK, f"scratch write failed: {e}")

    return {"systemMessage": rendered + _update_nudge(cfg)}
