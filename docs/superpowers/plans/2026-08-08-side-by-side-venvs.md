# Side-by-side venvs: `firekeep update` without closing a single session

**Date:** 2026-08-08 · **Target:** client 0.1.35 · **Status:** approved, implementing

## The problem, as measured on the owner's machine

`firekeep update` on Windows printed a wall of ~93 raw PIDs and told the operator to
close every agent session — for a product whose entire audience lives inside agent
sessions. Three stacked defects, one root cause:

1. **Console spew.** The client `Popen`s the bootstrap *detached* and exits, because the
   in-place rebuild must overwrite the very `firekeep.exe` that launched it. The
   interactive prompt returns and the installer's output tears across it.
2. **The PID wall.** The in-use guard enumerates every holder and prints
   `name (pid N), …` ninety times.
3. **The dead end.** The guard's advice — close everything, re-run — is the only advice
   an in-place rebuild *can* give.

All three exist **only** because the venv is rebuilt in place at `~/.firekeep/venv`.

## Design

```
~/.firekeep/
  venvs/
    0.1.35/          # a full uv venv, provisioned AT THIS PATH, never moved
    0.1.34/          # previous version, kept as instant rollback
  current            # Windows: NTFS junction -> venvs/0.1.35 (no admin needed)
                     # POSIX:  symlink, flipped atomically via os.replace
  shims/firekeep.cmd # -> %~dp0..\current\Scripts\firekeep.exe
  venv/              # legacy dir; untouched while held, GC'd by a later update
```

Every rendered surface — the PATH shim, all four adapters' MCP/hook commands, the
`/personal` command file, doctor — routes through `current`. Update = provision
`venvs/<V>` beside whatever is running, flip `current`, re-render. Running sessions keep
executing the old version untouched (their open file handles pin the real files, not the
link); everything started afterwards gets the new one.

### Why this is safe where 0.1.26's attempt was not

0.1.26 tried build-beside-and-**rename** (`${VENV}.new` → `mv`) and it failed: uv bakes
the venv's absolute path into `pyvenv.cfg` and every console-script interpreter line
(`install.sh:240-270`; guarded by `test_bootstrap_venv_provisioning.py`). Here each venv
is provisioned at its **final** path and never moves — only the *alias* flips. Probe-
verified on the owner's Windows 11 box (non-elevated): junction creation, exe-stub
resolution through the link, flip under a live process, old-process survival, and
`sys.prefix` staying on the *junction* path (so rendered configs stay valid across
flips) all hold.

### What each defect becomes

| Before | After |
|---|---|
| Detached bootstrap, torn console | Foreground child; parent's exe is never overwritten, so it can simply `wait()` and stream. POSIX keeps `execve`. |
| 93-PID wall + hard refusal | Nothing. The venv being replaced is a *new directory*. |
| "Close every session" | Gone for updates. Survives only for `FIREKEEP_FORCE_REINSTALL` of the *held, current* version — rare repair path, message stays humane (counts by process name, never raw PIDs). |

### GC: rename-probe, never enumerate-and-hope

Probe finding with teeth: `Remove-Item -Recurse` on a held venv **partially guts it**
before hitting the exe lock — "delete failed" means "venv now corrupt". But renaming a
directory with open files anywhere beneath it fails atomically with no partial state.
So GC's primitive is *rename-then-delete*: try `venvs/0.1.33` → `venvs/.gc-0.1.33`;
success proves nothing holds it, then delete the renamed dir; failure means held — skip
with one line ("kept 0.1.33 — still in use by open agent sessions; a future update will
remove it"). Policy: keep `current` + previous (instant rollback), GC older + the legacy
`venv` dir. Leftover `.gc-*` dirs from a crashed GC are re-swept. POSIX: same shape;
rename always succeeds there, and deleting a held venv is inode-safe, but we still keep
current+previous so a live session's `gateway` (which resolves `sys.executable` to the
real dir) can respawn backends.

### Post-flip lazy-import hazard, closed

A long-running process launched via `current` resolves *future* imports through the
link — after a flip it would mix modules from two versions. `pin_import_paths()`
(realpath every `sys.path` entry at startup) in the four stdio entry points freezes
module resolution to the real versioned dir the process started under. Same fix covers
the POSIX symlink case.

### Fast path = idempotence + rollback, one rule

If `venvs/<V>` exists and its own `python -I` probe reports `<V>`: flip `current` to it
(if needed), run the wizard, done — zero downloads. That heals interrupted installs
(crash-after-flip re-runs to the same state) and makes `firekeep update --to <prev>` an
instant flip while `venvs/<prev>` survives GC.

### Transition

- **≤0.1.34 → 0.1.35**: `firekeep update` fetches the *target* release's bootstrap, so
  the new side-by-side installer runs even when driven by the old client. Legacy `venv`
  stays held and ignored; one last detached-console handoff (old client code). A fresh
  `irm | iex` is the clean path and needs no sessions closed.
- **`firekeep install` (wizard)**: when running from an installed venv, derive the venv
  root from `sys.executable` — not `home/'venv'` — so the never-rebuild guard survives
  the layout change. Checkout installs provision `venvs/<version-from-pyproject>` + flip
  through the same helpers.
- **doctor**: new `current-link` row (exists, resolves, target version == installed
  version); stale advice text updated.
- **install.ps1 self-defense**: pins `PSModulePath` to `$PSHOME\Modules` at entry
  (saved/restored for `irm | iex` callers), so a pwsh-7-poisoned environment can never
  again break 5.1's `Get-FileHash` mid-checksum.

## Tasks

1. `cli.py`: layout helpers (`current` link, versioned venvs, legacy fallback),
   `cmd_install` + doctor + foreground `_exec_bootstrap`.
2. `pathenv.py`: shim templates route through `current` (both OSes).
3. `stdio.py`: `pin_import_paths()` + call in gateway/hooks/shim/decision entry points.
4. `install.ps1`: versioned provision, junction flip (`New-Item -ItemType Junction`;
   removal via `cmd /c rmdir` — never `Remove-Item -Recurse` a junction), rename-probe
   GC, fast path incl. rollback, PSModulePath defense, humane messages.
5. `install.sh`: same, symlink + `os.replace` flip via the fresh venv's python.
6. Tests: rewrite `test_bootstrap_venv_provisioning.py` around the new invariant
   (provision-at-final-path, alias-only flip); fix the literal-layout clusters
   (`test_e2e_bootstrap`, `test_bootstrap_sh/ps1/reinstall`, `test_cli_install`,
   `test_kit_smoke`, `test_pathenv` — incl. closing its silent-green gap); new guards
   (shim routes through current, adapters never embed a versioned path, GC rename-probe,
   doctor link row).
7. Docs: `client-kit.md` layout/Updating/auto-update passages, `CLAUDE.md` sentence,
   `RELEASE-GITHUB.md` notes (backfill 0.1.33/0.1.34, add 0.1.35). `autoupdate.py`
   docstring finally becomes true — reword to match reality.
8. Bump 3 version markers, tag `client-v0.1.35`, release, verify live on the owner's
   machine **with all sessions open** — the exact scenario that failed.

## Honest limits

- **H1** Windows flip window: `rmdir`+`mklink` is two ops; a spawn in that ~ms window
  fails file-not-found. Hooks fail open by design; retried spawns succeed. (Replaces
  today's 30–120 s window where `~/.firekeep/venv` doesn't exist mid-rebuild.)
- **H2** A session surviving *two* updates on POSIX can lose its gateway's respawn
  target when its venv ages past "previous" — mitigated by keep-2 policy, documented.
- **H3** External schedulers pointing at `~/.firekeep/venv/bin/firekeep` (night shift
  cron) break when legacy GC lands; the stable path is `~/.firekeep/shims/firekeep`.
- **H4** Junctions require NTFS local volumes; a `%USERPROFILE%` on a network share
  cannot host `current` (junctions can't target UNC). Un-mitigated; fails loudly at
  junction creation.
