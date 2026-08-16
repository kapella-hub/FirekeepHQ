# Generic "any MCP client" adapter — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `generic` runtime adapter that connects any MCP client — prints the gateway config snippet and manages an optional hook-free `AGENTS.md` block — selected via `firekeep install --runtime generic --agents-md <path>`, byte-identical for the existing four adapters.

**Architecture:** A new `GenericAdapter` reuses the universal floor (register the gateway + render an instruction block). A content-derived block stamp lets it carry its own hash without touching the four's bytes. A config-aware `_selected_runtimes("all")` makes `generic` join install/update/uninstall only when `[generic] agents_md` is persisted.

**Tech Stack:** Python 3.10+, `configparser`, pytest. Spec: `docs/superpowers/specs/2026-08-16-generic-mcp-adapter-design.md`.

## Global Constraints

- **Byte-identical for the four**: every shared-code change either defaults to today's behavior or is gated on `[generic]` being configured. Proof = the guard suite in Task 12 stays green.
- **No `resolver.load_config()` in the config probe** — it migrates/rewrites/raises. Use a bare `configparser.ConfigParser().read()`.
- **`_selected_runtimes` stays pure** — the generic flag is computed at the call site and passed in.
- **Persist `[generic]` before the render loop** (inside `_configure`, before `cli.py:353-355` writes the config).
- **Hook-free text**: the generic block must NOT contain *"routine single-file edits are already gated by hooks and need no declaration"* (base.py ~405-407).
- **Codex is not touched** (its latent same-issue is a separate follow-up).
- **`--runtime` attribution tag** is `["gateway","--runtime","generic"]` (client 0.1.41 convention).
- Run tests from `client/`: `cd client && python -m pytest ...`.

---

### Task 1: Content-derived block stamp + generic instruction text (base.py)

**Files:**
- Modify: `client/firekeep_client/adapters/base.py` (`upsert_marked_block` ~569-595; add `MEMORY_INSTRUCTIONS_NO_HOOKS`, `GENERIC_INSTRUCTIONS`, `RENDERED_GENERIC_INSTRUCTIONS_HASH`, `_stamped_begin`)
- Test: `client/tests/adapters/test_base.py`

**Interfaces:**
- Produces: `GENERIC_INSTRUCTIONS: str`, `RENDERED_GENERIC_INSTRUCTIONS_HASH: str`, `_stamped_begin(content: str) -> str`; `upsert_marked_block(existing, content)` now stamps from `content`.
- Consumes: existing `INSTRUCTIONS_BEGIN_PREFIX`, `INSTRUCTIONS_END`, `_hash12`, `RENDERED_INSTRUCTIONS_HASH`, `MEMORY_INSTRUCTIONS`, `DECISION_INSTRUCTIONS`, `KNOWLEDGE_INGEST_INSTRUCTIONS`.

- [ ] **Step 1: Write the failing tests**

```python
# client/tests/adapters/test_base.py
from firekeep_client.adapters import base

def test_upsert_marked_block_is_byte_identical_for_four_content():
    # Byte-for-byte regression pin: the four render FIREKEEP_INSTRUCTIONS, whose
    # hash IS RENDERED_INSTRUCTIONS_HASH, so the content-derived stamp must equal
    # the old hardcoded INSTRUCTIONS_BEGIN line.
    out = base.upsert_marked_block("", base.FIREKEEP_INSTRUCTIONS)
    assert out.splitlines()[0] == base.INSTRUCTIONS_BEGIN

def test_upsert_marked_block_stamps_generic_content_with_generic_hash():
    out = base.upsert_marked_block("", base.GENERIC_INSTRUCTIONS)
    assert f"h={base.RENDERED_GENERIC_INSTRUCTIONS_HASH}" in out.splitlines()[0]
    assert base.RENDERED_GENERIC_INSTRUCTIONS_HASH != base.RENDERED_INSTRUCTIONS_HASH

def test_generic_instructions_omit_the_hooks_gating_clause():
    assert "gated by hooks" not in base.GENERIC_INSTRUCTIONS
    # but the rest of the memory protocol survives:
    assert "memory_recall" in base.GENERIC_INSTRUCTIONS
    assert "decision_board" in base.GENERIC_INSTRUCTIONS
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd client && python -m pytest tests/adapters/test_base.py -k "generic or byte_identical" -v`
Expected: FAIL — `GENERIC_INSTRUCTIONS`/`RENDERED_GENERIC_INSTRUCTIONS_HASH` undefined.

- [ ] **Step 3: Implement**

In `base.py`, split the hooks clause out of `MEMORY_INSTRUCTIONS`. Locate the sentence near line 405-407 (*"routine single-file edits are already gated by hooks and need no declaration"*) inside the `**Declare consequential actions before taking them.**` paragraph. Factor it so both variants share the rest:

```python
# base.py — replace the single hooks sentence with a substituted token so the two
# memory variants differ by exactly that clause. Keep MEMORY_INSTRUCTIONS's rendered
# text unchanged for the four.
_HOOKS_CLAUSE = (
    " Your stated confidence is scored against reality (calibration); routine "
    "single-file edits are already gated by hooks and need no declaration."
)
_NO_HOOKS_CLAUSE = (
    " Your stated confidence is scored against reality (calibration)."
)
# MEMORY_INSTRUCTIONS keeps _HOOKS_CLAUSE verbatim (byte-identical to today).
MEMORY_INSTRUCTIONS_NO_HOOKS = MEMORY_INSTRUCTIONS.replace(_HOOKS_CLAUSE, _NO_HOOKS_CLAUSE)

GENERIC_INSTRUCTIONS = (
    f"{MEMORY_INSTRUCTIONS_NO_HOOKS}\n\n{DECISION_INSTRUCTIONS}\n\n{KNOWLEDGE_INGEST_INSTRUCTIONS}"
)
RENDERED_GENERIC_INSTRUCTIONS_HASH = _hash12(GENERIC_INSTRUCTIONS)


def _stamped_begin(content: str) -> str:
    """BEGIN marker line stamped with the hash of the CONTENT it wraps (base.py:503-509
    says the stamp must be a pure function of content). For FIREKEEP_INSTRUCTIONS this
    equals the module constant INSTRUCTIONS_BEGIN byte-for-byte."""
    return (
        f"{INSTRUCTIONS_BEGIN_PREFIX} h={_hash12(content)}"
        " — firekeep-owned block, do not edit; re-rendered by `firekeep install` -->"
    )
```

Then in `upsert_marked_block` change the block line:

```python
    # was: block = f"{INSTRUCTIONS_BEGIN}\n{content}{INSTRUCTIONS_END}\n"
    block = f"{_stamped_begin(content)}\n{content}{INSTRUCTIONS_END}\n"
```

> **Care:** `_HOOKS_CLAUSE` must match the real sentence in `MEMORY_INSTRUCTIONS` verbatim. Read base.py ~400-410 and copy the exact bytes; if `MEMORY_INSTRUCTIONS_NO_HOOKS == MEMORY_INSTRUCTIONS` (no replacement happened) the third test still catches nothing — add `assert MEMORY_INSTRUCTIONS_NO_HOOKS != MEMORY_INSTRUCTIONS` to Step 1 to prove the replace landed.

- [ ] **Step 4: Run to verify pass**

Run: `cd client && python -m pytest tests/adapters/test_base.py tests/adapters/test_instruction_stamp.py tests/adapters/test_instructions.py tests/adapters/test_write_stability.py -v`
Expected: PASS — including the pre-existing stamp/stability tests (byte-identical proof).

- [ ] **Step 5: Commit**

```bash
git add client/firekeep_client/adapters/base.py client/tests/adapters/test_base.py
git commit -m "feat(client): content-derived block stamp + hook-free GENERIC_INSTRUCTIONS"
```

---

### Task 2: GenericAdapter + get_adapter branch (generic.py)

**Files:**
- Create: `client/firekeep_client/adapters/generic.py`
- Modify: `client/firekeep_client/adapters/__init__.py` (`get_adapter` ladder ~7-22)
- Test: `client/tests/adapters/test_generic.py`

**Interfaces:**
- Produces: `GenericAdapter(agents_md: Path | None = None)` with `name="generic"`, `render(*, venv_bin)`, `unrender()`, and module `KNOWN_INSTRUCTION_PATHS() -> list[Path]` (the four fixed paths, for the collision guard).
- Consumes: Task 1 `GENERIC_INSTRUCTIONS`; base `shim_servers`, `console_script_path`, `upsert_marked_block`, `strip_marked_block`, `has_marked_begin`, `write_text_if_changed`; `rendered_instructions_path` for the four paths.

- [ ] **Step 1: Write failing tests** (mold: `test_codex.py`)

```python
# client/tests/adapters/test_generic.py
import json, sys
import pytest
from pathlib import Path
from firekeep_client.adapters.generic import GenericAdapter
from firekeep_client.adapters import base

def _exe(p): return str(p) + (".exe" if sys.platform == "win32" else "")

def test_generic_render_prints_mcp_snippet(tmp_path, capsys):
    GenericAdapter().render(venv_bin=tmp_path)
    out = capsys.readouterr().out
    blob = json.loads(out[out.index("{"): out.rindex("}") + 1])
    srv = blob["mcpServers"]["firekeep"]
    assert srv["command"] == _exe(tmp_path / "firekeep")
    assert srv["args"] == ["gateway", "--runtime", "generic"]

def test_generic_output_states_no_lifecycle_automation(tmp_path, capsys):
    GenericAdapter().render(venv_bin=tmp_path)
    out = capsys.readouterr().out.lower()
    assert "no hooks" in out or "does not" in out
    assert "auto-briefing" in out or "briefing" in out

def test_generic_render_writes_nothing_without_agents_md(tmp_path, capsys):
    before = set(tmp_path.rglob("*"))
    GenericAdapter().render(venv_bin=tmp_path)
    assert set(tmp_path.rglob("*")) == before

def test_generic_agents_md_upserts_hookfree_block_and_keeps_user_text(tmp_path):
    target = tmp_path / "AGENTS.md"
    target.write_text("# My rules\nkeep me\n", encoding="utf-8")
    GenericAdapter(agents_md=target).render(venv_bin=tmp_path / "venv")
    text = target.read_text(encoding="utf-8")
    assert "keep me" in text
    assert base.GENERIC_INSTRUCTIONS.splitlines()[0] in text
    assert "gated by hooks" not in text

def test_generic_unrender_strips_only_our_block(tmp_path):
    target = tmp_path / "AGENTS.md"
    target.write_text("# My rules\nkeep me\n", encoding="utf-8")
    a = GenericAdapter(agents_md=target)
    a.render(venv_bin=tmp_path / "venv")
    a.unrender()
    text = target.read_text(encoding="utf-8")
    assert "keep me" in text
    assert not base.has_marked_begin(text)

def test_generic_unrender_is_noop_when_never_opted_in(tmp_path):
    GenericAdapter().unrender()  # must not raise

def test_generic_rerender_is_byte_identical(tmp_path):
    target = tmp_path / "AGENTS.md"
    target.write_text("x\n", encoding="utf-8")
    a = GenericAdapter(agents_md=target)
    a.render(venv_bin=tmp_path / "venv"); first = target.read_bytes()
    a.render(venv_bin=tmp_path / "venv"); assert target.read_bytes() == first

def test_generic_refuses_a_target_managed_by_another_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path)); monkeypatch.setenv("USERPROFILE", str(tmp_path))
    codex_agents = tmp_path / ".codex" / "AGENTS.md"
    with pytest.raises(ValueError, match="already managed"):
        GenericAdapter(agents_md=codex_agents).render(venv_bin=tmp_path / "venv")
```

- [ ] **Step 2: Run to verify fail**

Run: `cd client && python -m pytest tests/adapters/test_generic.py -v`
Expected: FAIL — `firekeep_client.adapters.generic` does not exist.

- [ ] **Step 3: Implement `generic.py`**

```python
"""Generic adapter: any MCP client. Prints a paste-in gateway snippet and,
when pointed at a rules/AGENTS.md file, manages a hook-free instruction block.
No hooks — the honest degraded tier (see contract/matrix.py 'generic')."""
from __future__ import annotations
import json
from pathlib import Path

from firekeep_client.adapters.base import (
    Adapter, GENERIC_INSTRUCTIONS, console_script_path, shim_servers,
    upsert_marked_block, strip_marked_block, has_marked_begin, write_text_if_changed,
    rendered_instructions_path,
)

_FOUR = ("claude", "codex", "kiro", "opencode")

def known_instruction_paths() -> list[Path]:
    return [p for rt in _FOUR if (p := rendered_instructions_path(rt)) is not None]

_NOTE = (
    "\nYou get: all MCP tools, and the cognitive protocol is delivered automatically\n"
    "when your client connects. You do NOT get (a generic client exposes no hooks\n"
    "Firekeep can wire): auto-briefing, the pre-edit blocking gate, stop->learn, and\n"
    "the pre-compaction checkpoint. Point --agents-md at your client's rules file to\n"
    "also install the protocol as text.\n"
)

class GenericAdapter(Adapter):
    name = "generic"

    def __init__(self, agents_md: Path | None = None) -> None:
        self.agents_md = Path(agents_md).expanduser().resolve() if agents_md else None

    def render(self, *, venv_bin: Path) -> None:
        servers = shim_servers(venv_bin, "generic")
        cmd, args = servers["firekeep"]
        snippet = json.dumps(
            {"mcpServers": {"firekeep": {"command": str(cmd), "args": list(args)}}}, indent=2
        )
        print("Firekeep works with any MCP client. Paste this into your client's MCP config:\n")
        print(snippet)
        print(_NOTE)
        if self.agents_md is not None:
            self._render_block()

    def _render_block(self) -> None:
        target = self.agents_md
        for other in known_instruction_paths():
            if target == other.expanduser().resolve():
                raise ValueError(
                    f"{target} is already managed by another Firekeep adapter; "
                    "point --agents-md at a different file."
                )
        try:
            existing = target.read_text(encoding="utf-8") if target.exists() else ""
            write_text_if_changed(target, upsert_marked_block(existing, GENERIC_INSTRUCTIONS))
        except OSError as exc:
            import sys
            print(f"firekeep: WARNING — could not update {target}: {exc}", file=sys.stderr)

    def unrender(self) -> None:
        target = self.agents_md
        if target is None or not target.exists():
            return
        try:
            text = target.read_text(encoding="utf-8")
            if has_marked_begin(text):
                write_text_if_changed(target, strip_marked_block(text))
        except OSError:
            pass
```

> **Verify** `shim_servers` returns a mapping whose `"firekeep"` value is a `(command, args)` tuple (base.py:97) — adjust the unpack if the shape differs. Confirm `Adapter` is importable from `base`.

Add to `__init__.py get_adapter`:

```python
    if name == "generic":
        from firekeep_client.adapters.generic import GenericAdapter
        from firekeep_client.cli import generic_agents_md   # persisted path (Task 3)
        return GenericAdapter(agents_md=generic_agents_md())
```

> If importing `cli` from `adapters` risks a cycle, instead read the config in `generic.py` with the raw probe from Task 3 (move `generic_agents_md` to `resolver.py`). Prefer `resolver.py` for `generic_agents_md` to keep `adapters -> resolver` (no cycle). Update Task 3 accordingly.

- [ ] **Step 4: Run to verify pass**

Run: `cd client && python -m pytest tests/adapters/test_generic.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add client/firekeep_client/adapters/generic.py client/firekeep_client/adapters/__init__.py client/tests/adapters/test_generic.py
git commit -m "feat(client): GenericAdapter — any MCP client (print snippet + optional AGENTS.md block)"
```

---

### Task 3: Config persistence + pure config-aware selection (resolver.py, cli.py)

**Files:**
- Modify: `client/firekeep_client/resolver.py` (add `generic_agents_md`, `set_generic_agents_md`, `clear_generic_agents_md`, raw probe)
- Modify: `client/firekeep_client/cli.py` (`_selected_runtimes` ~262, add `_generic_is_configured`)
- Test: `client/tests/test_generic_config.py` (new)

**Interfaces:**
- Produces: `resolver.generic_agents_md() -> Path | None`, `resolver.set_generic_agents_md(path)`, `resolver.clear_generic_agents_md()`; `cli._generic_is_configured() -> bool`; `cli._selected_runtimes(runtime, *, include_generic=False)`.
- Consumes: `resolver._config_path`, `configparser`.

- [ ] **Step 1: Write failing tests**

```python
# client/tests/test_generic_config.py
import configparser
from pathlib import Path
from firekeep_client import resolver
from firekeep_client import cli

def test_generic_is_configured_false_without_section(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_CONFIG", str(tmp_path / "config"))
    (tmp_path / "config").write_text("[identity]\nagent_id = a\n", encoding="utf-8")
    assert cli._generic_is_configured() is False

def test_generic_probe_never_migrates_a_serverless_config(tmp_path, monkeypatch):
    p = tmp_path / "config"
    p.write_text("[identity]\nagent_id = a\n", encoding="utf-8")  # no [server] -> load_config would migrate
    monkeypatch.setenv("FIREKEEP_CONFIG", str(p))
    before = p.read_bytes()
    cli._generic_is_configured()
    assert p.read_bytes() == before  # untouched: raw read, no migration

def test_set_then_read_generic_agents_md(tmp_path, monkeypatch):
    p = tmp_path / "config"
    p.write_text("[server]\nx = 1\n[identity]\nagent_id = a\n", encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(p))
    resolver.set_generic_agents_md(tmp_path / "AGENTS.md")
    assert resolver.generic_agents_md() == (tmp_path / "AGENTS.md").resolve()
    assert cli._generic_is_configured() is True
    assert "[server]" in p.read_text(encoding="utf-8")  # other sections preserved

def test_selected_runtimes_all_excludes_generic_by_default():
    assert cli._selected_runtimes("all") == ["claude", "codex", "kiro", "opencode"]

def test_selected_runtimes_all_includes_generic_when_flagged():
    assert cli._selected_runtimes("all", include_generic=True)[-1] == "generic"

def test_selected_runtimes_single_is_unchanged():
    assert cli._selected_runtimes("generic") == ["generic"]
```

- [ ] **Step 2: Run to verify fail** — Run: `cd client && python -m pytest tests/test_generic_config.py -v` → FAIL (functions undefined).

- [ ] **Step 3: Implement**

In `resolver.py`:

```python
def generic_agents_md(path: Path | None = None) -> Path | None:
    cfg = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
    try:
        cfg.read(_config_path(path), encoding="utf-8")   # raw; never migrates or raises on missing
    except (configparser.Error, OSError, UnicodeError):
        return None
    val = cfg.get("generic", "agents_md", fallback="").strip()
    return Path(val).expanduser().resolve() if val else None

def _round_trip(path: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
    if path.exists():
        cfg.read(path, encoding="utf-8")
    return cfg

def set_generic_agents_md(target: Path, path: Path | None = None) -> None:
    p = _config_path(path); cfg = _round_trip(p)
    if not cfg.has_section("generic"):
        cfg.add_section("generic")
    cfg.set("generic", "agents_md", str(Path(target).expanduser().resolve()))
    with open(p, "w", encoding="utf-8") as fh:
        cfg.write(fh)

def clear_generic_agents_md(path: Path | None = None) -> None:
    p = _config_path(path); cfg = _round_trip(p)
    if cfg.has_section("generic"):
        cfg.remove_section("generic")
        with open(p, "w", encoding="utf-8") as fh:
            cfg.write(fh)
```

In `cli.py`, replace `_selected_runtimes` and add the probe (delegates to resolver so there is one raw-read):

```python
def _generic_is_configured() -> bool:
    from firekeep_client import resolver
    return resolver.generic_agents_md() is not None

def generic_agents_md():
    from firekeep_client import resolver
    return resolver.generic_agents_md()

def _selected_runtimes(runtime: str, *, include_generic: bool = False) -> list[str]:
    if runtime == "all":
        return ["claude", "codex", "kiro", "opencode"] + (["generic"] if include_generic else [])
    return [runtime]
```

> Update the `get_adapter` branch (Task 2) to import `generic_agents_md` from `resolver`, not `cli`, to avoid a cycle.

- [ ] **Step 4: Verify pass** — Run the new test file + `tests/test_cli_install.py tests/test_cli_uninstall.py` → PASS (the four-count invariant holds; `_selected_runtimes` callers still pass one arg until Task 4/6).

- [ ] **Step 5: Commit** —
```bash
git add client/firekeep_client/resolver.py client/firekeep_client/cli.py client/firekeep_client/adapters/__init__.py client/tests/test_generic_config.py
git commit -m "feat(client): persist [generic] agents_md + pure config-aware _selected_runtimes"
```

---

### Task 4: CLI flags + persist-before-render (cli.py)

**Files:** Modify `client/firekeep_client/cli.py` (`--runtime choices` ~2112, add `--agents-md`, `gateway --runtime` help ~2221, `_configure` ~330-355, install render loop ~450). Test: `client/tests/test_cli_generic_install.py` (new).

**Interfaces:** Consumes Task 3 `set_generic_agents_md`, `_generic_is_configured`, `_selected_runtimes(..., include_generic=)`.

- [ ] **Step 1: Failing test**

```python
# client/tests/test_cli_generic_install.py — assert persist happens BEFORE the render loop
def test_install_generic_persists_agents_md_and_renders_generic(tmp_path, monkeypatch):
    # Arrange an isolated home + config; run cmd_install with a fake argparse Namespace
    # (runtime="generic", agents_md=<file>, non_interactive=True, host=...); capture the
    # rendered adapters. Assert: resolver.generic_agents_md() == the file AND GenericAdapter
    # was among the rendered runtimes.  (Follow the fixture style in test_cli_install.py.)
    ...

def test_install_agents_md_without_generic_runtime_errors(tmp_path):
    # cmd_install(Namespace(runtime="claude", agents_md=<file>)) -> nonzero / SystemExit
    ...
```

- [ ] **Step 2: Verify fail.**
- [ ] **Step 3: Implement.** In the `--runtime` argument, extend `choices` to include `"generic"`. Add `inst.add_argument("--agents-md", type=str, default=None)`. In `cmd_install`, after parsing and before `_configure`'s render, add the manual guard and persist:

```python
    if getattr(args, "agents_md", None) and args.runtime != "generic":
        print("firekeep: --agents-md is only valid with --runtime generic", file=sys.stderr)
        return 2
```

Inside `_configure`, just before `if changed: cfg.write(handle)` (cli.py:353), persist so the render loop can read it:

```python
    if getattr(args, "runtime", None) == "generic" and getattr(args, "agents_md", None):
        cfg.setdefault("generic", {})  # ConfigParser: use add_section if missing
        if not cfg.has_section("generic"):
            cfg.add_section("generic")
        cfg.set("generic", "agents_md", str(Path(args.agents_md).expanduser().resolve()))
        changed = True
```

Update the render loop (cli.py:450) and uninstall loop to pass the flag:

```python
    for name in _selected_runtimes(args.runtime, include_generic=_generic_is_configured()):
        get_adapter(name).render(venv_bin=venv_bin)
```

Update the `gateway --runtime` help text (cli.py:2221) to mention generic.

- [ ] **Step 4: Verify pass** + rerun `tests/test_cli_install.py`.
- [ ] **Step 5: Commit** `feat(client): --runtime generic + --agents-md, persist before render`.

---

### Task 5: Doctor — per-runtime hash + configured-but-broken row (cli.py, base.py)

**Files:** Modify `cli.py` (`_INSTRUCTION_RUNTIMES` ~1083, `_check_runtime_instructions` ~1095-1126, add generic hint), `base.py` (`rendered_instructions_path` ~630-648, `read_rendered_instructions_hash` — add `"generic"` branch reading the persisted path). Test: `client/tests/test_cli_doctor_generic.py`.

**Interfaces:** Consumes Task 1 `RENDERED_GENERIC_INSTRUCTIONS_HASH`, Task 3 `generic_agents_md`.

- [ ] **Step 1: Failing tests**

```python
def test_doctor_generic_block_reports_ok_not_edited(tmp_path, monkeypatch):
    # configure [generic] agents_md at a file, render the generic block there,
    # then _check_runtime_instructions("generic") -> status "ok" (NOT "edited"/"stale").
    ...
def test_doctor_generic_configured_but_missing_target_reports_broken(tmp_path, monkeypatch):
    # [generic] set, file absent -> a "warn" row naming the path (NOT None/silent).
    ...
def test_doctor_four_runtime_user_gets_no_generic_row(tmp_path, monkeypatch):
    # no [generic] -> _check_runtime_instructions("generic") is None.
    ...
```

- [ ] **Step 2: Verify fail.**
- [ ] **Step 3: Implement.**
  - `base.rendered_instructions_path`: add `if runtime == "generic": return resolver.generic_agents_md()`.
  - In `_check_runtime_instructions` (cli.py), compute a per-runtime expected hash and use it at lines 1112 and 1125:
    ```python
    expected = RENDERED_GENERIC_INSTRUCTIONS_HASH if runtime == "generic" else RENDERED_INSTRUCTIONS_HASH
    # ... replace `== RENDERED_INSTRUCTIONS_HASH` with `== expected` and the stale-message hash with `expected`.
    ```
  - Presence-gating split: today `root.exists()` False → `return None`. For generic, when it is *configured* (`generic_agents_md() is not None`) but the target/parent is missing, return `("generic-instructions", "warn", f"target {path} is missing — run `firekeep install --runtime generic --agents-md {path}`")` instead of None.
  - Add `"generic"` to `_INSTRUCTION_RUNTIMES`.
  - Add the discovery hint in `cmd_doctor`/install summary when `[generic]` is absent.
- [ ] **Step 4: Verify pass** + rerun `tests/test_cli_doctor.py`.
- [ ] **Step 5: Commit** `feat(client): doctor — generic instruction row (per-runtime hash, broken-target)`.

---

### Task 6: Uninstall — include generic + dynamic banner + orphan reporting (cli.py)

**Files:** Modify `cli.py` `cmd_uninstall` (~635-718: loop ~691, banner ~647, failed/kept ~709-718). Test: `client/tests/test_cli_uninstall_generic.py`.

- [ ] **Step 1: Failing tests**

```python
def test_uninstall_strips_generic_block(tmp_path, monkeypatch):
    # [generic] set + block rendered -> uninstall removes the block from the user's file.
    ...
def test_uninstall_reports_generic_when_config_read_fails(tmp_path, monkeypatch):
    # corrupt config so the generic path can't be read -> uninstall lists the orphaned
    # file in the 'kept/failed' output rather than silently deleting ~/.firekeep.
    ...
def test_uninstall_four_runtime_user_unchanged(tmp_path):
    # no [generic] -> exactly four unrender() calls (existing invariant).
    ...
```

- [ ] **Step 2: Verify fail.**
- [ ] **Step 3: Implement.** Read the generic path *before* any deletion. Pass `include_generic=_generic_is_configured()` to the uninstall `_selected_runtimes("all", ...)` loop. Make the banner render the actual selected list. On a skipped/failed generic unrender, append the target file to the printed `failed`/`kept` lines.
- [ ] **Step 4: Verify pass** + rerun `tests/test_cli_uninstall.py`.
- [ ] **Step 5: Commit** `feat(client): uninstall reaches generic + dynamic banner + orphan report`.

---

### Task 7: Contract-matrix generic column (matrix.py)

**Files:** Modify `client/firekeep_client/contract/matrix.py` (`RUNTIMES` ~43, `MATRIX` cells). Test: extend `client/tests/contract/test_matrix.py`.

- [ ] **Step 1: Failing test** — `capabilities("generic")` returns the six cells:
```python
def test_generic_column_is_honestly_degraded():
    caps = capabilities("generic")
    assert caps["briefing"] == "none (MCP only)"
    assert caps["pre_edit_block"] == "none"
    assert caps["precompact"] == "none"
    assert caps["presence"] == "sidecar (manual today)"
    assert caps["reconcile"] == "self-reported"
    assert "no /personal command" in caps["bypass"]
```
- [ ] **Step 2: Verify fail.**
- [ ] **Step 3: Implement.** Add `"generic"` to `RUNTIMES` and a `"generic"` key to each of the six `MATRIX[cap]` dicts with the values above (match the file's existing substring style — `no /personal command`, `sidecar (manual today)`).
- [ ] **Step 4: Verify pass** — `cd client && python -m pytest tests/contract/test_matrix.py -v` (incl. `test_all_runtimes_have_full_capability_set`).
- [ ] **Step 5: Commit** `feat(client): honest generic column in the capability matrix`.

---

### Task 8: Wizard discovery prompt (wizard.py)

**Files:** Modify `client/firekeep_client/wizard.py` (add a field to `Plan` ~52 and one skippable question in `prompt_config`), `cli.py` (consume `plan.generic_agents_md`). Test: `client/tests/test_wizard_generic.py`.

- [ ] **Step 1: Failing test** — a stubbed-input `prompt_config` returns a `Plan` whose `generic_agents_md` is the pasted path (and `None` when the answer is empty).
- [ ] **Step 2: Verify fail.**
- [ ] **Step 3: Implement.** Add `generic_agents_md: str | None = None` to the `Plan` NamedTuple. After the existing questions, ask: *"Also use an MCP client we don't ship an adapter for (Cursor, Windsurf, Gemini CLI, …)? Paste the path to its rules/AGENTS.md file, or press Enter to skip."* Stash the (non-empty) answer on the `Plan` — do **not** touch the filesystem (wizard contract). In `cmd_install`, when `plan.generic_agents_md`, set `args.runtime="generic"`-equivalent persistence: `resolver.set_generic_agents_md(plan.generic_agents_md)` before the render loop.
- [ ] **Step 4: Verify pass.**
- [ ] **Step 5: Commit** `feat(client): wizard — optional 'other MCP client' discovery prompt`.

---

### Task 9: Full client suite — the zero-loss gate

- [ ] **Step 1:** `cd client && python -m pytest -q` → all green.
- [ ] **Step 2:** Specifically confirm green: `tests/adapters/` `tests/test_kit_smoke.py` `tests/test_cli_doctor.py` `tests/test_cli_install.py` `tests/test_cli_uninstall.py` `tests/contract/test_matrix.py` `tests/adapters/test_instruction_stamp.py` `tests/adapters/test_write_stability.py`.
- [ ] **Step 3:** If anything in the four's suites moved, STOP — a byte-identical violation. Fix before proceeding.
- [ ] **Step 4: Commit** (if any fixups) `test(client): confirm zero-loss guard suite green`.

---

### Task 10: Repo docs (CLAUDE.md, guides, DEPLOYMENT.md)

**Files:** `CLAUDE.md` (adapter/runtime references), `docs/guides/client-kit.md` (the adapters section), `docs/DEPLOYMENT.md` (runtime list). No new tests; prose.

- [ ] **Step 1:** Add "any MCP client (`--runtime generic`)" as a supported runtime with the honest note (MCP tools + auto-instructions-on-connect; no hook lifecycle). Note `--agents-md` and the wizard prompt.
- [ ] **Step 2:** Verify the Change-Consistency-Checklist files (CLAUDE.md) are all updated where they enumerate the four adapters.
- [ ] **Step 3: Commit** `docs: document the generic (any MCP client) runtime`.

---

### Task 11: Site text — "any MCP client" tier + caveat co-location (firekeep-site)

**Files (E:\Documents\Projects\firekeep-site):** `index.html` (hero lede :840, trust-row :854, runtime band :887-896, #product arch box + aria-label :1313-1315, how-step :1335, pricing :1361, FAQ :1395, meta/OG/JSON-LD :7/:16/:26/:38), `docs.html` (capability matrix :1050-1053 — new generic row beside codex; intro :250, :868, :902, :1041), mirrors (`index.md`, `llms.txt`, `llms-full.txt`, `server-card.json`, `agents-md-vs-memory.html`, `dexes.html`).

- [ ] **Step 1:** Add "any MCP client" as a first-class tier everywhere the four are listed, and **carry the "MCP tools yes, lifecycle automation no" caveat next to every prominent mention** (hero, trust-row, pricing, meta), not only the matrix/FAQ.
- [ ] **Step 2:** Validate HTML/JSON well-formed (parse index.html/docs.html; json.load server-card.json).
- [ ] **Step 3: Commit** (site repo) `feat(site): add the "any MCP client" adapter tier + caveats`.

---

### Task 12: Site logos (firekeep-site)

**Files:** create `firekeep-site/brand/logos/{claude,codex,kiro,opencode,generic}.svg` (official, unmodified; generic = neutral MCP glyph), modify `index.html` runtime band (`:887-896`) + arch box, add trademark footnote. CSP: self-hosted SVG only (`img-src 'self' data:`).

- [ ] **Step 1:** Source each official SVG; commit under `brand/logos/`. Codex → OpenAI mark; generic → neutral glyph (no vendor mark).
- [ ] **Step 2:** Swap the CSS monogram boxes for the logos at ~19px; add the generic tile; add "Works with — <names> are trademarks of their respective owners; shown to indicate compatibility."
- [ ] **Step 3:** Render at 1280px (playwright, local http server) and eyeball; validate HTML.
- [ ] **Step 4: Commit** (site) `feat(site): official adapter logos, self-hosted + attributed`.
- [ ] **Step 5: Deploy** — backup then tar-over-SSH per the site-publish runbook; verify `/` 200 + the new tier/logos live.

---

## Self-Review

1. **Spec coverage** — §4.1 GenericAdapter→T2; §4.2 stamp/text→T1; §4.3 persist→T3/T4; §4.4 selection→T3; §4.5 flags→T4; §4.6 wizard→T8; §4.7 doctor→T5; §4.8 matrix→T7; §4.9 uninstall→T6; §4.10 hints→T5; §5 flow→T4/T6; §6 snippet→T2; §7 errors→T2/T3; §8 tests→each task; §9 site/docs→T10/T11/T12; §10 zero-loss→T9; §11 codex-follow-up not implemented (correct). ✔ all covered.
2. **Placeholder scan** — T4/T5/T6/T8 use "follow the fixture style in test_cli_*.py" for the *arrange* half of CLI tests rather than a full fixture transcription; the *assert* is concrete. This is a deliberate pointer to an existing pattern the implementer must read, not a TBD — acceptable, but the implementer MUST open `test_cli_install.py` first. No literal TODO/TBD.
3. **Type consistency** — `_selected_runtimes(runtime, *, include_generic=False)`, `generic_agents_md() -> Path|None`, `set_generic_agents_md(target)`, `GenericAdapter(agents_md=Path|None)`, `RENDERED_GENERIC_INSTRUCTIONS_HASH`, `GENERIC_INSTRUCTIONS` — consistent across T1-T8. `generic_agents_md` lives in `resolver` (imported by both `cli` and `adapters.generic`) to avoid a cycle — consistent in T2/T3/T5.
