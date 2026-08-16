# Generic "any MCP client" adapter — Design Spec

**Date:** 2026-08-16
**Status:** Approved for planning (design + adversarial spec-verification complete)
**Scope:** Add a `generic` runtime adapter to the Firekeep client kit that connects *any*
MCP-capable client (Cursor, Windsurf, Gemini CLI, Cline, Zed, …). Additive — **byte-identical**
behavior for the existing four adapters. Defers the manifest-registry refactor.

> **Revision note (post-verification):** an adversarial pass against the source found that the
> marker-block engine stamps every block with one global hash, and that `resolver.load_config()`
> has write side effects. §4.2/§4.4/§4.7 and §10 below are written to those facts. The zero-loss
> guarantee is therefore **"shared helpers gain optional parameters that default to today's exact
> behavior; the four render byte-identically,"** not "no shared code is touched."

---

## 1. Goal

Let a user on an MCP client the kit ships no bespoke adapter for get Firekeep's **universal floor**
— the MCP gateway registered and the instruction protocol delivered — through a real, first-class
runtime (`firekeep install --runtime generic --agents-md <path>`), while being **honest** that the
hook-driven lifecycle automation (auto-briefing, pre-edit blocking gate, stop→learn, compaction
checkpoint, presence) does **not** apply to a client with no hook surface.

## 2. Background — what the four adapters actually do

Confirmed by full reads of `client/firekeep_client/adapters/{base,claude,codex,kiro,opencode}.py`.

**The universal floor is exactly two things**, done by every adapter:
1. **Register the one MCP gateway** — `shim_servers(venv_bin, self.name)` → `{"firekeep": (<firekeep
   exe>, ["gateway","--runtime",<name>])}` written into the runtime's native config, differing only
   in serialization shape.
2. **Render the instruction surface** — the same `FIREKEEP_INSTRUCTIONS` text (base.py:444), injected
   as an `INSTRUCTIONS_BEGIN`/`INSTRUCTIONS_END` marker block into a user-owned file (claude
   `CLAUDE.md`, codex/opencode `AGENTS.md`) or a whole owned file (kiro steering).

**A free third tier**: the gateway's MCP `initialize` handshake already serves `GATEWAY_INSTRUCTIONS`
(embedding `MCP_SERVER_INSTRUCTIONS`) to *any* client that connects — so a generic client receives
the short-form protocol on connect with zero adapter involvement.

**Per-runtime extras a generic client cannot get** (all ride hooks): auto-briefing, pre-compaction
checkpoint, stop→learn, pre-edit **blocking** gate, presence deregister, turn cadence — plus the
bespoke machinery (opencode JS bridge, kiro steering+grants, claude `/personal`, codex doctor check).
**Codex is the precedent**: already the no-hooks runtime.

### Integration seams (grounded, verified with file:line)

- **Dispatch** — `adapters/__init__.py:7` `get_adapter(name)` `if`-ladder, lazy import, `ValueError`
  on unknown; **no test asserts the message**.
- **Contract** — `adapters/base.py:267` `Adapter(ABC)`: `render(*, venv_bin)` + `unrender()`.
- **Marker engine** — `upsert_marked_block(existing, content)` (`base.py:~569`) builds
  `f"{INSTRUCTIONS_BEGIN}\n{content}{INSTRUCTIONS_END}\n"` at `base.py:577`; `INSTRUCTIONS_BEGIN`
  (`base.py:510`) hardcodes `h={RENDERED_INSTRUCTIONS_HASH}` = `_hash12(FIREKEEP_INSTRUCTIONS)`
  (`base.py:500`). `strip_marked_block` keys off `INSTRUCTIONS_BEGIN_PREFIX`.
- **Install** — `cli.py:330` `_configure` forces `args.runtime="all"` when unset; `cli.py:2112`
  `--runtime choices=[claude,codex,kiro,opencode,all]`; `cli.py:262` **pure**
  `_selected_runtimes(runtime)` → four for `"all"`; render loop `cli.py:450`
  `get_adapter(name).render(venv_bin=venv_bin)` (no path argument channel).
- **Uninstall** — `cli.py:691` loops `_selected_runtimes("all")` → `unrender()`; banner `cli.py:647`
  hardcodes the four (**no test asserts its text**); `~/.firekeep` deleted at `cli.py:705`.
- **Doctor** — `cli.py:1083` `_INSTRUCTION_RUNTIMES`; `_check_runtime_instructions` (`cli.py:1096`)
  returns `None`/silent when there is no on-disk trace (path `None` from
  `rendered_instructions_path`, `base.py:648`, returns `None` for unknown), **and compares the
  on-disk hash against the single global `RENDERED_INSTRUCTIONS_HASH`** (`cli.py:1108-1126`).
  `_check_codex_adapter` (`cli.py:1037`) is codex-specific.
- **Wizard** — asks only identity + "where is your server"; no runtime step; contract: never touches
  the filesystem, returns a `Plan` (`wizard.py:52`).
- **Config** — INI at `~/.firekeep/config` (`resolver.py:16`); `resolver.load_config()`
  (`resolver.py:164`) **raises `ConfigError` on a missing file (`:166`) and — critically — MIGRATES
  and REWRITES the file (`.bak` backup, atomic rewrite, stderr output, possible
  `ConfigMigrationConflict`) when `[server]` is absent (`:185-189`)**. It is not a pure read.
- **Matrix** — `contract/matrix.py:43` `RUNTIMES` × 6 caps; human-read only. Adding a runtime forces
  a full column via **two** tests (`test_all_runtimes_have_full_capability_set`,
  `test_matrix_contains_no_retired_profile_pin_capability`); the four existing columns are asserted
  by name (unaffected).

## 3. Design overview

A real `GenericAdapter` selected by `--runtime generic`, made lifecycle-correct by **one mechanism**:
persist the generic target path in the kit config, and make the `"all"` fan-out **config-aware** so
`generic` joins install/update/uninstall *only when configured*.

- **Four-runtime user** (no `[generic]`): the fan-out is exactly the four → **byte-identical**.
- **Generic user** (`[generic] agents_md=<path>`): `firekeep update`/reinstall re-renders the block,
  `firekeep uninstall` strips it.

## 4. Components

### 4.1 `GenericAdapter` — `client/firekeep_client/adapters/generic.py` (new)

`class GenericAdapter(Adapter)`, `name = "generic"`, constructed with an optional target
(`GenericAdapter(agents_md: Path | None = None)`). `get_adapter("generic")` builds it from the
**persisted** `[generic] agents_md` (§4.3), so the install/uninstall loops need no signature change.

- `render(*, venv_bin)`:
  1. **Always** print a standard MCP-server JSON snippet from `shim_servers(venv_bin, "generic")` →
     `json.dumps({"mcpServers":{"firekeep":{"command":<exe>,"args":["gateway","--runtime","generic"]}}}, indent=2)`,
     with a paste instruction and the honest-degradation note (§6).
  2. **If `self.agents_md` is set**: upsert the generic instruction block into that file (§4.2) via
     `write_text_if_changed`, after the **collision check** (§7).
- `unrender()`: strip the generic block if the target carries the generic marker; else **no-op**
  (safe when never opted in).

### 4.2 Two instruction texts, correctly stamped (the P1/P2 fix) — `base.py`

**Problem:** `upsert_marked_block` hardcodes `INSTRUCTIONS_BEGIN`, whose stamp is the *four's* hash.
A generic block rendered through it would carry the wrong stamp, and doctor (`cli.py:1108-1126`)
compares against the one global `RENDERED_INSTRUCTIONS_HASH` → a permanent false "edited/stale" warn.

**Resolution — make the stamp content-derived** (which the code's own comment, base.py:503-509,
already says it *should* be — `upsert_marked_block` just hardcodes the constant instead):
- `upsert_marked_block(existing, content)` computes its begin line **from `content`** —
  `_stamped_begin(content) = f"{INSTRUCTIONS_BEGIN_PREFIX} h={_hash12(content)} — firekeep-owned
  block, do not edit; re-rendered by \`firekeep install\` -->"` — replacing the hardcoded
  `INSTRUCTIONS_BEGIN` at base.py:577. For the four (content = `FIREKEEP_INSTRUCTIONS`),
  `_hash12(content) == RENDERED_INSTRUCTIONS_HASH`, so the begin line is **byte-identical** — no
  change to the four's rendered files (pinned by test 11 + existing `test_instruction_stamp`).
- New text for the no-hooks runtimes:
  `GENERIC_INSTRUCTIONS = MEMORY_INSTRUCTIONS_NO_HOOKS + "\n\n" + DECISION_INSTRUCTIONS + "\n\n" +
  KNOWLEDGE_INGEST_INSTRUCTIONS`; `RENDERED_GENERIC_INSTRUCTIONS_HASH = _hash12(GENERIC_INSTRUCTIONS)`.
  `MEMORY_INSTRUCTIONS_NO_HOOKS` = `MEMORY_INSTRUCTIONS` with the one hook-dependent clause
  (~base.py:405-407, *"routine single-file edits are already gated by hooks and need no declaration"*)
  **removed/reworded** — a generic client has no gate, so that line would tell it a gate exists.
  Factor the shared text so the two memory variants differ by exactly that clause.
- Generic renders `GENERIC_INSTRUCTIONS` through the **same** `upsert_marked_block` — the
  content-derived stamp gives it `RENDERED_GENERIC_INSTRUCTIONS_HASH` automatically. The begin
  **prefix** and `INSTRUCTIONS_END` are unchanged/shared, so `strip_marked_block` (prefix-matched)
  removes a generic block unchanged. The collision guard (§7) keeps a generic block and a four block
  from ever sharing a file, so a shared prefix is never ambiguous. **No parameterization of the
  marker helpers is needed** — only the stamp *source* changes (constant → content), byte-identical
  for the four.

**Codex note (out of scope, §11):** codex is also hookless and renders `FIREKEEP_INSTRUCTIONS`
(the with-hooks text), so it carries the same over-statement today. Reconciling codex to
`GENERIC_INSTRUCTIONS` is a *separate* zero-loss change; not done here, so codex stays byte-identical.

### 4.3 Config persistence — `[generic]` section, persisted BEFORE the render loop

```ini
[generic]
agents_md = /abs/resolved/path/to/AGENTS.md
```

- **Written inside `_configure` (before the `cli.py:450` render loop)** so `get_adapter("generic")`
  can read it during render (the loop offers no argument channel). Ordering is load-bearing: persist
  first, render second, or `--runtime generic --agents-md …` silently renders print-only and drops
  the flag on first run.
- Helpers: `set_generic_agents_md(path)` / `clear_generic_agents_md()` (write) and
  `generic_agents_md() -> Path | None` (read). The write **round-trips a loaded `ConfigParser`** and
  re-serializes all sections — never truncates `[server]`/`[identity]`/`[pins]`.
- `--agents-md` absent → print-only, **no `[generic]` section written** (so generic does not join
  `"all"`; it was a one-shot print).
- Path stored **absolute, `Path.resolve()`d**.

### 4.4 Config-aware selection — explicit parameter (the P3/P4 fix) — `cli.py`

Keep `_selected_runtimes` **pure**; compute the flag at the call site with a **side-effect-free**
config read (never `resolver.load_config()`, which can migrate/rewrite/raise):

```python
def _generic_is_configured() -> bool:
    # Raw, read-only, never migrates: a bare ConfigParser, no [server] requirement.
    cfg = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
    try:
        cfg.read(_config_path(), encoding="utf-8")   # silent on missing/corrupt
    except (configparser.Error, OSError, UnicodeError):
        return False
    return cfg.has_option("generic", "agents_md") and bool(cfg.get("generic", "agents_md").strip())

def _selected_runtimes(runtime: str, *, include_generic: bool = False) -> list[str]:
    if runtime == "all":
        return ["claude", "codex", "kiro", "opencode"] + (["generic"] if include_generic else [])
    return [runtime]
```

Both call sites — install (`cli.py:450`) and uninstall (`cli.py:691`) — pass
`include_generic=_generic_is_configured()`. `_selected_runtimes` stays a pure function of its args
(no home-directory dependence — see the P4 test-fixture hazard). Unconfigured → exactly the four.

### 4.5 CLI flags (`cli.py:2112`, `_configure` `cli.py:330`)

- Add `"generic"` to `--runtime choices`.
- Add `--agents-md <path>`. argparse cannot express "only valid with `--runtime generic`" → a
  **manual check in `cmd_install`**: error if `--agents-md` given with any other `--runtime`.
- `--runtime generic` bypasses the `args.runtime="all"` default.
- **Consistency:** `gateway --runtime` (`cli.py:2221`) has no `choices` (so `--runtime generic`
  already works there) but its help text lists four — update it.

### 4.6 Wizard discovery prompt (`wizard.py`)

One **skippable** question after the existing two (ask→`Plan`→cli, no filesystem touch): *"Also use
an MCP client we don't ship an adapter for (Cursor, Windsurf, Gemini CLI, …)? Paste the path to its
rules/AGENTS.md file, or press Enter to skip."* A non-empty answer flows to `cli.py`, which persists
`[generic] agents_md` (before render, §4.3) and renders generic. This is the primary discovery path.

### 4.7 Doctor — per-runtime hash + configured-but-broken row (P2/P8) — `cli.py`, `base.py`

- Add `"generic"` to `_INSTRUCTION_RUNTIMES` (`cli.py:1083`). `rendered_instructions_path("generic")`
  returns the **persisted** path (or `None` when unconfigured).
- **`_check_runtime_instructions` must compare against a per-runtime expected hash**, not the global
  `RENDERED_INSTRUCTIONS_HASH`: four → `RENDERED_INSTRUCTIONS_HASH`, generic →
  `RENDERED_GENERIC_INSTRUCTIONS_HASH`. Without this, a correctly-rendered generic block reads as
  "edited/stale" forever (P2). This is an additive map; the four keep `RENDERED_INSTRUCTIONS_HASH`.
- **Presence-gating honesty split (P8):** for the four, a missing target = "runtime not installed" →
  silent (unchanged). For generic, `[generic]` is *configured*, so a missing target file/dir is a
  **broken** state doctor should **report** ("generic instruction target `<path>` is missing —
  re-run `firekeep install --runtime generic --agents-md <path>`"), not hide. Four-runtime users
  (no `[generic]`, path `None`) still get **no generic row**.
- Print-only registration (no `--agents-md`) → nothing to verify → doctor stays silent about it.
  `_check_codex_adapter` untouched.

### 4.8 Contract-matrix generic column (`contract/matrix.py:43`)

Add `"generic"` to `RUNTIMES` + a full 6-cell column with explicit `test_matrix.py` assertions,
using the file's **existing substring conventions** (P9):

| capability | generic |
|---|---|
| briefing | `none (MCP only)` |
| presence | `sidecar (manual today)` |
| pre_edit_block | `none` |
| precompact | `none` |
| reconcile | `self-reported` |
| bypass | `firekeep personal CLI / env (no /personal command)` |

### 4.9 Uninstall (`cli.py:691`, banner `cli.py:647`, orphan reporting `cli.py:709-718`) — P7

- Loop passes `include_generic=_generic_is_configured()` → reaches
  `GenericAdapter(agents_md=<persisted>).unrender()` → strips the block.
- Banner made **dynamic** (render `_selected_runtimes("all", include_generic=…)`), never a stale
  four-name literal.
- **Orphan safety (P7):** if the config read fails (corrupt) so generic is skipped, then `~/.firekeep`
  is deleted (`cli.py:705`) — the block is orphaned in the user's AGENTS.md **and** the record of its
  path is gone, unrecoverable. So: read the generic path **before** any deletion, and if unrender was
  skipped/failed, add an explicit line to the `failed`/`kept` report (`cli.py:709-718`) naming the
  file the user must clean by hand. (The "clear `[generic]`" step is moot on full uninstall — the
  home is deleted — but matters for a future per-runtime `unrender`.)

### 4.10 Discovery hints

- **Install summary** + **`firekeep doctor`** (when `[generic]` absent): one dim line — "Using
  another MCP client? `firekeep install --runtime generic --agents-md <path>`, or re-run the
  installer and answer the last question."

## 5. Data flow

**Install (generic):** `firekeep install --runtime generic --agents-md ~/.cursor/AGENTS.md` →
`_configure` persists `[generic] agents_md` → render loop builds generic from config → prints snippet
+ writes the hook-free block. **Wizard:** same effect from the discovery answer.

**Update / reinstall:** `cmd_update` → `_exec_bootstrap` → `bootstrap/install.sh` →
`firekeep install …` → `cmd_install` → `_selected_runtimes(args.runtime, include_generic=…)` with
`args.runtime` defaulted to `"all"` → re-renders four **+ generic** (kept current). **Caveat:** if the
pre-existing `FIREKEEP_RUNTIME` env var is set, the bootstrap passes `--runtime <that>` and an update
re-renders exactly one runtime (`install.sh:259`, `_exec_bootstrap` copies `os.environ` wholesale) —
so "update keeps the generic block current" holds only when `FIREKEEP_RUNTIME` is unset. Not
introduced here; state it.

**Uninstall:** loop includes generic (when configured) → strips the block → orphan-safe reporting.

## 6. Printed snippet + honest degradation

Stdout on `render` (illustrative):

```
Firekeep works with any MCP client. Paste this into your client's MCP config:

  { "mcpServers": { "firekeep": { "command": "<~/.firekeep/current/bin/firekeep>",
                                  "args": ["gateway","--runtime","generic"] } } }

You get: all MCP tools (memory, sessions, coordination, code intelligence), and the
cognitive protocol is delivered automatically when your client connects.
You do NOT get (a generic client exposes no hooks Firekeep can wire): auto-briefing,
the pre-edit blocking gate, stop→learn, and the pre-compaction checkpoint.
Point --agents-md at your client's rules file to also install the protocol as text.
```

With `--agents-md`, the JSON is printed **and** the hook-free block is upserted into the named file.

## 7. Error handling

- `--agents-md` unwritable / parent missing → **warn to stderr, do not abort**; the printed snippet
  (the load-bearing half) still succeeds. Mirrors codex's instruction-best-effort discipline
  (`test_codex_instruction_write_failure_warns_but_keeps_mcp_config`).
- `--agents-md` without `--runtime generic` → manual `cmd_install` error (argparse can't express it).
- `[generic] agents_md` points at a since-deleted file: on update, re-create it (upsert into a fresh
  file); on uninstall, absent file → no-op strip; doctor reports the broken target (§4.7).
- Config probe (`_generic_is_configured`) uses a **raw `ConfigParser.read()`**, never
  `resolver.load_config()` — no migration, no write, no raise, no `ConfigMigrationConflict` swallow.
- **Colliding target:** refuse an `--agents-md` equal (by `Path.resolve()`) to any of the four
  adapters' fixed instruction paths — `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`,
  `~/.kiro/steering/firekeep-instructions.md`, `<opencode config>/AGENTS.md` (`XDG_CONFIG_HOME` or
  `~/.config/opencode`) — with "that file is already managed by the <runtime> adapter." **Re-check on
  every render** (`XDG_CONFIG_HOME` can change between installs).

## 8. Testing

**New — `client/tests/adapters/test_generic.py`** (mold: `test_codex.py`, the no-hooks template):
1. `test_generic_render_prints_mcp_snippet` — `capsys` stdout is valid
   `{"mcpServers":{"firekeep":{...}}}`, `command == _exe(venv_bin/"firekeep")`,
   `args == ["gateway","--runtime","generic"]`.
2. `test_generic_render_writes_no_native_config` — nothing written outside the given AGENTS.md.
3. `test_generic_render_writes_no_hooks`.
4. `test_generic_win32_appends_exe` / `test_generic_posix_no_exe`.
5. `test_generic_output_states_no_lifecycle_automation`.
6. `test_generic_unrender_is_noop_when_never_opted_in`.
7. `test_generic_agents_md_upserts_hookfree_block` — asserts the block content is `GENERIC_INSTRUCTIONS`,
   is stamped with `RENDERED_GENERIC_INSTRUCTIONS_HASH`, uses the generic prefix, and **does NOT
   contain the "gated by hooks" clause**.
8. `test_generic_agents_md_non_clobbering` + `test_generic_unrender_strips_only_our_block`.
9. `test_generic_agents_md_rerender_is_byte_identical`.
10. `test_generic_refuses_colliding_target` (each of the four fixed paths).

**New — engine parameterization (base.py):**
11. `test_upsert_marked_block_default_args_are_byte_identical` — with no new kwargs, output equals the
    pre-change output for `FIREKEEP_INSTRUCTIONS` (the zero-loss pin for §4.2).
12. `test_upsert_marked_block_with_generic_markers_stamps_generic_hash`.

**New — config + selection:**
13. `test_selected_runtimes_all_excludes_generic_by_default` (pure, `include_generic=False` → four).
14. `test_selected_runtimes_all_includes_generic_when_flag_true`.
15. `test_generic_is_configured_never_migrates` — a config missing `[server]` is NOT rewritten by the
    probe (guards the P3 fix; assert file mtime/bytes unchanged).
16. `test_install_generic_persists_agents_md_before_render` / `test_uninstall_generic_strips_block_and_reports`.

**New — doctor:**
17. `test_doctor_generic_block_reports_ok_not_edited` (guards P2 — per-runtime hash).
18. `test_doctor_generic_configured_but_missing_target_reports_broken` (P8).
19. `test_doctor_four_runtime_user_gets_no_generic_row`.

**New — matrix:** explicit `capabilities("generic")[...]` for all six cells (§4.8).

**Zero-loss guard (must stay green, unchanged) — the evidence for §10:**
`client/tests/adapters/` (all four), `test_base.py`, `test_instructions.py`, `test_instruction_stamp.py`,
`test_write_stability.py`, `test_kit_smoke.py::test_kit_hangs_together`, `test_cli_doctor.py`,
`test_predecessor_migration.py`, `test_matrix.py` (four columns), **and the two that bind the
selection invariant: `test_cli_install.py` (`len(rec.calls)==4` at :219/:232/:239/:250) and
`test_cli_uninstall.py` (`len(unrendered)==4` at :66)** (P6). All run with no `[generic]` section
(conftest.py:85 autouse isolation), so a green board proves the four unchanged.

## 9. Site + docs + logos (firekeep-site + repo docs)

**Text list** — extend the ~14 adapter enumerations to add "any MCP client" as a first-class tier and
**carry the "MCP tools yes, lifecycle automation no" caveat next to every prominent mention** — hero
lede `index.html:840`, trust-row `:854`, pricing `:1361`, meta/OG/JSON-LD `:7/:16/:26/:38`,
`server-card.json:4` — not only the four spots that already have it (runtime-band note `:896`, docs
matrix `docs.html:1050-1053`, FAQ `:1395`, pre-edit table `:1220-1226`). Add the generic row beside
codex in the docs matrix.

**Logos** ("real logos, done safely") — self-host each **official** SVG unmodified under
`firekeep-site/brand/logos/`, under a "Works with" caption + a trademark-attribution footnote
("Claude, Codex, Kiro, OpenCode are trademarks of their respective owners; shown to indicate
compatibility"). CSP (`.htaccess` `img-src 'self' data:`) forbids CDNs → self-hosted only. Swap the
CSS monogram boxes (`index.html:887-896`) for logo glyphs at ~19px; extend the `#product` arch box
(`:1313-1315`, incl. its `aria-label`); add a **generic tile** ("Any MCP client", neutral MCP glyph —
no vendor mark). Note: Codex rides the OpenAI mark (no distinct logo); Kiro is least-recognizable.

**Docs to update** — `docs.html` capability matrix + "which clients" prose; repo
`docs/guides/client-kit.md` (adapter/runtime section) + `docs/DEPLOYMENT.md`; `CLAUDE.md`
client/adapter references; site mirrors (`index.md`, `llms.txt`, `llms-full.txt`, `server-card.json`,
`agents-md-vs-memory.html`, `dexes.html`).

## 10. Zero-loss guarantee

The four adapters render **byte-identically** because every shared-code change is gated on
`[generic]` being configured or is a new default-preserving parameter:
- `_selected_runtimes` stays pure; `include_generic=False` → the four (tests 13, and the binding
  `test_cli_install`/`test_cli_uninstall` count tests, P6).
- `upsert_marked_block`/`strip_marked_block` gain keyword-only args defaulting to the four's constants
  → byte-identical (test 11; the existing `test_instruction_stamp`/`test_write_stability`).
- Doctor's per-runtime hash keeps `RENDERED_INSTRUCTIONS_HASH` for the four; generic contributes no
  row for unconfigured users (tests 17-19; `test_cli_doctor`).
- `FIREKEEP_INSTRUCTIONS`, the four adapters, all hook helpers, all TOML helpers, and codex — **not
  changed at all**.
- Matrix — additive column; the four columns' named assertions stay.

## 11. Follow-ups & out of scope

- **Out of scope:** the manifest-driven adapter registry (separate, test-guarded refactor).
- **Follow-up (separate zero-loss task):** codex, being hookless, renders the with-hooks text and
  carries the same "gated by hooks" over-statement; reconcile it to `GENERIC_INSTRUCTIONS` under its
  own change. **Decision for this spec: do NOT fold codex in here** — the parameterized marker engine
  (§4.2) handles generic without touching codex, keeping codex literally byte-identical and this
  change strictly additive. (If the owner prefers, folding codex now collapses to one no-hooks text
  for both no-hooks runtimes — but that changes codex's rendered bytes and is its own review.)
- **Possible v2:** auto-detect a client's config file to *write* rather than print — excluded; the kit
  does no tool-detection and guessing an unknown format risks clobbering.
