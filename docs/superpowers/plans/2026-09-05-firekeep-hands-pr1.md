# Firekeep Hands PR1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `firekeep-hands` — an opt-in, kit-mounted MCP server that lets any installed runtime (Claude Code, Codex, Kiro, OpenCode, Studio) observe and operate this computer's apps and a Hands-managed browser, on Windows and macOS, with every consequential step gated by a separate approval broker that only real human input (or a phone tap) can satisfy.

**Architecture:** One new wheel at repo-root `hands/` (`firekeep_hands`) with three processes: the MCP server the gateway mounts (`firekeep-hands`), the approval broker (`firekeep-hands-broker`, loopback HTTP + OS input listener + relay phone bridge) and the Hands-managed Chrome/Edge reached over DevTools. Platform work sits behind one `Backend` protocol (`backends/win.py`, `backends/mac.py`, `backends/fake.py` for tests); policy classifies every action into protected classes; a deterministic router picks accessibility over pixels; a local evidence ledger records every step; the Keep sees `action_before/after`, a relay lease and `hands_permit` tasks through the kit's own `call_tool`. The client kit learns a `role` on `DexManifest`, a never-seeded `hands` registry entry, `firekeep hands …` and a doctor row.

**Tech Stack:** Python 3.11 (kit venv), `mcp` SDK (stdio), `websocket-client` (CDP), `pillow`; Windows: `uiautomation`, `mss`, ctypes (`SendInput`, `WH_KEYBOARD_LL`); macOS: pyobjc (`Quartz`, `ApplicationServices`, `Cocoa`), `screencapture`, `CGEventTap`; stdlib `http.server` for the broker; relay MCP tools via `firekeep_client.hooks._mcp.call_tool`.

**Spec:** `docs/superpowers/specs/2026-09-05-firekeep-hands-design.md` (commit ea91744). The plan argues from it; conflicts resolve against the spec.

## Global Constraints

- **Client spine stays stdlib-only.** Nothing under `client/firekeep_client/` may import `firekeep_hands`, `mcp`, pyobjc or any third-party module at module level (`client/tests/test_import_boundary.py`). Every wheel import in the kit is lazy and inside the function that needs it.
- **Hands is never seeded and never bundled.** `dexes.ensure_migrated` writes exactly `{"symdex", "docdex"}`; `client/scripts/make_release.py`, `client/bootstrap/install.sh`, `client/bootstrap/install.ps1` and `cmd_install`'s checkout loop do not learn about `hands`. `firekeep hands enable` pip-installs the wheel into the kit venv, then registers it.
- **Registry entry:** `KNOWN_DEXES["hands"] = DexManifest(id="firekeep.hands", name="hands", title="Hands", indexes="desktop", kind="mcp-stdio", console_script="firekeep-hands", import_probe="firekeep_hands", description=…, role="capability")`. `DexManifest.role` defaults to `"index"`; symdex/docdex/maildex keep the default.
- **Fail closed.** A protected action (any of `send`, `money`, `destroy`, `credential`, `install`, `boundary`) never executes without a permit; if the broker is unreachable the action is refused with a message naming `firekeep hands status`. Unprotected actions proceed.
- **Permits are one-use, 60 s TTL, in-memory, bound to a deterministic challenge id** `challenge_id_for(machine_id, session_id, task_id, step_index, action_hash)`; the server recomputes the id and refuses a permit whose id does not match the action it is about to run.
- **Only real input approves.** Windows listener ignores any `KBDLLHOOKSTRUCT` with `LLKHF_INJECTED` (0x10) or `LLKHF_LOWER_IL_INJECTED` (0x02); macOS listener ignores events whose `kCGEventSourceUserData == HANDS_TAG` or whose `kCGEventSourceStateID != kCGEventSourceStateHIDSystemState`. Every synthetic event Hands emits carries `HANDS_TAG` (`dwExtraInfo` / user data). There is no CLI or MCP tool that approves a permit.
- **No bare model coordinates.** `click` and `scroll` take a `ref` from the current observation (or `"window"` for scroll); a point is always computed by Hands from an observed rect.
- **Perception budget:** ≤ 200 controls per observe, ≤ 4000 chars of text, screenshots downscaled to ≤ 1280 px wide PNG, ≤ 400 steps per task, one before/after image pair per step.
- **Evidence:** `~/.firekeep/hands/evidence/<task_id>/{task.json,steps.jsonl,NNN-before.png,NNN-after.png}`, sha256 chain per line, pruned after 14 days at `hands_task_start`. Keep-side evidence in PR1 = `action_before`/`action_after` (cortex MCP tools) + relay lease `hands:<machine_id>`; no replay POST route (PR2).
- **Hands files live under `~/.firekeep/hands/`** (resolved through `firekeep_client.resolver._config_path().parent / "hands"` so `FIREKEEP_CONFIG` isolates tests): `config.json`, `policy.json`, `broker.json` (0600), `machine_id`, `chrome-profile/`, `evidence/`.
- **Both platforms from day one.** Every backend method has a Windows and a macOS implementation in this PR; Linux gets `hands_status` reporting `backend: "unsupported"` and every other tool refusing.
- **Tests run on Linux CI without OS libraries.** Platform modules are imported lazily; test files for `win`/`mac` use `pytest.importorskip` or `sys.platform` skips; the fake backend carries the tool-surface tests.
- **Copy rules:** no client version numbers in docs; the guide discloses the honest limits (locked screen, elevated windows, screenshots leave the machine when the runtime is a cloud model, prompt injection through observed UI text, two-hop trust).
- **Commit style:** `feat(hands): …`, `feat(client): …`, `docs(hands): …`, `ci: …`; each task ends in its own commit(s) on `feat/hands`.

---

## File map

**New wheel `hands/`**
- `hands/pyproject.toml`, `hands/README.md`, `hands/LICENSE` (copy of `symdex/LICENSE`), `hands/NOTICE` (copy of `symdex/NOTICE`)
- `hands/src/firekeep_hands/__init__.py` — `__version__`, `HANDS_TAG`
- `hands/src/firekeep_hands/paths.py` — every path under `~/.firekeep/hands/`
- `hands/src/firekeep_hands/config.py` — `HandsConfig`, `Policy` load/save
- `hands/src/firekeep_hands/ids.py` — `machine_id()`, `action_hash()`, `challenge_id_for()`
- `hands/src/firekeep_hands/backends/base.py` — `Rect`, `Control`, `WindowInfo`, `Observation`, `Backend` protocol, `HandsError`
- `hands/src/firekeep_hands/backends/fake.py` — scripted in-memory backend for tests
- `hands/src/firekeep_hands/backends/win.py`, `backends/mac.py`, `backends/__init__.py` (`load_backend()`)
- `hands/src/firekeep_hands/policy.py` — protected classes, allowlist, `decide()`
- `hands/src/firekeep_hands/routing.py` — `route()`; stale-ref and coordinate rejection
- `hands/src/firekeep_hands/evidence.py` — `Ledger`
- `hands/src/firekeep_hands/keep.py` — `KeepLink` (action_before/after, lease, permit tasks) over `call_tool`
- `hands/src/firekeep_hands/broker/permits.py` — `PermitStore`
- `hands/src/firekeep_hands/broker/server.py` — loopback HTTP API + listener/phone threads
- `hands/src/firekeep_hands/broker/client.py` — `BrokerClient` used by the MCP server and doctor
- `hands/src/firekeep_hands/broker/listeners/win.py`, `listeners/mac.py` — chord listeners
- `hands/src/firekeep_hands/broker/phone.py` — relay `hands_permit:` task bridge
- `hands/src/firekeep_hands/broker/autostart.py` — Task Scheduler / LaunchAgent
- `hands/src/firekeep_hands/broker/__main__.py` — `firekeep-hands-broker run|install-autostart|uninstall-autostart|status`
- `hands/src/firekeep_hands/browser.py` — Hands-managed Chrome/Edge over CDP
- `hands/src/firekeep_hands/session.py` — `HandsSession` (task lifecycle, budget, permits, ledger)
- `hands/src/firekeep_hands/server.py` — the MCP tool surface (`main()`)
- `hands/src/firekeep_hands/cli.py` — `status|allow|chord|config|evidence` (what `firekeep hands` delegates to)
- `hands/tests/…` one file per module above

**Client kit**
- `client/firekeep_client/dexes.py` — `role` field, `hands` manifest
- `client/firekeep_client/cli.py` — `cmd_hands`, `_check_hands`, subparser, `dex list` role label
- `client/tests/test_dexes.py`, `client/tests/test_cli_dex.py`, `client/tests/test_cli_hands.py` (new), `client/tests/test_cli_doctor.py`

**Server / dashboard**
- `dashboard/index.html` — Approve/Deny on `hands_permit:` relay tasks; `tests/test_dashboard_hands.py` (new)

**Release / CI / docs**
- `.github/workflows/release.yml`, `.github/workflows/ci.yml`, `tests/test_requirements_lock.py`, `client/tests/test_make_release.py`
- `docs/guides/hands.md` (new), `docs/guides/dexes.md`, `docs/guides/client-kit.md`, `docs/THREAT-MODEL.md`, `CLAUDE.md`, `README.md`

---

### Task 1: Registry `role` and the `hands` manifest (client kit)

**Files:**
- Modify: `client/firekeep_client/dexes.py:41-101` (dataclass + `KNOWN_DEXES`), `:228-270` (`ensure_migrated` docstring only)
- Modify: `client/firekeep_client/cli.py:1984-1989` (`dex list` line)
- Test: `client/tests/test_dexes.py`, `client/tests/test_cli_dex.py`

**Interfaces:**
- Produces: `DexManifest.role: str = "index"`; `KNOWN_DEXES["hands"]` (values in Global Constraints); `dexes.registered()` returns it when `"hands"` is in `dexes.json`; the gateway (`gateway.py:318-319`) mounts it unchanged because `kind == "mcp-stdio"`.

- [ ] **Step 1: Write the failing tests**

Append to `client/tests/test_dexes.py` (its `registry_home` fixture at `:21-27` sets `FIREKEEP_CONFIG`/`FIREKEEP_LOG_DIR` to a tmp dir; `test_cli_dex.py` and the new `test_cli_hands.py` import or redefine the same fixture — copy those seven lines into a file that lacks it):

```python
def test_manifest_role_defaults_to_index():
    from firekeep_client import dexes
    assert dexes.DexManifest.__dataclass_fields__["role"].default == "index"
    assert dexes.KNOWN_DEXES["symdex"].role == "index"
    assert dexes.KNOWN_DEXES["docdex"].role == "index"


def test_hands_manifest_is_a_capability_mounted_as_mcp_stdio():
    from firekeep_client import dexes
    m = dexes.KNOWN_DEXES["hands"]
    assert (m.id, m.name, m.title, m.indexes) == ("firekeep.hands", "hands", "Hands", "desktop")
    assert m.kind == "mcp-stdio"
    assert m.console_script == "firekeep-hands"
    assert m.import_probe == "firekeep_hands"
    assert m.role == "capability"


def test_hands_is_never_seeded(registry_home):
    from firekeep_client import dexes
    dexes.ensure_migrated()
    assert set(dexes.read_registry()) == {"symdex", "docdex"}
```

Append to `client/tests/test_cli_dex.py` (use its existing capsys/registry fixtures):

```python
def test_dex_list_labels_capabilities(registry_home, capsys):
    from firekeep_client import cli
    cli.cmd_dex(type("A", (), {"action": "list"})())
    out = capsys.readouterr().out
    assert "hands  [not installed]  operates desktop" in out
    assert "symdex  [" in out and "indexes code" in out
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd client && python -m pytest tests/test_dexes.py tests/test_cli_dex.py -q -k "role or hands or capabilities"`
Expected: FAIL — `KeyError: 'role'` / `KeyError: 'hands'`.

- [ ] **Step 3: Add the field and the manifest**

In `dexes.py`, extend the dataclass (defaulted field last) and the registry:

```python
    import_probe: str
    description: str
    # "index" (symdex, docdex, maildex) or "capability" (hands). The gateway
    # does not read it — `kind` decides mounting — but `dex list`, doctor and
    # the docs do: a capability OPERATES its domain rather than indexing it,
    # and is never part of the default seed (ensure_migrated).
    role: str = "index"
```

```python
    "hands": DexManifest(
        id="firekeep.hands",
        name="hands",
        title="Hands",
        indexes="desktop",
        kind="mcp-stdio",
        console_script="firekeep-hands",
        import_probe="firekeep_hands",
        description=(
            "Desktop operator — your runtime observes and operates this "
            "computer's apps and a Hands-managed browser; consequential steps "
            "wait for your chord or phone tap. Opt in with `firekeep hands enable`."
        ),
        role="capability",
    ),
```

Add one sentence to `ensure_migrated`'s docstring after the maildex paragraph: "hands is likewise never seeded: a capability that can move the mouse is opt-in by `firekeep hands enable`, which installs the wheel and registers it in one step."

In `cli.py` `cmd_dex` list branch replace the `indexes` line:

```python
            verb = "operates" if manifest.role == "capability" else "indexes"
            print(f"  {manifest.name}  [{_dex_state(manifest, registry)}]  "
                  f"{verb} {manifest.indexes}")
```

- [ ] **Step 4: Run the client suite**

Run: `cd client && python -m pytest tests -q`
Expected: all green (existing tests that enumerate `KNOWN_DEXES` by count must be updated to 4 if any assert 3 — fix the assertion, not the registry).

- [ ] **Step 5: Commit**

```bash
git add client/firekeep_client/dexes.py client/firekeep_client/cli.py client/tests/test_dexes.py client/tests/test_cli_dex.py
git commit -m "feat(client): dex registry learns a role; hands is a never-seeded capability"
```

---

### Task 2: `firekeep hands` CLI and the doctor row (client kit)

**Files:**
- Modify: `client/firekeep_client/cli.py` — new `HANDS_WHEEL_SPEC`, `cmd_hands`, `_check_hands`; `_check_dexes` (`:1206-1238`) gains a hands row; the `dex`/`docdex` subparser block in `main()` (find it with `grep -n 'add_parser("docdex"' client/firekeep_client/cli.py`) gains `hands`
- Test: `client/tests/test_cli_hands.py` (new), `client/tests/test_cli_doctor.py`

**Interfaces:**
- Consumes: `dexes.KNOWN_DEXES["hands"]`, `dexes.add/remove/is_installed`, `_pip_install(python, spec)` (exists, `cli.py`, used at `:564`).
- Produces: `firekeep hands enable [--from <path-or-spec>] [--no-autostart]`, `firekeep hands disable [--purge]`, `firekeep hands status|allow|chord|config|evidence …` (delegated to `firekeep_hands.cli.main(argv)` — Task 12 provides it); doctor row `("hands", state, text)`; `read_broker_health(timeout=1.0) -> dict | None` (stdlib urllib) reused by the doctor.

- [ ] **Step 1: Write the failing tests**

`client/tests/test_cli_hands.py`:

```python
import json
import types

import pytest

from firekeep_client import cli, dexes


def _args(**kw):
    base = {"action": None, "source": None, "no_autostart": False, "purge": False, "rest": []}
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_enable_installs_registers_and_installs_autostart(registry_home, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli, "_pip_install", lambda python, spec: calls.append(("pip", spec)))
    monkeypatch.setattr(dexes, "is_installed", lambda m: True)
    monkeypatch.setattr(cli, "_run_hands_broker", lambda argv: calls.append(("broker", tuple(argv))) or 0)
    assert cli.cmd_hands(_args(action="enable")) == 0
    assert calls == [("pip", cli.HANDS_WHEEL_SPEC), ("broker", ("install-autostart",))]
    assert "hands" in dexes.read_registry()
    assert "next agent session" in capsys.readouterr().out


def test_enable_from_local_path_uses_that_path(registry_home, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(cli, "_pip_install", lambda python, spec: calls.append(spec))
    monkeypatch.setattr(dexes, "is_installed", lambda m: True)
    monkeypatch.setattr(cli, "_run_hands_broker", lambda argv: 0)
    src = tmp_path / "hands"; src.mkdir(); (src / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert cli.cmd_hands(_args(action="enable", source=str(src))) == 0
    assert calls == [str(src)]


def test_enable_refuses_to_register_when_import_probe_fails(registry_home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_pip_install", lambda python, spec: None)
    monkeypatch.setattr(dexes, "is_installed", lambda m: False)
    assert cli.cmd_hands(_args(action="enable")) == 1
    assert "hands" not in dexes.read_registry()
    assert "not importable" in capsys.readouterr().err


def test_disable_deregisters_and_removes_autostart(registry_home, monkeypatch):
    dexes.add("hands")
    calls = []
    monkeypatch.setattr(cli, "_run_hands_broker", lambda argv: calls.append(tuple(argv)) or 0)
    assert cli.cmd_hands(_args(action="disable")) == 0
    assert "hands" not in dexes.read_registry()
    assert calls == [("uninstall-autostart",)]


def test_disable_purge_removes_hands_dir(registry_home, monkeypatch, tmp_path):
    hands_dir = dexes.registry_path().parent / "hands"
    (hands_dir / "evidence").mkdir(parents=True)
    monkeypatch.setattr(cli, "_run_hands_broker", lambda argv: 0)
    assert cli.cmd_hands(_args(action="disable", purge=True)) == 0
    assert not hands_dir.exists()


def test_other_actions_delegate_to_the_wheel(monkeypatch):
    seen = []
    fake = types.SimpleNamespace(main=lambda argv: seen.append(list(argv)) or 0)
    monkeypatch.setitem(__import__("sys").modules, "firekeep_hands.cli", fake)
    monkeypatch.setitem(__import__("sys").modules, "firekeep_hands", types.SimpleNamespace(cli=fake))
    assert cli.cmd_hands(_args(action="allow", rest=["domain", "example.com"])) == 0
    assert seen == [["allow", "domain", "example.com"]]


def test_delegation_without_wheel_explains_enable(monkeypatch, capsys):
    import builtins
    real = builtins.__import__
    def fake_import(name, *a, **k):
        if name.startswith("firekeep_hands"):
            raise ImportError(name)
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert cli.cmd_hands(_args(action="status")) == 1
    assert "firekeep hands enable" in capsys.readouterr().err


def test_doctor_hands_row_reports_broker(registry_home, monkeypatch):
    dexes.add("hands")
    monkeypatch.setattr(dexes, "is_installed", lambda m: True)
    monkeypatch.setattr(cli, "read_broker_health", lambda timeout=1.0: {"ok": True, "chord": "ctrl+alt+y", "listeners": {"chord": "active", "phone": "active"}})
    rows = dict((r[0], r) for r in cli._check_dexes())
    assert rows["hands"][1] == "ok"
    assert "chord ctrl+alt+y" in rows["hands"][2]
    monkeypatch.setattr(cli, "read_broker_health", lambda timeout=1.0: None)
    rows = dict((r[0], r) for r in cli._check_dexes())
    assert rows["hands"][1] == "warn"
    assert "broker not running" in rows["hands"][2]
```

Add to `client/tests/test_cli_doctor.py` nothing new beyond the above (the row test lives in the new file; keep doctor's file focused).

- [ ] **Step 2: Run to verify they fail**

Run: `cd client && python -m pytest tests/test_cli_hands.py -q`
Expected: FAIL — `AttributeError: module 'firekeep_client.cli' has no attribute 'cmd_hands'`.

- [ ] **Step 3: Implement `cmd_hands`, helpers and the doctor row**

Add near `EMPTY_REGISTRY_HINT` (`cli.py:152`):

```python
# The PyPI spec `firekeep hands enable` installs by default. Hands is NOT
# bundled by the bootstrap (a capability that moves the mouse is opt-in, spec
# §3.1), so this is the one place in the kit that names the wheel's source.
HANDS_WHEEL_SPEC = "firekeep-hands>=0.1,<0.2"
```

Add these functions after `cmd_docdex`:

```python
def _hands_dir() -> Path:
    return _config_path().parent / "hands"


def read_broker_health(timeout: float = 1.0) -> dict | None:
    """The approval broker's /health, or None when it is not running.

    Stdlib only and read from disk first: `broker.json` names the loopback
    port and bearer token the running broker chose. No file, or a refused
    connection, both mean "not running" — doctor must not hang on it."""
    import urllib.error
    import urllib.request
    path = _hands_dir() / "broker.json"
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        port, token = int(info["port"]), str(info["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/health", headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — loopback only
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("ok") else None


def _run_hands_broker(argv: list[str]) -> int:
    """Run the wheel's broker console script (install-autostart etc.) in-process."""
    try:
        from firekeep_hands.broker.__main__ import main as broker_main
    except ImportError:
        print("firekeep: firekeep-hands is not importable in this venv — run "
              "`firekeep hands enable`", file=sys.stderr)
        return 1
    return int(broker_main(argv) or 0)


def cmd_hands(args) -> int:
    """`firekeep hands enable|disable|status|allow|chord|config|evidence`.

    enable/disable are the kit's own (they touch the venv and the registry —
    spec §3.1: Hands is installed on demand, never bundled). Everything else
    is a translator onto `firekeep_hands.cli.main`, imported lazily so a kit
    without the wheel keeps every other command working."""
    action = getattr(args, "action", None) or "status"
    manifest = dexes.KNOWN_DEXES["hands"]

    if action == "enable":
        source = (getattr(args, "source", None) or "").strip() or HANDS_WHEEL_SPEC
        print(f"firekeep: installing {source} into this kit's venv …")
        try:
            _pip_install(sys.executable, source)
        except Exception as exc:  # noqa: BLE001 — pip's failure text is the diagnosis
            print(f"firekeep: install failed: {exc}", file=sys.stderr)
            return 1
        if not dexes.is_installed(manifest):
            print("firekeep: firekeep-hands installed but not importable "
                  f"(no module '{manifest.import_probe}') — not registering it",
                  file=sys.stderr)
            return 1
        dexes.add("hands")
        rc = 0
        if not getattr(args, "no_autostart", False):
            rc = _run_hands_broker(["install-autostart"])
            if rc:
                print("firekeep: broker autostart could not be installed — start it by "
                      "hand with `firekeep-hands-broker run`", file=sys.stderr)
        print("firekeep: Hands is registered — your runtimes get the hands_* tools on "
              "the next agent session.\n"
              "firekeep: approve consequential steps with the chord "
              "(`firekeep hands chord` shows it) or from the dashboard on your phone.")
        return 0

    if action == "disable":
        if "hands" in dexes.read_registry():
            dexes.remove("hands")
        _run_hands_broker(["uninstall-autostart"])
        if getattr(args, "purge", False):
            shutil.rmtree(_hands_dir(), ignore_errors=True)
        print("firekeep: Hands is off — the tools disappear on the next agent session"
              + (" and its local files are gone." if getattr(args, "purge", False)
                 else "; `firekeep hands disable --purge` also removes its files."))
        return 0

    try:
        from firekeep_hands import cli as hands_cli
    except ImportError:
        print("firekeep: Hands is not installed — run `firekeep hands enable`", file=sys.stderr)
        return 1
    return int(hands_cli.main([action, *list(getattr(args, "rest", []) or [])]) or 0)
```

(`shutil` and `json` are already imported at the top of `cli.py`; verify with grep and add if not.)

Doctor: in `_check_dexes` after the maildex line add

```python
    if any(m.name == "hands" for m in registered):
        rows.append(_check_hands())
```

and

```python
def _check_hands() -> tuple[str, str, str]:
    """Hands' own row: wheel present, broker answering, and what approves.

    The broker is the safety boundary — with it down every protected step is
    refused (fail closed), so its absence is the one thing this row must say
    loudly. Permissions (accessibility, screen recording, input monitoring)
    are the wheel's to report; `firekeep hands status` shows them."""
    if not dexes.is_installed(dexes.KNOWN_DEXES["hands"]):
        return ("hands", "fail", "registered but the wheel is missing — `firekeep hands enable`")
    health = read_broker_health()
    if not health:
        return ("hands", "warn",
                "broker not running — protected steps are refused until it is "
                "(`firekeep-hands-broker run`, or re-run `firekeep hands enable`)")
    listeners = health.get("listeners") or {}
    return ("hands", "ok",
            f"broker up · chord {health.get('chord', '?')} ({listeners.get('chord', '?')}) "
            f"· phone {listeners.get('phone', '?')}")
```

Subparser (beside `docdex` in `main()`):

```python
    p_hands = sub.add_parser("hands", help="desktop operator — enable, disable, status, allow, chord, config, evidence")
    p_hands.add_argument("action", nargs="?", default="status",
                         choices=["enable", "disable", "status", "allow", "chord", "config", "evidence"])
    # nargs="*", NOT argparse.REMAINDER: REMAINDER after a positional swallows
    # `--from X` into `rest` and leaves `source` None — the exact command the
    # live smoke depends on. With "*" the options still parse wherever they sit.
    p_hands.add_argument("rest", nargs="*")
    p_hands.add_argument("--from", dest="source", default=None,
                         help="wheel source for enable: a local checkout dir or a pip spec")
    p_hands.add_argument("--pypi", action="store_true",
                         help="enable: install the published wheel from PyPI (HANDS_WHEEL_SPEC)")
    p_hands.add_argument("--no-autostart", action="store_true")
    p_hands.add_argument("--purge", action="store_true", help="with disable: delete ~/.firekeep/hands")
    p_hands.set_defaults(func=cmd_hands)
```

(Match how the file dispatches — if it uses an `if args.command == "dex"` chain instead of `set_defaults(func=…)`, add the `hands` branch in the same chain.)

**PyPI squat guard (ruling, 2026-09-05):** `https://pypi.org/pypi/firekeep-hands/json` returns 404 today — nobody owns the name, and the repo's rule for the other wheels (`cli.py:553-555`, `:569-571`) is never to `pip install` a bare name a third party could claim. So `enable` resolves its source as: `--from <path-or-spec>` wins; else `--pypi` installs `HANDS_WHEEL_SPEC` **only if** `HANDS_PYPI_PUBLISHED` is `True`; else it refuses:

```python
# Flip to True in the release that first publishes firekeep-hands through the
# `pypi-hands` trusted publisher (Task 13). Until then a bare `pip install
# firekeep-hands` could resolve to whoever registers the name first.
HANDS_PYPI_PUBLISHED = False
```

```python
        source = (getattr(args, "source", None) or "").strip()
        if not source:
            if getattr(args, "pypi", False) and HANDS_PYPI_PUBLISHED:
                source = HANDS_WHEEL_SPEC
            else:
                print("firekeep: firekeep-hands is not yet published to PyPI — install from a "
                      "checkout: `firekeep hands enable --from <checkout>/hands`", file=sys.stderr)
                return 2
```

Update the first test accordingly (`_args(action="enable", source=None)` → exit 2 with that message; a `monkeypatch.setattr(cli, "HANDS_PYPI_PUBLISHED", True)` + `pypi=True` variant installs `HANDS_WHEEL_SPEC`), and add one parser-level test:

```python
def test_parser_keeps_from_out_of_rest():
    from firekeep_client import cli
    args = cli._build_parser().parse_args(["hands", "enable", "--from", "X:/hands", "--no-autostart"])
    assert (args.action, args.source, args.no_autostart, args.rest) == ("enable", "X:/hands", True, [])
    args = cli._build_parser().parse_args(["hands", "allow", "domain", "example.com"])
    assert (args.action, args.rest) == ("allow", ["domain", "example.com"])
```

(`_build_parser` is whatever `main()` uses to construct the parser — if the parser is built inline in `main()`, extract it into `_build_parser()` first; that is a pure refactor.)

- [ ] **Step 4: Run the suite**

Run: `cd client && python -m pytest tests -q`
Expected: green, including `tests/test_import_boundary.py` (all new imports are inside functions).

- [ ] **Step 5: Commit**

```bash
git add client/firekeep_client/cli.py client/tests/test_cli_hands.py
git commit -m "feat(client): firekeep hands enable/disable/status and a doctor row"
```

---

### Task 3: Wheel scaffold, paths, config, ids, backend protocol, fake backend

**Files:**
- Create: `hands/pyproject.toml`, `hands/README.md`, `hands/LICENSE`, `hands/NOTICE`, `hands/src/firekeep_hands/__init__.py`, `paths.py`, `config.py`, `ids.py`, `backends/__init__.py`, `backends/base.py`, `backends/fake.py`
- Test: `hands/tests/conftest.py`, `hands/tests/test_paths.py`, `hands/tests/test_config.py`, `hands/tests/test_ids.py`, `hands/tests/test_fake_backend.py`

**Interfaces (produced, used by every later task):**

```python
# firekeep_hands/__init__.py
__version__ = "0.1.0"
HANDS_TAG = 0x46494B48   # "FIKH": dwExtraInfo on Windows, kCGEventSourceUserData on macOS

# paths.py
def hands_home() -> Path                 # resolver._config_path().parent / "hands"
def config_path() -> Path                # hands_home()/config.json
def policy_path() -> Path                # hands_home()/policy.json
def broker_info_path() -> Path           # hands_home()/broker.json
def machine_id_path() -> Path            # hands_home()/machine_id
def evidence_root() -> Path              # hands_home()/evidence
def chrome_profile_dir() -> Path         # hands_home()/chrome-profile

# config.py
@dataclass
class HandsConfig:
    chord: str = "ctrl+alt+y"; deny_chord: str = "ctrl+alt+n"
    permit_ttl_s: int = 60; max_steps: int = 400; max_nodes: int = 200
    text_budget: int = 4000; screenshot_max_width: int = 1280
    evidence_retention_days: int = 14; browser: str = "auto"   # auto|chrome|edge
def load_config() -> HandsConfig; def save_config(cfg) -> None
@dataclass
class Remembered: cls: str; app: str; match: str; until: str   # ISO-8601 UTC
@dataclass
class Policy: apps: list[str]; domains: list[str]; remembered: list[Remembered]
def load_policy() -> Policy; def save_policy(p) -> None

# ids.py
def machine_id() -> str                       # 32 hex, created once, 0600
def action_hash(action: dict) -> str          # sha256(json.dumps(action, sort_keys=True, separators=(",",":")))[:16]
def challenge_id_for(machine: str, session: str, task: str, step_index: int, ahash: str) -> str
    # sha256("hands|"+machine+"|"+session+"|"+task+"|"+str(step_index)+"|"+ahash)[:32]

# backends/base.py
class HandsError(Exception): code: str   # "stale_ref","not_found","unsupported","elevated_target","permission","backend"
@dataclass(frozen=True) class Rect: x:int; y:int; w:int; h:int
    def center(self) -> tuple[int,int]
@dataclass(frozen=True) class Control: ref:str; role:str; name:str; value:str; rect:Rect; app:str; patterns:tuple[str,...]; enabled:bool=True
@dataclass(frozen=True) class WindowInfo: app:str; title:str; pid:int; rect:Rect; elevated:bool=False
@dataclass class Observation: generation:int; window:WindowInfo|None; controls:list[Control]; text:str; screenshot_png:bytes|None; truncated:bool
class Backend(Protocol):
    name: str
    def permissions(self) -> dict[str, str]           # accessibility|screen|input -> ok|missing|unknown
    def active_window(self) -> WindowInfo | None
    def windows(self) -> list[WindowInfo]
    def observe(self, *, app: str | None, region: Rect | None, max_nodes: int, text_budget: int, screenshot: bool, max_width: int) -> Observation
    def find(self, query: str, *, role: str | None, app: str | None, limit: int) -> list[Control]
    def invoke(self, control: Control) -> None
    def set_value(self, control: Control, value: str) -> None
    def click(self, point: tuple[int, int], *, button: str = "left", double: bool = False) -> None
    def type_text(self, text: str) -> None
    def key(self, chord: str) -> None
    def scroll(self, point: tuple[int, int], dy: int) -> None
    def focus_app(self, app: str) -> bool
    def open_app(self, app: str) -> bool
    def clipboard_get(self) -> str
    def clipboard_set(self, text: str) -> None

# backends/__init__.py
def load_backend() -> Backend      # win32 -> WinBackend, darwin -> MacBackend, else UnsupportedBackend (every method raises HandsError("unsupported"))

# backends/fake.py
class FakeBackend:                 # implements Backend; scene = list[Control]; records every call in .calls
    def __init__(self, controls: list[Control] | None = None, window: WindowInfo | None = None, text: str = "", permissions=None)
    calls: list[tuple]             # ("invoke", ref), ("click", (x,y), "left", False), ("type", text) ...
    values: dict[str, str]         # set_value results by ref
```

- [ ] **Step 1: pyproject and package files**

`hands/pyproject.toml`:

```toml
[project]
name = "firekeep-hands"
version = "0.1.0"
description = "Firekeep Hands — a screen-aware operator for your computer, behind your Keep's approval broker"
readme = "README.md"
requires-python = ">=3.10"
license = "LicenseRef-Firekeep-BUSL-1.1"
license-files = ["LICENSE", "NOTICE"]
dependencies = [
    "mcp>=1.0.0,<2.0.0",
    "websocket-client>=1.7,<2.0",
    "pillow>=10.0,<12.0",
    "uiautomation>=2.0.18; sys_platform == 'win32'",
    "mss>=9.0,<11.0; sys_platform == 'win32'",
    "pyobjc-framework-Quartz>=10.0; sys_platform == 'darwin'",
    "pyobjc-framework-ApplicationServices>=10.0; sys_platform == 'darwin'",
    "pyobjc-framework-Cocoa>=10.0; sys_platform == 'darwin'",
]

[project.optional-dependencies]
test = ["pytest", "pytest-timeout"]

[project.scripts]
firekeep-hands = "firekeep_hands.server:main"
firekeep-hands-broker = "firekeep_hands.broker.__main__:main"

[project.urls]
Homepage = "https://firekeep.ai"
Documentation = "https://firekeep.ai/docs.html"

[build-system]
requires = ["hatchling>=1.27"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/firekeep_hands"]

[tool.pytest.ini_options]
testpaths = ["tests"]
timeout = 60
```

`hands/README.md`: three paragraphs — what Hands is (one sentence from the spec §1), how it is turned on (`firekeep hands enable` — the supported install; the wheel imports `firekeep_client` from the kit venv and deliberately does **not** declare it as a dependency, because the PyPI name `firekeep-client` is owned by a third party, so a bare `pip install firekeep-hands` outside the kit venv is unsupported), and a pointer to `docs/guides/hands.md`. Copy `symdex/LICENSE` and `symdex/NOTICE` verbatim.

**Platform-module rule for every backend/listener module (T6, T7, T8):** nothing OS-specific at import time. `ctypes.WinDLL(...)`, `import uiautomation`, `import Quartz` and friends happen inside the functions that use them (or in a module-level `_user32()` accessor cached on first call). The pure parts — struct layouts, `kb_event_is_real`, `ChordTracker`, `event_is_real`, `KEYCODES`, the INPUT builders — must import and run on Linux CI and on the other platform. Struct fields use fixed-width ctypes (`c_int32`, `c_uint32`, `c_uint16`, `c_size_t`), never `wintypes.LONG`/`DWORD` (`c_long` is 8 bytes on Linux x64, which would make `INPUT` measure 48 there).

`hands/tests/conftest.py`:

```python
import os
import pytest

@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Every test gets its own ~/.firekeep: paths.py resolves through the kit's
    resolver, which honours FIREKEEP_CONFIG."""
    cfg = tmp_path / "firekeep" / "config"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("[server]\nurl = http://127.0.0.1:1\napi_key = test\n", encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(cfg))
    monkeypatch.setenv("FIREKEEP_HANDS_OFFLINE", "1")
    yield tmp_path / "firekeep"
```

(If `resolver._config_path()` does not honour `FIREKEEP_CONFIG` exactly this way, read `client/firekeep_client/resolver.py` and mirror what `client/tests/conftest.py` does — the contract is "tmp dir per test", not the env var name.)

- [ ] **Step 2: Failing tests for paths/config/ids**

`hands/tests/test_paths.py`:

```python
from firekeep_hands import paths

def test_everything_lives_under_hands_home(isolated_home):
    home = paths.hands_home()
    assert home == isolated_home / "hands"
    for fn in (paths.config_path, paths.policy_path, paths.broker_info_path,
               paths.machine_id_path, paths.evidence_root, paths.chrome_profile_dir):
        assert fn().is_relative_to(home)
```

`hands/tests/test_config.py`:

```python
from firekeep_hands import config

def test_defaults_when_no_files():
    cfg = config.load_config()
    assert (cfg.chord, cfg.deny_chord, cfg.permit_ttl_s, cfg.max_steps) == ("ctrl+alt+y", "ctrl+alt+n", 60, 400)
    pol = config.load_policy()
    assert (pol.apps, pol.domains, pol.remembered) == ([], [], [])

def test_roundtrip_and_unknown_keys_survive(isolated_home):
    cfg = config.load_config(); cfg.chord = "ctrl+alt+u"; config.save_config(cfg)
    assert config.load_config().chord == "ctrl+alt+u"
    pol = config.load_policy(); pol.domains.append("example.com")
    pol.remembered.append(config.Remembered(cls="send", app="Mail", match="Send", until="2099-01-01T00:00:00Z"))
    config.save_policy(pol)
    again = config.load_policy()
    assert again.domains == ["example.com"] and again.remembered[0].cls == "send"

def test_corrupt_policy_is_treated_as_empty_not_fatal(isolated_home):
    p = isolated_home / "hands" / "policy.json"; p.parent.mkdir(parents=True); p.write_text("{nope")
    assert config.load_policy().apps == []
```

`hands/tests/test_ids.py`:

```python
from firekeep_hands import ids

def test_machine_id_is_stable_and_private(isolated_home):
    a = ids.machine_id(); b = ids.machine_id()
    assert a == b and len(a) == 32 and int(a, 16) >= 0

def test_action_hash_is_order_independent():
    assert ids.action_hash({"kind": "click", "ref": "c1"}) == ids.action_hash({"ref": "c1", "kind": "click"})
    assert len(ids.action_hash({"kind": "wait"})) == 16

def test_challenge_id_is_deterministic_and_sensitive_to_every_field():
    base = ids.challenge_id_for("m", "s", "t", 3, "abcd")
    assert base == ids.challenge_id_for("m", "s", "t", 3, "abcd") and len(base) == 32
    for variant in [("x","s","t",3,"abcd"), ("m","x","t",3,"abcd"), ("m","s","x",3,"abcd"),
                    ("m","s","t",4,"abcd"), ("m","s","t",3,"abce")]:
        assert ids.challenge_id_for(*variant) != base
```

`hands/tests/test_fake_backend.py`:

```python
from firekeep_hands.backends.base import Control, Rect, WindowInfo
from firekeep_hands.backends.fake import FakeBackend

def _scene():
    return [Control("c1", "Button", "Save", "", Rect(10, 10, 80, 30), "Notepad", ("Invoke",)),
            Control("c2", "Edit", "Text Editor", "", Rect(0, 50, 600, 400), "Notepad", ("Value",))]

def test_observe_find_invoke_and_set_value_are_recorded():
    be = FakeBackend(_scene(), WindowInfo("Notepad", "Untitled - Notepad", 1, Rect(0, 0, 800, 600)))
    obs = be.observe(app=None, region=None, max_nodes=200, text_budget=4000, screenshot=False, max_width=1280)
    assert [c.ref for c in obs.controls] == ["c1", "c2"] and obs.generation == 1
    assert be.find("save", role=None, app=None, limit=5)[0].ref == "c1"
    be.invoke(obs.controls[0]); be.set_value(obs.controls[1], "hello")
    assert be.calls[-2:] == [("invoke", "c1"), ("set_value", "c2", "hello")] and be.values["c2"] == "hello"

def test_max_nodes_truncates():
    be = FakeBackend(_scene())
    obs = be.observe(app=None, region=None, max_nodes=1, text_budget=4000, screenshot=False, max_width=1280)
    assert len(obs.controls) == 1 and obs.truncated is True
```

- [ ] **Step 3: Run to verify they fail**

Run: `cd hands && pip install -e ".[test]" && python -m pytest -q`
Expected: FAIL with import errors.

- [ ] **Step 4: Implement**

`paths.py` — thin functions over `from firekeep_client import resolver` (`resolver._config_path().parent / "hands"`); `hands_home()` creates the directory with `mkdir(parents=True, exist_ok=True)`.

`config.py` — `dataclasses.asdict` to JSON, atomic write (temp + `os.replace`), private permissions via `firekeep_client.state._private(path)`; `load_*` return defaults on missing/corrupt files (log with `firekeep_client.hooklog.log_failure("hands", …)`), unknown keys preserved by storing the raw dict on the instance (`_extra`) and merging on save.

`ids.py` — `machine_id()` reads the file or writes `secrets.token_hex(16)` privately; hashes per the interface block.

`backends/base.py` — the dataclasses and protocol as specified; `Rect.center()` returns `(x + w // 2, y + h // 2)`.

`backends/fake.py` — deterministic: `observe` copies the scene (respecting `max_nodes`, setting `truncated`), increments `generation`, `find` is case-insensitive substring over `name`/`value` (optionally filtered by `role`), every mutating method appends to `calls`, `focus_app/open_app` return True and record, `clipboard_*` hold a string, `permissions()` returns `{"accessibility": "ok", "screen": "ok", "input": "ok"}` unless overridden.

`backends/__init__.py`:

```python
def load_backend():
    if sys.platform == "win32":
        from .win import WinBackend; return WinBackend()
    if sys.platform == "darwin":
        from .mac import MacBackend; return MacBackend()
    return UnsupportedBackend()
```

(`UnsupportedBackend` defined in `base.py`: `name = "unsupported"`, `permissions()` returns all `"missing"`, every other method raises `HandsError("unsupported", "Hands supports Windows and macOS; this is " + sys.platform)`.)

- [ ] **Step 5: Run, then commit**

Run: `cd hands && python -m pytest -q` → PASS.

```bash
git add hands/
git commit -m "feat(hands): wheel scaffold — paths, config, ids, backend protocol, fake backend"
```

---

### Task 4: Policy (protected classes, allowlist) and routing

**Files:**
- Create: `hands/src/firekeep_hands/policy.py`, `hands/src/firekeep_hands/routing.py`
- Test: `hands/tests/test_policy.py`, `hands/tests/test_routing.py`

**Interfaces:**
- Consumes: `Control`, `Rect`, `Observation`, `HandsError`, `Policy`, `Remembered`.
- Produces:

```python
# policy.py
CLASSES = ("send", "money", "destroy", "credential", "install", "boundary")
@dataclass(frozen=True)
class Decision: verdict: str; classes: tuple[str, ...]; reason: str   # verdict: "allow" | "permit"
def classify(action: dict, control: Control | None, window: WindowInfo | None, url: str | None, policy: Policy, task_apps: list[str]) -> tuple[str, ...]
def decide(action, control, window, url, policy, task_apps, now: datetime | None = None) -> Decision
def remember(policy: Policy, cls: str, app: str, match: str, days: int = 30, now=None) -> None
def host_allowed(policy: Policy, url: str) -> bool     # exact host or parent-domain match against policy.domains

# routing.py
@dataclass(frozen=True)
class Routed: kind: str; route: str; control: Control | None; point: tuple[int, int] | None; payload: dict
def route(action: dict, observation: Observation | None) -> Routed
```

Action union (the only shapes `route` accepts; anything else raises `HandsError("invalid_action", …)`):

| kind | required | optional | route chosen |
|---|---|---|---|
| `invoke` | `ref` | | `accessibility` if `"Invoke"`/`"AXPress"` in patterns else `pixel` (click centre) |
| `set_value` | `ref`, `value` | | `accessibility` if `"Value"`/`"AXValue"` in patterns else `pixel+type` (click centre, select-all, type) |
| `click` | `ref` | `button` (left/right), `double` | `pixel` (centre of the control's rect) |
| `type` | `text` | | `input` |
| `key` | `chord` | | `shortcut` |
| `scroll` | `ref` or `"window"`, `dy` | | `pixel` |
| `focus_app` | `app` | | `os` |
| `open_app` | `app` | | `os` |
| `open_url` | `url` | | `browser` |
| `clipboard_set` | `text` | | `os` |
| `wait` | `seconds` (≤ 10) | | `none` |

`route` raises `HandsError("stale_ref")` when `observation is None` or the `ref` is not in `observation.controls`, and `HandsError("invalid_action")` when the dict carries `x`/`y`/`point`/`coordinates` keys (model-supplied coordinates are rejected by construction).

Classifier rules (all case-insensitive, word-boundary regexes on `control.name` + `control.value`, window title, and the action payload):

| class | triggers |
|---|---|
| `send` | invoke/click on `\b(send|post|publish|submit|reply|tweet|share)\b`; `key` chord `ctrl+enter`/`cmd+enter`/`cmd+shift+d` |
| `money` | `\b(pay|buy|purchase|checkout|transfer|donate|place order|order now|confirm payment|subscribe)\b` |
| `destroy` | `\b(delete|remove|erase|format|uninstall|empty (the )?(trash|recycle bin)|discard|shred|factory reset|permanently)\b`; `key` chord `delete`/`shift+delete`/`cmd+backspace`/`cmd+delete` while the window app is Explorer/Finder |
| `credential` | `type`/`set_value` when `control.role in {"PasswordBox","AXSecureTextField"}` or name/value matches `\b(password|passcode|passphrase|otp|2fa|verification code|secret|api key|token)\b`; `clipboard_set` of a string that matches `^[A-Za-z0-9_\-]{32,}$` |
| `install` | `\b(install|run as administrator|allow access|grant|enable extension|add extension|trust this)\b`; `open_app` whose target ends in `.msi/.exe/.pkg/.dmg/.app` path outside `policy.apps` |
| `boundary` | `open_url` / browser navigate to a host `not host_allowed`; `open_app`/`focus_app` to an app not in `task_apps` and not in `policy.apps` |

`decide`: `classes = classify(...)`; drop any class with a live `Remembered` entry (`cls` equal, `app` equal to `window.app` or `"*"`, `match` a case-insensitive substring of `control.name`/url, `until > now`); verdict `"permit"` if any class remains else `"allow"`; `reason` names the class and the matched text.

- [ ] **Step 1: Failing tests**

`hands/tests/test_policy.py` (table-driven — write all rows):

```python
import datetime as dt
import pytest
from firekeep_hands import policy
from firekeep_hands.config import Policy, Remembered
from firekeep_hands.backends.base import Control, Rect, WindowInfo

W = WindowInfo("Mail", "Inbox — Mail", 1, Rect(0, 0, 800, 600))
def C(name, role="Button", value="", app="Mail", patterns=("Invoke",)):
    return Control("r", role, name, value, Rect(0, 0, 10, 10), app, patterns)

@pytest.mark.parametrize("action,control,url,expected", [
    ({"kind": "invoke", "ref": "r"}, C("Send"), None, ("send",)),
    ({"kind": "click", "ref": "r"}, C("Place order"), None, ("money",)),
    ({"kind": "invoke", "ref": "r"}, C("Delete permanently"), None, ("destroy",)),
    ({"kind": "set_value", "ref": "r", "value": "x"}, C("Password", role="PasswordBox", patterns=("Value",)), None, ("credential",)),
    ({"kind": "type", "text": "hunter2"}, C("Password", role="Edit"), None, ("credential",)),
    ({"kind": "invoke", "ref": "r"}, C("Install"), None, ("install",)),
    ({"kind": "open_url", "url": "https://evil.example/x"}, None, "https://evil.example/x", ("boundary",)),
    ({"kind": "invoke", "ref": "r"}, C("Save"), None, ()),
    ({"kind": "key", "chord": "ctrl+enter"}, None, None, ("send",)),
    ({"kind": "wait", "seconds": 1}, None, None, ()),
])
def test_classify_table(action, control, url, expected):
    assert policy.classify(action, control, W, url, Policy([], [], []), ["Mail"]) == expected

def test_allowlisted_domain_is_not_a_boundary():
    pol = Policy([], ["example.com"], [])
    assert policy.classify({"kind": "open_url", "url": "https://docs.example.com/a"}, None, W, "https://docs.example.com/a", pol, []) == ()

def test_app_outside_task_apps_is_a_boundary_unless_allowlisted():
    assert policy.classify({"kind": "open_app", "app": "Terminal"}, None, W, None, Policy([], [], []), ["Mail"]) == ("boundary",)
    assert policy.classify({"kind": "open_app", "app": "Terminal"}, None, W, None, Policy(["Terminal"], [], []), ["Mail"]) == ()

def test_remembered_approval_downgrades_to_allow_until_expiry():
    now = dt.datetime(2026, 9, 5, tzinfo=dt.timezone.utc)
    pol = Policy([], [], [Remembered("send", "Mail", "send", "2026-10-01T00:00:00Z")])
    d = policy.decide({"kind": "invoke", "ref": "r"}, C("Send"), W, None, pol, ["Mail"], now=now)
    assert d.verdict == "allow"
    late = dt.datetime(2026, 10, 2, tzinfo=dt.timezone.utc)
    assert policy.decide({"kind": "invoke", "ref": "r"}, C("Send"), W, None, pol, ["Mail"], now=late).verdict == "permit"

def test_remember_writes_a_30_day_entry():
    pol = Policy([], [], []); now = dt.datetime(2026, 9, 5, tzinfo=dt.timezone.utc)
    policy.remember(pol, "money", "Amazon", "place order", now=now)
    assert pol.remembered[0].until == "2026-10-05T00:00:00Z"
```

`hands/tests/test_routing.py`:

```python
import pytest
from firekeep_hands import routing
from firekeep_hands.backends.base import Control, HandsError, Observation, Rect

def _obs():
    return Observation(1, None, [
        Control("b", "Button", "OK", "", Rect(100, 100, 50, 20), "App", ("Invoke",)),
        Control("e", "Edit", "Name", "", Rect(0, 0, 200, 30), "App", ("Value",)),
        Control("p", "Pane", "Canvas", "", Rect(0, 0, 400, 400), "App", ()),
    ], "", None, False)

def test_invoke_prefers_accessibility_and_click_uses_centre():
    r = routing.route({"kind": "invoke", "ref": "b"}, _obs())
    assert (r.route, r.point) == ("accessibility", None)
    r = routing.route({"kind": "click", "ref": "b"}, _obs())
    assert (r.route, r.point) == ("pixel", (125, 110))

def test_invoke_without_pattern_falls_back_to_pixel():
    assert routing.route({"kind": "invoke", "ref": "p"}, _obs()).route == "pixel"

def test_set_value_routes_by_pattern():
    assert routing.route({"kind": "set_value", "ref": "e", "value": "x"}, _obs()).route == "accessibility"
    assert routing.route({"kind": "set_value", "ref": "p", "value": "x"}, _obs()).route == "pixel+type"

@pytest.mark.parametrize("bad", [
    {"kind": "click", "x": 10, "y": 10},
    {"kind": "click", "ref": "b", "point": [1, 2]},
    {"kind": "teleport"},
    {"kind": "wait", "seconds": 99},
    {"kind": "click"},
])
def test_invalid_actions_are_rejected(bad):
    with pytest.raises(HandsError) as ei:
        routing.route(bad, _obs())
    assert ei.value.code == "invalid_action"

def test_unknown_or_stale_ref_is_rejected():
    with pytest.raises(HandsError) as ei:
        routing.route({"kind": "click", "ref": "zzz"}, _obs())
    assert ei.value.code == "stale_ref"
    with pytest.raises(HandsError):
        routing.route({"kind": "click", "ref": "b"}, None)

def test_scroll_window_needs_no_ref():
    r = routing.route({"kind": "scroll", "ref": "window", "dy": -3}, _obs())
    assert r.route == "pixel" and r.point is None and r.payload["dy"] == -3
```

- [ ] **Step 2: Run to verify they fail** — `cd hands && python -m pytest tests/test_policy.py tests/test_routing.py -q` → import errors.

- [ ] **Step 3: Implement `policy.py` and `routing.py`** exactly per the tables above. Keep regexes as module constants (`_SEND_RE`, …) so the guide can quote them. `host_allowed` parses with `urllib.parse.urlsplit`, lowercases, and matches `host == d or host.endswith("." + d)`.

- [ ] **Step 4: Run** → PASS. **Commit:**

```bash
git add hands/src/firekeep_hands/policy.py hands/src/firekeep_hands/routing.py hands/tests/test_policy.py hands/tests/test_routing.py
git commit -m "feat(hands): protected-class policy with a remembered allowlist, and deterministic routing"
```

---

### Task 5: Evidence ledger and the Keep link

**Files:**
- Create: `hands/src/firekeep_hands/evidence.py`, `hands/src/firekeep_hands/keep.py`
- Test: `hands/tests/test_evidence.py`, `hands/tests/test_keep.py`

**Interfaces:**
- Consumes: `paths.evidence_root()`, `firekeep_client.hooks._mcp.call_tool(service, tool, arguments, timeout=…)`, `firekeep_client.transport.TransportError`.
- Produces:

```python
# evidence.py
class Ledger:
    def __init__(self, task_id: str, *, goal: str, apps: list[str], machine_id: str, session_id: str)   # creates dir + task.json
    dir: Path
    def record(self, *, step_index: int, action: dict, route: str, classes: tuple[str, ...], permit: dict | None,
               before_png: bytes | None, after_png: bytes | None, outcome: str, error: str | None) -> dict   # returns the line written
    def close(self, outcome: str, summary: str) -> None            # updates task.json
    def steps(self) -> list[dict]
def prune(root: Path, *, older_than_days: int, now=None) -> int   # returns count removed; reads task.json "started"

# keep.py
class KeepLink:
    def __init__(self, *, agent_id: str, machine_id: str, offline: bool | None = None)   # offline defaults to env FIREKEEP_HANDS_OFFLINE == "1"
    def action_before(self, *, goal: str, task_id: str, apps: list[str]) -> str | None    # cortex action_before(action_type="hands_task", target=f"desktop:{machine_id}", intent=goal, success_criteria="task ends with outcome=done", confidence=0.6) -> action_id
    def action_after(self, action_id: str | None, outcome: str, summary: str) -> None
    def acquire_lease(self, ttl_minutes: int = 30) -> dict | None   # relay_lease(resource_id=f"hands:{machine_id}", agent_id, ttl_minutes)
    def renew_lease(self) -> None
    def release_lease(self) -> None                                 # relay_release(resource_id, agent_id, fencing_token)
    def post_permit_task(self, *, challenge: str, title: str, classes: tuple[str, ...], task_id: str, step_index: int, expires_at: str) -> str | None   # relay_task_post(title=f"hands_permit:{challenge}", assigner=agent_id, description=…, priority="high", context=json)
    def permit_task_state(self, challenge: str) -> str | None       # relay_task_list(title=f"hands_permit:{challenge}", limit=1) -> "approve"|"deny"|"pending"|None
    def close_permit_task(self, task_id: str, result: str) -> None  # relay_task_update(task_id, status="cancelled", result=result)
```

Every `KeepLink` method: best-effort, 5 s timeout, returns `None`/no-op on `TransportError`/`Exception` after `hooklog.log_failure("hands", …)`; when `offline` every method returns `None` without a network call. `permit_task_state` maps relay status: `completed` + result starting with `approve` → `"approve"`; `completed` otherwise, `cancelled` or `failed` → `"deny"`; `pending`/`in-progress` → `"pending"`.

- [ ] **Step 1: Failing tests**

`hands/tests/test_evidence.py`:

```python
import datetime as dt, hashlib, json
from firekeep_hands import evidence, paths

def test_ledger_writes_chained_lines_and_images(isolated_home):
    led = evidence.Ledger("t1", goal="g", apps=["Notepad"], machine_id="m", session_id="s")
    assert (led.dir / "task.json").exists() and led.dir.parent == paths.evidence_root()
    l1 = led.record(step_index=0, action={"kind": "wait", "seconds": 1}, route="none", classes=(), permit=None,
                    before_png=b"\x89PNG1", after_png=None, outcome="ok", error=None)
    l2 = led.record(step_index=1, action={"kind": "key", "chord": "ctrl+s"}, route="shortcut", classes=(), permit=None,
                    before_png=None, after_png=b"\x89PNG2", outcome="ok", error=None)
    lines = (led.dir / "steps.jsonl").read_text().splitlines()
    assert len(lines) == 2 and (led.dir / "000-before.png").read_bytes() == b"\x89PNG1" and (led.dir / "001-after.png").exists()
    assert l1["before"] == hashlib.sha256(b"\x89PNG1").hexdigest() and l1["after"] is None
    body1 = json.dumps({k: v for k, v in l1.items() if k != "chain"}, sort_keys=True, separators=(",", ":"))
    assert l1["chain"] == hashlib.sha256(("" + body1).encode()).hexdigest()
    body2 = json.dumps({k: v for k, v in l2.items() if k != "chain"}, sort_keys=True, separators=(",", ":"))
    assert l2["chain"] == hashlib.sha256((l1["chain"] + body2).encode()).hexdigest()
    led.close("done", "saved")
    assert json.loads((led.dir / "task.json").read_text())["outcome"] == "done"

def test_prune_removes_only_old_tasks(isolated_home):
    root = paths.evidence_root(); root.mkdir(parents=True)
    for name, started in (("old", "2026-01-01T00:00:00Z"), ("new", "2026-09-04T00:00:00Z")):
        d = root / name; d.mkdir(); (d / "task.json").write_text(json.dumps({"started": started}))
    now = dt.datetime(2026, 9, 5, tzinfo=dt.timezone.utc)
    assert evidence.prune(root, older_than_days=14, now=now) == 1
    assert not (root / "old").exists() and (root / "new").exists()
```

`hands/tests/test_keep.py`:

```python
from firekeep_client import transport
from firekeep_hands import keep

def test_offline_makes_every_call_a_noop(monkeypatch):
    called = []
    monkeypatch.setattr(keep, "call_tool", lambda *a, **k: called.append(a))
    link = keep.KeepLink(agent_id="a", machine_id="m", offline=True)
    assert link.action_before(goal="g", task_id="t", apps=[]) is None
    assert link.acquire_lease() is None and link.permit_task_state("c") is None
    assert called == []

def test_online_calls_map_to_the_right_tools(monkeypatch):
    seen = []
    def fake(service, tool, args, **kw):
        seen.append((service, tool, args))
        return {"cortex.action_before": {"action_id": "A1"},
                "relay.relay_lease": {"status": "acquired", "fencing_token": 7},
                "relay.relay_task_post": {"status": "created", "task": {"id": "task-1"}},
                "relay.relay_task_list": {"tasks": [{"id": "task-1", "status": "completed", "result": "approve"}]},
                }.get(f"{service}.{tool}", {})
    monkeypatch.setattr(keep, "call_tool", fake)
    link = keep.KeepLink(agent_id="a", machine_id="m", offline=False)
    assert link.action_before(goal="g", task_id="t", apps=["X"]) == "A1"
    assert link.acquire_lease()["fencing_token"] == 7
    assert link.post_permit_task(challenge="c", title="Send", classes=("send",), task_id="t", step_index=2, expires_at="x") == "task-1"
    assert link.permit_task_state("c") == "approve"
    link.release_lease(); link.action_after("A1", "done", "ok")
    tools = [(s, t) for s, t, _ in seen]
    assert tools == [("cortex", "action_before"), ("relay", "relay_lease"), ("relay", "relay_task_post"),
                     ("relay", "relay_task_list"), ("relay", "relay_release"), ("cortex", "action_after")]
    assert seen[1][2]["resource_id"] == "hands:m" and seen[4][2]["fencing_token"] == 7
    assert seen[2][2]["title"] == "hands_permit:c" and seen[3][2]["title"] == "hands_permit:c"

def test_transport_errors_are_swallowed(monkeypatch):
    def boom(*a, **k): raise transport.TransportError("down")
    monkeypatch.setattr(keep, "call_tool", boom)
    link = keep.KeepLink(agent_id="a", machine_id="m", offline=False)
    assert link.action_before(goal="g", task_id="t", apps=[]) is None
    assert link.permit_task_state("c") is None
```

- [ ] **Step 2: Run to verify they fail**, **Step 3: implement** (`keep.py` does `from firekeep_client.hooks._mcp import call_tool` at module top so tests can monkeypatch `keep.call_tool`), **Step 4: run** → PASS.

- [ ] **Step 5: Commit**

```bash
git add hands/src/firekeep_hands/evidence.py hands/src/firekeep_hands/keep.py hands/tests/test_evidence.py hands/tests/test_keep.py
git commit -m "feat(hands): local evidence ledger with a hash chain; Keep link for action_before/after, lease and permit tasks"
```

---

### Task 6: The approval broker — permits, loopback API, listeners, phone bridge, autostart

**Files:**
- Create: `hands/src/firekeep_hands/broker/__init__.py`, `permits.py`, `server.py`, `client.py`, `phone.py`, `autostart.py`, `__main__.py`, `listeners/__init__.py`, `listeners/win.py`, `listeners/mac.py`
- Test: `hands/tests/test_permits.py`, `hands/tests/test_broker_server.py`, `hands/tests/test_phone.py`, `hands/tests/test_listener_win.py`, `hands/tests/test_listener_mac.py`, `hands/tests/test_autostart.py`

**Interfaces:**

```python
# permits.py
@dataclass
class Permit: challenge: str; title: str; classes: tuple[str, ...]; task_id: str; step_index: int
              created: float; expires_at: float; state: str = "pending"; via: str | None = None; phone_task_id: str | None = None
class PermitStore:
    def __init__(self, *, ttl_s: int = 60, clock=time.monotonic)
    def request(self, *, challenge, title, classes, task_id, step_index) -> Permit   # idempotent per challenge while pending
    def get(self, challenge) -> Permit | None                                        # sweeps expiry first
    def decide_oldest(self, decision: str, via: str) -> Permit | None               # "approve"|"deny" on the oldest pending
    def decide(self, challenge, decision, via) -> Permit | None
    def consume(self, challenge) -> bool           # approved -> consumed, True once; anything else False
    def pending(self) -> list[Permit]

# server.py
class BrokerServer:
    def __init__(self, store: PermitStore, *, chord: str, listeners: dict[str, str])    # listeners: {"chord": "active|unavailable", "phone": "active|offline"}
    def start(self) -> tuple[int, str]   # binds 127.0.0.1:0, writes broker.json {port, token, pid, started_at, chord}, returns (port, token)
    def stop(self) -> None
# HTTP (Authorization: Bearer <token>; anything else -> 401):
#   GET  /health                          -> {"ok": true, "chord": "...", "listeners": {...}, "pending": n}
#   POST /permits {challenge,title,classes,task_id,step_index} -> permit json (201)
#   GET  /permits/<challenge>             -> permit json | 404
#   POST /permits/<challenge>/consume     -> {"state":"consumed"} | 409 {"state": <current>}
def run(argv) -> int   # builds store, listeners for this platform, phone bridge, server; blocks; SIGTERM/Ctrl+C -> clean stop

# client.py
class BrokerClient:
    @classmethod
    def from_disk(cls, timeout: float = 2.0) -> "BrokerClient | None"   # reads broker.json, GET /health; None if not running
    def request(self, **fields) -> dict
    def get(self, challenge) -> dict | None
    def wait(self, challenge, timeout_s: float) -> dict        # polls every 0.25 s until state != pending or timeout
    def consume(self, challenge) -> bool

# listeners/win.py  (import only on win32)
def kb_event_is_real(flags: int) -> bool          # not (flags & 0x10) and not (flags & 0x02)
class ChordTracker:                                # pure: feed(vk: int, down: bool, real: bool) -> "approve"|"deny"|None
    def __init__(self, approve: str, deny: str)
def run_listener(tracker: ChordTracker, on_decision: Callable[[str], None]) -> None   # installs WH_KEYBOARD_LL, pumps messages; thread-blocking

# listeners/mac.py  (import only on darwin)
def event_is_real(user_data: int, source_state_id: int) -> bool   # user_data != HANDS_TAG and source_state_id == kCGEventSourceStateHIDSystemState (1)
class ChordTracker: same as win but fed (keycode: int, flags: int, real: bool)
def run_listener(tracker, on_decision) -> None                   # CGEventTap listen-only, CFRunLoop

# phone.py
class PhoneBridge(threading.Thread):
    def __init__(self, store: PermitStore, link: KeepLink, poll_s: float = 3.0)
    # on new pending permit -> link.post_permit_task(...) (stores phone_task_id)
    # every poll: for each pending permit with a task -> link.permit_task_state(); "approve"/"deny" -> store.decide(challenge, …, via="phone")
    # on a permit leaving pending for any other reason -> link.close_permit_task(task_id, result=state)

# autostart.py
def install() -> None      # win32: schtasks /Create /TN FirekeepHandsBroker /TR "<Scripts>\firekeep-hands-broker.exe run" /SC ONLOGON /RL LIMITED /F ; then start it now (subprocess.Popen detached)
                           # darwin: write ~/Library/LaunchAgents/ai.firekeep.hands-broker.plist (ProgramArguments [<bin>/firekeep-hands-broker, run], RunAtLoad, KeepAlive) + launchctl bootstrap gui/<uid> <plist>
def uninstall() -> None    # schtasks /Delete /TN FirekeepHandsBroker /F ; launchctl bootout gui/<uid>/ai.firekeep.hands-broker + remove plist ; then kill the pid in broker.json if alive
def command_for(platform: str, script_path: str) -> list[str]   # pure, tested: the argv install() would run

# __main__.py
def main(argv=None) -> int   # run | install-autostart | uninstall-autostart | status
```

The chord parser (shared helper `parse_chord("ctrl+alt+y") -> (frozenset({"ctrl","alt"}), "y")`) lives in `broker/__init__.py`; Windows VK map: ctrl 0x11/0xA2/0xA3, alt 0x12/0xA4/0xA5, shift 0x10/0xA0/0xA1, letters `ord(ch.upper())`; macOS keycodes for letters from a static table (`y`=16, `n`=45, full a–z table in the module), flags `kCGEventFlagMaskControl=0x40000`, `Alternate=0x80000`, `Shift=0x20000`, `Command=0x100000`.

- [ ] **Step 1: Failing tests**

`hands/tests/test_permits.py`:

```python
from firekeep_hands.broker.permits import PermitStore

class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t

def _store():
    c = Clock(); return PermitStore(ttl_s=60, clock=c), c

def test_request_is_idempotent_and_expires():
    s, c = _store()
    p = s.request(challenge="c1", title="Send", classes=("send",), task_id="t", step_index=1)
    assert s.request(challenge="c1", title="Send", classes=("send",), task_id="t", step_index=1) is p
    c.t += 61
    assert s.get("c1").state == "expired"

def test_oldest_pending_is_the_one_a_chord_approves():
    s, c = _store()
    s.request(challenge="a", title="A", classes=("send",), task_id="t", step_index=1); c.t += 1
    s.request(challenge="b", title="B", classes=("money",), task_id="t", step_index=2)
    assert s.decide_oldest("approve", via="chord").challenge == "a"
    assert s.get("a").state == "approved" and s.get("a").via == "chord" and s.get("b").state == "pending"

def test_consume_is_one_use_and_only_after_approval():
    s, _ = _store()
    s.request(challenge="c", title="x", classes=("destroy",), task_id="t", step_index=0)
    assert s.consume("c") is False
    s.decide("c", "approve", via="phone")
    assert s.consume("c") is True and s.consume("c") is False and s.get("c").state == "consumed"

def test_denied_and_expired_cannot_be_consumed_or_reapproved():
    s, c = _store()
    s.request(challenge="d", title="x", classes=("send",), task_id="t", step_index=0)
    s.decide("d", "deny", via="chord")
    assert s.decide("d", "approve", via="chord") is None and s.consume("d") is False
    s.request(challenge="e", title="x", classes=("send",), task_id="t", step_index=1); c.t += 61
    assert s.decide("e", "approve", via="chord") is None
```

`hands/tests/test_broker_server.py` (real loopback server, stdlib client):

```python
import json, urllib.request, urllib.error
import pytest
from firekeep_hands import paths
from firekeep_hands.broker.permits import PermitStore
from firekeep_hands.broker.server import BrokerServer
from firekeep_hands.broker.client import BrokerClient

@pytest.fixture
def broker(isolated_home):
    store = PermitStore(ttl_s=60)
    srv = BrokerServer(store, chord="ctrl+alt+y", listeners={"chord": "unavailable", "phone": "offline"})
    port, token = srv.start()
    yield srv, store, port, token
    srv.stop()

def _req(port, token, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=2) as r: return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"null")

def test_health_requires_token_and_writes_broker_json(broker):
    srv, store, port, token = broker
    assert _req(port, "wrong", "GET", "/health")[0] == 401
    status, body = _req(port, token, "GET", "/health")
    assert status == 200 and body["ok"] is True and body["chord"] == "ctrl+alt+y"
    info = json.loads(paths.broker_info_path().read_text())
    assert info["port"] == port and info["token"] == token

def test_permit_lifecycle_over_http(broker):
    srv, store, port, token = broker
    status, p = _req(port, token, "POST", "/permits", {"challenge": "c", "title": "Send", "classes": ["send"], "task_id": "t", "step_index": 1})
    assert status == 201 and p["state"] == "pending"
    assert _req(port, token, "POST", "/permits/c/consume")[0] == 409
    store.decide("c", "approve", via="chord")           # what a listener does
    assert _req(port, token, "GET", "/permits/c")[1]["state"] == "approved"
    assert _req(port, token, "POST", "/permits/c/consume") == (200, {"state": "consumed"})
    assert _req(port, token, "POST", "/permits/c/consume")[0] == 409
    assert _req(port, token, "GET", "/permits/nope")[0] == 404

def test_client_from_disk_and_wait(broker):
    srv, store, port, token = broker
    c = BrokerClient.from_disk()
    assert c is not None
    c.request(challenge="w", title="x", classes=["send"], task_id="t", step_index=0)
    import threading, time
    threading.Timer(0.3, lambda: store.decide("w", "approve", via="chord")).start()
    assert c.wait("w", timeout_s=3)["state"] == "approved"
    assert c.consume("w") is True

def test_no_broker_json_means_no_client(isolated_home):
    assert BrokerClient.from_disk() is None
```

`hands/tests/test_phone.py`:

```python
from firekeep_hands.broker.permits import PermitStore
from firekeep_hands.broker.phone import PhoneBridge

class FakeLink:
    def __init__(self): self.posted = []; self.states = {}; self.closed = []
    def post_permit_task(self, **kw): self.posted.append(kw); return "task-" + kw["challenge"]
    def permit_task_state(self, challenge): return self.states.get(challenge, "pending")
    def close_permit_task(self, task_id, result): self.closed.append((task_id, result))

def test_bridge_posts_polls_and_decides():
    store = PermitStore(ttl_s=60); link = FakeLink()
    bridge = PhoneBridge(store, link, poll_s=0.01)
    store.request(challenge="c", title="Send", classes=("send",), task_id="t", step_index=1)
    bridge.tick()
    assert link.posted[0]["challenge"] == "c" and store.get("c").phone_task_id == "task-c"
    link.states["c"] = "approve"; bridge.tick()
    assert store.get("c").state == "approved" and store.get("c").via == "phone"

def test_bridge_closes_task_when_permit_resolves_elsewhere():
    store = PermitStore(ttl_s=60); link = FakeLink()
    bridge = PhoneBridge(store, link, poll_s=0.01)
    store.request(challenge="c", title="x", classes=("send",), task_id="t", step_index=1); bridge.tick()
    store.decide("c", "approve", via="chord"); bridge.tick()
    assert link.closed == [("task-c", "approved")]
```

`hands/tests/test_listener_win.py` (pure parts run everywhere):

```python
from firekeep_hands.broker.listeners.win import ChordTracker, kb_event_is_real

def test_injected_flags_are_not_real():
    assert kb_event_is_real(0x00) and kb_event_is_real(0x80)
    assert not kb_event_is_real(0x10) and not kb_event_is_real(0x02) and not kb_event_is_real(0x12)

def test_chord_requires_real_modifiers_and_trigger():
    t = ChordTracker("ctrl+alt+y", "ctrl+alt+n")
    assert t.feed(0xA2, True, True) is None and t.feed(0xA4, True, True) is None
    assert t.feed(ord("Y"), True, True) == "approve"
    assert t.feed(ord("Y"), False, True) is None
    assert t.feed(ord("N"), True, True) == "deny"

def test_injected_events_never_count_even_for_modifiers():
    t = ChordTracker("ctrl+alt+y", "ctrl+alt+n")
    t.feed(0xA2, True, False); t.feed(0xA4, True, False)
    assert t.feed(ord("Y"), True, False) is None
    assert t.feed(ord("Y"), True, True) is None      # modifiers were injected, so they do not count
```

`hands/tests/test_listener_mac.py`:

```python
from firekeep_hands import HANDS_TAG
from firekeep_hands.broker.listeners.mac import ChordTracker, event_is_real, KEYCODES, FLAG_CONTROL, FLAG_ALT

def test_tagged_or_non_hid_events_are_not_real():
    assert event_is_real(0, 1)
    assert not event_is_real(HANDS_TAG, 1) and not event_is_real(0, 0)

def test_chord_from_flags_and_keycode():
    t = ChordTracker("ctrl+alt+y", "ctrl+alt+n")
    assert t.feed(KEYCODES["y"], FLAG_CONTROL | FLAG_ALT, True) == "approve"
    assert t.feed(KEYCODES["n"], FLAG_CONTROL | FLAG_ALT, True) == "deny"
    assert t.feed(KEYCODES["y"], FLAG_CONTROL, True) is None
    assert t.feed(KEYCODES["y"], FLAG_CONTROL | FLAG_ALT, False) is None
```

`hands/tests/test_autostart.py`:

```python
from firekeep_hands.broker import autostart

def test_windows_command_is_a_logon_task_at_limited_rights():
    argv = autostart.command_for("win32", r"C:\v\Scripts\firekeep-hands-broker.exe")
    assert argv[:3] == ["schtasks", "/Create", "/TN"] and "FirekeepHandsBroker" in argv
    assert "/SC" in argv and argv[argv.index("/SC") + 1] == "ONLOGON"
    assert "/RL" in argv and argv[argv.index("/RL") + 1] == "LIMITED"

def test_macos_plist_content():
    plist = autostart.launch_agent_plist("/v/bin/firekeep-hands-broker")
    assert "ai.firekeep.hands-broker" in plist and "<string>run</string>" in plist and "RunAtLoad" in plist
```

- [ ] **Step 2: Run to verify they fail** — `cd hands && python -m pytest tests/test_permits.py tests/test_broker_server.py tests/test_phone.py tests/test_listener_win.py tests/test_listener_mac.py tests/test_autostart.py -q`.

- [ ] **Step 3: Implement**

`permits.py` per interface (a `threading.Lock` around every method; `_sweep()` marks `expired` where `clock() >= expires_at` and state is `pending` or `approved`).

`server.py`: `ThreadingHTTPServer(("127.0.0.1", 0), Handler)` with `daemon_threads = True`; token `secrets.token_urlsafe(32)`; `broker.json` written atomically and `state._private`d; handler checks the bearer on every route, rejects bodies over 16 KiB, answers JSON. `run(argv)`:

```python
def run(argv) -> int:
    cfg = load_config()
    store = PermitStore(ttl_s=cfg.permit_ttl_s)
    listeners = {"chord": "unavailable", "phone": "offline"}
    threads = []
    if sys.platform == "win32":
        from .listeners.win import ChordTracker, run_listener
    elif sys.platform == "darwin":
        from .listeners.mac import ChordTracker, run_listener
    else:
        ChordTracker = run_listener = None
    if run_listener:
        tracker = ChordTracker(cfg.chord, cfg.deny_chord)
        t = threading.Thread(target=run_listener, args=(tracker, lambda d: store.decide_oldest(d, via="chord")), daemon=True, name="hands-chord")
        t.start(); threads.append(t); listeners["chord"] = "active"
    link = KeepLink(agent_id=os.environ.get("NEXUS_AGENT_ID") or f"hands-{machine_id()[:8]}", machine_id=machine_id())
    if not link.offline:
        PhoneBridge(store, link).start(); listeners["phone"] = "active"
    srv = BrokerServer(store, chord=cfg.chord, listeners=listeners)
    port, _ = srv.start()
    log.info("firekeep-hands-broker listening on 127.0.0.1:%s", port)
    try:
        signal.pause() if hasattr(signal, "pause") else threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop()
    return 0
```

`listeners/win.py` — ctypes (verified in the 2026-09-05 probe: `LLKHF_INJECTED` is set on `SendInput` events; declare `argtypes`):

```python
import ctypes
WH_KEYBOARD_LL = 13; WM_KEYDOWN = 0x0100; WM_SYSKEYDOWN = 0x0104; WM_KEYUP = 0x0101; WM_SYSKEYUP = 0x0105
LLKHF_LOWER_IL_INJECTED = 0x02; LLKHF_INJECTED = 0x10

class KBDLLHOOKSTRUCT(ctypes.Structure):          # fixed-width: importable and correct on every host
    _fields_ = [("vkCode", ctypes.c_uint32), ("scanCode", ctypes.c_uint32), ("flags", ctypes.c_uint32),
                ("time", ctypes.c_uint32), ("dwExtraInfo", ctypes.c_size_t)]

def kb_event_is_real(flags: int) -> bool:
    return not (flags & LLKHF_INJECTED) and not (flags & LLKHF_LOWER_IL_INJECTED)

def run_listener(tracker, on_decision):
    # Everything Win32 is resolved HERE, not at import: this module's pure parts
    # (kb_event_is_real, ChordTracker) are unit-tested on Linux CI and macOS.
    import ctypes.wintypes as w
    HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, w.WPARAM, w.LPARAM)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetModuleHandleW.argtypes = [w.LPCWSTR]; kernel32.GetModuleHandleW.restype = w.HMODULE
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, w.HINSTANCE, w.DWORD]; user32.SetWindowsHookExW.restype = w.HHOOK
    user32.CallNextHookEx.argtypes = [w.HHOOK, ctypes.c_int, w.WPARAM, w.LPARAM]; user32.CallNextHookEx.restype = ctypes.c_ssize_t
    user32.GetMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]

    def proc(nCode, wParam, lParam):
        if nCode >= 0:
            ks = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            down = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
            decision = tracker.feed(ks.vkCode, down, kb_event_is_real(ks.flags))
            if decision:
                on_decision(decision)
        return user32.CallNextHookEx(None, nCode, wParam, lParam)
    cb = HOOKPROC(proc)                      # keep a reference for the life of the hook
    # hMod=None is what the 2026-09-05 probe used and what was seen to work
    # (a low-level hook runs in the installing process; no DLL handle needed).
    hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, cb, None, 0)
    if not hook:
        raise OSError(ctypes.get_last_error())
    msg = w.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg)); user32.DispatchMessageW(ctypes.byref(msg))
```

(Drop the `kernel32`/`GetModuleHandleW` lines above if unused. The probe declared `HOOKPROC` with `CFUNCTYPE(c_long, …)` and it worked; `WINFUNCTYPE(c_ssize_t, …)` is the documented calling convention/LRESULT width on x64 — keep `WINFUNCTYPE`, and if the live test in Task 15 shows the hook not firing, fall back to the probe's exact declaration.)

`ChordTracker.feed(vk, down, real)`: injected events are ignored entirely (return None, no state change); real modifier down/up toggles `held`; a real trigger-key down with `held ⊇ required` returns the decision.

`listeners/mac.py` — `Quartz.CGEventTapCreate(kCGSessionEventTap, kCGHeadInsertEventTap, kCGEventTapOptionListenOnly, CGEventMaskBit(kCGEventKeyDown), callback, None)`; in the callback read `CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)`, `CGEventGetFlags(event)`, `CGEventGetIntegerValueField(event, kCGEventSourceUserData)` and `…(event, kCGEventSourceStateID)`; `event_is_real` as specified; run loop via `CFRunLoopAddSource` + `CFRunLoopRun()`. If `CGEventTapCreate` returns None (no Input Monitoring permission) raise `PermissionError("Input Monitoring permission missing")` — `run()` catches it and reports `listeners["chord"] = "unavailable"`.

`phone.py` — a `threading.Thread` whose `run()` loops `tick(); sleep(poll_s)`; `tick()` is the tested unit; store the mapping in `Permit.phone_task_id`.

`autostart.py` — `command_for`, `launch_agent_plist`, `install`, `uninstall` per the interface (the console script path is `Path(sys.executable).parent / ("firekeep-hands-broker.exe" if win32 else "firekeep-hands-broker")`). `install()` also starts the broker immediately via `subprocess.Popen([...,"run"], creationflags=DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP on Windows, start_new_session=True elsewhere, stdin/stdout/stderr=DEVNULL)` unless a live broker already answers.

`__main__.py`: argparse with the four sub-commands; `status` prints `/health` or "not running".

- [ ] **Step 4: Run** → PASS. **Commit:**

```bash
git add hands/src/firekeep_hands/broker hands/tests/test_permits.py hands/tests/test_broker_server.py hands/tests/test_phone.py hands/tests/test_listener_win.py hands/tests/test_listener_mac.py hands/tests/test_autostart.py
git commit -m "feat(hands): approval broker — one-use permits, loopback API, real-input chord listeners, phone bridge, autostart"
```

---

### Task 7: Windows backend

**Files:**
- Create: `hands/src/firekeep_hands/backends/win.py`, `hands/src/firekeep_hands/backends/_win_input.py` (ctypes `SendInput`, separated so its struct tests need no `uiautomation`)
- Test: `hands/tests/test_win_input.py` (runs on any platform for the pure struct checks), `hands/tests/test_win_backend.py` (`pytest.importorskip("uiautomation")`), `hands/tests/live/test_win_notepad.py` (skipped unless `FIREKEEP_HANDS_LIVE=1`)

**Interfaces:**
- Produces: `WinBackend()` implementing `Backend`; `_win_input.INPUT` (size 40 on 64-bit: `type` + union of `MOUSEINPUT`/`KEYBDINPUT`/`HARDWAREINPUT`), `send_key_chord(chord)`, `send_text(text)` (Unicode via `KEYEVENTF_UNICODE`), `send_click(x, y, button, double)`, `send_scroll(x, y, dy)`, every `INPUT.dwExtraInfo = HANDS_TAG`.

Control refs: `"w<hwnd-hex>:<runtime-id joined by '.'>"`; `observe` walks `uiautomation.GetForegroundControl()`'s top-level window (or the window whose `ProcessName`/`Name` matches `app`) depth-first with a node cap, keeping controls whose `ControlType` is interactive (Button, Edit, ComboBox, CheckBox, RadioButton, MenuItem, ListItem, TreeItem, TabItem, Hyperlink, Document, Text-with-name ≤ 80 chars) and on-screen (`BoundingRectangle` non-empty and inside the window/region). `patterns` = which of `InvokePattern`, `ValuePattern`, `TogglePattern`, `SelectionItemPattern`, `ExpandCollapsePattern`, `ScrollPattern` the element supports (`GetPattern(...)` non-None → name without "Pattern"). Text = window title + first `text_budget` chars of the concatenated names/values. Screenshot via `mss` of the window rect, downscaled with Pillow (`thumbnail((max_width, 10**6))`), PNG bytes. `WindowInfo.elevated` = `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` fails with `ERROR_ACCESS_DENIED (5)` **or** the window's process is `consent.exe`/has `IsImmersiveProcess` false and `GetTokenInformation(TokenElevation)` unavailable → `True`; `invoke/click` on an elevated window raises `HandsError("elevated_target")`. `focus_app` = find the window, `SetForegroundWindow`, and if that fails `AttachThreadInput(current, target, True)` then retry. `open_app` = `os.startfile(app)` for a path, else `subprocess.Popen(["cmd", "/c", "start", "", app])` for a Store/AppUserModelId or command name (`shell:AppsFolder\…` accepted). Clipboard via `OpenClipboard/GetClipboardData(CF_UNICODETEXT)` / `SetClipboardData` (ctypes).

- [ ] **Step 1: Failing struct tests (any platform)**

`hands/tests/test_win_input.py`:

```python
import ctypes, sys
import pytest
from firekeep_hands import HANDS_TAG
from firekeep_hands.backends import _win_input as wi

def test_input_struct_matches_win32_layout():
    assert ctypes.sizeof(wi.INPUT) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
    assert ctypes.sizeof(wi.MOUSEINPUT) == (32 if ctypes.sizeof(ctypes.c_void_p) == 8 else 24)

def test_every_built_event_carries_the_hands_tag():
    for inp in wi.build_key_chord("ctrl+alt+y") + wi.build_text("hé") + wi.build_click(10, 20, "left", False) + wi.build_scroll(1, 2, -3):
        assert inp.union.ki.dwExtraInfo == HANDS_TAG or inp.union.mi.dwExtraInfo == HANDS_TAG

def test_chord_builds_press_and_release_in_order():
    seq = wi.build_key_chord("ctrl+s")
    vks = [(i.union.ki.wVk, bool(i.union.ki.dwFlags & wi.KEYEVENTF_KEYUP)) for i in seq]
    assert vks == [(0x11, False), (ord("S"), False), (ord("S"), True), (0x11, True)]

@pytest.mark.skipif(sys.platform != "win32", reason="SendInput is Win32")
def test_send_returns_count_for_an_empty_batch():
    assert wi.send([]) == 0
```

`hands/tests/test_win_backend.py` (unit, monkeypatched `uiautomation` module — build a tiny fake with `GetForegroundControl`, `Control` objects exposing `ControlTypeName`, `Name`, `BoundingRectangle` (left, top, right, bottom), `GetChildren()`, `GetInvokePattern()`, `GetValuePattern()`, `GetRuntimeId()`, `NativeWindowHandle`, `ProcessId`): assert `observe` yields refs/rects/patterns as specified, `max_nodes` truncates, `find("save")` matches by name, `invoke` calls `Invoke()` on the fake pattern, `set_value` calls `SetValue`.

`hands/tests/live/test_win_notepad.py` (skipped unless `FIREKEEP_HANDS_LIVE=1` and win32): open Notepad, `find("Text Editor")`, `set_value` or `type_text("hands live")`, `key("ctrl+a")`, assert the clipboard after `ctrl+c` contains `hands live`, close with `alt+f4` + `key("n")`.

- [ ] **Step 2: Run to verify they fail**, **Step 3: implement `_win_input.py` and `win.py`**

`_win_input.py` core (from the verified probe):

```python
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0002, 0x0004
MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE = 0x0001, 0x8000
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP, MOUSEEVENTF_WHEEL = 0x0002, 0x0004, 0x0008, 0x0010, 0x0800

# Fixed-width fields on purpose: wintypes.LONG is c_long, which is 8 bytes on
# Linux x64 and would make these structs measure wrong on the CI host that runs
# the layout test. No WinDLL at import time — see the platform-module rule (T3).
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_int32), ("dy", ctypes.c_int32), ("mouseData", ctypes.c_uint32),
                ("dwFlags", ctypes.c_uint32), ("time", ctypes.c_uint32), ("dwExtraInfo", ctypes.c_size_t)]
class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_uint16), ("wScan", ctypes.c_uint16), ("dwFlags", ctypes.c_uint32),
                ("time", ctypes.c_uint32), ("dwExtraInfo", ctypes.c_size_t)]
class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_uint32), ("wParamL", ctypes.c_uint16), ("wParamH", ctypes.c_uint16)]
class _U(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]
class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("union", _U)]   # named, so tests read inp.union.ki / inp.union.mi

_user32 = None
def user32():
    global _user32
    if _user32 is None:
        _user32 = ctypes.WinDLL("user32", use_last_error=True)
        _user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
        _user32.SendInput.restype = ctypes.c_uint
    return _user32

def send(batch: list[INPUT]) -> int:
    if not batch: return 0
    arr = (INPUT * len(batch))(*batch)
    n = user32().SendInput(len(batch), arr, ctypes.sizeof(INPUT))
    if n != len(batch): raise HandsError("backend", f"SendInput sent {n}/{len(batch)} (err {ctypes.get_last_error()})")
    return n
```

Absolute mouse coordinates: `dx = round(x * 65535 / (screen_w - 1))`, same for y, with `MOUSEEVENTF_MOVE|ABSOLUTE` first, then down/up (`double` repeats the pair). Text: one `KEYBDINPUT` pair per UTF-16 code unit with `KEYEVENTF_UNICODE`. Chords: modifiers down, key down/up, modifiers up (reverse order).

- [ ] **Step 4: Run unit tests** (`cd hands && python -m pytest tests/test_win_input.py tests/test_win_backend.py -q`) → PASS; on this PC also run `FIREKEEP_HANDS_LIVE=1 python -m pytest tests/live/test_win_notepad.py -q -s` → PASS (record the run in the Task 13 evidence).

- [ ] **Step 5: Commit**

```bash
git add hands/src/firekeep_hands/backends/win.py hands/src/firekeep_hands/backends/_win_input.py hands/tests/test_win_input.py hands/tests/test_win_backend.py hands/tests/live/test_win_notepad.py
git commit -m "feat(hands): Windows backend — UI Automation trees, tagged SendInput, mss screenshots, elevation guard"
```

---

### Task 8: macOS backend

**Files:**
- Create: `hands/src/firekeep_hands/backends/mac.py`, `hands/src/firekeep_hands/backends/_mac_ax.py` (thin wrappers over `ApplicationServices` so the backend can be unit-tested against a fake module)
- Test: `hands/tests/test_mac_backend.py` (fake `Quartz`/`ApplicationServices`/`AppKit` injected into `sys.modules` before import), `hands/tests/live/test_mac_textedit.py` (skipped unless `FIREKEEP_HANDS_LIVE=1` and darwin)

**Interfaces:**
- Produces: `MacBackend()` implementing `Backend`; `_mac_ax.AX` façade with `system_wide()`, `focused_app()`, `app_for_pid(pid)`, `attr(el, name)`, `children(el)`, `perform(el, action)`, `set_attr(el, name, value)`, `trusted() -> bool` (`AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: False})`), `screen_ok() -> bool` (`CGPreflightScreenCaptureAccess`).

Refs: `"p<pid>:<index path from the app element, e.g. 0.3.2>"` — the path is re-walked on use and `HandsError("stale_ref")` raised if the role at the path changed. Interactive roles: `AXButton, AXTextField, AXTextArea, AXSecureTextField, AXCheckBox, AXRadioButton, AXPopUpButton, AXMenuItem, AXMenuBarItem, AXLink, AXRow, AXCell, AXTab, AXComboBox, AXStaticText (name ≤ 80)`. `patterns`: `("AXPress",)` when `AXPress` in `AXUIElementCopyActionNames`, plus `("AXValue",)` when `AXValue` is settable (`AXUIElementIsAttributeSettable`). Rect from `AXPosition`/`AXSize` (top-left origin, Quartz coordinates). Screenshot: `subprocess.run(["screencapture", "-x", "-o", "-R", f"{x},{y},{w},{h}", tmp.png])` then Pillow downscale. Input: `CGEventCreateMouseEvent` / `CGEventCreateKeyboardEvent`, `CGEventSetIntegerValueField(ev, kCGEventSourceUserData, HANDS_TAG)`, `CGEventPost(kCGHIDEventTap, ev)`; text typing via `CGEventKeyboardSetUnicodeString` on a keycode-0 event (no layout map needed); chords via the static a–z/digits/named-key table with `CGEventSetFlags`. Clipboard: `AppKit.NSPasteboard.generalPasteboard()`. Apps: `open -a <name>` / `open -b <bundle-id>` / `open <path>`; focus via `NSRunningApplication.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)`. `permissions()`: accessibility from `trusted()`, screen from `screen_ok()`, input `"unknown"` (the broker's tap reports its own).

- [ ] **Step 1: Failing unit tests** with a fake AX tree (an `AXApplication` with two children: `AXButton "Save"` with action `AXPress`, `AXTextArea` settable `AXValue`): `observe` produces refs `p42:0` / `p42:1` with rects from position/size; `invoke` calls `perform(el, "AXPress")`; `set_value` calls `set_attr(el, "AXValue", "hello")`; `find("save")` finds the button; a changed role at a path raises `stale_ref`; `permissions()` reflects the fake `trusted()`/`screen_ok()`. Every synthetic event builder sets user data to `HANDS_TAG` (assert on the fake `CGEventSetIntegerValueField` calls).

- [ ] **Step 2: Run to verify they fail**, **Step 3: implement**, **Step 4: run unit tests → PASS.** The live TextEdit test runs on the MacBook in Task 13.

- [ ] **Step 5: Commit**

```bash
git add hands/src/firekeep_hands/backends/mac.py hands/src/firekeep_hands/backends/_mac_ax.py hands/tests/test_mac_backend.py hands/tests/live/test_mac_textedit.py
git commit -m "feat(hands): macOS backend — AXUIElement trees, tagged CGEvents, screencapture, TCC-aware permissions"
```

---

### Task 9: The Hands-managed browser (CDP)

**Files:**
- Create: `hands/src/firekeep_hands/browser.py`, `hands/src/firekeep_hands/_cdp.py` (transport: launch + websocket JSON-RPC with ids), `hands/src/firekeep_hands/_dom_probe.js` (packaged data file: the injected DOM reader)
- Test: `hands/tests/test_browser.py` (fake transport), `hands/tests/test_dom_probe.py` (runs the JS through `node` if present, else skipped)

**Interfaces:**

```python
class Browser:
    def __init__(self, transport: "CdpTransport | None" = None, *, kind: str = "auto")   # transport None -> launch on first use
    def open(self) -> dict                    # launches if needed; returns {"tabs": [...]}
    def tabs(self) -> list[dict]              # [{"id","url","title"}]
    def navigate(self, url: str, *, tab: str | None = None) -> dict   # waits for Page.loadEventFired or 10 s
    def read(self, *, tab=None, budget: int = 4000) -> dict           # {"url","title","text"} innerText trimmed
    def find(self, query: str, *, tab=None, limit: int = 10) -> list[dict]   # DOM controls: {"ref","role","name","value","rect":[x,y,w,h],"href"}
    def click(self, ref: str, *, tab=None) -> None                     # Input.dispatchMouseEvent at the element's centre (from the probe's rect)
    def fill(self, ref: str, text: str, *, tab=None) -> None           # focus via probe, then Input.insertText
    def screenshot(self, *, tab=None, max_width: int = 1280) -> bytes  # Page.captureScreenshot, downscaled
    def current_url(self, tab=None) -> str
    def close(self) -> None

class CdpTransport:     # _cdp.py
    @classmethod
    def launch(cls, kind: str, profile_dir: Path) -> "CdpTransport"   # finds chrome/msedge, starts with --remote-debugging-port=0 --user-data-dir=<profile> --no-first-run --no-default-browser-check --disable-sync --password-store=basic; reads DevToolsActivePort
    def send(self, method: str, params: dict | None = None, *, session: str | None = None, timeout: float = 10.0) -> dict
    def wait_event(self, name: str, *, session: str | None, timeout: float) -> dict | None
    def attach(self, target_id: str) -> str   # Target.attachToTarget(flatten=True) -> sessionId
```

`_dom_probe.js` assigns `data-hands-ref="d<N>"` to interactive elements (`a[href], button, input, select, textarea, [role=button], [role=link], [contenteditable], [onclick]`), skips invisible ones (`getBoundingClientRect` empty or `visibility: hidden`), returns `{controls: [{ref, role, name, value, rect: [x,y,w,h] (CSS px, viewport-relative), href}], truncated}` capped at `max_nodes`, plus `find(query)` and `focus(ref)` entry points. Browser kind resolution: `chrome` → `google-chrome`/`chrome.exe`/`Google Chrome.app`; `edge` → `msedge`; `auto` → chrome then edge; not found → `HandsError("backend", "no Chrome or Edge found")`. The profile dir is `paths.chrome_profile_dir()`; the guide says plainly this profile has no logins until the human signs in through Hands.

- [ ] **Step 1: Failing tests** with a `FakeTransport` that records `send(method, params)` and returns canned results (`Target.getTargets` → one page target; `Runtime.evaluate` → the probe's JSON; `Page.captureScreenshot` → base64 of a 1×1 PNG): `open()` attaches; `navigate("https://example.com")` sends `Page.navigate` then waits for `Page.loadEventFired`; `find("sign in")` returns refs; `click("d1")` dispatches `mousePressed`/`mouseReleased` at the centre of the rect the probe returned (never a caller-supplied point); `fill("d2","x")` focuses then `Input.insertText`; `screenshot()` returns PNG bytes.

- [ ] **Step 2: Run to verify they fail**, **Step 3: implement**, **Step 4: run → PASS.** On this PC also run the live check `FIREKEEP_HANDS_LIVE=1 python -c "from firekeep_hands.browser import Browser; b=Browser(); b.open(); print(b.navigate('https://example.com')['title']); print(b.find('more information')); b.close()"`.

- [ ] **Step 5: Commit**

```bash
git add hands/src/firekeep_hands/browser.py hands/src/firekeep_hands/_cdp.py hands/src/firekeep_hands/_dom_probe.js hands/tests/test_browser.py hands/tests/test_dom_probe.py hands/pyproject.toml
git commit -m "feat(hands): Hands-managed Chrome/Edge over DevTools with a DOM probe and computed clicks"
```

(Add `[tool.hatch.build.targets.wheel] include`/`artifacts` so `_dom_probe.js` ships in the wheel, and load it with `importlib.resources.files("firekeep_hands") / "_dom_probe.js"`.)

---

### Task 10: `HandsSession` and the MCP tool surface

**Files:**
- Create: `hands/src/firekeep_hands/session.py`, `hands/src/firekeep_hands/server.py`
- Test: `hands/tests/test_session.py`, `hands/tests/test_server_tools.py`

**Interfaces:**

```python
class HandsSession:
    def __init__(self, *, backend: Backend, broker: "BrokerClient | None", link: KeepLink, browser: Browser | None, config: HandsConfig, policy: Policy, session_id: str)
    def status(self) -> dict
    def task_start(self, goal: str, apps: list[str] | None) -> dict     # prune evidence; task_id = "h-" + 12 hex; ledger; lease; action_before; step_index = 0
    def observe(self, *, detail: str = "controls", app=None, region=None, max_nodes=None) -> dict   # detail summary|controls|screenshot; stores last Observation
    def find(self, query, *, role=None, app=None, limit=10) -> dict
    def act(self, action: dict, *, permit: str | None = None) -> dict
    def request_permit(self, challenge: str, wait_s: int = 45) -> dict
    def browser_op(self, op: str, **kw) -> dict
    def task_end(self, outcome: str, summary: str = "") -> dict
```

`act` algorithm (the heart of PR1 — implement exactly):

```
1. no task -> HandsError("no_task", "call hands_task_start first")
2. step_index >= config.max_steps -> HandsError("budget", ...)
3. routed = route(action, self.last_obs)                       # raises stale_ref / invalid_action
4. control = routed.control; window = backend.active_window(); url = action.get("url")
5. decision = policy.decide(action, control, window, url, self.policy, self.task_apps)
6. ahash = action_hash(action); challenge = challenge_id_for(machine_id(), session_id, task_id, step_index, ahash)
7. if decision.verdict == "permit":
     if broker is None: return {"ok": False, "error": "approval broker unreachable — protected step refused; run `firekeep hands status`", "classes": decision.classes}
     if permit != challenge: 
         broker.request(challenge=challenge, title=<human title e.g. 'invoke "Send" in Mail'>, classes=list(decision.classes), task_id=task_id, step_index=step_index)
         return {"ok": False, "needs_permit": {"challenge": challenge, "title": title, "classes": decision.classes, "reason": decision.reason, "expires_in_s": config.permit_ttl_s}}
     if not broker.consume(challenge): return {"ok": False, "error": "permit not approved, expired or already used", "needs_permit": {...same...}}
8. before = screenshot if detail budget allows (always for protected steps, else only when config says)   -> PR1: always capture before/after for protected steps, never otherwise
9. execute routed via backend / browser (table in Task 4); catch HandsError -> outcome "error"
10. after = screenshot for protected steps
11. ledger.record(step_index, action, routed.route, decision.classes, permit={"challenge":..., "via": broker.get(challenge)["via"]} if used else None, before, after, outcome, error)
12. link.renew_lease() every 10 steps
13. step_index += 1; self.last_obs = None (a mutation invalidates refs); return {"ok": outcome == "ok", "step_index": step_index-1, "route": routed.route, "classes": decision.classes, "error": error}
```

`request_permit(challenge, wait_s)`: `broker.wait(challenge, min(wait_s, 55))` → `{"state": ..., "via": ...}`; when approved via chord/phone it also calls `policy.remember(...)` **only** if the permit's classes are all in `{"send","money","boundary"}` and the human approved twice for the same `(class, app, match)` — PR1 simplification: never auto-remember; `firekeep hands allow` is the only writer. (Ruling recorded here so the reviewer does not flag the missing "remember" path.)

`task_end`: `ledger.close`, `link.action_after(action_id, outcome, summary)`, `link.release_lease()`, browser stays open (the human may be mid-login), state reset.

`server.py` (mcp SDK, stdio):

```python
from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as t

TOOLS = [
  t.Tool(name="hands_status", description="What Hands can do on this machine right now: platform, permissions, approval broker, current task.", inputSchema={"type":"object","properties":{}}),
  t.Tool(name="hands_task_start", description="Begin an operator task. Declares the goal and the apps you expect to touch; anything outside them is a boundary step that needs approval.", inputSchema={"type":"object","required":["goal"],"properties":{"goal":{"type":"string"},"apps":{"type":"array","items":{"type":"string"}}}}),
  t.Tool(name="hands_observe", description="Look at the screen: the active window's interactive controls with refs you can act on. detail=summary|controls|screenshot.", inputSchema={"type":"object","properties":{"detail":{"type":"string","enum":["summary","controls","screenshot"]},"app":{"type":"string"},"region":{"type":"array","items":{"type":"integer"},"minItems":4,"maxItems":4},"max_nodes":{"type":"integer"}}}),
  t.Tool(name="hands_find", description="Find controls by name/value text in the active window (or a named app).", inputSchema={"type":"object","required":["query"],"properties":{"query":{"type":"string"},"role":{"type":"string"},"app":{"type":"string"},"limit":{"type":"integer"}}}),
  t.Tool(name="hands_act", description="Do one thing: {kind: invoke|set_value|click|type|key|scroll|focus_app|open_app|open_url|clipboard_set|wait, ...}. Refs come from hands_observe/hands_find; raw coordinates are refused. A protected step returns needs_permit — call hands_request_permit, then repeat the same action with permit=<challenge>.", inputSchema={"type":"object","required":["action"],"properties":{"action":{"type":"object"},"permit":{"type":"string"}}}),
  t.Tool(name="hands_request_permit", description="Wait for the human to approve a protected step (chord on the keyboard or a tap on the dashboard). Returns the permit state.", inputSchema={"type":"object","required":["challenge"],"properties":{"challenge":{"type":"string"},"wait_s":{"type":"integer"}}}),
  t.Tool(name="hands_browser", description="Operate the Hands-managed browser: op=open|tabs|navigate|read|find|click|fill|screenshot. Navigating to a host outside the allowlist is a boundary step.", inputSchema={"type":"object","required":["op"],"properties":{"op":{"type":"string"},"url":{"type":"string"},"ref":{"type":"string"},"text":{"type":"string"},"query":{"type":"string"},"tab":{"type":"string"},"permit":{"type":"string"}}}),
  t.Tool(name="hands_task_end", description="Finish the task: outcome=done|failed|abandoned with a one-line summary. Releases the machine lease and closes the evidence ledger.", inputSchema={"type":"object","required":["outcome"],"properties":{"outcome":{"type":"string","enum":["done","failed","abandoned"]},"summary":{"type":"string"}}}),
]
```

`call_tool` dispatches to the session, returns `[t.TextContent(text=json.dumps(result))]`, plus `t.ImageContent(data=b64, mimeType="image/png")` for `detail == "screenshot"` and `hands_browser screenshot`; every `HandsError` becomes `{"ok": false, "error": code + ": " + message}` (never an MCP protocol error, so the model can recover). `main()` builds `backend = load_backend()`, `broker = BrokerClient.from_disk()` (re-probed on every protected `act` when `None`, so a broker started mid-session is picked up), `link = KeepLink(agent_id=os.environ.get("NEXUS_AGENT_ID", "hands"), machine_id=machine_id())`, `session_id = os.environ.get("FIREKEEP_SESSION_ID") or uuid4().hex[:12]`, and serves stdio.

- [ ] **Step 1: Failing tests** (`FakeBackend`, an in-process `PermitStore`-backed fake `BrokerClient`, a `FakeLink`):

```python
def test_unprotected_action_runs_and_is_ledgered(session):
    session.task_start("save the note", ["Notepad"]); session.observe()
    r = session.act({"kind": "invoke", "ref": "c1"})          # "Save" button
    assert r["ok"] and r["route"] == "accessibility" and session.backend.calls[-1] == ("invoke", "c1")
    assert session.ledger.steps()[0]["classes"] == []

def test_protected_action_needs_a_permit_then_runs_once(session, store):
    session.task_start("send the mail", ["Mail"]); session.observe()
    r = session.act({"kind": "invoke", "ref": "send"})
    assert r["ok"] is False and r["needs_permit"]["classes"] == ["send"]
    ch = r["needs_permit"]["challenge"]
    assert session.act({"kind": "invoke", "ref": "send"}, permit=ch)["ok"] is False   # not approved yet
    store.decide(ch, "approve", via="chord")
    assert session.request_permit(ch, wait_s=1)["state"] == "approved"
    session.observe()
    ok = session.act({"kind": "invoke", "ref": "send"}, permit=ch)
    assert ok["ok"] and session.ledger.steps()[-1]["permit"] == {"challenge": ch, "via": "chord"}
    session.observe()
    again = session.act({"kind": "invoke", "ref": "send"}, permit=ch)                # one-use
    assert again["ok"] is False and "needs_permit" in again

def test_permit_is_bound_to_the_exact_action(session, store):
    session.task_start("x", ["Mail"]); session.observe()
    ch = session.act({"kind": "invoke", "ref": "send"})["needs_permit"]["challenge"]
    store.decide(ch, "approve", via="chord")
    session.observe()
    r = session.act({"kind": "invoke", "ref": "delete"}, permit=ch)   # different action, same permit
    assert r["ok"] is False and r["needs_permit"]["challenge"] != ch

def test_no_broker_fails_closed_for_protected_only(session_without_broker):
    s = session_without_broker; s.task_start("x", ["Mail"]); s.observe()
    assert s.act({"kind": "invoke", "ref": "c1"})["ok"] is True
    r = s.act({"kind": "invoke", "ref": "send"})
    assert r["ok"] is False and "broker" in r["error"]

def test_refs_go_stale_after_any_action(session):
    session.task_start("x", ["Notepad"]); session.observe()
    session.act({"kind": "type", "text": "a"})
    r = session.act({"kind": "invoke", "ref": "c1"})
    assert r["ok"] is False and r["error"].startswith("stale_ref")

def test_budget_and_lifecycle(session):
    session.config.max_steps = 2; session.task_start("x", ["Notepad"])
    session.act({"kind": "wait", "seconds": 0}); session.act({"kind": "wait", "seconds": 0})
    assert session.act({"kind": "wait", "seconds": 0})["error"].startswith("budget")
    end = session.task_end("done", "ok")
    assert end["steps"] == 2 and session.link.after == [("A1", "done", "ok")] and session.link.released is True

def test_tools_are_exposed_with_the_spec_names(server_tools):
    assert [t.name for t in server_tools] == ["hands_status", "hands_task_start", "hands_observe", "hands_find", "hands_act", "hands_request_permit", "hands_browser", "hands_task_end"]
```

(The `session` fixture's scene: `c1` "Save" Button (Invoke), `send` "Send" Button (Invoke), `delete` "Delete" Button (Invoke), `edit` "Text Editor" Edit (Value); window app "Mail" for the Mail tests, "Notepad" otherwise — parametrize or build two fixtures.)

- [ ] **Step 2: Run to verify they fail**, **Step 3: implement `session.py` and `server.py`**, **Step 4: run the whole hands suite** (`cd hands && python -m pytest -q`) → PASS. Also start it as the gateway would: `firekeep-hands` on stdin/stdout and send an `initialize` + `tools/list` JSON-RPC pair by hand (or with `python -m firekeep_client.gateway`'s probe if one exists) and confirm eight tools.

- [ ] **Step 5: Commit**

```bash
git add hands/src/firekeep_hands/session.py hands/src/firekeep_hands/server.py hands/tests/test_session.py hands/tests/test_server_tools.py
git commit -m "feat(hands): HandsSession lifecycle and the eight-tool MCP surface with permit-bound protected steps"
```

---

### Task 11: Wheel CLI (`firekeep hands status|allow|chord|config|evidence`)

**Files:**
- Create: `hands/src/firekeep_hands/cli.py`
- Test: `hands/tests/test_cli.py`

**Interfaces:** `main(argv: list[str]) -> int`. Sub-commands:
- `status` — platform, backend name, `permissions()`, broker `/health` or "not running", chord, policy counts, last task (from the newest `task.json`).
- `allow app <name>` / `allow domain <host>` / `allow list` / `allow forget <class> <app> <match>`
- `chord` (print) / `chord set <chord>` (validates with `parse_chord`; prints "restart the broker: `firekeep-hands-broker run` or log out/in")
- `config` (print JSON) / `config set <key> <value>` (ints coerced; unknown key → exit 2)
- `evidence` (list tasks: id, started, outcome, steps) / `evidence <task_id>` (print steps.jsonl lines, one per line, images named)

- [ ] **Step 1: Failing tests** — `allow domain example.com` writes `policy.json`; `chord set ctrl+alt+u` persists; `chord set bogus` exits 2; `evidence` lists a ledger created with `Ledger(...)`; `status` prints "not running" when there is no broker.json (capture stdout).
- [ ] **Step 2: Run to verify they fail**, **Step 3: implement (argparse, stdlib only besides our modules)**, **Step 4: run → PASS.**
- [ ] **Step 5: Commit** — `git commit -m "feat(hands): firekeep hands status/allow/chord/config/evidence"`.

---

### Task 12: Dashboard — Approve / Deny for `hands_permit:` tasks

**Files:**
- Modify: `dashboard/index.html:3910-3953` (`loadRelayTasks`)
- Test: `tests/test_dashboard_hands.py` (new; follow the shape of `tests/test_dashboard_autopilot.py` — read `dashboard/index.html`, assert on strings)

**Interfaces:**
- Consumes: relay MCP `relay_task_update(task_id, status, result)` through the existing `mcpCall(CONFIG.RELAY_API, …)`.
- Produces: for a task whose `title` starts with `hands_permit:` and `status === "pending"`, the Title cell shows the description (the step's human title) under the title, and the last cell shows two buttons — **Approve** → `relay_task_update({task_id, status: "completed", result: "approve"})`, **Deny** → `relay_task_update({task_id, status: "cancelled", result: "deny"})` — each followed by `loadRelayTasks()`. Non-Hands tasks keep the delete button.

- [ ] **Step 1: Failing test**

```python
from pathlib import Path
HTML = Path(__file__).resolve().parents[1] / "dashboard" / "index.html"

def test_dashboard_offers_approve_and_deny_for_hands_permits():
    src = HTML.read_text(encoding="utf-8")
    assert "hands_permit:" in src
    assert "btn-hands-approve" in src and "btn-hands-deny" in src
    assert "result: 'approve'" in src and "status: 'completed'" in src
    assert "result: 'deny'" in src and "status: 'cancelled'" in src
    assert "function decideHandsPermit(" in src
```

- [ ] **Step 2: Run** (`python -m pytest tests/test_dashboard_hands.py -q`) → FAIL.
- [ ] **Step 3: Implement** — add `function decideHandsPermit(taskId, approve) { mcpCall(CONFIG.RELAY_API, 'relay_task_update', approve ? { task_id: taskId, status: 'completed', result: 'approve' } : { task_id: taskId, status: 'cancelled', result: 'deny' }).then(function(r){ if (r.error) throw new Error(r.error.message); toast(approve ? 'Approved' : 'Denied', 'success'); loadRelayTasks(); }).catch(function(e){ toast('Failed: ' + e.message, 'error'); }); }` and, in the table render, the conditional buttons `<button class="btn-hands-approve" data-task-id="…">Approve</button> <button class="btn-hands-deny" data-task-id="…">Deny</button>` with click handlers wired the same way `.btn-delete-task` is. Style them with the existing button classes (Approve accent, Deny red).
- [ ] **Step 4: Run** the dashboard pin tests (`python -m pytest tests/test_dashboard_*.py -q`) → PASS; open the dashboard locally and eyeball the Relay tab with a fake `hands_permit:` task posted via `relay_task_post`.
- [ ] **Step 5: Commit** — `git commit -m "feat(dashboard): approve or deny Hands permits from the Relay tab"`.

---

### Task 13: Release, CI and repo guards

**Files:**
- Modify: `.github/workflows/release.yml:197-199` (build), `:348-349`, `:363-364`, `:461-462` (asset lists), `:557-574` (pypi matrix); `.github/workflows/ci.yml:41-43` (test matrix), `:359-368` (license gates)
- Modify: `tests/test_requirements_lock.py` (unlocked set), `client/tests/test_make_release.py` (not-bundled guard), `scripts/check_licenses.py` if it lists first-party names (grep `firekeep_maildex`)

- [ ] **Step 1: Failing guard tests**

In `tests/test_requirements_lock.py`, extend the "deliberately unlocked" assertion to include `hands` (read the existing test to find the tuple; add `"hands"`). In `client/tests/test_make_release.py` add:

```python
def test_hands_is_not_a_bundled_wheel():
    from pathlib import Path
    import re
    src = (Path(__file__).resolve().parents[1] / "scripts" / "make_release.py").read_text(encoding="utf-8")
    assert "firekeep_hands" not in src and "firekeep-hands" not in src
    for boot in ("install.sh", "install.ps1"):
        text = (Path(__file__).resolve().parents[1] / "bootstrap" / boot).read_text(encoding="utf-8")
        assert "firekeep_hands" not in text and "firekeep-hands" not in text
```

- [ ] **Step 2: Workflow edits**

release.yml: after line 199 add `(cd hands && python -m build --wheel --outdir ../dist)`; add `firekeep_hands-*.whl` to the three asset globs so the GitHub release carries it (SHA256SUMS covers it automatically if `make_release.py` sums `dist/*.whl` — verify; if it enumerates names, leave it out there on purpose and rely on the PyPI publish + GitHub asset); add to the pypi matrix:

```yaml
          - package_dir: hands
            environment: pypi-hands
```

and add the sentence "pypi-hands" to the comment block at `:540-545`. **Founder step (not automatable, put in the PR description):** create the `pypi-hands` GitHub environment and the PyPI trusted publisher for `firekeep-hands` before the next `client-v*` tag.

ci.yml: add a matrix entry

```yaml
          - name: hands
            # The wheel imports firekeep_client from the kit venv and does NOT
            # declare it (the PyPI name is a third party's) — install the
            # checkout's client first, exactly as `firekeep hands enable` finds it.
            install: pip install -e client -e "hands[test]"
            test: pytest hands/tests -q --ignore=hands/tests/live
```

and a `hands-windows` job mirroring `symdex-windows` (`pip install -e client -e "hands[test]"`, `pytest hands/tests -q --ignore=hands/tests/live`), plus a license gate step "Gate hands (firekeep-hands wheel) dependencies" mirroring the docdex one at `ci.yml:364-368` (`pip install -q ./client ./hands` into the throwaway venv so the import-time dependency is present). If `scripts/check_licenses.py` carries a first-party allowlist, add `firekeep-hands`. The same `pip install -e client -e "hands[test]"` is how a developer runs the hands suite locally; write it into `hands/README.md`.

- [ ] **Step 3: Run** `python -m pytest tests/test_requirements_lock.py client/tests/test_make_release.py -q` → PASS; `python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml')); yaml.safe_load(open('.github/workflows/release.yml'))"` → no error.
- [ ] **Step 4: Commit** — `git commit -m "ci: build, test, license-gate and publish the firekeep-hands wheel; it is never bundled"`.

---

### Task 14: Documentation

**Files:**
- Create: `docs/guides/hands.md`
- Modify: `docs/guides/dexes.md` (registry section: the `role` field, "a capability is never seeded"), `docs/guides/client-kit.md` (CLI table: `firekeep hands …`; doctor rows: `hands`), `docs/THREAT-MODEL.md` (new §"Hands — desktop operator"), `CLAUDE.md` (architecture table row + kit paragraph sentence), `README.md` (one row/sentence where dexes are listed)

`docs/guides/hands.md` sections, in order: **What it is** (spec §1, one paragraph, the Violoop comparison in one sentence: same operator model, no device — your runtime is the brain, the broker is the button); **Turn it on** (`firekeep hands enable`, what it installs, the permissions each OS asks for: Windows none beyond the logon task; macOS Accessibility + Screen Recording for the server, Input Monitoring for the broker); **How a task runs** (the eight tools with the needs_permit loop, one worked example: "open Notepad, type, save"); **What needs approval** (the six classes with their exact triggers from Task 4's table, the chord, the phone flow through the dashboard Relay tab, `firekeep hands allow`); **Modes** (spec §2: how Instant / Long-running / Away map onto chord vs phone); **Evidence** (ledger layout, hash chain, retention, what reaches the Keep in PR1 and what does not yet); **The browser** (dedicated profile, no logins until you sign in through it, allowlist); **Honest limits** (locked screen, elevated/UAC windows, screenshots leave the machine when the runtime is a cloud model, prompt injection through observed UI text, two-hop trust, no Linux); **Turning it off** (`disable`, `--purge`); **Verified** (dated table filled by Task 15: Windows this PC, macOS MacBook, injected-chord rejection). No version numbers.

`docs/THREAT-MODEL.md` addition: assets (the machine, the human's sessions in every app, the Chrome profile), attacker models (compromised runtime/model, prompt-injected UI text, local malware sending synthetic chords), mitigations (broker in its own process; injected-flag / source-state filtering; one-use deterministic permits; boundary class; fail closed), residuals marked **OPEN** (a process running as the same user can read `broker.json` and consume permits it did not earn — the permit still requires a real human chord to exist; kernel-level input injection drivers set no injected flag; screenshots to cloud models).

`CLAUDE.md` row: `| FirekeepHands | `hands/` | none (client-side MCP server, opt-in) | Desktop operator. **CLIENT-SIDE, OPT-IN, NEVER BUNDLED** — `firekeep hands enable` pip-installs the `firekeep-hands` wheel into the kit venv and registers it as a `capability`-role entry the gateway mounts like any `mcp-stdio` dex; a separate approval broker (`firekeep-hands-broker`, logon task / LaunchAgent) is the only thing that can approve a protected step, and only from real input or a dashboard tap. See [`docs/guides/hands.md`](docs/guides/hands.md). |`

- [ ] **Step 1: Write the docs.** **Step 2:** `python -m pytest tests -q -k "docs or guide or claude_md"` (whatever doc-consistency tests exist) → PASS. **Step 3: Commit** — `git commit -m "docs(hands): guide, registry role, kit CLI and doctor rows, threat model, CLAUDE.md"`.

---

### Task 15: Live smoke on Windows (this PC) and macOS (MacBook), verified section

**Files:**
- Create: `hands/scripts/demo_notepad.md` (the exact prompt to paste into Claude Code) and `hands/scripts/demo_textedit.md`
- Modify: `docs/guides/hands.md` "Verified" table

Windows, from this worktree:

- [ ] `cd client && .\install.ps1` (or `firekeep install` if the venv is current) so the kit code from Task 1–2 is what runs; `firekeep hands enable --from E:\Documents\Projects\Firekeep\.claude\worktrees\fleet-as-gpu\hands` → exit 0; `firekeep dex list` shows `hands  [registered]  operates desktop`; `firekeep doctor` shows the `hands` row `ok` with `chord ctrl+alt+y (active)`.
- [ ] Start a fresh Claude Code session in any folder; `firekeep_gateway_status` lists the `hands` backend with eight tools.
- [ ] Paste `hands/scripts/demo_notepad.md`: "Use the hands_* tools. Start a task 'write a note', apps ['Notepad']. Open Notepad, type `Hands was here <today's date>`, save it as `%USERPROFILE%\.firekeep\hands\demo.txt` (this is a Save As dialog: use hands_find for the file-name box and the Save button), then end the task with outcome done." Expected: no permit needed (Save is unprotected); `demo.txt` exists; `firekeep hands evidence` lists the task with ≥ 6 steps.
- [ ] Protected path: in the same session ask it to "empty the Recycle Bin via Explorer" → `needs_permit` with class `destroy`; press `Ctrl+Alt+Y` → the step runs; check `steps.jsonl` shows `"via": "chord"`. Deny path: repeat, press `Ctrl+Alt+N` → `{"state": "denied"}` and nothing happens.
- [ ] Injection rejection: with a permit pending, run `python -c "import ctypes; from firekeep_hands.backends import _win_input as w; w.send(w.build_key_chord('ctrl+alt+y'))"` → the permit stays `pending` (the broker ignored the injected chord); then press the real chord to clear it.
- [ ] Phone path: with a permit pending, open the dashboard on the phone (tailnet) → Relay tab → the `hands_permit:` row → Approve → the step runs, `"via": "phone"`.
- [ ] Browser: "open https://example.com in the Hands browser and read the heading" → works without a permit once `firekeep hands allow domain example.com`; before that it returns `needs_permit` with class `boundary`.

macOS, on the MacBook (a Claude Code session there; the worktree branch pushed first):

- [ ] `firekeep hands enable --from <checkout>/hands`; grant Accessibility + Screen Recording to the terminal/python when macOS prompts; Input Monitoring to the broker; `firekeep doctor` `hands` row `ok`.
- [ ] `hands/scripts/demo_textedit.md`: TextEdit, type, ⌘S to `~/.firekeep/hands/demo.txt`; then the protected + injected (`python -c` posting a CGEvent chord with `HANDS_TAG`) + phone checks as above.
- [ ] **Measure, do not assume, the macOS source-state claim.** With the broker's tap running in a debug mode that logs every key event's `(keycode, flags, userData, sourceStateID)`, post an **untagged** chord via `CGEventCreateKeyboardEvent(None, …)` + `CGEventPost(kCGHIDEventTap, …)` and record what `kCGEventSourceStateID` reports for it versus a real key press. If synthetic events also report `1` (HID system state), then the tag is the only discriminator for Hands' own events and untagged synthetic input from another process is NOT filtered — write the guide's "Honest limits" and the threat model's residual from the observed values, and drop the source-state sentence from the Global Constraints in a follow-up commit on this branch.

- [ ] Fill the "Verified" table in `docs/guides/hands.md` with dates, OS builds and outcomes exactly as observed (a failed row stays a failed row). Commit: `docs(hands): verified on Windows and macOS`.

---

### Task 16: Final verification and PR

- [ ] Full suites: `cd client && python -m pytest -q`; `cd hands && python -m pytest -q --ignore=tests/live`; `python -m pytest tests -q` (root); `cd cortex && pytest tests -q`, `cd relay && pytest tests -q` (touched nothing, but the merge gate runs them).
- [ ] `git status` clean; `git log --oneline main..feat/hands` reads as the task list above.
- [ ] Whole-branch review (subagent-driven-development final review on the most capable model), one fix round, re-review.
- [ ] Push `feat/hands`, open the PR against `main` with: what Hands is (two sentences), the eight tools, the approval model, the verified table, the founder steps (PyPI trusted publisher + `pypi-hands` environment before the next client tag; macOS permissions are per-machine), and the PR2 list from spec §9 (Studio Operate mode, replay POST route, Living Procedures `ui-step`, app-scripting adapters, auto-remember).

---

## Self-review

**Spec coverage:** §1–2 → guide (T14) + modes note; §3.1 packaging → T3, T13, never-bundled guard; registry role + never seeded → T1; CLI + doctor → T2, T11; §3.2 tools → T10; §3.3 action union + routing + coordinate rejection → T4; §3.4 browser → T9; §3.5 broker, real-input filter, phone, one-use deterministic permits, fail closed → T6, T10, T12; §3.6 evidence + action_before/after + lease → T5, T10; §4 budget → T3 config + T10; §5 classes + allowlist → T4, T11; §6 platforms → T7, T8, Linux unsupported in T3; §7 limits → T14; §8 CLI → T2 + T11; §9 phasing → PR description (T16); §10 gates → T15. Open questions from §11 that PR1 rules on: auto-remember is off (T10 ruling); Linux is unsupported, not partial.

**Type consistency:** `Control(ref, role, name, value, rect, app, patterns, enabled)` used identically in T3, T4, T7, T8, T10 tests; `Observation.generation` drives stale refs (T4 uses `observation is None or ref not in controls`, T10 clears `last_obs` after every act); `PermitStore.decide/decide_oldest/consume` names match between T6 and T10; `KeepLink` method names match between T5, T6 (`PhoneBridge`) and T10; `BrokerClient.request/get/wait/consume` match T6 and T10; `read_broker_health` (kit, T2) reads the same `broker.json` shape `BrokerServer.start` writes (T6): `{port, token, pid, started_at, chord}`.

**Placeholders:** none — every OS constant, regex, path, tool schema and command is written out; the only "find it with grep" pointers are to existing repo lines whose numbers may shift (subparser registration, unlocked-set tuple, make_release wheel enumeration).
