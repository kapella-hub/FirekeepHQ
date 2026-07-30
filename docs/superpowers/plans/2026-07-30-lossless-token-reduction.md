# Lossless Token Reduction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Firekeep a compaction signal and a delta-restore path so a customer gets more turns before compaction and loses no working state when it happens — with zero loss of accuracy, capability, or behaviour.

**Architecture:** Three independently shippable phases. Phase B adds a `precompact` hook core (checkpoint + cursor invalidation) on the only runtime that can signal compaction. Phase C adds the *residency contract*: `ctx_get_shadow()` stays byte-identical and remains the default, while `ctx_get_shadow(since=<cursor>)` returns a delta the agent explicitly opts into. Phase D ships the remaining independent wins. The design's load-bearing property is that **every failure path returns a full restore** — where it can be wrong, it is wrong in the direction of sending too much.

**Tech Stack:** Python 3.11+, FastMCP 3.1.x, Redis (bridge DB 3), pytest + fakeredis, stdlib-only client kit.

## Global Constraints

- **Zero degradation.** Any saving whose mechanism is "give the agent less of what it asked for" is out of scope, however large. A delta may never let an agent conclude that omitted content *does not exist*.
- **Duplication beats omission.** Every boundary comparison is inclusive. Re-sending one entry costs tokens; dropping one costs correctness.
- **`assemble_shadow`'s signature does not change.** It has four consumers (§Blast radius). Filtering happens on the `data` dict *before* assembly.
- **No new MCP tool.** The spec rejects growing the tool surface; that applies to our own additions. The epoch bump rides on the existing `ctx_update(category="scratch")`.
- **The client kit is stdlib-only** (`firekeep_client.hooks` import boundary). No new third-party dependency in `client/`.
- **Every hook core is `@never_raise`, bypass-gated first, and best-effort.** A slow or failing hook must never break a customer's session.
- **Cache integrity is a correctness property.** Nothing may make the rendered surface non-byte-stable. Guarded by `client/tests/adapters/test_write_stability.py`.
- **Do not rename any string in `adapters/base.py`'s legacy blocks** (`base.py:25-33` warning). Renaming a legacy token is not a rename, it is a deletion.

---

## Prerequisite — already shipped (2026-07-30)

Do not re-implement. Verify present, then proceed.

- Per-key scratch TTL: `state.write_scratch(name, value, *, ttl_seconds=None)`, `_scratch_ttl_path`, `_scratch_expired`, `_reap_expired_scratch`. Opt-in per key; a key passing no `ttl_seconds` is bit-identical to pre-change behaviour. Tests: `client/tests/test_scratch_ttl.py` (8).
- `tasks_digest` forever-suppression bug fixed (`hooks/prompt.py`, `_TASKS_DIGEST_TTL_SECONDS = 12 * 3600`).
- `write_text_if_changed` cache-integrity guard + byte-stability test.
- Free wins 3, 4, 5, 7 (dead tool reference, corpus descriptions, orphan noun phrase, repeated briefing header) with two permanent doc guards.

Verify: `cd client && python -m pytest tests/test_scratch_ttl.py tests/adapters/test_write_stability.py -q` → 15 passed.

---

## Ground truth (verified by reading code — do not re-derive)

**Shadow storage** (`bridge/app/session.py`):

| Section | Redis | Per-entry timestamp | Eviction |
|---|---|---|---|
| `plan` | `SET nb:session:{sid}:plan` | none (single string) | n/a, overwritten |
| `decisions` | `LPUSH` + `LTRIM 0 DECISIONS_MAX-1` | `{"timestamp": ISO-µs, "content"}` | **oldest evicted** |
| `progress` | `LPUSH` + `LTRIM 0 PROGRESS_MAX-1` | `{"timestamp": ISO-µs, "content"}` | **oldest evicted** |
| `files` | `HSET nb:session:{sid}:files` field=path | `{"summary", "last_action": ISO-µs}` | oldest `hkeys` slice over `FILES_MAX` |
| `scratch` | `HSET nb:session:{sid}:scratch` field=key | **NONE — value is the raw content string** | none |

Two consequences drive the cursor design:

1. **An index/count cursor is unsafe.** `LTRIM` evicts oldest entries, so "I have seen the first 47" stops meaning anything once the window shifts — it would *skip* entries silently. Use timestamps.
2. **`scratch` cannot be time-filtered at all** (no timestamp). A delta therefore **always sends `scratch` in full**. This is not a compromise: `precompact` writes the workspace snapshot into scratch, so it is exactly what a post-compaction agent needs most. `plan` is a single string, so it is compared by SHA-256 recorded in the cursor.

`get_session_data` returns `decisions`/`progress` oldest→newest (`reversed(lrange)`).

**`assemble_shadow(data: dict[str, Any]) -> str`** (`bridge/app/shadow.py:8`) returns a **Markdown string**. Fixed section order: Plan, Decisions, Files Known, Progress, Scratchpad.

**Blast radius — the four consumers, all of which must keep working untouched:**

| Consumer | Site |
|---|---|
| `ctx_get_shadow` MCP tool | `bridge/app/mcp_server.py:357` → `{"shadow": shadow, ...}` |
| `ctx_resume_session` | `bridge/app/mcp_server.py:487` |
| `GET /sessions/{session_id}` | `bridge/app/mcp_server.py:552-568` |
| replay context snapshot on plan/decision `ctx_update` | `bridge/app/mcp_server.py:297-300` |

**Pre-existing bug, adjacent (Task 12):** `GET /sessions/{id}` returns `"shadow"` as a *string*, but `cortex/app/skills/scorer.py:154-156` does `shadow = resp.json().get("shadow") or {}` then `shadow.get("scratch", {})` — `str` has no `.get`, so it raises `AttributeError` inside a bare `except Exception:` and `_resolution_language_score` **always returns 0.0**. `SKILL_RESOLUTION_WEIGHT=0.35` is the largest weight in the skill scorer and is permanently dead. Same shape at `cortex/app/skills/synthesizer.py:288-294`.

**Hook dispatcher** (`client/firekeep_client/hooks/__main__.py`): `_CORE_MODULES` maps name→module; `_DICT_CORES` print `json.dumps(run(payload))` to stdout and always exit 0; `_INT_CORES` exit with `run()`'s code. `_BYPASS_EXEMPT = {"stop", "session_end"}` — every other core is short-circuited by the dispatcher's bypass gate, which prints `_BYPASS_MSG`.

**Matrix + tests that must change:**
- `client/firekeep_client/contract/matrix.py:41` — `"precompact": {"claude": "none", ...}`.
- `client/tests/contract/test_matrix.py::test_precompact_is_wired_nowhere` — asserts `"none"` for **all four** runtimes. Corrected 2026-07-29 after the matrix was found overstating coverage; its intent (never claim a capability the kit does not render) is preserved, only claude's row changes.
- `client/tests/adapters/test_claude.py::test_claude_render_leaves_precompact_hook_alone` — asserts `len(hooks["PreCompact"]) == 1`.
- `client/firekeep_client/adapters/base.py:25-33` — comment states "the kit renders no PreCompact hook of its own". That premise becomes false; amend the prose, **touch no legacy token string**.

---

## File Structure

**Create:**
- `bridge/app/residency.py` — cursor codec + `filter_since`. Pure functions, no I/O, no Redis. Separate from `shadow.py` so the rendering module keeps one responsibility and the fail-safe logic is unit-testable without a session.
- `bridge/tests/test_residency.py` — codec + filter + fail-safe matrix.
- `bridge/tests/test_shadow_delta.py` — tool-level fail-safes + delta-union equivalence.
- `client/firekeep_client/hooks/precompact.py` — the new dict core.
- `client/tests/hooks/test_precompact.py`.

**Modify:**
- `bridge/app/session.py` — `shadow_epoch` accessor.
- `bridge/app/mcp_server.py:335-365` — `ctx_get_shadow(since=...)`.
- `client/firekeep_client/hooks/__main__.py` — register the core.
- `client/firekeep_client/adapters/claude.py` — render the PreCompact group.
- `client/firekeep_client/adapters/base.py:25-33` — amend the rationale prose.
- `client/firekeep_client/contract/matrix.py:41` + `client/tests/contract/test_matrix.py`.
- `client/tests/adapters/test_claude.py:231-243`.
- `cortex/app/skills/scorer.py`, `cortex/app/skills/synthesizer.py` (Task 12).

---

# Phase B — the `precompact` hook core

Independently shippable. Delivers a workspace checkpoint at the one moment it matters, with no dependency on Phase C.

### Task 1: The `precompact` dict core

**Files:**
- Create: `client/firekeep_client/hooks/precompact.py`
- Test: `client/tests/hooks/test_precompact.py`

**Interfaces:**
- Consumes: `resolver.is_bypassed()`, `state.write_session_stash`/`read_session_stash`, `hooks._git.workspace_snapshot()`, `hooks._mcp.call_tool`, `hooks.never_raise`.
- Produces: `precompact.run(payload: dict) -> dict` — a dict core. Returns `{}` or `{"systemMessage": str}`.

**Scope discipline:** a PreCompact hook fires *before* compaction but cannot read the agent's unstated reasoning. It **cannot** recover decisions the agent never wrote via `ctx_update`. Claims otherwise are wrong. It does four cheap, certain things.

- [ ] **Step 1: Write the failing tests**

```python
# client/tests/hooks/test_precompact.py
"""PreCompact core: checkpoint the workspace, invalidate the shadow cursor,
stamp that a compaction happened. Best-effort, never blocking."""
from __future__ import annotations


def _record_mcp(monkeypatch):
    from firekeep_client.hooks import _mcp
    calls = []

    def fake_call(service, tool, args, **k):
        calls.append((service, tool, args))
        return {"status": "ok"}

    monkeypatch.setattr(_mcp, "call_tool", fake_call)
    return calls


class TestPrecompact:
    def test_bypass_returns_immediately_and_touches_nothing(self, client_env, monkeypatch):
        from firekeep_client import resolver
        from firekeep_client.hooks import precompact
        monkeypatch.setattr(resolver, "is_bypassed", lambda: True)
        calls = _record_mcp(monkeypatch)
        assert precompact.run({}) == {}
        assert calls == []          # personal mode must reach nothing

    def test_checkpoints_the_workspace_snapshot_to_bridge_scratch(self, client_env, monkeypatch):
        from firekeep_client.hooks import _git, precompact
        monkeypatch.setattr(_git, "workspace_snapshot", lambda *a, **k: "branch=main commits=3")
        calls = _record_mcp(monkeypatch)
        precompact.run({})
        updates = [a for s, t, a in calls if t == "ctx_update"]
        snap = [u for u in updates if u.get("key") == "workspace_snapshot"]
        assert len(snap) == 1
        assert snap[0]["category"] == "scratch"
        assert "branch=main" in snap[0]["content"]

    def test_bumps_the_shadow_epoch_via_ctx_update_not_a_new_tool(self, client_env, monkeypatch):
        from firekeep_client.hooks import precompact
        calls = _record_mcp(monkeypatch)
        precompact.run({})
        tools = {t for _, t, _ in calls}
        assert tools <= {"ctx_update"}, f"precompact must add no new tool surface: {tools}"
        epochs = [a for _, t, a in calls if t == "ctx_update" and a.get("key") == "shadow_epoch"]
        assert len(epochs) == 1
        assert epochs[0]["category"] == "scratch"

    def test_clears_the_local_shadow_cursor(self, client_env, monkeypatch):
        from firekeep_client import state
        from firekeep_client.hooks import precompact
        _record_mcp(monkeypatch)
        state.write_session_stash("tester", "personal", session_id="s1")
        state.write_shadow_cursor("tester", "personal", "cursor-abc")
        precompact.run({})
        assert state.read_shadow_cursor("tester", "personal") is None

    def test_emits_one_short_line_telling_the_agent_where_state_is(self, client_env, monkeypatch):
        from firekeep_client.hooks import precompact
        _record_mcp(monkeypatch)
        out = precompact.run({})
        assert "ctx_get_shadow" in out["systemMessage"]

    def test_never_raises_when_bridge_is_unreachable(self, client_env, monkeypatch):
        from firekeep_client.hooks import _mcp, precompact

        def boom(*a, **k):
            raise RuntimeError("bridge down")

        monkeypatch.setattr(_mcp, "call_tool", boom)
        assert precompact.run({}) == {} or isinstance(precompact.run({}), dict)

    def test_does_not_read_the_transcript_path(self, client_env, monkeypatch):
        """Pushing a raw transcript tail to the server is a privacy decision for a
        sold product, not an engineering one. Deliberately out of scope — this
        test is the guard that it stays out until that decision is made."""
        from firekeep_client.hooks import precompact
        calls = _record_mcp(monkeypatch)
        precompact.run({"transcript_path": "/tmp/should-not-be-read.jsonl"})
        blob = repr(calls)
        assert "should-not-be-read" not in blob
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd client && python -m pytest tests/hooks/test_precompact.py -q`
Expected: FAIL — `ModuleNotFoundError: firekeep_client.hooks.precompact` (and, once the module exists, `AttributeError: state.write_shadow_cursor`).

- [ ] **Step 3: Add the cursor stash accessors to `state.py`**

**Correction (2026-07-30, caught in Task 1 review):** the spec's §5.2 says the cursor must not live in "bare `write_scratch`", and this plan originally repeated that as "use the session-stash pattern (self-enforced embedded-ts TTL)". That mischaracterizes the code below, which passes `write_scratch(..., ttl_seconds=...)` — the *declared-expiry* mechanism, not the stash's embedded-`ts` one.

The code is correct and the prose was wrong. §5.2's actual requirement is "must have a TTL", and `write_scratch` gained one after the spec was written (Phase A). Both mechanisms fail the same safe way — `_scratch_expired`'s docstring already names this exact consumer: *"a lapsed cursor forces a full restore (lossless)"*. Declared-expiry is the better fit here because the cursor is an opaque string with no JSON envelope to embed a timestamp in. Do not "fix" this toward the stash pattern.

```python
# client/firekeep_client/state.py — append near the session-stash helpers

def _shadow_cursor_key(agent_id: str, profile: str) -> str:
    return f"shadow_cursor_{agent_id}@{profile}"


def write_shadow_cursor(agent_id: str, profile: str, cursor: str) -> None:
    """Stash the opaque shadow cursor. TTL'd like the session stash: a cursor
    that outlives its session must expire rather than be replayed. Never raises."""
    try:
        write_scratch(_shadow_cursor_key(agent_id, profile), cursor,
                      ttl_seconds=_session_stash_ttl_seconds())
    except Exception:
        pass


def read_shadow_cursor(agent_id: str, profile: str) -> str | None:
    """The stashed cursor, or None if absent/expired. None means 'ask for a full
    restore' — the safe default."""
    try:
        return read_scratch(_shadow_cursor_key(agent_id, profile))
    except Exception:
        return None


def clear_shadow_cursor(agent_id: str, profile: str) -> None:
    """Idempotent, never raises. Called by precompact: after a compaction the
    agent can no longer vouch for what is still in its context."""
    try:
        delete_scratch(_shadow_cursor_key(agent_id, profile))
    except Exception:
        pass
```

- [ ] **Step 4: Write the core**

```python
# client/firekeep_client/hooks/precompact.py
"""PreCompact core — checkpoint before the context is compacted.

Claude is the only runtime that exposes a compaction event. Scope is deliberately
narrow: this hook fires BEFORE compaction but cannot read the agent's unstated
reasoning, so it CANNOT recover decisions the agent never wrote via ctx_update.
It does four cheap, certain things: checkpoint the workspace, invalidate the
shadow cursor (locally and server-side), stamp that a compaction happened, and
tell the agent in one line where its working state lives.

Budgeted like session_start (~15s) and best-effort throughout: a slow hook
stalls the customer mid-compaction, which is worse than a missed checkpoint.
"""
from __future__ import annotations

from datetime import datetime, timezone

from firekeep_client import hooklog, resolver, state
from firekeep_client.hooks import _git, _mcp, never_raise

_HOOK = "precompact"

_NOTICE = (
    "Context was compacted. Your plan, decisions, file knowledge and progress are "
    "in Bridge — call ctx_get_shadow() to restore them before asking the user to repeat anything."
)


# never_raise takes ONE argument: the safe default. The hook name for
# hooklog.log_failure is derived from the wrapped function's module.
@never_raise({})
def run(payload: dict) -> dict:
    # 1. Bypass gate FIRST — before any config resolution or network call.
    if resolver.is_bypassed():
        return {}

    # Identity resolution copied verbatim from hooks/session_start.py:73-75.
    # Called UNGUARDED on purpose: a malformed config raises ConfigError, which
    # @never_raise degrades to {} rather than crashing the caller.
    cfg = resolver.load_config()
    profile = resolver.active_profile(cfg)
    agent = resolver.agent_id(cfg, profile)

    # 2. Workspace checkpoint — cheap, real, already implemented.
    try:
        snapshot = _git.workspace_snapshot()
        if snapshot:
            _mcp.call_tool("bridge", "ctx_update", {
                "category": "scratch", "key": "workspace_snapshot",
                "content": snapshot, "agent_id": agent,
            }, cfg=cfg)
    except Exception as e:  # noqa: BLE001
        hooklog.log_failure(_HOOK, f"workspace checkpoint failed: {e}")

    # 3. Invalidate the shadow cursor, locally AND server-side. Load-bearing for
    #    the residency contract: after compaction the agent can no longer vouch
    #    for what is still in its context, so any cursor it holds is a lie.
    #    The server-side half rides on ordinary ctx_update — no new MCP tool.
    state.clear_shadow_cursor(agent, profile)
    try:
        _mcp.call_tool("bridge", "ctx_update", {
            "category": "scratch", "key": "shadow_epoch",
            "content": str(int(time.time() * 1000)), "agent_id": agent,
        }, cfg=cfg)
    except Exception as e:  # noqa: BLE001
        hooklog.log_failure(_HOOK, f"epoch bump failed: {e}")

    # 4. Stamp that a compaction occurred. This lands in the session scratch, so
    #    it is visible in every subsequent shadow restore — including the delta,
    #    which always sends scratch in full. See the note below on consumers.
    try:
        _mcp.call_tool("bridge", "ctx_update", {
            "category": "scratch", "key": "compacted_at",
            "content": datetime.now(timezone.utc).isoformat(),
            "agent_id": agent,
        }, cfg=cfg)
    except Exception as e:  # noqa: BLE001
        hooklog.log_failure(_HOOK, f"compacted_at stamp failed: {e}")

    return {"systemMessage": _NOTICE}
```

**On `compacted_at`'s consumers — read this before implementing.** The design says the stamp exists "so `stop` and the next `session_start` know a compaction occurred", but **this plan adds no reader for it**. A stamp nothing reads is write-only machinery, and this repo has deleted exactly that before (the backlink-reinforcement pass, removed for being "write-only machinery never queried by recall"). It is included here on one narrower justification only: it lands in session scratch, which the delta always sends in full, so it is *visible to the agent* in any restore — that is a reader. Do not add a `stop`/`session_start` branch on it speculatively. If no consumer materialises, the honest follow-up is to delete the stamp, not to keep it.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd client && python -m pytest tests/hooks/test_precompact.py -q`
Expected: PASS (7 tests). The resolver calls above are verified against `hooks/session_start.py:73-75` and `never_raise`'s real one-argument signature (`hooks/__init__.py:12`) — use them as written.

- [ ] **Step 6: Run the whole client suite**

Run: `cd client && python -m pytest tests/ -q`
Expected: no regressions (813 passed baseline after Phase A).

- [ ] **Step 7: Commit**

```bash
git add client/firekeep_client/hooks/precompact.py client/firekeep_client/state.py client/tests/hooks/test_precompact.py
git commit -m "feat(hooks): a compaction is a checkpoint, not a surprise"
```

### Task 2: Wire the core into the dispatcher

**Files:**
- Modify: `client/firekeep_client/hooks/__main__.py`
- Test: `client/tests/hooks/test_dispatcher.py` (add to the existing file; if absent, create it)

**Interfaces:**
- Consumes: `precompact.run` from Task 1.
- Produces: dispatcher accepts `python -m firekeep_client.hooks precompact`.

- [ ] **Step 1: Write the failing tests**

**Read `client/tests/hooks/test_dispatcher.py` before writing anything.** Two corrections to how this must be tested — both were wrong in an earlier draft of this plan:

1. **`main(argv: list[str] | None = None) -> int` RETURNS an int.** It does not raise `SystemExit` — `sys.exit(main())` sits at module scope under `if __name__ == "__main__"`. A test wrapping `main()` in `pytest.raises(SystemExit)` asserts a thing that cannot happen.
2. **The load-bearing test here is a SUBPROCESS test.** That file's own docstring says why: this module exists because rendered hook commands were silently dead, and "an in-process call to `main()` cannot prove the rendered command line is alive end-to-end." A core missing from `_CORE_MODULES` fails *silently at exit 0* — exactly the bug class a subprocess test catches and an in-process one does not.

Mirror the existing `test_session_start_degrades_gracefully_and_prints_systemmessage` and its `_write_subprocess_config(tmp_path)` helper (which points at a reserved port, so the core degrades against a real connection-refused rather than a mock). Reuse those helpers — do not write new ones.

```python
    def test_precompact_command_line_is_alive_and_prints_its_systemmessage(self, tmp_path):
        """The bug this file exists for: a core absent from _CORE_MODULES exits 0
        silently and the rendered hook is dead. Only the real command line proves
        otherwise. Mirror _write_subprocess_config + the subprocess invocation the
        session_start test above uses; assert exit 0, and that stdout parses as JSON
        whose systemMessage mentions ctx_get_shadow."""



def test_precompact_is_registered_and_treated_as_a_dict_core():
    """`_DICT_CORES` is INERT — verified: the dispatcher only ever consults
    `_INT_CORES` (lines 195, 215). What actually makes a dict core is membership
    in `_CORE_MODULES` plus absence from `_INT_CORES`. A core missing from
    `_CORE_MODULES` fails SILENTLY at exit 0, which is why this asserts the real
    mechanism and not the decorative set."""
    from firekeep_client.hooks import __main__ as dispatcher
    assert "precompact" in dispatcher._CORE_MODULES      # load-bearing
    assert "precompact" not in dispatcher._INT_CORES     # load-bearing
    assert "precompact" not in dispatcher._BYPASS_EXEMPT
```

**On `_BYPASS_EXEMPT`:** `precompact` stays out of it, matching `session_start`/`prompt`. The consequence is that in personal mode the dispatcher short-circuits the core and prints the PERSONAL MODE banner at the compaction boundary instead of checking in. That is the correct trade — personal mode must reach nothing, and a loud banner is the documented behaviour everywhere else. The core's own bypass gate (Task 1, step 1) is belt-and-braces for direct callers.

- [ ] **Step 2: Run to verify failure**

Run: `cd client && python -m pytest tests/hooks/test_dispatcher.py -q -k precompact`
Expected: FAIL — `KeyError`/`AssertionError`: `'precompact'` not in `_CORE_MODULES`.

- [ ] **Step 3: Register the core**

```python
# client/firekeep_client/hooks/__main__.py
from firekeep_client.hooks import (
    post_tool,
    pre_tool,
    precompact,          # <- add
    prompt,
    session_end,
    session_start,
    stop,
)

_CORE_MODULES = {
    "session_start": session_start,
    "stop": stop,
    "session_end": session_end,
    "prompt": prompt,
    "precompact": precompact,        # <- add
    "pre_tool": pre_tool,
    "post_tool": post_tool,
}
_DICT_CORES = frozenset({"session_start", "stop", "session_end", "prompt", "precompact"})
```

Also update the module docstring's `Contract:` list — it enumerates the core names and would otherwise lie.

- [ ] **Step 4: Run to verify pass**

Run: `cd client && python -m pytest tests/hooks/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add client/firekeep_client/hooks/__main__.py client/tests/hooks/test_dispatcher.py
git commit -m "feat(hooks): dispatch the precompact core"
```

### Task 3: Render the PreCompact hook, and stop the matrix lying

**Files:**
- Modify: `client/firekeep_client/adapters/claude.py`, `client/firekeep_client/adapters/base.py` (prose only), `client/firekeep_client/contract/matrix.py:41`
- Test: `client/tests/adapters/test_claude.py:231-243` (amend), `client/tests/contract/test_matrix.py::test_precompact_is_wired_nowhere` (amend)

**Interfaces:**
- Consumes: `hook_command(venv_bin, "precompact")` from `adapters/base.py`.
- Produces: a firekeep-owned `PreCompact` hook group in `~/.claude/settings.json`, coexisting with the legacy echo hook.

- [ ] **Step 1: Amend the two tests that encode the old truth**

The existing claude test's intent — *never clobber a foreign hook* — is preserved. Only its arithmetic changes.

```python
# client/tests/adapters/test_claude.py — replace test_claude_render_leaves_precompact_hook_alone
def test_claude_render_adds_its_precompact_group_beside_the_legacy_echo(fake_home, tmp_path):
    """The kit now renders a PreCompact hook of its own. The legacy echo hook is
    still deliberately treated as foreign-but-working: migration removes what is
    BROKEN, not everything the old installer happened to write. Both must survive.
    """
    (fake_home / ".claude").mkdir()
    (fake_home / ".claude" / "settings.json").write_text(json.dumps(_legacy_settings()))

    get_adapter("claude").render(venv_bin=tmp_path / "venv" / "bin")
    groups = _read(fake_home / ".claude" / "settings.json")["hooks"]["PreCompact"]

    commands = [h["command"] for g in groups for h in g["hooks"]]
    assert any("systemMessage" in c for c in commands), "legacy echo hook was clobbered"
    assert any(c.endswith("-m firekeep_client.hooks precompact") for c in commands)


def test_claude_unrender_removes_only_our_precompact_group(fake_home, tmp_path):
    (fake_home / ".claude").mkdir()
    (fake_home / ".claude" / "settings.json").write_text(json.dumps(_legacy_settings()))
    adapter = get_adapter("claude")
    adapter.render(venv_bin=tmp_path / "venv" / "bin")
    adapter.unrender()

    groups = _read(fake_home / ".claude" / "settings.json")["hooks"].get("PreCompact", [])
    commands = [h["command"] for g in groups for h in g["hooks"]]
    assert any("systemMessage" in c for c in commands)      # legacy survives unrender
    assert not any("firekeep_client.hooks" in c for c in commands)
```

```python
# client/tests/contract/test_matrix.py — replace test_precompact_is_wired_nowhere
def test_precompact_is_claimed_only_where_the_kit_renders_it():
    """Corrected twice, for the same reason each time: the matrix must never
    overstate coverage, because `firekeep doctor` and the docs read from it.
    2026-07-29 it claimed claude="yes" while nothing rendered a PreCompact hook.
    It is now "hook" for claude because the claude adapter renders one and a
    precompact core exists — and still "none" everywhere else, because no other
    runtime exposes a compaction event at all."""
    assert capabilities("claude")["precompact"] == "hook"
    for runtime in ("kiro", "codex", "opencode"):
        assert capabilities(runtime)["precompact"] == "none", (
            f"{runtime} claims a precompact capability; that runtime exposes no "
            f"compaction event, so the claim would be false"
        )
```

- [ ] **Step 2: Run to verify failure**

Run: `cd client && python -m pytest tests/adapters/test_claude.py tests/contract/test_matrix.py -q`
Expected: FAIL — no rendered precompact command; matrix still `"none"` for claude.

- [ ] **Step 3: Render the hook group**

`CLAUDE_HOOKS` (`claude.py:44-51`) is the whole hook table — 4-tuples of `(Claude event, hook core, matcher | None, timeout_seconds)`. Add one row. No matcher (PreCompact takes none) and no `--block-exit` (a dict core always exits 0). Timeout `15` matches `session_start`, per the design's stated ~15s ceiling:

```python
CLAUDE_HOOKS = (
    ("SessionStart", "session_start", None, 15),
    ("Stop", "stop", None, 5),
    ("SessionEnd", "session_end", None, 5),
    ("UserPromptSubmit", "prompt", None, 8),
    ("PreCompact", "precompact", None, 15),          # <- add
    ("PreToolUse", "pre_tool", "^(Edit|Write)$", 5),
    ("PostToolUse", "post_tool", "^(Edit|Write|MultiEdit|Bash)$", 10),
)
```

`upsert_hook_group()` collapses *all* firekeep groups for an event into the one rendered group, which is what makes a both-layers-present machine converge. The legacy echo hook carries no `HOOK_MARKER`, so it is foreign to that collapse and is appended-beside rather than replaced — which is exactly why the existing test's count goes from 1 to 2. **Do not add the echo's command to `LEGACY_HOOK_MARKERS`**: that would make `prune_hook_groups` delete a working user behaviour at unrender, which `base.py:34-39` forbids.

- [ ] **Step 4: Update the matrix row**

```python
    # Only Claude exposes a compaction event; the other three runtimes have no
    # such lifecycle hook to wire, so this degrades honestly rather than silently.
    "precompact": {"claude": "hook", "kiro": "none", "codex": "none", "opencode": "none"},
```

- [ ] **Step 5: Amend the `base.py` rationale prose**

Change **only** the prose sentence whose premise is now false. Do not touch any string inside `LEGACY_HOOK_MARKERS`.

```python
# Deliberately NOT listed: the legacy PreCompact `echo` hook. It still works, and
# the kit's own PreCompact group (rendered since the precompact core landed) is a
# SEPARATE, marker-identified group that coexists with it — so silently deleting a
# working behavior is still worse than leaving one tidy artifact behind.
```

- [ ] **Step 6: Run to verify pass, then the full suite**

Run: `cd client && python -m pytest tests/ -q`
Expected: PASS, no regressions. Confirm `tests/adapters/test_write_stability.py` still passes — the new rendered group must not break byte-stability.

- [ ] **Step 7: Commit**

```bash
git add client/firekeep_client/adapters/ client/firekeep_client/contract/matrix.py client/tests/
git commit -m "feat(claude): render the PreCompact hook, and stop the matrix understating it"
```

### Task 4: Document Phase B

**Files:**
- Modify: `CLAUDE.md` (Session Hooks section), `docs/MULTI-AGENT.md` if it enumerates hook cores

- [ ] **Step 1: Add the core to the hook list in `CLAUDE.md`**

Add to the five-hook list under "Session Hooks (client kit)":

```markdown
- `precompact` (PreCompact — **Claude only**; no other runtime exposes a compaction event) — checkpoints the workspace snapshot to Bridge scratch, invalidates the shadow cursor locally and server-side (via an ordinary `ctx_update(category="scratch", key="shadow_epoch")` — no new MCP tool), stamps `compacted_at`, and emits one line pointing the agent at `ctx_get_shadow()`. Cannot recover decisions the agent never wrote via `ctx_update` — it fires before compaction but cannot read unstated reasoning. `transcript_path` is present in the payload and deliberately NOT read: shipping a customer's raw conversation to the server is a privacy decision, not an engineering one.
```

- [ ] **Step 2: Verify the forbidden-token gate**

Run: `cd .. && python -m pytest tests/test_forbidden_tokens.py -q`
Expected: PASS. **Note:** this gate is currently red from an unrelated uncommitted `CLAUDE.md` line (symdex auto-index work) that names the predecessor product. Do not confuse it with your own change; if it fires on a line you did not write, leave it and report it.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md docs/
git commit -m "docs: the kit has a compaction signal now"
```

---

# Phase C — the residency contract

Independently shippable, and does **not** depend on Phase B: the fail-safe defaults hold on every runtime; Phase B only adds a server-side belt on Claude.

The insight: the only party that can observe whether earlier content is still in context is the agent looking at its own context. So do not detect compaction — make residency the agent's affirmation, with the safe answer as the default.

### Task 4.5: MEASURE FIRST — the go/no-go gate on this whole phase

**Do not skip this. It can cancel Phase C, and that is a legitimate outcome.**

A lossless delta must re-send `scratch` in full (no per-entry timestamp) and `proactive_memories` in full (replaced wholesale, not appended). Scratch **values are uncapped** — only the 50-entry count is capped — and the client writes an entire `workspace_snapshot` blob into a single scratch key every fifth prompt. So the delta omits only decisions, files and progress *lines*, which is plausibly the smaller half of the document. Nothing in this repo measures that, and every token figure in the design is `chars/4`, which under-counts JSON by 10–25%.

**Files:** Create `bridge/scripts/measure_shadow_delta.py` (a throwaway measurement script, not shipped code).

- [ ] **Step 1: Capture a real shadow**

Take a genuine long-running session from a live Bridge (or the largest session in the dev instance) and dump `get_session_data(sid)` to JSON.

- [ ] **Step 2: Measure both documents with a real tokenizer**

Count tokens (not `chars/4`) for: (a) `assemble_shadow(data)` — the full document, and (b) `assemble_shadow(filter_since(data, cursor_at_75_percent)[0])` — a delta taken after three quarters of the session, with scratch and proactive re-sent as the design requires.

- [ ] **Step 3: Decide, and record the number**

- Delta saves **≥30%** of the full document → proceed to Task 5.
- Delta saves **<30%** → **stop Phase C.** Ship Phase B and Phase D only, and record the measured figure in `docs/HISTORY-NOTES.md` as the reason. A cursor with this blast radius is not worth a single-digit saving, and the honest deliverable in that case is the precompact checkpoint plus the free wins.

Either way, write the measured number into `docs/HISTORY-NOTES.md`. Do not quote the design's savings figures to anyone until this step has produced a real one.

### Task 5: The cursor codec and `filter_since` (pure functions)

**Files:**
- Create: `bridge/app/residency.py`, `bridge/tests/test_residency.py`

**Interfaces:**
- Produces:
  - `encode_cursor(session_id: str, epoch: str, high_water: str, plan_sha: str) -> str`
  - `decode_cursor(cursor: str) -> dict | None` — `None` on anything malformed
  - `filter_since(data: dict, cursor: str | None, *, session_id: str, epoch: str) -> tuple[dict, dict | None]` — returns `(filtered_data, omission_report)`. `omission_report is None` means "no filtering happened; this is a full restore."
  - `high_water_of(data: dict) -> str`
  - `plan_sha_of(data: dict) -> str`

- [ ] **Step 1: Write the failing tests**

```python
# bridge/tests/test_residency.py
"""The residency contract's fail-safe matrix.

These are the tests that make the losslessness claim real. Every one of them
asserts the SAME thing from a different angle: when anything at all is doubtful,
the caller gets a FULL restore. Written first, deliberately.
"""
from __future__ import annotations

from app.residency import (
    decode_cursor, encode_cursor, filter_since, high_water_of, plan_sha_of,
)

SID = "sess-1"
EPOCH = "1000"


def _data(**over):
    d = {
        "goal": "g", "status": "active", "plan": "- [ ] step one",
        "decisions": [
            {"timestamp": "2026-07-30T10:00:00.000001+00:00", "content": "chose A"},
            {"timestamp": "2026-07-30T12:00:00.000001+00:00", "content": "chose B"},
        ],
        "progress": [
            {"timestamp": "2026-07-30T11:00:00.000001+00:00", "content": "did X"},
        ],
        "files": {
            "a.py": {"summary": "old", "last_action": "2026-07-30T09:00:00.000001+00:00"},
            "b.py": {"summary": "new", "last_action": "2026-07-30T13:00:00.000001+00:00"},
        },
        "scratch": {"workspace_snapshot": "branch=main"},
    }
    d.update(over)
    return d


# --- codec -----------------------------------------------------------------

def test_cursor_round_trips():
    c = encode_cursor(SID, EPOCH, "2026-07-30T11:00:00.000001+00:00", "abc123")
    got = decode_cursor(c)
    assert got["sid"] == SID and got["epoch"] == EPOCH
    assert got["hw"] == "2026-07-30T11:00:00.000001+00:00"
    assert got["plan_sha"] == "abc123"


def test_garbage_cursor_decodes_to_none():
    for junk in ("", "not-base64!!", "eyJ9", "null", "[]"):
        assert decode_cursor(junk) is None


# --- the five fail-safes: every one must yield a FULL restore --------------

def test_no_cursor_is_a_full_restore():
    out, omitted = filter_since(_data(), None, session_id=SID, epoch=EPOCH)
    assert out == _data()
    assert omitted is None


def test_unknown_cursor_is_a_full_restore():
    out, omitted = filter_since(_data(), "garbage", session_id=SID, epoch=EPOCH)
    assert out == _data()
    assert omitted is None


def test_cursor_from_a_different_session_is_a_full_restore():
    c = encode_cursor("other-session", EPOCH, "2026-07-30T12:00:00.000001+00:00", "x")
    out, omitted = filter_since(_data(), c, session_id=SID, epoch=EPOCH)
    assert out == _data()
    assert omitted is None


def test_cursor_with_a_stale_epoch_is_a_full_restore():
    """precompact bumped the epoch: the agent's context was compacted, so any
    cursor it still holds is a lie about what it can see."""
    c = encode_cursor(SID, "999", "2026-07-30T12:00:00.000001+00:00", "x")
    out, omitted = filter_since(_data(), c, session_id=SID, epoch="1000")
    assert out == _data()
    assert omitted is None


def test_cursor_with_no_high_water_is_a_full_restore():
    c = encode_cursor(SID, EPOCH, "", "x")
    out, omitted = filter_since(_data(), c, session_id=SID, epoch=EPOCH)
    assert out == _data()
    assert omitted is None


# --- the delta itself ------------------------------------------------------

def test_delta_keeps_entries_at_or_after_the_high_water_mark():
    """INCLUSIVE comparison: re-sending the boundary entry costs a few tokens,
    dropping it costs correctness. Duplication beats omission."""
    hw = "2026-07-30T11:00:00.000001+00:00"
    c = encode_cursor(SID, EPOCH, hw, plan_sha_of(_data()))
    out, omitted = filter_since(_data(), c, session_id=SID, epoch=EPOCH)
    assert [d["content"] for d in out["decisions"]] == ["chose B"]
    assert [p["content"] for p in out["progress"]] == ["did X"]     # == hw, kept
    assert list(out["files"]) == ["b.py"]
    assert omitted["decisions"] == 1
    assert omitted["files"] == 1


def test_delta_always_sends_scratch_in_full():
    """scratch entries carry NO timestamp (bridge/app/session.py: hset(key,
    content)), so they cannot be time-filtered. Sending them all is the only
    lossless option — and precompact's workspace snapshot lives here, which is
    exactly what a post-compaction agent needs most."""
    c = encode_cursor(SID, EPOCH, "2026-07-30T23:00:00.000001+00:00", plan_sha_of(_data()))
    out, _ = filter_since(_data(), c, session_id=SID, epoch=EPOCH)
    assert out["scratch"] == {"workspace_snapshot": "branch=main"}


def test_delta_omits_the_plan_only_when_its_hash_matches():
    d = _data()
    c = encode_cursor(SID, EPOCH, "2026-07-30T23:00:00.000001+00:00", plan_sha_of(d))
    out, omitted = filter_since(d, c, session_id=SID, epoch=EPOCH)
    assert out["plan"] == ""
    assert omitted["plan"] is True

    changed = _data(plan="- [x] step one")
    out2, omitted2 = filter_since(changed, c, session_id=SID, epoch=EPOCH)
    assert out2["plan"] == "- [x] step one"
    assert omitted2["plan"] is False


def test_delta_always_sends_proactive_memories_in_full():
    """`set_proactive_memories` REPLACES the whole `nb:session:{sid}:proactive`
    JSON blob on each proactive-recall trigger — it is not append-only and has no
    per-entry timestamp, so treating its absence as 'unchanged' would hide a full
    replacement. filter_since must not touch it. Note the `### Relevant Past
    Experience` section is also CONDITIONAL (emitted only when non-empty), so
    nothing may assume the shadow has a fixed section count."""
    d = _data(proactive_memories=[{"score": 0.9, "content": "seen this before"}])
    c = encode_cursor(SID, EPOCH, "2026-07-30T23:00:00.000001+00:00", plan_sha_of(d))
    out, _ = filter_since(d, c, session_id=SID, epoch=EPOCH)
    assert out["proactive_memories"] == d["proactive_memories"]


def test_delta_preserves_the_header_fields():
    """goal/status/created_at drive the shadow header. Omitting them would make a
    delta unreadable as a document."""
    c = encode_cursor(SID, EPOCH, "2026-07-30T23:00:00.000001+00:00", "x")
    out, _ = filter_since(_data(), c, session_id=SID, epoch=EPOCH)
    assert out["goal"] == "g" and out["status"] == "active"


def test_high_water_of_is_the_newest_timestamp_across_every_section():
    assert high_water_of(_data()) == "2026-07-30T13:00:00.000001+00:00"   # b.py


def test_high_water_of_empty_session_is_empty_not_a_crash():
    assert high_water_of({"decisions": [], "progress": [], "files": {}}) == ""


def test_delta_union_equals_a_full_restore():
    """No entry may be reachable only via one path. A full restore, then a delta
    taken at that point, must together cover every entry the session holds."""
    d = _data()
    full, _ = filter_since(d, None, session_id=SID, epoch=EPOCH)
    c = encode_cursor(SID, EPOCH, high_water_of(d), plan_sha_of(d))
    later = _data()
    later["decisions"] = d["decisions"] + [
        {"timestamp": "2026-07-30T14:00:00.000001+00:00", "content": "chose C"}]
    delta, _ = filter_since(later, c, session_id=SID, epoch=EPOCH)

    seen = {x["content"] for x in full["decisions"]} | {x["content"] for x in delta["decisions"]}
    assert seen == {"chose A", "chose B", "chose C"}
```

- [ ] **Step 2: Run to verify failure**

Run: `cd bridge && python -m pytest tests/test_residency.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.residency'`.

- [ ] **Step 3: Write `bridge/app/residency.py`**

```python
"""The residency contract — pure functions, no I/O.

`ctx_get_shadow()` with no argument is a FULL restore, byte-identical to what it
has always returned. That is the default and it is always correct. A caller may
opt into a delta by passing back the opaque cursor from an earlier full response,
which asserts one thing only: *the earlier shadow is still visible in my context*.

Every doubtful path here returns the full document. The design has no path that
omits content the agent lost; where it can be wrong, it is wrong in the direction
of sending too much.

Why timestamps and not counts: decisions/progress are stored LPUSH + LTRIM
(bridge/app/session.py), so the OLDEST entries are evicted. "I have seen the
first 47" stops meaning anything once that window shifts, and would silently
SKIP entries. Timestamps are stable under eviction because eviction only ever
removes entries the agent already received.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
from typing import Any

_CURSOR_VERSION = 1
# Sections whose entries carry a per-entry timestamp and can therefore be filtered.
_LIST_SECTIONS = ("decisions", "progress")


def plan_sha_of(data: dict[str, Any]) -> str:
    return hashlib.sha256((data.get("plan") or "").encode("utf-8")).hexdigest()[:16]


def high_water_of(data: dict[str, Any]) -> str:
    """The newest timestamp anywhere in the session, as an ISO string. Empty when
    the session has no timestamped entry yet — which decodes to a full restore."""
    stamps: list[str] = []
    for section in _LIST_SECTIONS:
        for entry in data.get(section) or []:
            ts = entry.get("timestamp") or ""
            if ts:
                stamps.append(ts)
    for entry in (data.get("files") or {}).values():
        ts = (entry or {}).get("last_action") or ""
        if ts:
            stamps.append(ts)
    return max(stamps) if stamps else ""


def encode_cursor(session_id: str, epoch: str, high_water: str, plan_sha: str) -> str:
    raw = json.dumps({"v": _CURSOR_VERSION, "sid": session_id, "epoch": str(epoch),
                      "hw": high_water, "plan_sha": plan_sha},
                     separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> dict[str, Any] | None:
    """Parse a cursor, or None if it is anything other than one of ours. None is
    the safe answer: the caller turns it into a full restore."""
    if not cursor:
        return None
    try:
        pad = "=" * (-len(cursor) % 4)
        obj = json.loads(base64.urlsafe_b64decode(cursor + pad).decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict) or obj.get("v") != _CURSOR_VERSION:
        return None
    if not isinstance(obj.get("sid"), str) or not isinstance(obj.get("hw"), str):
        return None
    return obj


def filter_since(
    data: dict[str, Any],
    cursor: str | None,
    *,
    session_id: str,
    epoch: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Return (data_to_render, omission_report).

    `omission_report is None` means no filtering happened — the caller must treat
    the result as a full restore and mint a fresh cursor. Any of these yields a
    full restore: no cursor, unparsable cursor, cursor minted for a different
    session, cursor carrying a stale epoch (precompact bumped it), or a cursor
    with no high-water mark.
    """
    parsed = decode_cursor(cursor) if cursor else None
    if parsed is None:
        return data, None
    if parsed.get("sid") != session_id:
        return data, None
    if str(parsed.get("epoch") or "") != str(epoch or ""):
        return data, None
    hw = parsed.get("hw") or ""
    if not hw:
        return data, None

    out = dict(data)
    omitted: dict[str, Any] = {}

    # INCLUSIVE (>=): a boundary entry may be re-sent. Duplication beats omission.
    for section in _LIST_SECTIONS:
        entries = data.get(section) or []
        kept = [e for e in entries if (e.get("timestamp") or "") >= hw]
        out[section] = kept
        omitted[section] = len(entries) - len(kept)

    files = data.get("files") or {}
    kept_files = {k: v for k, v in files.items()
                  if ((v or {}).get("last_action") or "") >= hw}
    out["files"] = kept_files
    omitted["files"] = len(files) - len(kept_files)

    # scratch has NO per-entry timestamp — always sent in full, never counted as
    # omitted, because nothing was omitted.
    out["scratch"] = data.get("scratch") or {}

    plan_unchanged = plan_sha_of(data) == parsed.get("plan_sha")
    out["plan"] = "" if plan_unchanged else (data.get("plan") or "")
    omitted["plan"] = plan_unchanged

    return out, omitted


def omission_notice(omitted: dict[str, Any]) -> str:
    """The sentence that makes a delta safe to read.

    An agent reading a delta must never be able to conclude the omitted content
    DOES NOT EXIST — that inference is the degradation, not the omission. So the
    delta names what it withheld and how to get it.
    """
    parts: list[str] = []
    for label, key in (("decisions", "decisions"), ("progress entries", "progress"),
                       ("files", "files")):
        n = omitted.get(key) or 0
        if n:
            parts.append(f"{n} {label}")
    if omitted.get("plan"):
        parts.append("your unchanged plan")
    if not parts:
        return ""
    return (
        "DELTA RESTORE — omitted " + ", ".join(parts) + " that were delivered to you "
        "earlier in this conversation. They still exist. If they are no longer "
        "visible to you, call ctx_get_shadow() with no arguments for the full document."
    )
```

- [ ] **Step 4: Run to verify pass**

Run: `cd bridge && python -m pytest tests/test_residency.py -q`
Expected: PASS (15 tests).

- [ ] **Step 5: Commit**

```bash
git add bridge/app/residency.py bridge/tests/test_residency.py
git commit -m "feat(bridge): a cursor that fails safe in five directions"
```

### Task 6: `shadow_epoch` on the session

**Files:**
- Modify: `bridge/app/session.py`
- Test: `bridge/tests/test_shadow_delta.py` (create)

**Interfaces:**
- Produces: `SessionManager.get_shadow_epoch(session_id: str) -> str | None` — reads the `shadow_epoch` scratch field. `""` = never bumped (a real, MATCHABLE state carried by every cursor minted before the first compaction); `None` = the read FAILED and is unmatchable by construction. Collapsing the two is the C2 fail-open.

The epoch is written by `precompact` through the ordinary `ctx_update(category="scratch", key="shadow_epoch")` path, so **no write method is needed** — only a reader.

- [ ] **Step 1: Write the failing test**

**Read this before writing the test.** `bridge/tests/` uses **no fakeredis** (despite `fakeredis>=2.21,<3` sitting unused in `bridge/requirements-dev.txt`). The single shared fixture is `mock_redis` in `bridge/tests/conftest.py` — a hand-stubbed `AsyncMock` with ~25 methods. It **does not round-trip writes**: `r.hget = AsyncMock(return_value=None)`, so a value written via `update_context` is *not* readable back. Stub the return explicitly, as the existing suite does per-test. Do **not** swap in a fakeredis-backed fixture: it would round-trip writes and silently change the meaning of the ~19 existing `hset.call_args_list[0].kwargs['mapping']` assertions.

Also: bridge has **no pytest config**, so pytest-asyncio runs in strict mode — an async test without `@pytest.mark.asyncio` is silently **skipped**, not failed.

```python
# bridge/tests/test_shadow_delta.py
import pytest
from unittest.mock import AsyncMock

from app.config import Settings
from app.session import SessionManager


class TestShadowEpoch:
    @pytest.mark.asyncio
    async def test_shadow_epoch_is_empty_when_never_bumped(self, mock_redis):
        mock_redis.hget = AsyncMock(return_value=None)
        mgr = SessionManager(mock_redis, Settings())
        assert await mgr.get_shadow_epoch("sess-1") == ""

    @pytest.mark.asyncio
    async def test_shadow_epoch_reads_the_scratch_field_precompact_wrote(self, mock_redis):
        """precompact bumps the epoch through the ordinary ctx_update scratch path —
        no new MCP tool, and no new Redis key."""
        mock_redis.hget = AsyncMock(return_value="1700000000000")
        mgr = SessionManager(mock_redis, Settings())
        assert await mgr.get_shadow_epoch("sess-1") == "1700000000000"
        mock_redis.hget.assert_awaited_once_with("nb:session:sess-1:scratch", "shadow_epoch")

    @pytest.mark.asyncio
    async def test_epoch_is_NONE_not_empty_when_the_read_fails(self, mock_redis):
        """AMENDED 2026-07-30 (C2, Critical). An earlier version of this task returned
        "" on a read error and claimed that "mismatches every cursor". That was FALSE:
        "" is a real, matchable state carried by every cursor minted before the first
        compaction, so an errored read matched a STALE post-compaction cursor and served
        a delta to an agent that had just lost its context — a guard that failed OPEN.
        None is unmatchable by construction, so a failure cannot pass for a state."""
        mock_redis.hget = AsyncMock(side_effect=RuntimeError("redis down"))
        mgr = SessionManager(mock_redis, Settings())
        assert await mgr.get_shadow_epoch("sess-1") is None
```

**Verified fixture facts — do not deviate.** `bridge/tests/conftest.py` defines exactly ONE fixture, `mock_redis`. There is **no** `session_mgr` / `session_manager` fixture; every existing test builds the manager inline as `SessionManager(mock_redis, Settings())` (see `bridge/tests/test_briefing_id_field.py:21`). `_scratch_key` is a `@staticmethod` returning `f"nb:session:{sid}:scratch"` (`session.py:120-122`), which is where the asserted key string comes from.

**Critically: do not add this read inside `get_session_data`.** `bridge/tests/test_sessions_route.py:98-121` encodes that function's exact Redis *call order* via positional `side_effect` lists (`hgetall` → [meta, files, scratch], `get` → [plan, proactive]). One extra read there shifts the lists and either raises `StopAsyncIteration` or — far worse — silently assigns the files hash to scratch while some assertions still pass. A separate method has none of that blast radius.

- [ ] **Step 2: Run to verify failure**

Run: `cd bridge && python -m pytest tests/test_shadow_delta.py -q`
Expected: FAIL — `AttributeError: 'SessionManager' object has no attribute 'get_shadow_epoch'`.

- [ ] **Step 3: Implement the reader**

```python
    async def get_shadow_epoch(self, session_id: str) -> str | None:
        """The session's shadow epoch. "" means never bumped; None means the read FAILED.

        The distinction is load-bearing (C2). "" is a real, matchable state — every
        cursor minted before the first compaction carries it. If a read failure also
        returned "", an errored read would silently match a stale cursor and serve a
        delta to an agent that had just lost its context. None is unmatchable.

        precompact writes this via ctx_update(category="scratch", key="shadow_epoch"),
        so there is no dedicated writer and no new MCP tool.
        """
        try:
            value = await self._r.hget(self._scratch_key(session_id), "shadow_epoch")
        except Exception:
            return None          # could not read -> unmatchable -> full restore
        return value or ""
```

- [ ] **Step 4: Run to verify pass**

Run: `cd bridge && python -m pytest tests/test_shadow_delta.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bridge/app/session.py bridge/tests/
git commit -m "feat(bridge): a session can say which epoch its shadow belongs to"
```

### Task 7: `ctx_get_shadow(since=...)`

**Files:**
- Modify: `bridge/app/mcp_server.py:335-365`
- Test: `bridge/tests/test_shadow_delta.py` (extend)

**Interfaces:**
- Consumes: `residency.filter_since`, `residency.encode_cursor`, `residency.high_water_of`, `residency.plan_sha_of`, `residency.omission_notice`, `SessionManager.get_shadow_epoch`.
- Produces: `ctx_get_shadow(session_id=None, agent_id="default", since=None) -> dict` with an added `shadow_cursor` key on every response **except when the epoch read failed** (`get_shadow_epoch` returned `None`), where it is deliberately omitted — a response carrying a cursor could seed a later delta on a session whose epoch was never readable. Plus `delta: bool`.

**`assemble_shadow` is not touched.** Its four consumers are unaffected because filtering happens on the `data` dict before it is handed over.

- [ ] **Step 1: Write the failing tests**

```python
# bridge/tests/test_shadow_delta.py — appended to the epoch tests above
#
# HARNESS: there is no `bridge_tools` fixture. Every bridge MCP-tool test patches
# app.mcp_server._get_manager with an AsyncMock manager and awaits the tool
# function directly — see bridge/tests/test_mcp_tools.py:44-51. Copy that shape.
from unittest.mock import AsyncMock, patch


def _session_data():
    return {"goal": "g", "status": "active", "plan": "- [ ] one",
            "decisions": [{"timestamp": "2026-07-30T10:00:00.000001+00:00",
                           "content": "chose A"}],
            "progress": [], "files": {}, "scratch": {}, "proactive_memories": []}


def _mgr(epoch=""):
    mgr = AsyncMock()
    mgr.get_active_session_id = AsyncMock(return_value="sess-1")
    mgr.get_session_data = AsyncMock(return_value=_session_data())
    mgr.get_shadow_epoch = AsyncMock(return_value=epoch)
    return mgr


class TestShadowDelta:
    @pytest.mark.asyncio
    async def test_full_restore_returns_a_cursor_and_is_not_a_delta(self):
        from app.mcp_server import ctx_get_shadow
        with patch("app.mcp_server._get_manager", return_value=_mgr()):
            out = await ctx_get_shadow(agent_id="a")
        assert out["delta"] is False
        assert out["shadow_cursor"]
        assert "### Decisions" in out["shadow"]

    @pytest.mark.asyncio
    async def test_a_fresh_cursor_yields_a_delta_that_names_what_it_withheld(self):
        from app.mcp_server import ctx_get_shadow
        with patch("app.mcp_server._get_manager", return_value=_mgr()):
            first = await ctx_get_shadow(agent_id="a")
            second = await ctx_get_shadow(agent_id="a", since=first["shadow_cursor"])
        assert second["delta"] is True
        assert "still exist" in second["note"]
        assert "ctx_get_shadow()" in second["note"]

    @pytest.mark.asyncio
    async def test_every_bad_cursor_yields_a_full_restore(self):
        """The tool-level half of the fail-safe matrix. The pure-function half lives
        in tests/test_residency.py; both must hold."""
        from app.mcp_server import ctx_get_shadow
        for bad in (None, "", "garbage", "eyJ2IjoxfQ"):
            with patch("app.mcp_server._get_manager", return_value=_mgr()):
                out = await ctx_get_shadow(agent_id="a", since=bad)
            assert out["delta"] is False, f"cursor {bad!r} produced a delta"

    @pytest.mark.asyncio
    async def test_a_cursor_is_refused_after_the_epoch_is_bumped(self):
        """precompact's server-side belt: the agent wrongly passes a stale cursor
        after a compaction, and Bridge answers with everything anyway."""
        from app.mcp_server import ctx_get_shadow
        with patch("app.mcp_server._get_manager", return_value=_mgr(epoch="1000")):
            first = await ctx_get_shadow(agent_id="a")
        with patch("app.mcp_server._get_manager", return_value=_mgr(epoch="9999")):
            out = await ctx_get_shadow(agent_id="a", since=first["shadow_cursor"])
        assert out["delta"] is False

    @pytest.mark.asyncio
    async def test_ctx_resume_session_never_returns_a_delta(self):
        """A resume is by definition a context the agent cannot vouch for. It takes
        no `since` and must always be full — the signature is also load-bearing:
        ctx_resume_session has no such parameter and FastMCP would reject the kwarg."""
        import inspect
        from app.mcp_server import ctx_resume_session
        assert "since" not in inspect.signature(ctx_resume_session).parameters
```

- [ ] **Step 2: Run to verify failure**

Run: `cd bridge && python -m pytest tests/test_shadow_delta.py -q`
Expected: FAIL — `ctx_get_shadow() got an unexpected keyword argument 'since'`.

- [ ] **Step 3: Rewrite the tool**

```python
@mcp.tool()
async def ctx_get_shadow(session_id: str | None = None, agent_id: str = "default",
                         since: str | None = None) -> dict:
    """Retrieve your full working context as a Markdown document.

    Call this after context compression or when starting a new conversation to restore
    your working state. Returns everything: your plan, decisions, file knowledge,
    progress, and scratchpad.

    Args:
        session_id: Specific session to retrieve (defaults to your active session).
        agent_id: Your agent identifier.
        since: OPTIONAL. The `shadow_cursor` from an earlier response in THIS
            conversation. Pass it ONLY if that earlier shadow is still visible in
            your context — it returns just what has changed since. If you are
            unsure, or your context was compacted, OMIT it and receive the full
            document. Omitting it is always correct.
    """
    agent_id = _default_agent_id(agent_id)
    mgr = await _get_manager()
    # NOTE: get_active_session_ID — verified name (session.py:332). Guard shape
    # copied from the current implementation, which uses `is None`, not falsiness.
    if session_id is None:
        session_id = await mgr.get_active_session_id(agent_id)
    if not session_id:
        return {"error": "No active session. Start one with ctx_start_session."}

    data = await mgr.get_session_data(session_id)
    if not data:
        return {"error": f"Session {session_id} not found."}

    # AMENDED 2026-07-30 (C1 + C2).
    epoch = await mgr.get_shadow_epoch(session_id)
    if epoch is None:
        # C2: the epoch read FAILED, so we cannot tell whether a cursor is stale.
        # Force a full restore AND mint no cursor — a response carrying no cursor
        # cannot produce a later delta, which is the safe outcome. Never coerce a
        # failed read to "", which would match every pre-compaction cursor.
        return {
            "session_id": session_id,
            "goal": data.get("goal", ""),
            "status": data.get("status", ""),
            "shadow": assemble_shadow(data),
            "delta": False,
        }

    rendered, omitted = residency.filter_since(
        data, since, session_id=session_id, epoch=epoch)
    # C1: the omission report goes INTO the rendered document, not just beside it.
    # Without this, an omitted section renders as '*No decisions recorded*' — an
    # affirmative denial that the agent's own work exists.
    shadow = assemble_shadow(rendered, omitted=omitted)

    result = {
        "session_id": session_id,
        "goal": data.get("goal", ""),
        "status": data.get("status", ""),
        "shadow": shadow,
        # Always minted from the FULL data, never the filtered copy: the cursor
        # describes what the caller now holds in total, not what this response carried.
        "shadow_cursor": residency.encode_cursor(
            session_id, epoch, residency.high_water_of(data), residency.plan_sha_of(data)),
        "delta": omitted is not None,
    }
    if omitted is not None:
        note = residency.omission_notice(omitted)
        if note:
            result["note"] = note   # belt and braces; the markdown now says it too
    return result
```

Add `from app import residency` to the imports beside `from app.shadow import assemble_shadow`.

Preserve the existing keys in that return dict exactly. `ctx_get_shadow` returns precisely four today — `session_id`, `goal`, `status`, `shadow` (`mcp_server.py:358-363`, verified) — and every one must survive. Adding keys is safe; renaming or dropping one is not.

**Also mint a cursor on `ctx_resume_session`.** It stays full-only — it takes no `since`, ever, because a resumed session is by definition one the agent cannot vouch for — but it should still return a `shadow_cursor` in its response dict, built the same way from the same full `data`. A resume delivers the *complete* document, so minting a cursor there is exactly as safe as minting one on a full `ctx_get_shadow`; omitting it merely forfeits the entire saving for every subsequent restore in a resumed session, which is the common case after a crash. Its return shape is `{session_id, goal, status, shadow}` with `status` hardcoded to `"active"` (`mcp_server.py:483-488`) — add `shadow_cursor` and leave the rest untouched. Do NOT add a `delta` key there: it is never a delta, and an always-false flag invites someone to start passing `since`.

- [ ] **Step 3b: Assert on the RENDERED DOCUMENT, not only the returned data**

This obligation exists because of how Task 5's worst defect survived review. Fifteen tests,
five redundant fail-safes, an adversarial critique and three reviews all passed while the
delta rendered `*No decisions recorded*` over withheld content — an affirmative denial that
the agent's own work existed. Every one of those tests asserted on the filtered **data**.
Nobody rendered the result and read it. The data was correct at every step; the document
built from it was false.

So `ctx_get_shadow`'s tests must assert on `out["shadow"]` — the markdown string the agent
actually reads — not merely on `out["delta"]` and the omission counts:

```python
    @pytest.mark.asyncio
    async def test_a_delta_document_never_denies_that_withheld_content_exists(self):
        """The C1 regression test, at the layer C1 actually lived in."""
        from app.mcp_server import ctx_get_shadow
        with patch("app.mcp_server._get_manager", return_value=_mgr()):
            first = await ctx_get_shadow(agent_id="a")
            second = await ctx_get_shadow(agent_id="a", since=first["shadow_cursor"])
        doc = second["shadow"]
        # CORRECTED 2026-07-30 (plan defect, caught before commit). An earlier
        # version asserted a BLANKET "none of the four denial strings may appear".
        # That is not what C1 says. C1 is "never deny content that WAS withheld" —
        # a section that genuinely never had content renders a placeholder that is
        # simply TRUE, and asserting its absence asserts the wrong thing. Worse, a
        # blanket assertion fails on CORRECT code, and the natural way to make it
        # pass is to suppress the placeholders entirely — turning a true statement
        # into silence, which is the same defect class C1 was about.
        #
        # Use a LOCAL fixture with past-dated entries in every section, so all four
        # genuinely withhold something. Do NOT extend the shared _session_data():
        # that would silently change what every other delta test exercises.
        for key, denial in (("decisions", "No decisions recorded"),
                            ("progress", "No progress logged"),
                            ("files", "No files tracked")):
            if report.get(key):
                assert denial not in doc, f"{key}: denied content it withheld"
                assert "omitted" in doc, f"{key}: withheld content silently"
        if report.get("plan"):
            assert "No plan set" not in doc
        assert "ctx_get_shadow()" in doc, "document does not say how to recover the full set"
    @pytest.mark.asyncio
    async def test_a_full_restore_document_is_byte_identical_to_the_pre_change_output(self):
        """The no-regression half: with no cursor, the document must be exactly what
        callers got before this task existed. assemble_shadow(data) with omitted=None
        is the reference."""
        from app.mcp_server import ctx_get_shadow
        from app.shadow import assemble_shadow
        with patch("app.mcp_server._get_manager", return_value=_mgr()):
            out = await ctx_get_shadow(agent_id="a")
        assert out["shadow"] == assemble_shadow(_session_data())
```

Also assert that when the epoch read fails (`get_shadow_epoch` returns `None`), the response
contains **no** `shadow_cursor` key at all — a response carrying a cursor could seed a later
delta on a session whose epoch was never readable.

- [ ] **Step 4: Run to verify pass**

Run: `cd bridge && python -m pytest tests/ -q`
Expected: PASS, 154 baseline + new tests, no regressions.

- [ ] **Step 5: Verify the four `assemble_shadow` consumers are untouched**

Run: `cd bridge && grep -rn "assemble_shadow" app/ && python -m pytest tests/ -q`
Expected: every call site still passes a single positional `data` dict. Confirm `GET /sessions/{session_id}` still returns `"shadow"` as a Markdown string — Task 12 depends on that shape being unchanged.

- [ ] **Step 6: Commit**

```bash
git add bridge/app/mcp_server.py bridge/tests/test_shadow_delta.py
git commit -m "feat(bridge): a delta the agent opts into, and a default that never loses anything"
```

### Task 8: Capture the cursor client-side

**Files:**
- Modify: `client/firekeep_client/shim.py` (`_BridgeSessionTap.on_response`)
- Test: `client/tests/test_shim_identity.py` (extend — the tap tests live under the `# --- bridge session tap` divider from line ~116, with `_tools_call` / `_tool_result` helpers already defined)

**Interfaces:**
- Consumes: `state.write_shadow_cursor` / `clear_shadow_cursor` from Task 1.
- Produces: the tap stores `shadow_cursor` from a `ctx_get_shadow` response.

**Deliberately NOT done here:** the tap does **not** inject `since` into an agent's `ctx_get_shadow` call. Residency is the agent's affirmation about its own context; a client-side process cannot observe what is in the model's context and must never assert it on the agent's behalf. The stash exists so a future *agent-visible* surface can offer the cursor, and so `precompact` has something to invalidate.

- [ ] **Step 1: Write the failing tests**

Append these to `client/tests/test_shim_identity.py`, reusing the `_tools_call` / `_tool_result` helpers already in that file:

```python
def test_tap_captures_the_shadow_cursor_from_a_get_shadow_response(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    tap = shim._BridgeSessionTap("tester", "personal")

    tap.on_request(_tools_call(3, "ctx_get_shadow", {}))
    tap.on_response(_tool_result(3, {"shadow": "## Session: g",
                                     "shadow_cursor": "cursor-abc", "delta": False}))

    assert state.read_shadow_cursor("tester", "personal") == "cursor-abc"


def test_tap_never_injects_since_into_an_agent_call(tmp_path, monkeypatch):
    """The client cannot observe the model's context, so it must never assert
    residency on the agent's behalf. Only the agent may pass `since`."""
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    state.write_shadow_cursor("tester", "personal", "cursor-abc")
    tap = shim._BridgeSessionTap("tester", "personal")

    out = tap.on_request(_tools_call(4, "ctx_get_shadow", {}))

    assert "since" not in out.message.root.params["arguments"]


def test_tap_clears_the_cursor_when_the_session_ends(tmp_path, monkeypatch):
    """Server-authoritative session end, same trigger that clears the session
    stash today. A cursor outliving its session could only ever be wrong."""
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    state.write_shadow_cursor("tester", "personal", "cursor-abc")
    tap = shim._BridgeSessionTap("tester", "personal")

    tap.on_request(_tools_call(5, "ctx_complete_session", {"outcome": "done"}))
    tap.on_response(_tool_result(5, {"status": "completed"}))

    assert state.read_shadow_cursor("tester", "personal") is None
```

- [ ] **Step 2: Run to verify failure**

Run: `cd client && python -m pytest tests/test_shim_identity.py -q -k "cursor or since"`
Expected: FAIL — `AttributeError: module 'firekeep_client.state' has no attribute 'read_shadow_cursor'` if Task 1 is not yet merged; otherwise the cursor is simply never captured.

- [ ] **Step 3: Implement in `on_response`**

**Verified wiring — the tap's real structure (`shim.py:163-226`).** It keeps three frozensets and a `_pending` request-id map:

```python
    _INJECT_TOOLS  = frozenset({"ctx_start_session"})
    _CAPTURE_TOOLS = frozenset({"ctx_start_session", "ctx_resume_session"})
    _END_TOOLS     = frozenset({"ctx_complete_session", "ctx_abandon_session"})
```

`on_request` records `self._pending[rid] = name` for capture/end tools and injects only for `_INJECT_TOOLS`. `on_response` pops the name, clears the stash for an END tool, else runs `_extract_session_id(result)` (`shim.py:142`, which reads `structuredContent` first then falls back to parsing `content[0].text`) and writes the stash.

Make four changes, and no others:

1. Add a FOURTH, dedicated set: `_CURSOR_TOOLS = frozenset({"ctx_get_shadow"})`. Do **not** add `ctx_get_shadow` to `_CAPTURE_TOOLS` — that set's response handler extracts `session_id`, which is a different field. A separate set also makes it structurally impossible for `ctx_get_shadow` to drift into `_INJECT_TOOLS`, which is the property that matters most here (see below).
2. In `on_request`, include `_CURSOR_TOOLS` in the condition that records `self._pending[rid] = name`. Add **nothing** to the injection branch.
3. In `on_response`, before the session_id path: if the popped name is in `_CURSOR_TOOLS`, pull `shadow_cursor` out of the result and call `state.write_shadow_cursor(self._agent, self._profile, cursor)`. Write a sibling helper `_extract_shadow_cursor(result)` mirroring `_extract_session_id` exactly — same `structuredContent`-then-`content[0].text` order, same never-raises/return-None-on-mismatch contract. `ctx_get_shadow` is a `-> dict` tool so `structuredContent` will normally be populated, but the text fallback must exist for the same reason it does for session_id.
4. In the `_END_TOOLS` branch of `on_response`, add `state.clear_shadow_cursor(self._agent, self._profile)` beside the existing `clear_session_stash`. A cursor outliving its session could only ever be wrong.

**Why the injection boundary is load-bearing, not stylistic.** The client cannot observe what is in the model's context. `since` is an assertion *about the model's context* — "the earlier shadow is still visible to me" — so only the agent may make it. If the shim ever injected `since` on the agent's behalf, a process with no visibility into the context would be vouching for it, and the entire losslessness argument collapses: Bridge would withhold content the agent never received. Keeping `_CURSOR_TOOLS` disjoint from `_INJECT_TOOLS` is what makes that a structural property rather than a convention someone could later 'optimize' away. The test asserting no `since` is injected is the guard.

Follow the existing pattern exactly: synchronous transform, never raises, forwards the frame byte-identical on any error, and no `await` between the pending-map check and set (GIL-safe). Add **nothing** to the request side.

- [ ] **Step 4: Run the full client suite, then commit**

```bash
cd client && python -m pytest tests/ -q
git add client/firekeep_client/shim.py client/tests/
git commit -m "feat(shim): stash the shadow cursor; never speak for the agent's context"
```

### Task 9: Teach the instruction layer the contract

**Files:**
- Modify: `client/firekeep_client/adapters/base.py` (`FIREKEEP_INSTRUCTIONS`)
- Test: `client/tests/adapters/test_instructions.py`

- [ ] **Step 1: Write the failing test**

```python
def test_instructions_state_that_omitting_since_is_always_correct():
    from firekeep_client.adapters.base import FIREKEEP_INSTRUCTIONS
    assert "ctx_get_shadow" in FIREKEEP_INSTRUCTIONS
    assert "still visible" in FIREKEEP_INSTRUCTIONS
    assert "omit" in FIREKEEP_INSTRUCTIONS.lower()
```

- [ ] **Step 2: Run to verify failure, then amend the memory-protocol block**

```
- Your own earlier plan or decisions missing from context (after compaction) →
  `ctx_get_shadow` before asking the user to repeat themselves. Pass
  `since=<shadow_cursor>` ONLY if the earlier shadow is still visible in your
  context; if you are unsure, omit it — omitting it is always correct.
```

- [ ] **Step 3: Confirm byte-stability still holds, then commit**

```bash
cd client && python -m pytest tests/adapters/ -q
git add client/firekeep_client/adapters/base.py client/tests/adapters/test_instructions.py
git commit -m "docs(client): the residency contract, in the one place agents read"
```

---

# Phase D — independent wins

No shared state with B or C. Any order.

### Task 10: `output_schema=None` on the four `-> str` tools

**Files:**
- Modify: `cortex/app/mcp_server.py` (`memory_recall`, `skill_recall`, `skill_list`), `vault/api.py` or wherever `vault_list` is registered
- Test: `cortex/tests/test_mcp_output_schema.py` (create)

**Correct the design's premise before you start.** The spec claims a `-> str` tool "delivers `{"result": "<JSON-escaped markdown>"}` and that wrapped copy is what the runtime renders, so every newline ships as `\n`." **That is wrong**, verified empirically against the pinned fastmcp: `content[0].text` is **byte-identical** with and without `output_schema`. What actually disappears is the *duplicated* `structuredContent` copy of the same markdown. The win is real — the payload stops carrying the document twice — but it is roughly half the size the spec implies, and no escaping is involved. Do not repeat the spec's framing in a commit message or a customer-facing note.

`fastmcp` is pinned at **3.4.4** in `cortex/requirements.lock:303` and `bridge/requirements.lock:257` (range `fastmcp>=3.1,<4`); a locally-installed 3.1.1 is not what ships. `output_schema=None` is accepted by 3.4.4.

- [ ] **Step 1: Confirm the API on the LOCKED version, not the installed one**

Run: `python -c "import fastmcp, inspect; print(fastmcp.__version__); print('output_schema' in inspect.signature(fastmcp.FastMCP.tool).parameters)"`
Expected: `output_schema` present. If your local version differs from 3.4.4, verify against the lock in a clean venv before trusting the result. **If it is not accepted, STOP** and record that here rather than reaching for a fastmcp upgrade — a dependency bump is out of scope.

- [ ] **Step 2: Write the failing test**

```python
"""The markdown a tool returns must survive unwrapping byte-for-byte."""
import pytest


@pytest.mark.asyncio
async def test_memory_recall_markdown_is_not_json_escaped(monkeypatch):
    from app import mcp_server
    tool = await mcp_server.mcp.get_tool("memory_recall")
    assert tool.output_schema is None, (
        "a -> str tool with an output schema ships its markdown JSON-escaped, so "
        "every newline bills as the two characters backslash-n"
    )
```

- [ ] **Step 3: Verify the shipped consumer still parses it**

`symdex/src/firekeep_symdex/tools/recall_with_code.py:122` runs `_extract_keywords(context_block)` over the whole recall block and `:153` builds cross-references from it. Read both, then run: `cd symdex && python -m pytest tests/ -q`.
If that consumer receives the tool result through an MCP client that unwraps `{"result": ...}` itself, the change is transparent; if it string-matches on the escaped form, **it must be fixed in the same commit**.

- [ ] **Step 4: Apply and run every suite**

Add `output_schema=None` to the four decorators. Run the cortex, symdex and shared suites.

- [ ] **Step 5: Record the live check as a PRE-MERGE GATE, not a completed step**

**Decided 2026-07-30:** cortex-mcp runs in Docker on the VPS, so the cross-runtime render check cannot be performed from this branch — it needs a deploy. The implementer's job ends at code plus test evidence. Append this to the ledger verbatim, and do **not** describe this task as verified:

```
Task 10: PRE-MERGE GATE OPEN — output_schema=None applied and unit-verified, but the
cross-runtime render check requires a deploy of cortex-mcp. Before merging: confirm on a
real session on Claude AND one other runtime that recall/skill output renders identically,
then record the result in docs/HISTORY-NOTES.md. Tests alone are not evidence for this item.
```

- [ ] **Step 5: Commit**

```bash
git commit -m "perf(mcp): stop shipping markdown twice, once escaped"
```

### Task 11: ~~Strip auto-generated Pydantic `title` keys~~ — CANCELLED 2026-07-30, measured

**Do not implement. This task was cancelled by its own measurement, under the same standard Task 4.5 applies.**

Measured empirically against the pinned stack: the decision server uses the **mcp SDK's** bundled FastMCP (`from mcp.server.fastmcp import FastMCP`, `decision/server.py:680`), not the standalone `fastmcp` 3.x package. A representative tool's generated schema is **411 bytes and carries 4 `title` keys** — the three per-property titles plus a useless root `"title": "decision_boardArguments"` — totalling roughly **70 bytes per tool**. With two tools on a local stdio server that is **~140 bytes, about 35 tokens, once per session.**

Against that: the `FastMCP` instance is constructed **inside `main()`** (deliberately, so importing the module does not spin up a server), so there is no module-level object to post-process and no `tool_schemas()` seam. Making it testable means restructuring how the decision server registers its tools.

Restructuring tool registration to save 35 tokens per session fails the standard this plan sets in Task 4.5: if the saving does not justify the mechanism, do not build it. Recorded here rather than deleted so the measurement is not re-derived by the next person reading the spec's free-win list.

<details>
<summary>Original task text, retained for the record</summary>

### Task 11 (cancelled): Strip auto-generated Pydantic `title` keys from the decision server's schemas

**Files:**
- Modify: `client/firekeep_client/decision/server.py`
- Test: `client/tests/decision/test_schema_titles.py` (create)

- [ ] **Step 1: Write the failing test**

```python
def test_no_auto_generated_title_keys_in_the_tool_schemas():
    """A `title` on every property is fastmcp-generated noise the model never
    reads. Stripped by a post-process dict walk — deliberately NOT by migrating
    the client kit to fastmcp v3, which would add a heavyweight dependency."""
    from firekeep_client.decision.server import tool_schemas

    def titles(node):
        found = []
        if isinstance(node, dict):
            if "title" in node and "properties" not in node:
                found.append(node["title"])
            for v in node.values():
                found += titles(v)
        elif isinstance(node, list):
            for v in node:
                found += titles(v)
        return found

    for schema in tool_schemas():
        assert titles(schema) == []
```

- [ ] **Step 2: Run to verify failure, implement the walk, run to verify pass**

Read how `decision/server.py` currently builds schemas before writing the walk — if they are hand-rolled JSON with no `title` keys at all, this task is already satisfied: **delete the task and record why**, rather than inventing a stripper for a problem that does not exist.

- [ ] **Step 3: Commit**

```bash
git commit -m "perf(decision): drop schema titles no model reads"
```

</details>

### Task 12: The dead resolution-language signal

**Files:**
- Modify: `cortex/app/skills/scorer.py:154-156`, `cortex/app/skills/synthesizer.py:288-294`
- Test: `cortex/tests/test_skill_scorer_shadow.py` (create)

`GET /sessions/{id}` returns `"shadow"` as a **Markdown string** (verified: `bridge/app/mcp_server.py:562`). Both call sites do `shadow.get(...)` on it, raising `AttributeError` into a bare `except Exception:`. `_resolution_language_score` therefore always returns `0.0`, so `SKILL_RESOLUTION_WEIGHT=0.35` — the largest weight in the scorer — is permanently dead.

- [ ] **Step 1: Write the failing test**

```python
"""GET /sessions/{id} returns `shadow` as a MARKDOWN STRING, not a dict."""
import pytest


@pytest.mark.asyncio
async def test_resolution_score_reads_a_markdown_shadow(monkeypatch):
    from app.skills import scorer
    markdown = (
        "## Session: fix the collector\n### Decisions\n"
        "- root caused the failure and the fix works now\n"
        "### Scratchpad\n- resolved\n"
    )
    _patch_bridge_get(monkeypatch, {"shadow": markdown})
    score = await scorer._resolution_language_score("sess-1")
    assert score > 0.0, (
        "shadow is a str; calling .get() on it raises AttributeError into a bare "
        "except and silently zeroes the 0.35-weighted resolution signal"
    )


@pytest.mark.asyncio
async def test_resolution_score_is_zero_for_a_shadow_with_no_resolution_language(monkeypatch):
    from app.skills import scorer
    _patch_bridge_get(monkeypatch, {"shadow": "## Session: x\n### Plan\n- do a thing\n"})
    assert await scorer._resolution_language_score("sess-1") == 0.0
```

Write `_patch_bridge_get` to monkeypatch whatever httpx client the scorer uses — read the function first; do not guess the transport.

- [ ] **Step 2: Run to verify failure**

Expected: FAIL, `assert 0.0 > 0.0`. Confirm the *reason* is the swallowed `AttributeError`, not a missing phrase — add a temporary `raise` inside the `except` to see it, then remove it.

- [ ] **Step 3: Fix both call sites**

```python
            payload = resp.json()
            shadow = payload.get("shadow")
            # GET /sessions/{id} returns `shadow` as the assembled MARKDOWN
            # STRING (bridge/app/mcp_server.py: assemble_shadow(data)). This used
            # to call .get("scratch") on it, raising AttributeError into the bare
            # except below and permanently zeroing this 0.35-weighted signal.
            # Dict is still accepted so a future shape change degrades instead of
            # silently regressing.
            if isinstance(shadow, dict):
                # If this ever becomes a dict, these are the REAL key names:
                # `decisions`/`progress` (plural — session.py:325) with entries
                # shaped {timestamp, content}, NOT `decision` with {value}. The
                # old code had both wrong on top of the type error, so "fix the
                # AttributeError" alone yields an empty list and empty strings —
                # signal still zero, every test still green.
                texts = [str(v) for v in (shadow.get("scratch") or {}).values()]
                for section in ("decisions", "progress"):
                    texts += [str(e.get("content", "")) for e in (shadow.get(section) or [])]
                full_text = " ".join(texts).lower()
            else:
                full_text = str(shadow or "").lower()
```

**Three defects, not one.** The container type (`str` vs `dict`), the section key names (`decision` → `decisions`), and the entry key (`value` → `content`). Both existing cortex unit tests mock `shadow` as a dict *with the wrong keys*, which is why CI never saw any of it. Build the new test from a real `assemble_shadow(get_session_data(...))`-shaped value, not from a hand-written mock that would re-encode the same mistakes.

**Second-order consequence to state in the commit body:** this signal is currently a hard `0`, so `SKILL_SCORE_THRESHOLD=0.6` is effectively unreachable without an explicit `skill_worthy=True`. Making it work will start triggering server-side skill synthesis on any deploy with `SKILL_SYNTHESIS_ENABLED=true` — which on a CPU-only deploy exceeds `SKILL_SYNTH_TIMEOUT_SECONDS=300` and times out, burning worker slots. The flag defaults to **false**, so default deploys are unaffected; say so explicitly rather than letting it read as an unplanned regression.

- [ ] **Step 4: Run to verify pass, plus the whole cortex suite**

Run: `cd cortex && python -m pytest tests/ -q`
Expected: 1235 baseline + new, no regressions. Note server-side skill synthesis is off by default (`SKILL_SYNTHESIS_ENABLED=false`), so this only changes behaviour on a deploy that enabled it — state that in the commit body.

- [ ] **Step 5: Commit**

```bash
git commit -m "fix(skills): the heaviest scoring signal has always been zero"
```

### Task 13: Archive the predecessor instruction block

**Files:**
- Modify: `client/firekeep_client/adapters/base.py` (add `LEGACY_INSTRUCTION_MARKERS`), `client/firekeep_client/adapters/claude.py`
- Test: `client/tests/adapters/test_predecessor_migration.py` (extend)

**Re-measured on the live machine 2026-07-30; the spec's figures and the marker strings were both wrong. Use these.**

There are **two** predecessor blocks in `~/.claude/CLAUDE.md`, and only one of them may be touched:

| Block | Markers | Size | Similarity to the firekeep block | Verdict |
|---|---|---|---|---|
| Decision Board + Knowledge Ingest | `<!-- nexus:instructions:begin …` / `<!-- nexus:instructions:end -->` | 3,214 chars, 53 lines | **0.75** | **archive** |
| Agent Personality + Change Consistency Checklist + tool notes | `<!-- NexusStack Agent Guidelines -->` / `<!-- /NexusStack Agent Guidelines -->` | 2,441 chars, 28 lines | **0.03** | **LEAVE ALONE** |

Two corrections that matter:

1. **The spec's 0.998 similarity figure is wrong — it is 0.75.** The firekeep block is 5,238 chars against the nexus block's 3,214, because firekeep's carries a memory-protocol section the predecessor's lacks. The nexus block is a near-duplicate of a *subset*, not of the whole. Still worth ~800 tokens of rent per prompt for content that is three-quarters redundant, so it still goes — but do not repeat "0.998" anywhere.
2. **The second block is not a duplicate at all** (0.03 similar) and contains content the user still has and may still want. Stripping it would be a straight deletion of the user's information, which the zero-degradation constraint forbids. `LEGACY_INSTRUCTION_MARKERS` must contain **only** the `nexus:instructions:begin`/`end` pair.

Note the live file also contains a literal `\n` (backslash-n as text, not a newline) immediately before `<!-- NexusStack Agent Guidelines -->` — a cosmetic bug in the predecessor's writer. Do not try to clean it up; it sits outside the block you are removing.

The mechanism must **archive to `.bak`** in the manner of `adapters/kiro.py::_migrate_legacy`, never delete content-blind from a user-owned prose file — and the archive is what preserves the nexus block's unique 25%.

- [ ] **Step 1: Write the failing tests**

```python
def test_predecessor_instruction_block_is_archived_not_deleted(fake_home, tmp_path):
    md = fake_home / ".claude" / "CLAUDE.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text("# My notes\nkeep me\n\n<!-- nexus:instructions -->\nold block\n"
                  "<!-- /nexus:instructions -->\n\n# more of my notes\n", encoding="utf-8")

    get_adapter("claude").render(venv_bin=tmp_path / "venv" / "bin")

    body = md.read_text(encoding="utf-8")
    assert "old block" not in body                    # predecessor block gone
    assert "keep me" in body and "# more of my notes" in body   # user prose intact
    backups = list(md.parent.glob("CLAUDE.md*.bak"))
    assert backups, "a user-owned prose file must never be edited without a .bak"
    assert "old block" in backups[0].read_text(encoding="utf-8")


def test_render_writes_no_backup_when_there_is_no_predecessor_block(fake_home, tmp_path):
    md = fake_home / ".claude" / "CLAUDE.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text("# Just my notes\n", encoding="utf-8")
    get_adapter("claude").render(venv_bin=tmp_path / "venv" / "bin")
    assert not list(md.parent.glob("CLAUDE.md*.bak"))
```

- [ ] **Step 2: Run to verify failure, then implement**

Add the tuple beside the three existing legacy tuples, respecting the `DO NOT RENAME` warning at `base.py:25-33` — these strings name what a *previous generation* wrote and must keep spelling the old thing forever:

```python
# Generation 2's instruction block, upserted into the user's global CLAUDE.md
# under the predecessor product's markers. Measured on a live machine 2026-07-30:
# 3,214 chars, 0.75-similar to FIREKEEP_INSTRUCTIONS (a near-duplicate of a
# SUBSET — firekeep's block carries a memory-protocol section this one lacks).
# The sibling `<!-- NexusStack Agent Guidelines -->` block is deliberately NOT
# listed: at 0.03 similarity it is not a duplicate, it is content the user still
# has, and removing it would be a plain deletion of their information.
# DO NOT RENAME (see the warning above): renaming these disarms the migration on
# every machine that actually has the block.
LEGACY_INSTRUCTION_MARKERS = (
    ("<!-- nexus:instructions:begin", "<!-- nexus:instructions:end -->"),
)
```

The begin marker is matched by **prefix**, not in full. The live line reads
``<!-- nexus:instructions:begin — nexus-owned block, do not edit; re-rendered by `nexus install` -->``
and pinning that whole sentence would break the migration on any generation that reworded the comment. This mirrors how `INSTRUCTIONS_BEGIN`/`INSTRUCTIONS_END` are already used in this file (`base.py:293-294`, matched with `.find()`).

These strings were read off the live machine on 2026-07-30, not guessed — **a guessed marker is a no-op migration that keeps every test green**, exactly the failure mode the `DO NOT RENAME` comment describes. If you change them, re-read the live file first.

- [ ] **Step 3: Verify against the forbidden-token gate**

Run: `cd .. && python -m pytest tests/test_forbidden_tokens.py -q`
The predecessor name will now appear in `base.py` by necessity. If the gate rejects it, add a scoped allowance for the legacy-marker tuples only — mirroring however `LEGACY_MCP_KEYS` is already tolerated — and never a blanket exemption.

- [ ] **Step 4: Run the full client suite, then commit**

```bash
git commit -m "fix(adapters): the predecessor's instruction block, archived not deleted"
```

---

### Task 14: `GET /sessions/{session_id}` has no scope gate

Found during recon; **independent of everything above** and worth doing regardless of whether Phase C ships.

`bridge/app/mcp_server.py:552` `_get_session` has **no `require_scope_asgi` call**, while both of its siblings do — `/sessions/{agent_id}/context` (`:588`) and `/ops/distill-dlq/requeue` (`:504`). It already returns the full shadow including the whole `scratch` hash, which carries the client's `workspace_snapshot` blob (branch, recent commits, diff stats), unredacted. The ASGI auth middleware does cover it when `AUTH_ENABLED=true` (the default since 2026-07-26), so this is a missing *per-route scope check*, not an open door — but `bridge/tests/test_mcp_auth.py:50` only covers `/sessions`, not the single-session route, so nothing pins it either way.

**Files:**
- Modify: `bridge/app/mcp_server.py:552-571`
- Test: `bridge/tests/test_mcp_auth.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_get_single_session_requires_session_read_scope(monkeypatch):
    """This route returns the full shadow — scratch included, which carries the
    client's workspace snapshot. Its two siblings gate on scope; it did not."""
    # Mirror the existing /sessions auth test at test_mcp_auth.py:50 exactly,
    # including how it monkeypatches auth.asgi.get_auth_settings (NOT
    # keys._AUTH_ENABLED — see the Auth section of the root CLAUDE.md).
```

- [ ] **Step 2: Run to verify failure, then add the one-liner its siblings already have**

```python
        session_id = request.path_params["session_id"]
        await require_scope_asgi(request, "session:read")
```

Match the exact call form used at `:588` — read it rather than copying this snippet.

- [ ] **Step 3: Run the bridge suite, then commit**

```bash
cd bridge && python -m pytest tests/ -q
git commit -m "fix(bridge): the route that returns a whole session had no scope gate"
```

---

## CI mechanics that fail silently — check each before opening a PR

- [ ] **bridge has no pytest config**, so pytest-asyncio is strict: a new async test **without** `@pytest.mark.asyncio` is silently **skipped**, not failed. Confirm your new tests actually ran (`-v`, not just `-q`).
- [ ] **The client CI job installs `pytest` only.** A new file under `client/tests/` that imports `httpx`, `mcp`, or `anyio` at module level must be added to `_DEP_BEARING_TESTS` in `client/tests/conftest.py` (`:25`), or collection aborts for the **entire** client suite. `firekeep_client.adapters` and `firekeep_client.state` are dependency-free (proven by the existing unlisted tests that import them); `firekeep_client.shim` is not.
- [ ] `client/pyproject.toml` sets `addopts = "-m 'not e2e'"` — an unmarked test that touches the network **will** run in CI.
- [ ] A blocking repo-wide `ruff check .` (E4, E7, E9, F) runs on every push. F401 (unused import) in a new test file fails the build.
- [ ] `bridge/tests/` and `client/tests/**` are run as whole directories by `.github/workflows/ci.yml`, so new files need no workflow edit. **There is no `.gitlab-ci.yml` in this repo** despite the root `CLAUDE.md` describing one — any "update the GitLab job" step has no file to edit.

## Verification before calling this done

- [ ] `cd bridge && python -m pytest tests/ -q` — 154 baseline + new
- [ ] `cd client && python -m pytest tests/ -q` — 813 baseline + new
- [ ] `cd cortex && python -m pytest tests/ -q` — 1235 baseline + new
- [ ] `cd relay && python -m pytest tests/ -q` — 164
- [ ] `python -m pytest tests/ replay/tests auth/tests vault/tests corpus/tests -q`
- [ ] `cd client && python -m pytest tests/adapters/test_write_stability.py -q` — cache integrity intact
- [ ] `firekeep doctor` reports the precompact capability honestly
- [ ] Live: a real Claude session compacts; confirm the checkpoint landed and `ctx_get_shadow()` restores

## Open risks

- **A delta cannot express deletion.** There are no tombstones anywhere in Bridge: `decisions`/`progress` are `LTRIM`'d at 50, and `files`/`scratch` are `HDEL`'d on overflow in arbitrary `HKEYS` order. An agent that received entry X in a full shadow and then a delta silent about X concludes X is unchanged. That inference is *correct about its own context* — it still holds X — but it is wrong about the server, which has dropped it. This does not lose information the agent had; it does mean **a delta must never be read as an assertion of absence**, which is why `omission_notice` says the omitted content "still exists" rather than describing the server's current state. Mitigated, not eliminated. If tombstones are ever added, revisit.
- **Two safeguards on three runtimes, not three.** The spec claims three independent safeguards, but "precompact invalidates the cursor server-side" exists only on Claude — kiro has no compaction event and its hook payload carries no session id at all, codex has no hook surface, and opencode's plugin dispatches a fixed event set. Everywhere else the cursor's only invalidation is its TTL plus the session-id and epoch binding.
- **Concurrent sessions under one identity.** The cursor stash is one machine-global slot per `{agent}@{profile}`, last-writer-wins — the same documented hazard as the session-id stash. Window A could pass a cursor minted for window B's shadow. This is why `filter_since` **binds the cursor to its session_id and refuses a mismatch**: the cross-window case becomes a safe full restore instead of a silent omission. Without that binding the design is not lossless. The supported partition for genuinely concurrent work remains a distinct `FIREKEEP_AGENT_ID` per terminal.
- **Agent compliance.** The primary safeguard rests on the agent honestly reporting what it can still see. Mitigated by making the safe answer the default and the unsafe answer an explicit opt-in — not eliminated.
- **Cursor TTL semantics are indistinguishable from failure.** `read_shadow_cursor` returns `None` for "no cursor", "read failed", and "expired" alike. That is the correct fail direction (all three mean full restore) and must stay an asserted invariant rather than an assumption. Do not add a `raise` anywhere on that path.
- **Measurement.** Every token figure in the design is `chars/4`, which under-counts JSON tool schemas (they tokenize nearer 3.2–3.7 chars/token). Before any customer-facing savings claim, instrument a real session with a real tokenizer and real cache hit/miss accounting.
- **What this delivers is not "fewer tokens per turn."** That claim is worth about 1% and sits in the cached region. It is *more turns before compaction, and no lost working state when it happens.*

---

# ADDENDUM — Fix round 1 (2026-07-30). Supersedes the text above where they conflict.

Task 5's review returned **three Critical findings, all plan-mandated** — defects in this
document, faithfully transcribed. This addendum is the authoritative spec for the fix.
Where it contradicts anything earlier in this plan, the addendum governs.

## Global Constraint, amended

> ~~`assemble_shadow`'s signature does not change.~~

**Relaxed deliberately, with the user's approval (2026-07-30).** It becomes
`assemble_shadow(data, *, omitted=None)`. All four existing call sites pass exactly one
positional argument (`bridge/app/mcp_server.py:300, 357, 482, 561` — verified), so a
keyword-only parameter with a default has provably **zero** effect on them. The
constraint's purpose — no blast radius on four production consumers — is fully preserved;
only its letter is relaxed. This is not licence for any other signature change.

## C1 (Critical) — the delta asserted that omitted content does not exist

`assemble_shadow` renders an empty section as an affirmative denial: `*No plan set*`,
`*No decisions recorded*`, `*No files tracked*`, `*No progress logged*`. This plan made
"empty" the omission signal and put the corrective notice in a **sibling JSON field**
(`result["note"]`), outside the markdown the agent actually reads. A quiet delta therefore
rendered four confident denials with nothing in the document to contradict them — exactly
the inference this design forbids, pre-drawn and handed over as fact.

`""` was never a usable signal anyway: it collides with a legitimately empty plan.

**The fix.** The signal is the **omission report** that `filter_since` already returns, not
the emptiness of a section. `assemble_shadow` consumes it:

```python
def assemble_shadow(data: dict[str, Any], *, omitted: dict[str, Any] | None = None) -> str:
    """...

    `omitted` is filter_since's omission report. When present, a section whose entries
    were withheld renders a line SAYING SO instead of the "none recorded" placeholder.
    A delta must never let a reader conclude the omitted content does not exist — that
    inference is the degradation, not the omission.
    """
```

Per section, replace the bare `else` placeholder branch:

```python
    elif omitted and omitted.get("decisions"):
        lines.append(
            f"*{omitted['decisions']} earlier decision(s) omitted - delivered earlier in "
            "this conversation. Call ctx_get_shadow() with no arguments for the full document.*"
        )
    else:
        lines.append("*No decisions recorded*")
```

Same shape for `progress` and `files`. The plan's report value is a **bool**, not a count:

```python
    elif omitted and omitted.get("plan"):
        lines.append(
            "*Plan unchanged - delivered earlier in this conversation. "
            "Call ctx_get_shadow() with no arguments for the full document.*"
        )
```

Use `decision(s)` / `file(s)` / `entry(s)` rather than bare plurals — that also settles
I3's "1 decisions" cosmetic. Task 7 passes `omitted=omitted` through. `result["note"]`
stays as well, belt and braces, but it is no longer the only mitigation.

**Test obligations:** for each section, a delta with omissions renders neither
`No decisions recorded`, `No files tracked`, `No progress logged` nor `No plan set`, and the
rendered text contains `ctx_get_shadow()`. A FULL restore of a genuinely empty session must
still render the original placeholders — that path must not regress.

## C2 (Critical) — the epoch guard failed OPEN on a read error

`str(parsed.get("epoch") or "") != str(epoch or "")` coerces both sides, so `""` matches
every cursor minted before the first compaction — which is every cursor, normally. And Task
6 specified `get_shadow_epoch` to return `""` when the Redis read *fails*. Combined:

1. Cursor minted, epoch `""` (never compacted).
2. Compaction. `precompact` bumps `shadow_epoch`.
3. Agent presents the stale cursor; the `hget` errors and returns `""`.
4. `"" == ""` → **a delta is served to an agent that just lost its context.**

A Redis read error silently converted a stale cursor into a valid one.

**The fix, in Task 6.** `get_shadow_epoch(session_id) -> str | None`:

```python
    async def get_shadow_epoch(self, session_id: str) -> str | None:
        """The session's shadow epoch. "" means never bumped; None means the read FAILED.

        The distinction is load-bearing. "" is a real, matchable state - every cursor
        minted before the first compaction carries it. If a read failure also returned "",
        an errored read would silently match a stale cursor and serve a delta to an agent
        that had just lost its context. None is unmatchable by construction.
        """
        try:
            value = await self._r.hget(self._scratch_key(session_id), "shadow_epoch")
        except Exception:
            return None
        return value or ""
```

**In Task 7:** when `epoch is None`, force a full restore and mint **no** cursor —
`filter_since` is not consulted at all, and `shadow_cursor` is omitted from the response. A
response carrying no cursor cannot produce a later delta, which is the safe outcome. Do
**not** fix this by rejecting empty epochs inside `filter_since`: that would kill every
pre-first-compaction delta, i.e. the feature.

Task 6's brief contains a test docstring asserting an unreadable epoch "mismatches every
cursor". That claim was **false** and must be replaced by a test asserting `None`.

## C3 + I1 + M3 — one predicate replaces three fragile clauses

Three findings share a root cause, so they get one fix rather than three patches:

- **C3 (Critical)**: `(e.get("timestamp") or "") >= hw` turns a missing stamp into `""`, and
  `"" >= hw` is False, so the entry is **dropped** — directly beneath a comment reading
  "Duplication beats omission".
- **I1 (Important)**: `e.get(...)` assumes every entry is a dict; a bare string raises
  `AttributeError`. Not hypothetical — `shadow.py` already guards `files` values with
  `isinstance(info, dict)`. Task 7 makes it worse by calling `high_water_of` and
  `plan_sha_of` unconditionally on **every** `ctx_get_shadow`, so a malformed entry would
  turn a degraded-but-readable full restore into a hard failure.
- **M3 (Minor)**: raw ISO string comparison is sound only for today's exact writer. A naive
  stamp (`2026-07-30T10:00:00`) is a **prefix** of the same instant with an offset, so it
  sorts LESS and would be dropped despite being newer-or-equal.

**The fix** — add to `bridge/app/residency.py`:

```python
from datetime import datetime


def _keep_entry(stamp: object, hw: str) -> bool:
    """True if this entry must be KEPT.

    Unknown or unparseable age means we cannot PROVE the agent already has this entry, so
    it is kept: duplication beats omission. Parsing rather than comparing strings also
    removes a class of silent drop that raw comparison invites - a naive stamp
    ('2026-07-30T10:00:00') is a PREFIX of the same instant with an offset, so it sorts
    lexicographically LESS and would be dropped despite being newer-or-equal.
    """
    if not isinstance(stamp, str) or not stamp:
        return True                      # unknown -> keep
    try:
        a, b = datetime.fromisoformat(stamp), datetime.fromisoformat(hw)
    except (TypeError, ValueError):
        return True                      # unparseable -> keep
    try:
        return a >= b                    # INCLUSIVE: the boundary entry is re-sent
    except TypeError:
        return True                      # naive vs aware -> keep
```

Guard the container type at the call site so a non-dict entry survives into the output
rather than raising:

```python
    for section in _LIST_SECTIONS:
        entries = data.get(section) or []
        kept = [e for e in entries
                if not isinstance(e, dict) or _keep_entry(e.get("timestamp"), hw)]
        out[section] = kept
        omitted[section] = len(entries) - len(kept)

    files = data.get("files") or {}
    kept_files = {k: v for k, v in files.items()
                  if not isinstance(v, dict) or _keep_entry(v.get("last_action"), hw)}
```

`high_water_of` must skip non-dict entries too — Task 7 calls it on every request,
including full restores:

```python
    for section in _LIST_SECTIONS:
        for entry in data.get(section) or []:
            if isinstance(entry, dict) and (entry.get("timestamp") or ""):
                stamps.append(entry["timestamp"])
    for entry in (data.get("files") or {}).values():
        if isinstance(entry, dict) and (entry.get("last_action") or ""):
            stamps.append(entry["last_action"])
```

`plan_sha_of` must tolerate a non-str plan: `str(data.get("plan") or "")`.

**Test obligations:** an entry with no `timestamp` key is KEPT; `timestamp=""` is KEPT; a
bare-string entry is KEPT and does not raise; a bare-string `files` value is KEPT and does
not raise; a naive-stamp entry at the boundary is KEPT; a `Z`-suffixed stamp is KEPT;
`high_water_of` returns a value rather than raising when a section holds a non-dict.

## I2 (Important) — four of `decode_cursor`'s five guards are untested

`test_garbage_cursor_decodes_to_none` feeds only inputs that die in base64 or JSON, so
deleting `isinstance(obj, dict)`, the `v != _CURSOR_VERSION` check, or the
`isinstance(sid/hw, str)` checks leaves all 15 tests green. The version check is the
feature's **field kill switch** — bumping `_CURSOR_VERSION` is how every outstanding cursor
gets invalidated — and nothing proves it works.

Add a test that each of these decodes to `None`, each built as real base64 JSON:
`{"v": 2, ...}`, `{"v": 1, "sid": None, "hw": "x", "plan_sha": "y"}`,
`{"v": 1, "sid": "s", "hw": 12345, "plan_sha": "y"}`, a JSON array, and a JSON string.

## I3 (Important) — `omission_notice` has zero tests

It is the function whose exact wording carries the losslessness claim. Add tests: it names
every omitted section; it states the content still exists; it tells the reader how to get
the full document; it returns `""` when nothing was omitted; the plan bool never renders as
a count.

## Minors — fix the two that guard the invariant, log the rest

- **M2**: `hw="   "` passes the no-high-water gate because whitespace is truthy. The
  direction is safe (everything is kept) but the gate does not do what it reads as. Use
  `hw.strip()`.
- **M5/M7**: comment the `hw`-empty fail-safe test to say its `assert omitted is None` is
  the load-bearing assertion — with the guard deleted, `out == _data()` still passes. And
  add a decision entry exactly at the boundary so inclusivity is proven by more than one
  test.
- **M1, M4, M6** — deferred minors for the final review; not in this round.
