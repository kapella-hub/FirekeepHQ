# Dex Registry (milestone 1) + Docdex (D1/D2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the gateway's hardcoded `LOCAL_SERVERS` tuple with a manifest-driven dex registry, put Symdex behind it (opt-in for new installs, grandfathered for existing), and ship Docdex — the documents dex — as registry consumer #2.

**Architecture:** A new `firekeep_client/dexes.py` module owns known-dex manifests + the `~/.firekeep/dexes.json` installed-registry (atomic JSON, 0600). `gateway.py` mounts core services (shim×4 + decision) plus registered dexes whose manifest `kind == "mcp-stdio"`; `kind == "ingest-client"` mounts nothing. Docdex is a new top-level `docdex/` package → `firekeep-docdex` wheel (mirrors `symdex/` layout), bundled checksum-verified in releases exactly like symdex; **registration gates activity, not installation** — the signed supply chain is unchanged. Server-side Phase V is ALREADY BUILT (corpus visibility, dex scopes, committed generations) — zero server changes in this plan.

**Tech Stack:** Python 3.10+ stdlib for the client spine (import-boundary rule); `pypdf` + `python-docx` only inside the docdex wheel; hatchling build for docdex (symdex template).

## Global Constraints

- **Authoritative spec:** `docs/superpowers/specs/2026-08-15-docdex-design.md` — §2 modules, §3 wire contract, §5 invariants I1–I7, §6 tests. Where this plan compresses, the spec wins.
- Client spine stays stdlib-only (`client/tests/test_import_boundary.py`); `cli.py` may import `firekeep_docdex` ONLY lazily inside command functions.
- The two-question install experience must not regress — no new install-time questions (ROADMAP §5.4).
- Migration rule: an update never removes a capability an install already has — existing installs keep symdex; opt-in applies to new installs only.
- Dex credential (round 1, verified 2026-08-17): the enrolled member key already carries `dex:docdex` (`ENROLLABLE_SCOPES == SCOPES − {admin,*}`, `auth/keys.py:69`); no key minting client-side. Distinct per-dex subordinate keys = documented follow-up.
- Wire caps (spec §3): `FIREKEEP_DOCDEX_MAX_FILES=5000` (refuse source, loud), `FIREKEEP_DOCDEX_MAX_FILE_MB=25` (skip+count), `FIREKEEP_DOCDEX_MAX_EXTRACT_KB=400` (truncate+flag), `FIREKEEP_DOCDEX_SYNC_INTERVAL_HOURS=6`.
- Versions at release: client `1.0.0` (tag `client-v1.0.0`; bump `client/pyproject.toml`, `client/firekeep_client/__init__.py`, frozen pin `client/tests/test_package.py:10` together — memory `next-release-is-1-0-0`). Docdex wheel starts `0.1.0`. Symdex stays on its own line (0.2.x).
- Naming in user-facing text: "private-session mode (bypass)" for the client bypass (spec review #8); dexes are lowercase product names (symdex, docdex).

---

## Part A — Dex registry (milestone 1)

### Task A1: `firekeep_client/dexes.py` — manifests + registry file

**Files:**
- Create: `client/firekeep_client/dexes.py`
- Test: `client/tests/test_dexes.py`

**Interfaces (Produces):**
```python
KNOWN_DEXES: dict[str, DexManifest]          # {"symdex": ..., "docdex": ...}
@dataclass(frozen=True) class DexManifest:
    id: str            # "firekeep.symdex"
    name: str          # "symdex"  (registry key, console-script suffix)
    title: str         # "Symdex"
    indexes: str       # "code" | "documents"
    kind: str          # "mcp-stdio" | "ingest-client"
    console_script: str # "firekeep-symdex" | "firekeep-docdex"
    import_probe: str  # "firekeep_symdex" | "firekeep_docdex"
    description: str
def registry_path() -> Path                   # ~/.firekeep/dexes.json (FIREKEEP_CONFIG parent honored, like resolver)
def read_registry() -> dict[str, dict]        # {} on missing/corrupt (corrupt logged via hooklog, never raises)
def write_registry(entries: dict) -> None     # atomic tempfile+os.replace, chmod 0600
def registered() -> list[DexManifest]         # KNOWN manifests for names present in registry file
def add(name) / remove(name) -> DexManifest   # raise ValueError on unknown name; add stamps {"added_at": iso, "source": "bundled"}
def ensure_migrated(*, installing: bool = False) -> None   # the migration rule (Task A3)
```

The manifest is designed **as if public** (SDK ladder rung 1): every field a third-party dex.json would need, no client-internal leakage. `registry_path()` derives from the same home dir as `resolver.CONFIG_PATH` so `FIREKEEP_CONFIG`-isolated tests work unchanged.

- [ ] Write failing tests: known manifests (symdex mcp-stdio / docdex ingest-client), read-missing→{}, read-corrupt→{} (and a hooklog line), add/remove round-trip persists atomically with 0600, unknown name ValueError.
- [ ] Implement `dexes.py` (~120 lines; atomic write mirrors `state.py:479-488` `_write_atomic`).
- [ ] `pytest client/tests/test_dexes.py -v` → PASS. Commit `feat(client): dex manifests + installed registry`.

### Task A2: gateway mounts from the registry

**Files:**
- Modify: `client/firekeep_client/gateway.py:29,186-197`
- Modify: `client/tests/test_decision_registration.py:23,42-49` (deliberate contract update)
- Test: `client/tests/test_gateway.py` additions

`LOCAL_SERVERS = ("symdex", "decision")` splits: `CORE_LOCAL_SERVERS = ("decision",)` (decision is core infrastructure, not a dex — it indexes nothing). The local leg becomes:

```python
self.backends = [
    *(Backend(name, [shim, "--service", name]) for name in REMOTE_SERVICES),
    *(Backend(name, [_console_script(f"firekeep-{name}")]) for name in CORE_LOCAL_SERVERS),
    *(Backend(m.name, [_console_script(m.console_script)])
      for m in dexes.registered() if m.kind == "mcp-stdio"),
]
```

`dexes.ensure_migrated()` is called first (load fallback, Task A3). `backends` can never be empty (4 remote + decision), so `gateway.py:197` needs no guard — assert this in a test instead.

- [ ] Failing tests: registered symdex → symdex backend present with `firekeep-symdex` command; empty registry → no symdex backend, decision still present; ingest-client entries mount nothing.
- [ ] Implement; update `test_decision_registration.py` assertions to `CORE_LOCAL_SERVERS == ("decision",)` + registry-driven symdex (keep the shim_servers single-gateway assertion byte-identical).
- [ ] Full client suite → green. Commit `feat(client): gateway mounts dexes from the registry — decision stays core`.

### Task A3: migration rule

**Files:**
- Modify: `client/firekeep_client/dexes.py` (`ensure_migrated`)
- Modify: `client/firekeep_client/cli.py` `cmd_install` (call `ensure_migrated(installing=True)` before adapter render)
- Test: `client/tests/test_dexes.py` additions

Rule (deterministic, no questions): if `dexes.json` exists → do nothing. Absent: a config file with a `[server]` section exists (`resolver._raw_config()` — the side-effect-free reader, NEVER `load_config`) → **update** → write `{"symdex": {...}}` (grandfather). No `[server]` → **fresh** → write `{}` (opt-in). Gateway load calls `ensure_migrated()` too, covering updates that never re-ran install.

- [ ] Failing tests: absent+configured→symdex grandfathered; absent+unconfigured→empty file written; existing file untouched byte-for-byte; gateway-load path migrates.
- [ ] Implement. Suite green. Commit `feat(client): dex registry migration — updates keep symdex, fresh installs opt in`.

### Task A4: `firekeep dex` CLI

**Files:**
- Modify: `client/firekeep_client/cli.py` (new `cmd_dex`, parser at the `_build_parser` block ~cli.py:2213-2389)
- Test: `client/tests/test_cli_dex.py`

Positional-choices pattern (the `personal` precedent, cli.py:2255-2262 — no nested subparsers exist in the package):

```python
dex = sub.add_parser("dex", help="manage dexes — domain indexes the Keep understands")
dex.add_argument("action", nargs="?", choices=["list", "add", "remove"], default="list")
dex.add_argument("name", nargs="?", help="dex name (symdex, docdex)")
dex.set_defaults(func=cmd_dex)
```

`list` prints known dexes with registered/available state + one-line descriptions. `add`/`remove` mutate the registry and print what changed + "takes effect on next agent session" (gateway reads at startup). `add` verifies the wheel importable (`importlib.util.find_spec(manifest.import_probe)`) and FAILS LOUDLY naming the fix if absent. `remove symdex` warns it disables code intelligence but obeys.

- [ ] Failing tests: list output both states; add unknown → rc 1 + message; add w/ wheel present → registry written; remove; add w/o wheel → loud failure naming bootstrap reinstall.
- [ ] Implement + suite. Commit `feat(client): firekeep dex list/add/remove`.

### Task A5: doctor + suggestion surface

**Files:**
- Modify: `client/firekeep_client/cli.py` (`run_doctor` ~1390-1443, new `_check_dexes`)
- Test: `client/tests/test_cli_doctor.py` additions

One `dexes` row: `("dexes", "ok", "symdex (registered)")` / `("dexes", "ok", "none registered — add code intelligence with `firekeep dex add symdex`")` — the ROADMAP's suggestion-not-default funnel; "ok" either way (absence is a choice, not a fault). `_check_venv_scripts`'s wanted-tuple (cli.py:1047-1050) is UNCHANGED — wheels stay always-installed. Docdex rows arrive in Task C3.

- [ ] Failing tests both states; implement; suite. Commit `feat(client): doctor dexes row`.

---

## Part B — Docdex D1: the wheel

### Task B1: package scaffold + `sources.py`

**Files:**
- Create: `docdex/pyproject.toml` (hatchling, mirrors `symdex/pyproject.toml`; name `firekeep-docdex`, version `0.1.0`, deps `pypdf>=4,<7`, `python-docx>=1,<2`, `firekeep-client>=0.1.48` for resolver/transport; console script `firekeep-docdex = "firekeep_docdex.cli:main"`; LicenseRef-Firekeep-BUSL-1.1 + LICENSE/NOTICE copied from symdex/)
- Create: `docdex/src/firekeep_docdex/__init__.py` (`__version__ = "0.1.0"`), `sources.py`
- Test: `docdex/tests/test_sources.py`

`sources.py` per spec §2: `~/.firekeep/docdex/sources.json` 0600, entries `{id: secrets.token_hex(16), path: str(Path(p).expanduser().resolve()), visibility: "member"|"workspace", added_at, status: "active"|"pending_delete"}`. `add(path, shared=False)`, `remove_mark(id)`, `list_sources()` (missing path → reported flag, never dropped), `drop(id)` (post-confirmed-delete). Atomic writes.

- [ ] TDD the module: add defaults private, id uniqueness, resolve/expanduser, missing-path reported, pending_delete lifecycle, atomic+0600. Commit per green.

### Task B2: `extract.py` + `scan.py` + `state.py`

**Files:** Create `docdex/src/firekeep_docdex/{extract,scan,state}.py`; tests per module with real fixtures (`docdex/tests/fixtures/`: sample.md/.txt/.docx/.pdf + a text-free scanned-style pdf for the honest-zero case).

- `extract.py`: `.md`/`.txt` stdlib (utf-8, errors="replace"), `.pdf` pypdf, `.docx` python-docx; case-insensitive suffixes; returns `(text, error|None)`; NEVER raises; honest-zero (empty text, no error) is a valid result.
- `scan.py`: `walk(root, excludes) -> WalkResult{completed: bool, files: {relpath: sha256}, errors: [subtree]}`. Containment: `Path.resolve()` of every entry must stay under resolved root (symlinks/junctions out → skipped) — spec review #6. Missing/unreadable root → `completed=False`. Default excludes: dot-entries, `node_modules`, `__pycache__`, `.git`, `.env*`, `*.key`, `*.pem`, `*id_rsa*`. Relpath normalized NFC + forward slashes before hashing (spec §3).
- `state.py`: `~/.firekeep/docdex/state/<source_id>.json`, per-file `{seen_hash, ingested_hash, ingested_at, truncated, error, pending_delete}`; atomic replace. The seen/ingested SPLIT is the contract: stable-zero extraction records `seen_hash` (no retry); transient ingest failure leaves `ingested_hash` behind (retry next sync).
- **Deletion inference (I4a):** deletions computed ONLY when `completed=True`; errored subtrees excluded from inference. Test: unmounted root → zero deletions + loud list warning.

- [ ] TDD each; the walk-containment and completed-walk tests are the load-bearing ones. Commit per module.

### Task B3: `sync.py` — orchestration + wire

**Files:** Create `docdex/src/firekeep_docdex/{sync,wire}.py`; tests with a fake transport (assert exact wire shapes).

`wire.py` (thin, testable): `source_name(source_id, relpath) = f"docdex:{source_id}:{hashlib.sha256(relpath_normalized.encode()).hexdigest()}"`; `ingest_payload(...)` → `POST {cortex}/corpus/ingest` body `{content, source_name, source_type: "document", visibility, metadata: {path: relpath, mtime, dex: "firekeep.docdex", untrusted_content: True}}`; `delete_file` → `DELETE /corpus/sources/{source_name}`; `delete_source_bulk` → `DELETE /corpus/dex-sources/{source_id}`. Auth via `firekeep_client.resolver.resolve("cortex")` (`ep.rest_base`, `ep.headers`, `ep.verify`) — the nightshift `_evidence` precedent. NEVER an absolute path on the wire.

`sync.py`: per-source lock file (`~/.firekeep/docdex/locks/<id>.lock`, O_EXCL, stale after 1h) shared with remove; order: walk → (if completed) diff vs state → new/changed: extract→cap→ingest→record; deleted: tombstone→server delete→clear-on-confirm; pending_delete source: bulk delete, retried until confirmed. `resolver.is_bypassed()` checked before EVERY batch (I3 — in-flight suspension). Caps per Global Constraints, each breach behavior tested. Unreachable server → clean abort, state unchanged. Returns honest summary dict (spec §2 list).

- [ ] TDD: wire shapes byte-exact; lock exclusion (remove racing sync cannot resurrect — spec §6); cap breaches; bypass suspension; unreachable-server abort. Commit.

### Task B4: docdex CLI + `firekeep docdex` bridge

**Files:** Create `docdex/src/firekeep_docdex/cli.py`; Modify `client/firekeep_client/cli.py` (new `docdex` subcommand, LAZY import); tests both sides.

`firekeep-docdex` console script: `sync [--source ID] [--all] [--quiet]` (the detached-spawn target). Main-CLI bridge: `firekeep docdex add <path> [--shared] | list | sync [--source] | remove <id>` → `cmd_docdex` does `import firekeep_docdex.cli` INSIDE the function; ImportError → rc 1, "docdex is not installed — reinstall with the bootstrap or `firekeep dex add docdex` on a bundled install". `add`/`remove` require dex registered? NO — human CLI works regardless (folder control is human-only; registration gates the background trigger + doctor accounting). `remove` performs the §2 lifecycle (mark → lock → bulk delete → confirm/drop).

- [ ] TDD; verify import-boundary test still passes (lazy import). Commit.

### Task B5: bootstrap + release wiring

**Files (scout §7 touch list, all):**
- Modify: `client/bootstrap/install.sh` (§5b-style fetch+verify after symdex ~:391-402; install after :448-451; completeness probe :116-120 gains `firekeep_docdex`)
- Modify: `client/bootstrap/install.ps1` (:371-387, :464-467, probe :211-220)
- Modify: `client/firekeep_client/cli.py:461-474` (checkout path-install sibling `docdex/`)
- Modify: `client/scripts/make_release.py` (docdex presence guard, pattern of :188-196)
- Modify: `.github/workflows/release.yml:174-179` (build), `:319-320` (publish copy), pypi matrix `:462-467` + new env `pypi-docdex`
- Modify tests: `test_bootstrap_ps1.py:279-285` fetch count 6→7; `test_bootstrap_sh.py` + `test_bootstrap_venv_provisioning.py` docdex twins; `test_make_release.py` docdex fixtures; `test_e2e_bootstrap.py:93-125` builds+serves docdex wheel; new `test_bootstrap_docdex.py` (twin of `test_bootstrap_symdex.py`)

- [ ] Update tests first (they pin the contract), then the scripts/workflow until green. Commit `feat(release): bundle the firekeep-docdex wheel — fetched, verified, always installed`.

---

## Part C — Docdex D2: trigger + doctor + docs

### Task C1: `docdexsync.py` session-start trigger

**Files:** Create `client/firekeep_client/docdexsync.py`; Modify `client/firekeep_client/hooks/session_start.py:162-166`; Test `client/tests/test_docdexsync.py`.

Mirror `symdexindex.py` exactly (module docstring states the same three constraints): `is_enabled(cfg)` = docdex REGISTERED (via `dexes.read_registry()`) AND at least one active source AND `[docdex] auto_sync` not false AND `FIREKEEP_NO_AUTO_SYNC` unset; stamp = last-sync file older than `FIREKEEP_DOCDEX_SYNC_INTERVAL_HOURS`; claim via `state._scratch_file(f"docdex_sync.{stamp}")`; detached spawn `[sys.executable, "-m", "firekeep_docdex.sync", "--all", "--quiet"]` with the O_EXCL + DETACHED_PROCESS/start_new_session block (symdexindex.py:210-244). Bypass: dispatcher already gates session_start (hooks/__main__.py:212-225) — trigger never fires in private-session mode; in-flight suspension is B3's per-batch check. Return one-line nudge appended like `symdexindex.index_nudge`.

- [ ] TDD mirroring `test_symdexindex.py`'s key cases (:205-306). Commit.

### Task C2: docs + consistency sweep

**Files:** Create `docs/guides/dexes.md` (registry model, manifest schema, per-runtime sync-coverage table: hook-bearing = auto, MCP-only = manual `firekeep docdex sync`, stated honestly); Modify `CLAUDE.md` (symdex table row: registry-driven now; new docdex row), `docs/guides/client-kit.md` (dex section), `client/README.md`, adapters/next-steps output if they name symdex as always-on, `client/scripts/validate_kiro.py:61` stale comment.

- [ ] Sweep every Change-Consistency-Checklist file; grep `always-installed`/`always-on` for stale claims. Commit `docs: the dex registry model — dexes.md guide + sweep`.

### Task C3: doctor docdex rows

**Files:** Modify `client/firekeep_client/cli.py` (`_check_dexes` extended); tests.

When docdex registered: append `("docdex", ok|warn, "N sources · last sync <when> · M pending deletes · K failures")` via lazy import reading `sources.json` + state (no server call — doctor stays fast); warn on pending deletes or failures. Unregistered → the A5 row already covers it.

- [ ] TDD; commit.

---

## Part D — Verify, release 1.0.0, site

### Task D1: full verification + dogfood

- [ ] All suites: `client/`, `docdex/`, `symdex/` (untouched — must stay green), root `tests/` (image pins, requirements-lock: confirm `docdex/` joins the deliberately-NOT-locked list in `tests/test_requirements_lock.py` alongside client/symdex).
- [ ] E2E against the live VPS (server already has Phase V): `firekeep dex add docdex` (checkout install), `firekeep docdex add <real notes folder>` (private), `sync`, then `memory_recall` from THIS member sees content and a probe as another member does NOT (I1, measured-live); local file delete → next sync removes replica; `docdex remove` → bulk delete confirmed.
- [ ] Begin the spec's D2 dogfood: workstation notes folder stays synced for a week before public claims.

### Task D2: release client-v1.0.0

- [ ] Bump the THREE version sites to `1.0.0` + docdex `0.1.0`; changelog-worthy commit message (registry, dex CLI, docdex, symdex opt-in-for-new-installs with grandfathering).
- [ ] New GitHub environment `pypi-docdex` exists before tagging (workflow will fail without it — coordinate with owner if org perms needed).
- [ ] Tag `client-v1.0.0`, watch test→release→pypi×3 to green; `firekeep update` on workstation + VPS host; doctor green everywhere.

### Task D3: firekeep.ai

- [ ] `dexes.html`: Docdex section (shipped — folders, private-by-default, the honest threat boundary), family tease per ROADMAP order (chatdex designing, webdex planned); `docs.html`: `firekeep dex` + `firekeep docdex` CLI reference; accuracy rule — nothing claimed beyond what 1.0.0 ships. Deploy per `firekeep-site-publish` memory.

## Self-review notes (writing-plans checklist applied)

- Spec coverage: §2→B1-B4, §3→B3, §5 I1(D1 e2e)/I2(structural—no agent tool exists)/I3(B3+C1)/I4+I4a(B2/B3)/I5(caps tests B3, docs C2)/I6(C1 detached)/I7(wire metadata B3; recall-rendering delimiting is an OWNED FOLLOW-UP outside this plan, spec §8) — stated, not silently dropped.
- Type consistency: `DexManifest.name` is the registry key everywhere; `source_id` hex128 everywhere; summary dict keys fixed in B3 and reused by C3.
- The hardest pinned tests (scout §8) each have an owning task: `test_decision_registration` (A2), venv-scripts (A5 — unchanged), bootstrap suite (B5), matrix CAPS (untouched — no new row).
