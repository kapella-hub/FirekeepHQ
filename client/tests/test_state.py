"""Tests for firekeep_client.state — platform cache dir + pre_tool/post_tool shared
temp-state (design SS6.2). The load-bearing invariant under test is the
shared-state pin: pre_tool and post_tool MUST resolve the same cache_dir()
and key files by session_id/action_id identically, or /agent/action
before->after reconciliation silently breaks.

All file-op tests point FIREKEEP_CACHE_DIR at tmp_path so the real ~/.cache or
%LOCALAPPDATA% is never touched. Platform-default tests additionally
monkeypatch state.sys.platform + the platform env vars / Path.home so no
env leaks between tests (monkeypatch auto-reverts).
"""
from __future__ import annotations

import pytest

from firekeep_client import state


# --- cache_dir() ---------------------------------------------------------


def test_cache_dir_env_override_creates_dir_and_applies_private(tmp_path, monkeypatch):
    target = tmp_path / "custom-cache"
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(target))
    calls = []
    monkeypatch.setattr(state, "_private", lambda p: calls.append(p))

    d = state.cache_dir()

    assert d == target
    assert d.is_dir()
    assert calls == [target]


def test_cache_dir_win32_default_uses_localappdata(tmp_path, monkeypatch):
    monkeypatch.delenv("FIREKEEP_CACHE_DIR", raising=False)
    monkeypatch.setattr(state.sys, "platform", "win32")
    local_appdata = tmp_path / "AppData" / "Local"
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))

    d = state.cache_dir()

    assert d == local_appdata / "firekeep"
    assert d.is_dir()


def test_cache_dir_posix_default_uses_xdg_cache_home(tmp_path, monkeypatch):
    monkeypatch.delenv("FIREKEEP_CACHE_DIR", raising=False)
    monkeypatch.setattr(state.sys, "platform", "linux")
    xdg = tmp_path / "xdgcache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg))

    d = state.cache_dir()

    assert d == xdg / "firekeep"
    assert d.is_dir()


def test_cache_dir_posix_default_falls_back_to_home_cache(tmp_path, monkeypatch):
    monkeypatch.delenv("FIREKEEP_CACHE_DIR", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr(state.sys, "platform", "linux")
    monkeypatch.setattr(state.Path, "home", lambda: tmp_path)

    d = state.cache_dir()

    assert d == tmp_path / ".cache" / "firekeep"
    assert d.is_dir()


# --- _private() ------------------------------------------------------------


def test_private_win32_calls_icacls(tmp_path, monkeypatch):
    monkeypatch.setattr(state.sys, "platform", "win32")
    monkeypatch.setenv("USERNAME", "mogan")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(state.subprocess, "run", fake_run)

    target_dir = tmp_path / "adir"
    target_dir.mkdir()
    state._private(target_dir)

    assert captured["cmd"][0] == "icacls"
    assert captured["cmd"][1] == str(target_dir)
    assert "mogan" in captured["cmd"][-1]
    assert "(OI)(CI)F" in captured["cmd"][-1]


def test_private_win32_file_uses_f_flag_not_oici(tmp_path, monkeypatch):
    monkeypatch.setattr(state.sys, "platform", "win32")
    monkeypatch.setenv("USERNAME", "mogan")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(state.subprocess, "run", fake_run)

    target_file = tmp_path / "afile.txt"
    target_file.write_text("x", encoding="utf-8")
    state._private(target_file)

    assert captured["cmd"][-1] == "mogan:F"


def test_private_win32_missing_username_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(state.sys, "platform", "win32")
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.delenv("USER", raising=False)
    called = []
    monkeypatch.setattr(state.subprocess, "run", lambda *a, **k: called.append(a))

    state._private(tmp_path)

    assert called == []


def test_private_posix_dir_chmods_0700(tmp_path, monkeypatch):
    monkeypatch.setattr(state.sys, "platform", "linux")
    captured = {}
    monkeypatch.setattr(
        state.os, "chmod", lambda p, mode: captured.update(path=p, mode=mode)
    )

    target_dir = tmp_path / "adir"
    target_dir.mkdir()
    state._private(target_dir)

    assert captured["mode"] == 0o700


def test_private_posix_file_chmods_0600(tmp_path, monkeypatch):
    monkeypatch.setattr(state.sys, "platform", "linux")
    captured = {}
    monkeypatch.setattr(
        state.os, "chmod", lambda p, mode: captured.update(path=p, mode=mode)
    )

    target_file = tmp_path / "afile.txt"
    target_file.write_text("x", encoding="utf-8")
    state._private(target_file)

    assert captured["mode"] == 0o600


def test_private_swallows_exceptions(tmp_path, monkeypatch):
    monkeypatch.setattr(state.sys, "platform", "linux")

    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(state.os, "chmod", boom)

    # Must not raise.
    state._private(tmp_path)


# --- push_action / pop_action (LIFO per session) ----------------------------


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    """Every test in this module gets a private FIREKEEP_CACHE_DIR by default.

    This fixture applies to ALL tests in the module, including the
    platform-default tests above -- they still pass because each explicitly
    calls monkeypatch.delenv("FIREKEEP_CACHE_DIR") in its own body, which runs
    after this fixture's setenv and leaves the var unset for that test. If a
    platform-default test ever drops that delenv, it silently stops
    exercising the platform-default branch (falls through to this override
    instead) -- keep the delenv when touching those tests.
    """
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path / "cache"))


def test_push_pop_action_lifo_order():
    state.push_action("session-1", "action-a")
    state.push_action("session-1", "action-b")
    state.push_action("session-1", "action-c")

    assert state.pop_action("session-1") == "action-c"
    assert state.pop_action("session-1") == "action-b"
    assert state.pop_action("session-1") == "action-a"
    assert state.pop_action("session-1") is None


def test_pop_action_missing_session_returns_none():
    assert state.pop_action("no-such-session") is None


def test_push_pop_action_is_scoped_per_session():
    state.push_action("session-1", "a1")
    state.push_action("session-2", "b1")

    assert state.pop_action("session-2") == "b1"
    assert state.pop_action("session-1") == "a1"
    assert state.pop_action("session-2") is None
    assert state.pop_action("session-1") is None


# --- write_prestate / read_prestate roundtrip -------------------------------


def test_prestate_roundtrip():
    state.write_prestate("action-1", "deadbeef" * 4)
    assert state.read_prestate("action-1") == "deadbeef" * 4


def test_read_prestate_missing_returns_none():
    assert state.read_prestate("no-such-action") is None


# --- scratch roundtrip + delete idempotency + name sanitization ------------


def test_scratch_roundtrip():
    state.write_scratch("mykey", "myvalue")
    assert state.read_scratch("mykey") == "myvalue"


def test_read_scratch_missing_returns_none():
    assert state.read_scratch("no-such-key") is None


def test_delete_scratch_is_idempotent():
    state.write_scratch("mykey", "myvalue")
    state.delete_scratch("mykey")
    assert state.read_scratch("mykey") is None
    # Second delete on an already-missing key must not raise.
    state.delete_scratch("mykey")


def test_scratch_name_with_path_separators_is_sanitized(tmp_path):
    # A name containing path separators must not escape the scratch dir or
    # create nested directories -- it round-trips through the same
    # sanitization on write and read.
    state.write_scratch("a/b\\c", "value")
    assert state.read_scratch("a/b\\c") == "value"

    scratch_dir = state.cache_dir() / "scratch"
    children = list(scratch_dir.iterdir())
    assert all(c.is_file() for c in children)


# --- The shared-state pin (SS6.2 half #1) -----------------------------------


def test_shared_state_pin_pre_tool_then_post_tool():
    """pre_tool writes prestate + pushes the action; post_tool (a separate
    process in production, simulated here by calling the same module-level
    functions after the "pre_tool" calls) must see the identical values.
    If cache_dir()/file keying ever diverges between the two cores, this is
    the test that goes red.
    """
    # --- as pre_tool ---
    state.write_prestate("action-42", "sha-of-original-file")
    state.push_action("session-9", "action-42")

    # --- as post_tool ---
    popped_action_id = state.pop_action("session-9")
    prestate_sha = state.read_prestate(popped_action_id)

    assert popped_action_id == "action-42"
    assert prestate_sha == "sha-of-original-file"


# --- Containment: untrusted ids can never escape the cache dir ---------------


def test_traversal_session_id_stays_inside_cache_dir(tmp_path, monkeypatch):
    """action_id/session_id come from tool payloads (untrusted). A traversal
    id must be flattened into the cache dir, never written outside it."""
    cache = tmp_path / "cache"
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(cache))
    state.push_action("../../pwned", "evil-action-id")
    # Nothing escaped: no file appeared as a sibling of the cache dir...
    assert not (tmp_path / "pwned.queue").exists()
    # ...and the flattened queue file landed inside it, still functional.
    assert state.pop_action("../../pwned") == "evil-action-id"
    for p in cache.rglob("*"):
        assert str(p.resolve()).startswith(str(cache.resolve()))


def test_traversal_action_id_stays_inside_cache_dir(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(cache))
    state.write_prestate("..\\..\\evil", "sha-x")
    assert state.read_prestate("..\\..\\evil") == "sha-x"
    assert not (tmp_path / "evil.sha256").exists()


def test_bare_dotdot_scratch_name_is_contained(tmp_path, monkeypatch):
    """A bare '..' has no separators, so replace() alone can't fix it — the
    sanitizer must prefix it rather than resolve to the dir/parent."""
    cache = tmp_path / "cache"
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(cache))
    state.write_scratch("..", "v")  # must not raise / write to a directory
    assert state.read_scratch("..") == "v"
    state.delete_scratch("..")
    assert state.read_scratch("..") is None


# --- resolve_session_id() (SS6.2 half #2: pre_tool/post_tool must agree) ----


def _fake_endpoint(rest_base="http://127.0.0.1:8070", agent="mogan"):
    return state.resolver.Endpoint(
        mcp_url="http://127.0.0.1:8070/mcp",
        rest_base=rest_base,
        headers={"X-Agent-Id": agent},
        verify=False,
    )


def test_resolve_session_id_returns_payload_value_without_network_call(monkeypatch):
    def fail_resolve(*a, **k):
        raise AssertionError("resolver.resolve must not be called when payload has session_id")

    def fail_get_json(*a, **k):
        raise AssertionError("transport.get_json must not be called when payload has session_id")

    monkeypatch.setattr(state.resolver, "resolve", fail_resolve)
    monkeypatch.setattr(state.transport, "get_json", fail_get_json)

    assert state.resolve_session_id({"session_id": "sess-abc"}) == "sess-abc"


def test_resolve_session_id_fetches_active_session_from_bridge(monkeypatch):
    captured = {}
    monkeypatch.setattr(state.resolver, "resolve", lambda service, cfg=None: _fake_endpoint())

    def fake_get_json(url, *, headers, **kwargs):
        captured["url"] = url
        captured["headers"] = headers
        return {"sessions": [{"session_id": "sess-live-1"}]}

    monkeypatch.setattr(state.transport, "get_json", fake_get_json)

    sid = state.resolve_session_id({})

    assert sid == "sess-live-1"
    assert captured["url"].startswith("http://127.0.0.1:8070/sessions")
    assert captured["headers"].get("X-Agent-Id") == "mogan"


def test_resolve_session_id_transport_failure_degrades_to_unknown(monkeypatch):
    monkeypatch.setattr(state.resolver, "resolve", lambda service, cfg=None: _fake_endpoint())

    def boom(*a, **k):
        raise state.transport.TransportError("bridge unreachable")

    monkeypatch.setattr(state.transport, "get_json", boom)

    assert state.resolve_session_id({}) == "unknown"


def test_resolve_session_id_resolver_failure_degrades_to_unknown(monkeypatch):
    def boom(*a, **k):
        raise state.resolver.ConfigError("no config")

    monkeypatch.setattr(state.resolver, "resolve", boom)

    assert state.resolve_session_id({}) == "unknown"


def test_resolve_session_id_no_active_sessions_degrades_to_unknown(monkeypatch):
    monkeypatch.setattr(state.resolver, "resolve", lambda service, cfg=None: _fake_endpoint())
    monkeypatch.setattr(state.transport, "get_json", lambda *a, **k: {"sessions": []})

    assert state.resolve_session_id({}) == "unknown"


def test_resolve_session_id_url_quotes_agent_id(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        state.resolver, "resolve", lambda service, cfg=None: _fake_endpoint(agent="mo gan")
    )

    def fake_get_json(url, *, headers, **kwargs):
        captured["url"] = url
        return {"sessions": []}

    monkeypatch.setattr(state.transport, "get_json", fake_get_json)

    state.resolve_session_id({})

    assert "mo gan" not in captured["url"]
    assert "mo%20gan" in captured["url"]


def test_resolve_session_id_determinism_pin_pre_tool_then_post_tool_agree(monkeypatch):
    """The load-bearing invariant: pre_tool and post_tool call resolve_session_id
    with the same payload/cfg and MUST get back the identical session_id, or
    /agent/action before->after reconciliation breaks (SS6.2 half #2)."""
    monkeypatch.setattr(state.resolver, "resolve", lambda service, cfg=None: _fake_endpoint())
    monkeypatch.setattr(
        state.transport,
        "get_json",
        lambda *a, **k: {"sessions": [{"session_id": "sess-fixed"}]},
    )

    payload = {}
    sid_from_pre_tool = state.resolve_session_id(payload)
    sid_from_post_tool = state.resolve_session_id(payload)

    assert sid_from_pre_tool == sid_from_post_tool == "sess-fixed"


# --- prestate cleanup (bash parity: postaction unlinked, precheck reaped) -----


def test_delete_prestate_removes_snapshot():
    state.write_prestate("act-del", "sha-1")
    assert state.read_prestate("act-del") == "sha-1"
    state.delete_prestate("act-del")
    assert state.read_prestate("act-del") is None
    state.delete_prestate("act-del")  # idempotent


def test_reap_stale_removes_old_keeps_fresh():
    import os
    import time

    state.write_prestate("act-old", "sha-old")
    state.write_prestate("act-new", "sha-new")
    state.push_action("sess-old", "a1")
    # Age the "old" files by back-dating mtime beyond the window.
    old_pre = state._prestate_file("act-old")
    old_q = state._actions_file("sess-old")
    past = time.time() - 7200
    os.utime(old_pre, (past, past))
    os.utime(old_q, (past, past))

    state.reap_stale(max_age_seconds=3600)

    assert state.read_prestate("act-old") is None       # reaped
    assert state.read_prestate("act-new") == "sha-new"  # kept
    assert state.pop_action("sess-old") is None         # queue reaped


# --- registration keys -------------------------------------------------------


def test_registration_keys_are_agent_qualified(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    from firekeep_client import state
    state.mark_registered("agent-a")
    assert state.should_deregister("agent-b") is True
    assert state.should_deregister("agent-a") is False
    state.clear_registered("agent-b")
    assert state.should_deregister("agent-a") is False
    state.clear_registered("agent-a")
    assert state.should_deregister("agent-a") is True


def test_unqualified_keys_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    from firekeep_client import state
    state.mark_registered("agent-b")
    assert state.should_deregister("agent-b") is False
    state.clear_registered("agent-b")
    assert state.should_deregister("agent-b") is True


# --- session stash (identity auto-injection) --------------------------------


def test_session_stash_write_read_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    from firekeep_client import state
    state.write_session_stash("agent-a", briefing_id="brf-1")
    state.write_session_stash("agent-a", session_id="sess-9")
    got = state.read_session_stash("agent-a")
    # merge-write: both fields survive across two calls
    assert got["session_id"] == "sess-9"
    assert got["briefing_id"] == "brf-1"


def test_session_stash_is_agent_qualified(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    from firekeep_client import state
    state.write_session_stash("agent-a", session_id="sess-a")
    assert state.read_session_stash("agent-b") is None
    assert state.read_session_stash("agent-a")["session_id"] == "sess-a"


def test_session_stash_ttl_expiry_self_enforced(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    from firekeep_client import state
    now = [1000.0]
    monkeypatch.setattr(state.time, "time", lambda: now[0])
    state.write_session_stash("agent-a", session_id="sess-9")
    now[0] = 1000.0 + 13 * 3600  # 13h later, past the 12h default
    assert state.read_session_stash("agent-a") is None
    # within TTL it is returned
    now[0] = 1000.0 + 1 * 3600
    state.write_session_stash("agent-a", session_id="sess-fresh")
    now[0] = 1000.0 + 1.5 * 3600
    assert state.read_session_stash("agent-a")["session_id"] == "sess-fresh"


def test_session_stash_ttl_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("FIREKEEP_SESSION_STASH_TTL_HOURS", "1")
    from firekeep_client import state
    now = [500.0]
    monkeypatch.setattr(state.time, "time", lambda: now[0])
    state.write_session_stash("agent-a", session_id="sess-9")
    now[0] = 500.0 + 90 * 60  # 90min later, past the 1h override
    assert state.read_session_stash("agent-a") is None


def test_session_stash_clear(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    from firekeep_client import state
    state.write_session_stash("agent-a", session_id="sess-9")
    state.clear_session_stash("agent-a")
    assert state.read_session_stash("agent-a") is None
    state.clear_session_stash("agent-a")  # idempotent, no raise


def test_session_stash_read_never_raises_on_garbage(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    from firekeep_client import state
    state.write_scratch(state._session_stash_key("agent-a"), "not json {{{")
    assert state.read_session_stash("agent-a") is None
