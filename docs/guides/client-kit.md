# The client kit — install, hooks, night shift, personal mode

> Moved out of the root `CLAUDE.md`, which is a prompt prefix loaded into every
> session. This content is reference and decision history: read it when you are
> working on this area, not on every task. Nothing was reworded in the move.

## Local setup (portable client kit — `~/.firekeep` + runtime adapters)

**Teammates (bare machine, nothing installed):**
```bash
curl -fsSL <release-base>/latest/install.sh | sh      # macOS / Linux
irm <release-base>/latest/install.ps1 | iex           # Windows
```
Since client 0.1.15 the PUBLISHED bootstraps carry their own release URL — `make_release.py
--dist-base` bakes it (each release path bakes its own: GitLab CI the registry URL, the GitHub
workflow the Pages URL) BEFORE the bootstrap hashes are computed, so `firekeep update`'s
script-verification still holds. `FIREKEEP_DIST_BASE` still overrides when set, and the REPO
copies keep the `__FIREKEEP_DIST_BASE_DEFAULT__` placeholder — a raw-checkout run still fails
loudly with nowhere to fetch from. New-teammate sugar: the wizard can prefill the server
connection from `<release-base>/latest/org-defaults.json` when `[server]` is
unconfigured (published only via the GitLab registry from the `ORG_DEFAULTS_JSON` CI variable
— internal hostnames never go to public GitHub Pages; absent variable = no file = plain
prompts). Update awareness + auto-update: the `session_start` hook checks the dist host's
`latest.json` once per day (failures cached too, 3s timeout, silent on any failure) and, when
it's newer, background-auto-updates the client by default (client 0.1.20; opt out with
`FIREKEEP_NO_AUTO_UPDATE` / `firekeep update --auto off` — see Background auto-update below), falling
back to a one-line "client update available" nudge when opted out.
`<release-base>` is **version-agnostic**. Interim client releases are cut via GitHub Actions
(`.github/workflows/release.yml`) and served from GitHub Pages —
`FIREKEEP_DIST_BASE=https://kapella-hub.github.io/firekeep-dist` (see `docs/RELEASE-GITHUB.md`); the
GitLab generic package registry root (`.../packages/generic/firekeep-client`, via `.gitlab-ci.yml`)
remains the office path. Either way `latest/` is the stable entry point
(`install.sh`, `install.ps1`, `latest.json`), while every version keeps its own directory
(`<version>/SHA256SUMS`, `<version>/uv-<target>`, `<version>/firekeep_client-<version>-py3-
none-any.whl`), which is what lets `firekeep update --to <older>` reach that version's own
wheel instead of 404ing. The bootstrap (`client/bootstrap/`) resolves the version, fetches
that version's `SHA256SUMS` once, and checksum-verifies **both** a mirrored `uv` **and the
wheel** against it before either is used — the wheel is fetched to a local file and handed
to `uv pip install` by local path, never by URL (`uv pip install <url>` does no hash
checking). The wheel is never resolved **by name** either (`firekeep-client` on PyPI is a third
party's package). Both bootstraps (`client/bootstrap/install.sh` ~lines 22-35, `install.ps1`
mirrored) also export `UV_NATIVE_TLS=1` and neutralize a set `SSL_CERT_FILE` (a warning is
printed; `FIREKEEP_KEEP_SSL_CERT_FILE=1` opts back in) before invoking `uv`/pip — rustls treats
`SSL_CERT_FILE` as the EXCLUSIVE trust store (the native OS store is ignored), so a
corporate-CA-only file left behind by a proxy workaround would otherwise break every
NON-intercepted host; routing through the OS store instead is what MDM-managed corporate
machines need, since the corporate interception CA lives there alongside the public roots.
It then runs `firekeep install`, which prompts for identity and the single
server connection, then renders every runtime adapter.

**Layout (side-by-side venvs, client 0.1.35):** the kit lives at
`~/.firekeep/venvs/<version>` — one full uv venv per installed version, provisioned AT that
final path and never moved, because a uv venv is not relocatable: `pyvenv.cfg` and every
console-script interpreter line bake the absolute path (the recorded 0.1.26
build-beside-and-rename failure). `~/.firekeep/current` selects the active one — an NTFS
junction on Windows (works without admin, unlike a directory symlink), a plain symlink on
POSIX. Every rendered surface — the PATH shim, all four adapters' MCP/hook commands, the
`/personal` command file, doctor — routes through `current` and never a versioned path,
which is what makes updates render-free (the embedded paths stay literally identical across
flips) and keeps runtime configs from pinning a venv that GC will remove. One hazard the
alias creates is closed at startup: a long-running kit process launched through `current`
would resolve imports it performs *after* a flip through the NEW venv — mixing modules from
two client versions in one process — so every stdio entry point (gateway, hooks, shim,
decision) realpaths `sys.path` first thing (`stdio.pin_import_paths()`), freezing module
resolution to the versioned dir it actually started under. The legacy pre-0.1.35
`~/.firekeep/venv` is left alone while sessions still run from it and GC'd by a later
update (see Updating below).

**`firekeep` on PATH (`firekeep_client/pathenv.py`, client 0.1.20):** every install path funnels
through `firekeep install` (fresh bootstrap, `firekeep update` re-exec, checkout `./install`), so
that is where a `firekeep` launcher gets put on PATH — best-effort (a PATH failure NEVER fails
the install). It does **not** PATH the venv's bin dir: that dir holds the kit's standalone
CPython (`python`/`python3`/`pip`) and every internal entry point (`firekeep-shim`,
`firekeep-sidecar`, `firekeep-decision`, `firekeep-symdex`), so prepending it would shadow the user's
own `python3`. Instead it drops ONE launcher — `firekeep` — into a dedicated `~/.firekeep/shims`
dir (POSIX: a `#!/bin/sh` wrapper `exec`ing `~/.firekeep/current/bin/firekeep`; Windows:
`firekeep.cmd` → `%~dp0..\current\Scripts\firekeep.exe`, a relative hop to whatever root it
was rendered against — `current` under the side-by-side layout, the legacy `venv` on a
not-yet-migrated install, never a versioned `venvs\<X.Y.Z>` path that would pin the
launcher to a venv GC removes) and PATHs only that (the pipx/rustup pattern). Because the
launcher routes through `current`, it survives every update byte-identical —
`~/.firekeep/shims/firekeep` is the STABLE path for anything external (cron entries, CI,
night-shift schedulers); a scheduler still pointing at `~/.firekeep/venv/bin/firekeep`
breaks when a later update GCs the legacy dir. POSIX writes
a marker-delimited `export PATH=...` block into the shell rc for `$SHELL` (zsh→`.zshrc`;
bash→`.bashrc` + existing `.bash_profile`/`.profile`; fish→`conf.d/firekeep.fish`; else
`.profile`) — extras are updated only if they already exist, so a login-shell sourcing chain
is never disrupted; Windows prepends the shim dir to the `HKCU\Environment` `Path` via
`winreg` (REG_EXPAND_SZ type preserved, `WM_SETTINGCHANGE` broadcast). Idempotent (collapses
ALL prior firekeep blocks on re-render). Opt out with `firekeep install --no-modify-path` or
`FIREKEEP_NO_MODIFY_PATH=1` (sysadmins who manage PATH centrally). `pathenv.remove_from_path`
is the inverse, ready for a future `firekeep uninstall` (no such command today).

**Developers (from a checkout):**
```bash
cd client && ./install              # POSIX; .\install.ps1 on Windows
firekeep install --runtime claude      # re-render one runtime: claude | codex | kiro | opencode | all
# firekeep-symdex (stdio-local code intelligence) now installs automatically — no flag needed
firekeep install --non-interactive --agent-id ci-bot --host 10.0.0.4   # scripted/fleet
```
`./install` (from a checkout, requires a system `python3 >= 3.10`) provisions the kit into
`~/.firekeep/venvs/<version>` (version parsed from `client/pyproject.toml`), points `current`
at it, and renders the adapters. `firekeep install` (from that venv) **re-renders adapters
only** — it skips pip, because the code it would install is the code already running; the
venv it stands in is derived from `sys.executable`, never guessed, so it works from a
versioned venv and a legacy `venv/` alike.

**Updating (render-free since client 0.1.35):** `firekeep update` (`--check` to report only,
`--to X.Y.Z` to pin or roll back). It re-execs the bootstrap rather than pip-installing over
itself, so install and update are one code path — and the bootstrap provisions `venvs/<V>`
beside whatever is running, checksum-verifies and installs both wheels into it, and only
then flips `current`. **Updates no longer require closing sessions.** Live sessions keep
executing their old venv untouched — their open handles pin the real files, not the link —
and everything launched afterwards gets the new version. That retires both defects of the
in-place rebuild this replaces: the Windows holder-PID guard (it enumerated ~93 raw PIDs on
the owner's machine and told an audience that lives inside agent sessions to close every
one) and the 30–120 s POSIX window where `~/.firekeep/venv` did not exist mid-rebuild and
every fresh hook exec on every live session failed with "No such file or directory". The
flip itself is atomic on POSIX (`os.replace` over the symlink) and a millisecond
`rmdir`+junction recreate on Windows — a spawn in that window fails file-not-found once and
its retry succeeds (hooks fail open by design). The ONE refusal left is Windows-only:
`FIREKEEP_FORCE_REINSTALL=1` of the version `current` points at while sessions still run
it — clearing a held venv there dies mid-delete and leaves it gutted, so the bootstrap
refuses, summarizing holders as `Nx name` counts, never raw PIDs (POSIX clears a held venv
inode-safely and just proceeds). A `venvs/<V>` whose own `python -I`
probe reports `V` takes the fast path (flip + re-render, zero downloads) — one rule that
covers idempotent re-runs, crash-healing (a crash between flip and wizard re-runs to the
same state), and INSTANT rollback: `firekeep update --to <prev>` while `venvs/<prev>`
survives GC is just a flip. On Windows the client now waits on the bootstrap as a
FOREGROUND child, streaming its output in order to the same console — safe precisely
because the side-by-side install never overwrites the running `firekeep.exe`; the detached
spawn whose output tore across the caller's returned prompt is gone (POSIX keeps `execve`).
The Windows bootstrap still pins `uv venv --python-preference only-managed` so interpreter
discovery never walks the PATH into a dangling Windows Store python alias (zero-byte
APPEXECLINK stub → "os error 3") nor binds the venv to a non-standalone system Python
(mirrored on POSIX for the standalone-CPython contract).

**GC — rename-probe, keep two.** After the wizard hand-off the bootstrap removes every
versioned venv except the one `current` points at and the newest other version (kept as the
instant-rollback target), plus leftovers of interrupted GCs, plus the legacy pre-0.1.35
`~/.firekeep/venv`. Liveness is proven by RENAME, not process enumeration: renaming a
directory with open files anywhere beneath it fails atomically with no partial state,
whereas a recursive delete guts a held venv before hitting the first locked exe ("delete
failed" would mean "venv now corrupt") — so GC renames to `<name>.gc`, deletes the renamed
dir on success, and on failure keeps it with one humane line ("kept <name> — still in use
by open agent sessions; a future update will remove it"). A crash mid-GC leaves only a
`.gc` dir a future run re-sweeps. POSIX gates the LEGACY venv on `lsof` instead — rename
succeeds there even while held, and pre-0.1.35 rendered configs exec hooks from that dir by
absolute path, so deleting it under a live legacy session would break that session's fresh
hook execs; sessions opened before the migration keep running from it, and the wizard's
re-render is what moves every rendered path onto `current`. `~/.firekeep/config`'s
`[dist] base_url` (written by the bootstrap, or via `firekeep install --dist-base URL`) is how
`firekeep update` knows where its releases live; a checkout install has no `[dist]` section
and `firekeep update` says so plainly. `firekeep doctor` reports a `client-version` check when a
newer release exists, plus a `current-link` row: a link that is missing while `venvs/`
exists, dangling, or pointing at a different version than the running client is named
outright — every rendered surface routes through it, so a bad link is a dead client that
looks installed. A pure legacy layout (no `venvs/`, no link) gets no row: not yet updated
is not a fault.

**Background auto-update (`firekeep_client/autoupdate.py`, client 0.1.20) — ON by default.** The
`session_start` daily version check (below) no longer only nudges: when a newer release
exists it fire-and-forgets a DETACHED `firekeep update` (`autoupdate.maybe_spawn` →
`subprocess.Popen([venv/firekeep, "update"], start_new_session=True` / Windows
`DETACHED_PROCESS`). This automates the SAME operation a user runs by hand mid-session, and
since the side-by-side layout (0.1.35) it lands on every platform without closing anything:
the update provisions `venvs/<V>` beside the running install and flips `current`, touching
nothing the session holds. The old Windows caveat is **retired with the guard that caused
it** — the bootstrap's live-holder guard refused whenever a session's stdio MCP servers
(firekeep-decision, firekeep-symdex, shims) still held `~/.firekeep/venv`, which at session
start they always did, so Windows background updates silently no-opped during active use
and the manual path was the only reliable one. "Applies next session" stays true for
RUNNING sessions: they keep executing the venv they started under (the design, not a
limitation — that venv is pinned by their open handles and `pin_import_paths()`), and the
new version takes effect for everything launched after the flip. Guarded to **at most one spawn per calendar day per
target version** (the daily check caches a 'newer' verdict, so without this every session
start that day would relaunch — the guard is a `today|latest` stamp in scratch). The
detached update runs `--non-interactive` (no tty), so it never prompts and preserves config.
When auto-update is on the briefing line reads "updating client in background: X → Y (applies
next session)"; opted-out it's the old "run: firekeep update" nudge. Opt out with
`FIREKEEP_NO_AUTO_UPDATE=1` (env), `[dist] auto_update = false` (config), or `firekeep update
--auto off` (which writes that config key and does nothing else). Never blocks or fails a
session — `maybe_spawn` swallows every error and returns False. Release-host fetches on this
path (`firekeep update`'s manifest/wheel downloads, `firekeep doctor`'s `client-version` check) go
through a scoped `truststore` OS-trust SSL context (`client/firekeep_client/updater.py:
_dist_ssl_context`) — `truststore` is a new `truststore>=0.9.1` dependency in
`client/pyproject.toml` — never `truststore.inject_into_ssl()`, which replaces the
process-wide default context and would widen the configured server's `ca_path` trust
instead of leaving it scoped; when `truststore` isn't installed the call returns `None`
and the caller falls back to the stdlib default context.

**Release signing (docs/RELEASE-SIGNING.md — read it before touching keys or the verify
path):** `make_release.py` signs `SHA256SUMS` (Ed25519, minisign format, `SHA256SUMS.minisig`)
when the `FIREKEEP_SIGNING_KEY` CI secret is set — absent secret = loud UNSIGNED release, set-but-
garbage secret = failed release. SHA256SUMS now also lists `install.sh`/`install.ps1`, so the
signature covers the script `firekeep update` executes: `updater.fetch_signed_sums` verifies the
target release's sums against `signing.PINNED_PUBLIC_KEY` (`client/firekeep_client/signing.py`
— pure-stdlib RFC 8032 verifier, EMPTY until the operator mints keys per the runbook;
the import boundary is why it isn't `cryptography`), `updater.bootstrap_sha256` refuses a
`latest.json` that disagrees with the signed entry, and the trusted comment's `version:` token
kills cross-version replay. Verify-if-present: no pinned key or no published `.minisig` →
warn/skip (until `[dist] require_signed = true`, default false FOR NOW); an INVALID signature
is always fatal. The bootstraps carry a best-effort mirror (baked `__FIREKEEP_SIGNING_PUB_DEFAULT__`
placeholder, `FIREKEEP_SIGNING_PUB` override — which `firekeep update` now exports from the CLIENT's
pinned key, so the update path isn't circular on the host-baked one; `minisign`-binary-if-present;
absence never breaks a bare machine). **Security-review plumbing fixes (2026-08-05):** the verified
sums are THREADED THROUGH, never re-fetched — `cmd_update` writes the signature-verified SHA256SUMS
to a 0600 file and hands its path as `FIREKEEP_SUMS_FILE`; both bootstraps use it (honoured only
alongside `FIREKEEP_VERSION`, the client hand-off's shape; set-but-unreadable is fatal, and under it
NO sums/`.minisig` fetch happens at all), closing the two-fetch split where a host served honest
bytes to the client's verification fetch and attacker bytes to the bootstrap's re-fetch (guard:
`test_install_sh_two_fetch_split_no_longer_works`, request-log-proven, + the executable ps1 twin).
`--to X.Y.Z` verifies the TARGET's sums (what gets installed; latest's too when they differ — those
anchor the executed `latest/` bootstrap), so rollbacks are signed and an unsigned old target fails
under `require_signed`. Because the detached auto-update's stderr is DEVNULL, an unsigned-release
warning also persists a one-shot scratch marker (`state.note_unsigned_update`) that the next
`session_start` briefing prints. CI publishes `install.sh`/`install.ps1` under `<version>/` (the
sums list them, so the dir must serve them) and the verify step byte-compares the SERVED
`<version>/SHA256SUMS.minisig` against the built one on signed builds. `generate_signing_key.py`
creates the secret 0600 at open (`O_EXCL`), no chmod-after window. First install stays TOFU;
`latest.json` stays unsigned (downgrade residual); absence stays attacker-choosable until
`require_signed` flips — all stated in `docs/THREAT-MODEL.md` §5.6. Guards:
`client/tests/test_signing.py`, signing halves of `test_updater.py` / `test_cli_update.py` /
`test_make_release.py` / both bootstrap test files, and `tests/test_release_workflow.py`'s
signature-served class.

`~/.firekeep/config` (INI, `0600`) is the single source of truth: `[identity]` holds `agent_id`, `[server]` holds the one connection/auth/TLS policy, and optional `[dist]` holds update metadata. There is no active-profile selector or per-runtime pin; every adapter reads the same server, while `FIREKEEP_AGENT_ID` remains the supported per-process identity override. `firekeep doctor` runs health + versions (a verdict-free client/cortex report; the two ship on independent tag series so equality is meaningless) + client-version (staleness vs the release manifest — the only version row that renders a verdict) + key-ACL + CA-expiry preflight. Legacy profile configs migrate automatically when they identify one unambiguous server; conflicting connections are left untouched and reported with exit code 3.

**Install prompts (`firekeep_client/wizard.py`):** an interactive install asks for the agent identity and the one server connection — `host` (+ optional `api_key`) for an existing/default `kind=ports` connection, or `base_url` + `ca_path` + `api_key` when the existing/migrated connection is `kind=paths`. It never asks the user to choose a profile; Firekeep is one product, so there is no edition to ask about. Every prompt is prefilled with the current value, so Enter-through is a no-op and re-running the installer after a kit upgrade is safe. `ca_path` accepts the literal **`os`** (`resolver.OS_TRUST`) to verify TLS against the operating-system trust store instead of a CA file — the MDM-managed-corporate-CA case, where the CA lives in the OS keychain and there is no PEM to point at; the wizard offers `os` as the default automatically when a read-only TLS probe (`wizard._probe_os_trust`, best-effort — any failure just keeps the file prompt) shows the server cert verifying against the OS store, but never overrides a deliberately configured ca_path. Under the hood `transport._build_ssl_context("os")` builds a scoped `truststore` context shared by the stdlib and shim/httpx paths — still verified TLS, never a bypass — and `firekeep doctor` reports `ok` for `os` (the OS owns rotation). A ports-style connection is deliberately not offered a TLS toggle: `resolver._verify_for()` refuses `scheme=https` without both `verify_tls=true` and a `ca_path`. No TTY (CI, piped) or `--non-interactive` means no prompts; `--agent-id` and `--host` seed the prompts interactively and are written directly otherwise.

**Legacy-hook migration (`adapters/base.py`, `LEGACY_HOOK_MARKERS` / `LEGACY_ENV_KEYS`):** the retired bash hook layer and the retired `FIREKEEP_*_URL` env keys are treated as **firekeep-owned**, not foreign, so `render()` removes them from `~/.claude/settings.json` and `unrender()` cleans them up. Without this, a machine upgraded from the pre-kit installer fires every lifecycle event twice — once into a now-deleted shell script (a "No such file or directory" hook error at every session start), once into the real hook core. `upsert_hook_group()` collapses *all* firekeep groups for an event into the one rendered group (not just the first match) — that is what makes a both-layers-present machine converge instead of duplicating. The legacy `PreCompact` echo hook and `FIREKEEP_AGENT_ID` are intentionally left in place: both still work.

**kiro legacy migration (`adapters/kiro.py`, `_migrate_legacy`):** kiro's `render()` gets the same firekeep-owned-artifact treatment as the claude adapter above: it drops every `~/.kiro/settings/mcp.json` `mcpServers` entry whose key is a kit name or `<key>_`-prefixed (covers parked `firekeep-cortex_DISABLED`-style variants), and archives `~/.kiro/agents/firekeep.json` + `~/.kiro/firekeep.env` (pre-kit manual-setup artifacts) to `.bak`. Best-effort like the claude precedent: a missing file is a silent no-op, a malformed/wrong-shaped `mcp.json` is left untouched, and no migration step may ever fail `render()` or the install; it is one-way — `unrender()` does not restore the archived artifacts.

**Render stability — `write_text_if_changed` (`adapters/base.py`):** every adapter previously rewrote byte-identical content on every render, and rewriting identical bytes still moves mtime, which is not free. `firekeep update` re-execs `firekeep install`, which re-renders `~/.claude/CLAUDE.md` and `~/.claude/settings.json` — and background auto-update is ON by default, so this happens **mid-session on a customer's machine**. Those files sit in the prompt prefix: a host that re-reads a rendered instruction file because its mtime moved rebuilds that prefix and invalidates the prompt cache, re-billing the whole conversation at full rate for a zero-byte change. Whether a given host does that cannot be determined from this repo, which is exactly why touching mtime for nothing is indefensible. All rendered-file writes (including `write_json`, which delegates) now go through it. It **fails toward writing**: if the existing file cannot be read or decoded, we cannot prove it matches, so we write — failing to read is not evidence of a match — and it never skips a real change. Guarded by `client/tests/adapters/test_write_stability.py`, whose load-bearing case is `test_second_identical_claude_render_touches_no_rendered_file` (the whole-adapter check, not just the helper's unit behaviour).

## Session Hooks (client kit — `firekeep_client.hooks`)
The five bash hooks are retired; the adapter wires stdlib Python hook cores at install (Claude `settings.json`, kiro inline hooks, OpenCode via a rendered JS plugin bridge; Codex has no hook surface):
- `session_start` (SessionStart / kiro agentSpawn) — thin fetch-and-print of Cortex `GET /briefing` (server-side aggregator; auth via the resolver) plus local presence registration. Replaces the 610-line briefing assembly and structurally kills its `$SESSION_ID`-unbound + shell-injection bugs. Also stashes the server-minted `briefing_id` into the session stash (`state.write_session_stash`, `session_current_{agent}`) for the bridge shim's identity tap. Runs the once-a-day client-update check (`_update_nudge`): when a newer release exists it spawns the detached background auto-update (on by default — see Background auto-update above) and appends a one-line "updating in background" notice (or the manual "run: firekeep update" nudge when opted out). Finally calls `symdexindex.index_nudge` to background-index the workspace for symdex when the staleness policy says so (see Symdex auto-index below) — silent in every declining case, since a line on every start is the nag it replaces.

**Identity auto-injection (client 0.1.17):** the shim attaches `X-Session-Id` on every proxied request WITHOUT the agent passing `session_id` — killing the untagged-calls discipline problem structurally rather than nagging about it, and closing the briefing_id→session A/B join mechanically. Mechanism (all client-side; cortex/bridge unchanged — `_resolve_identity` already special-cases `session_id="unknown"` to fall through to the header): (1) the **bridge** shim runs a `_BridgeSessionTap` on both pump directions — it injects the stashed `briefing_id` into a `ctx_start_session`/`ctx_resume_session` the agent sends without one, and captures the returned `session_id` into the session stash (clearing it on `ctx_complete_session`/`ctx_abandon_session`); (2) **every** shim's httpx client carries `_StashSessionAuth`, which reads the stash per-request and sets `X-Session-Id` when a fresh id exists and `/personal` bypass is off. The stash is keyed by `{agent}` with a self-enforced TTL (`FIREKEEP_SESSION_STASH_TTL_HOURS`, default 12 — `reap_stale` does not sweep `scratch/`). Lifecycle: `session_start` clears the stash UNCONDITIONALLY at the top (a new session never inherits a crashed one's id, even if the briefing fetch fails) then writes `briefing_id` if present; the bridge tap clears it on `ctx_complete_session`/`ctx_abandon_session` (server-authoritative session end); the TTL backstops a crash. `stop` deliberately does NOT clear the stash — the `Stop` event fires every assistant turn, not at session end, so clearing there would drop attribution for turns 2..N. briefing_id is injected ONLY into `ctx_start_session` (not `ctx_resume_session`, whose bridge signature has no such param — FastMCP would reject the kwarg and break resume); both start and resume are tracked for session_id capture. Injection is a DEFAULT, not an override: an explicit agent-supplied `session_id`/`briefing_id` still wins server-side. First-turn pre-`ctx_start_session` calls stay `"unknown"` (correct — the discipline metric won't hit zero). The pump transform never raises, forwards byte-identical on error, and is GIL-safe (synchronous, no await between the pending-map check and set). **Concurrency limitation (known):** the stash is one machine-global slot per agent identity, so two concurrent sessions under the SAME identity (two Claude windows as the same person) are last-writer-wins — window B's `ctx_start_session` overwrites the slot and window A's still-running shims then attach B's `session_id` to A's calls (active mis-attribution of replay/eval joins, not merely missing headers). Consistent with Bridge's own one-active-session-per-`agent_id` model; the supported partition for genuinely concurrent work remains a distinct `FIREKEEP_AGENT_ID` per terminal (it flows into the stash key, the shim headers, and Bridge sessions coherently). A true fix (per-runtime-session stash keying) needs the shim to know its runtime session id — a follow-up. NOT changed here: `state.resolve_session_id`'s precedence (what `/agent/action/before` + evals key by) — a distinct attribution concern deferred to its own task.
- `stop` (Stop) — guided completion: final workspace snapshot, distill/tasks/lease reminders, and presence deregistration (race-guarded against a newer session's registration).
- `prompt` (UserPromptSubmit) — polls Relay for tasks/messages; periodic workspace snapshot to the platform cache dir.
- `pre_tool` (PreToolUse on Edit/Write) — the only blocking hook: lease check + `POST /agent/action/before`; preserves the exact block→stderr+nonzero / allow→proceed exit-code contract; falls through to allow (logged) if Cortex is unreachable. On kiro (validated on kiro-cli 2.12.1, `docs/KIRO-VALIDATION.md`) the pre-edit matcher is the exact tool name `fs_write` (Claude's `Edit`/`Write` names don't exist there) remapped via `--block-exit 2`, and the block is **advisory** — kiro 2.12.1 fires the hook but does not enforce the exit-2 block (the agent-gateway before-call still runs).
- `post_tool` (PostToolUse) — `POST /agent/action/after` reconcile, keyed to `pre_tool`'s shared temp-state.
- `precompact` (PreCompact) — **Claude only**. No other runtime exposes a compaction event, and `contract/matrix.py` says `none` for kiro/codex/opencode rather than implying a save that never happens. It fires BEFORE compaction and does four cheap, certain things: checkpoints the workspace snapshot to Bridge scratch, bumps `shadow_epoch` so Bridge's `filter_since` refuses any cursor minted before this compaction (see Shadow Residency Contract below), stamps `compacted_at`, and returns one `systemMessage` pointing the agent at `ctx_get_shadow()`. All three writes ride on ordinary `ctx_update(category="scratch")` calls — **deliberately no new MCP tool** for the epoch bump; scratch is already the server-authoritative channel, and `SessionManager.get_shadow_epoch` reads the same key, so there is no dedicated writer to keep in sync. Bypass gate is checked FIRST, before any config resolution or network call. Budgeted like `session_start` (~15s) and best-effort throughout — each write is individually caught to `hooklog` and the core is `@never_raise({})` — because a hook that stalls the customer mid-compaction is worse than a missed checkpoint. **Honest about its ceiling:** it fires before compaction but cannot read the agent's unstated reasoning, so it CANNOT recover decisions the agent never wrote via `ctx_update`. It preserves what was already stated; it does not rescue what was not. `transcript_path` is present in the payload and is deliberately never read (guarded by `test_does_not_read_the_transcript_path`) — shipping a customer's raw conversation to the server is a privacy decision, not an engineering one. Guards: `client/tests/hooks/test_precompact.py`.

Presence registration/heartbeat/periodic snapshots/deregistration for Claude Code — and for kiro and OpenCode, which wire the same five cross-runtime hook cores to their own lifecycle events (`precompact` is Claude-only and has no counterpart to wire) — are owned directly by the hook cores above: `session_start` registers, `prompt` heartbeats, `stop` deregisters. The **sidecar** (`firekeep-sidecar`, one daemon per agent identity) is the *intended* presence owner for MCP-only runtimes with no hook lifecycle at all (Codex today), but nothing currently spawns it automatically — a Codex user has no presence path unless they run `firekeep-sidecar` by hand. The retired launcher is replaced by the `FIREKEEP_AGENT_ID` env override: set it in the process environment to run differently-identified agents from one machine (it overrides `[identity] agent_id`).

**UTF-8 stdio (`client/firekeep_client/stdio.py`, `force_utf8_stdio()`).** Every kit process that speaks JSON on stdio read `sys.stdin` and wrote `sys.stdout` at the PLATFORM default encoding, which on Windows is the ANSI code page (cp1252), not UTF-8. Every non-ASCII character an agent wrote through the client was silently corrupted into mojibake in the live store: `relay_register(goal="probe — unicode ✓ test")` from the Windows client stored `"probe â€" unicode âœ" test"`, with Redis holding `c3a2 e282ac e2809d` where the em dash's `e2 80 94` belonged, while the identical call from the Linux VPS round-tripped byte-perfect — the server was never involved. This is not hypothetical: the owner's REAL presence entry already reads `"... Firekeep â€" due-diligence ..."`, and the same corruption is in the live replay stream's `session.started` payload and in every task/DM/bulletin written from that machine. It reaches every write surface, memories and skills and corpus text included. The gateway is the sharpest case — it pinned its BACKEND subprocess pipes correctly (`Popen(..., text=True, encoding="utf-8")`) and left its own stdio on the locale default, the one hop nobody configured. `force_utf8_stdio()` reconfigures stdin/stdout/stderr to UTF-8 (plus a literal-LF `newline` on stdout — Windows text mode translates LF to CRLF, which corrupts JSON-RPC FRAMING independently of the characters) and is called at the top of `gateway.run()`, `hooks/__main__.main()`, `shim.run()` and `decision/server.main()`. It is a no-op on a UTF-8 platform and swallows any failure — a stream that cannot be reconfigured is not a reason to refuse to start. **Existing corrupted records cannot be recovered automatically**; cleaning the history needs a one-off latin-1-encode → utf-8-decode repair pass, which is the owner's call. Guards: `client/tests/test_stdio_encoding.py` (including a real cp1252 → utf-8 round trip, so the file is not a no-op on Linux CI).

**OpenCode adapter (`client/firekeep_client/adapters/opencode.py`):** renders three surfaces — the one `firekeep gateway` MCP entry into `$XDG_CONFIG_HOME/opencode/opencode.json` (`mcp` key, opencode's native `{type: "local", command: [...], environment}` shape), a firekeep-owned marker-guarded JS plugin at `.../opencode/plugins/firekeep-hooks.js`, and the firekeep instruction block upserted into the user's global `.../opencode/AGENTS.md` (marker-delimited, claude-CLAUDE.md precedent). The plugin bridges opencode's hooks to the same five hook cores via the dispatcher: session_start fires from the FIRST hook seen (`ensureStarted` latch — empirical 1.14.22: in `opencode run` mode `session.created` publishes before plugins subscribe), `session.idle` (turn end)→`prompt`, `session.deleted`→`stop`, `tool.execute.before/after` (`edit`/`write` mapped to the Claude-shaped `Edit`/`Write` names the cores expect; `bash`→`Bash` on the after side)→`pre_tool`/`post_tool`. Pre-edit blocking THROWS on the dispatcher's `--block-exit 2` exit — **VALIDATED live on opencode 1.14.22 as a HARD gate** (`docs/OPENCODE-VALIDATION.md`; write to `.env` aborted with the policy reason, file untouched). Caveats: briefing/inbox text goes to opencode's console log, not model context (no systemMessage channel); `stop` fires only on session deletion (not every turn end like Claude), so hard quits rely on briefing crash detection; headless `opencode run` auto-rejects opencode's own `permission: ask` before the firekeep gate is reached. Foreign files at the plugin path (no marker) are never overwritten or deleted.

See `docs/MULTI-AGENT.md` for the full workflow guide.
Existing `relay_claim`/`relay_release` remain as backward-compatible aliases.

## Symdex auto-index (client kit — `firekeep_client.symdexindex`)
Background workspace indexing from the `session_start` hook core. **ON by default**; opt out with `FIREKEEP_NO_AUTO_INDEX=1` or `[symdex] auto_index = false` in `~/.firekeep/config`.

**What it replaces.** `symdex/claude-plugin/symdex/scripts/ensure-indexed.sh` (a SessionStart hook) only ever PRINTED `ACTION REQUIRED: call index_folder`. A bash hook has no MCP client, and symdex's only entry point was the stdio server `firekeep-symdex = firekeep_symdex.server:main`, so the script could not index even in principle — it could only ask the agent to. Sessions that ignored the ask left the repo unindexed while the hook kept reporting the problem as though reporting were a fix (the same hope-vs-guarantee failure as the pre-0.1.17 untagged-calls nagging and the decision-board instruction that lived only in one repo's CLAUDE.md). A registered marketplace in a dev's `~/.claude/settings.json` may still point at a retired checkout of that plugin; its legacy `.mcp.json` key differs from `firekeep-symdex`, so a future `.mcp.json` would silently fall through to its local-file branch.

**The missing seam: `python -m firekeep_symdex.reindex <path> [--incremental]`** (`symdex/src/firekeep_symdex/reindex.py`, symdex 0.2.14) — a headless one-shot index a hook can actually spawn, and a human can run verbatim to reproduce what the hook did. Deliberately `-m` rather than a console script (resolvable from `sys.executable` alone, no PATH dependency, no shim to keep in sync). Exit codes are an interface: `0` success, `1` indexing reported failure, `2` unexpected exception — always with a JSON line on stdout, because a detached caller can see nothing else. `use_ai_summaries` defaults **False** here (the MCP tool defaults True, which bills an Anthropic/Gemini key per index — a background index the user did not ask for must not spend money).

**Client side (`client/firekeep_client/symdexindex.py`).** Shape lifted wholesale from `autoupdate`: DETACHED spawn (a cold index is 10-30s against a 15s SessionStart hook timeout — inline would trade a missing index for a hung session start), ATOMIC `O_EXCL` claim per `(folder, stamp)` (two windows opening one repo must not both write the same `local-<name>.json`; the loser's partial write is what the next session loads), and never raises. Unlike auto-update there is **no "applies next session" caveat** — the index is plain data read at tool-call time, so a mid-session index becomes visible as soon as it lands. `index_root()` honours symdex's own `CODE_INDEX_PATH` override and `index_file()` mirrors `IndexStore._index_path` for `owner="local"` (slug `local-<basename>`) — disagreeing would report "not indexed" forever against a populated index. Eligibility is `is_indexable()`: a directory containing `.git` (`.exists()`, not `.is_dir()` — a linked worktree/submodule has `.git` as a FILE), which is the guard against indexing `$HOME` just because a session started there. **Import boundary:** this module must NOT import `firekeep_symdex` — the hook cores are stdlib-only and symdex carries tree-sitter, which would otherwise load in every PreToolUse gate on every Edit; the subprocess IS the seam, which is also why the freshness check parses `indexed_at` off disk rather than via `IndexStore`.

**Cadence is owned by one function: `should_index(folder, idx) -> str | None`.** Return `None` to skip; return a **stamp** to index, where the stamp doubles as the once-only claim key — so its granularity IS the cadence. It runs inside the hook ahead of the spawn, so it must be stat()-cheap; anything costlier belongs in the child. The shipped policy: **build unconditionally when the index is absent** (the only case where the user is strictly worse off than before this feature — every symdex tool answers "Repository not found"), otherwise stamp `date.gitref` so a refresh fires **on a new commit or once a day, whichever comes first**. `_git_tip_stamp` derives the git half from two small reads and no subprocess (`.git/HEAD` → raw sha when detached, else the loose branch-tip file's mtime), returning `None` — degrading to the daily floor — for a linked worktree/submodule (`.git` is a file), a `packed-refs` layout, or a branch with no commits. Deliberately NOT every session: starts are frequent and bursty (reopened window, crashed session, three terminals on one repo) and an unconditional reindex on each is the eager failure this avoids. **Known limitations:** symdex keys indexes by folder BASENAME, so two checkouts of one repo in different parents share a single index slot; and because this only runs at SessionStart, an index still goes stale *during* a long editing session regardless of policy — that is `watch_folder`'s job — which **could not see added files** until 2026-08-06. `tools/watch_folder.py::_get_indexed_mtimes` built its mtime map by iterating symbols ALREADY in the index, so a newly created file — which by definition contributes no symbols to an index built before it existed — could never enter the map, making the loop's own `if rel not in last_mtimes` addition test unreachable by construction (modifies and deletes of indexed files were caught). Since `session_start` already covers "changed since last session", noticing work done DURING a session is the only thing this tool adds, and creating files is most of that work. **Fixed** by snapshotting the FILESYSTEM instead of the index (`_scan_source_mtimes`). Deliberately NOT via `discover_local_files`, which is the obvious reuse and the wrong one: it opens every candidate to sniff for binary content and stat it for size, measured at **~4.0s on the Firekeep root against a 5s poll** — a watcher spending 80% of its life walking the disk. The walk was never the cost (a full `rglob` of 24,936 entries is 142ms; a pruned walk is 38ms) — the per-file opens were. `_scan_source_mtimes` applies only the PATH-based half of the same filter chain (directory pruning via `_PRUNE_DIRS`, `should_skip_file`, `.gitignore`, `is_secret_file`, the extension whitelist) and takes mtime from the `scandir` entry: **321ms on the Firekeep root, 74ms on symdex**. It is a deliberate SUPERSET of what gets indexed — a new file that passes the path filters but is rejected by indexing (binary, oversized) costs one reindex that then declines to index it, and cannot retrigger, since the snapshot records its mtime either way; a missed addition is silent forever. No `max_files` cap is applied, unlike indexing: the cap bounds index SIZE, not which changes matter. Post-reindex the loop advances to the snapshot it just COMPARED rather than re-scanning — a file edited while the reindex ran would otherwise have its new mtime recorded as already-seen and the edit lost permanently. Guards: `symdex/tests/test_watch_folder_sees_new_files.py` (21 tests, including a discriminator that runs the old index-derived logic and asserts it misses the addition).

**Fixed in passing (symdex 0.2.14):** `tools/index_folder.py` hardcoded `500` as both the truncation-note threshold and its message while `discover_local_files` actually caps at `DEFAULT_MAX_FILES` (1500, `FIREKEEP_SYMDEX_MAX_FILES`), so every repo over 500 files was told `"indexed first 500"` on a **complete** index — Firekeep's own 611-file index reported it. Both now read the real cap, matching `index_repo.py`'s already-correct version. This matters more than a cosmetic string because that JSON line is the only diagnostic a detached background index leaves behind. Guarded by `test_no_false_truncation_note_below_the_cap`.

**Deploy dependency:** the client's spawn target only exists in symdex **≥ 0.2.14**. Against an older installed wheel the detached child dies with `No module named firekeep_symdex.reindex` and — being detached with output to DEVNULL — fails invisibly. Bump the bundled symdex wheel (and its bootstrap checksum) alongside any client release carrying this.

**Guards:** `client/tests/test_symdexindex.py` (31 tests: opt-out precedence, `CODE_INDEX_PATH` agreement, IndexStore slug parity, git-tree eligibility incl. worktree-as-file, claim-path sanitisation of a hostile stamp, once-per-`(folder, stamp)`, claim release on failed launch, never-raises, honest nudge wording when the spawn fails, and the policy's build-if-absent / new-commit / daily-floor / degraded-git-layout branches), `symdex/tests/test_reindex.py` (8 tests: `-m` resolvability, exit-code contract, JSON-on-failure, AI-summaries-off default, no-false-truncation-note).

## Night Shift (client kit — `firekeep_client.nightshift`)
The Fleet-as-GPU drain for the `distill_session` Relay tasks the `stop` hook has enqueued since SP1b. `firekeep night-shift [--max N] [--dry-run]` runs where the free compute lives — the developer's machine, against a LOCAL model served by **LM Studio (`:1234`) or Ollama (`:11434`)**. Both speak the OpenAI-compatible API, so supporting both is pure DETECTION — nothing in the request path differs: with `FIREKEEP_NIGHTSHIFT_LLM_BASE` unset they are probed in that order and the first to answer wins, while an EXPLICIT base is probed alone so a typo fails loudly instead of silently landing on a different engine. Config: `FIREKEEP_NIGHTSHIFT_LLM_MODEL` default `qwen/qwen3.6-35b-a3b` (an LM Studio identifier — Ollama users must set this), identity `FIREKEEP_NIGHTSHIFT_AGENT_ID` default `night-shift`, `FIREKEEP_NIGHTSHIFT_ALLOW_REMOTE` to override the cloud-model refusal. Two pre-flight aborts happen BEFORE anything is leased: a `<name>:cloud` model is refused (Ollama routes those to a third party, silently inverting the local-only premise the feature rests on — the `SSL_CERT_FILE`/`FIREKEEP_KEEP_SSL_CERT_FILE` opt-out shape), and the configured model is checked against the backend's own `/models` list, since finding the BACKEND is not the same as having the MODEL and a mismatch otherwise failed deep in the run with a bare 404. That check is deliberately lenient — an empty or unreadable list means "cannot tell", never "absent", so a backend reporting an unexpected shape cannot veto a runnable shift. Per task: lease `distill.<task_id>` (fencing token), reconstruct evidence (Cortex replay summary + auto-evals, best-effort, plus the task's workspace snapshot), one STRICT-JSON local-LLM distillation (single retry, then the task is marked `failed` — visible, never retried forever), then write through the EXISTING review surfaces: `memory_learn` + optional `skill_create(status="draft")`, both attributed to the ORIGINAL session's agent/session (never the worker). The `stop` hook stamps `session_id=<sid>` (from the bridge tap's session stash) into the task description (0.1.23); older tasks without a stamp are completed as `legacy` with an honest note. Counting is honest: distilled/legacy/duplicates increment only after the relay CONFIRMS the update in-band (relay tools return `{"error": ...}` with HTTP 200 — never raise); a confirmed-stored memory whose completion update fails counts as `failed`, loudly. A TRANSIENT LLM failure defers the task (stays pending) and stops the shift; only malformed model output (one retry) marks a task `failed`. One session can enqueue N per-turn tasks (Stop fires every turn) — the stop hook dedupes via a scratch marker per session AND the worker closes same-session duplicates within a run. **That marker was keyed ONLY on the stash `session_id`**, which exists only once the agent has called `ctx_start_session`; a session that never did got `marker=""` and the guard short-circuited into re-enqueuing every turn. Measured 2026-08-02 on the live queue: **193 of 200 pending tasks were per-turn duplicates** from unstamped sessions, while all 7 stamped sessions held exactly one each. Dedup now falls back to the runtime `session_id` in the hook payload (no Bridge session, no network call), then to a parenthesised `(none)` sentinel that cannot collide with a real id. The `description` stamp stays stash-only on purpose — Night Shift keys evidence by the BRIDGE session, so stamping a runtime id would forge a task that looks distillable and is not. Only the authoritative stash key gets a permanent marker: `state.reap_stale` sweeps scratch by each marker's declared expiry and **never by file age**, so a permanent marker per runtime session would leave one file per session forever. Personal/bypass mode is a hard no-op checked before any call; no reachable backend aborts before any task is touched; dry-run touches nothing but the task listing. Stdlib-only (`hooks._mcp` + `transport`) — the import boundary holds. POST /skills persists `X-Agent-Id`/`X-Session-Id` provenance (previously resolved-then-discarded in the MCP proxy and hardcoded null in the route).

## Personal / Bypass Mode (client kit)
An in-session escape hatch: make Firekeep go **dormant** for personal work — nothing logged, recalled, or sent to the server. A single gate, `resolver.is_bypassed()`, is consulted everywhere; it returns true when a **transient marker file** (`~/.firekeep/personal`, deliberately NOT the config — toggling it never rewrites config) is present-and-fresh, OR the `FIREKEEP_BYPASS` env var is truthy. Fails toward NOT-bypassed on any error, so a bug here can never silently stop team logging.

**Two tiers, split by process lifecycle:**
- **`/personal` (live, mid-session)** — a rendered Claude slash command (`~/.claude/commands/personal.md`, firekeep-owned + marker-guarded so `unrender` removes only our copy) that runs `firekeep personal toggle`. The **hooks** re-read the marker every event, so the toggle takes effect at once: `session_start`/`prompt` no-op and instead emit a loud "⚠ PERSONAL MODE" systemMessage; `pre_tool`/`post_tool` allow (exit 0) with no agent-gateway call; the **decision** server checks per-call and returns a "suppressed — personal mode" notice (no Cortex synth, no socket); the **sidecar** (`firekeep-sidecar`, the presence owner for hookless runtimes) gates its register/heartbeat/snapshot/deregister on the same live gate, so no presence or workspace data reaches Relay/Bridge while bypassed. `stop` and `session_end` are the two hooks NOT short-circuited — both self-handle bypass. **`session_end` clears the marker** (**auto-clears personal mode at session end** — can't leak into the next session); `stop` only skips its own Relay/Bridge comms, because Stop fires at EVERY assistant turn end and clearing there ended personal mode after turn 1 (`/personal` protected exactly one turn). kiro has no session-end event, so there the marker persists to the TTL backstop instead — announced loudly by session_start's banner and `firekeep doctor`'s personal-mode row. A `FIREKEEP_PERSONAL_TTL_HOURS` (default 12) backstop reaps a marker a crashed session never cleared (also bounds an active single-session personal mode to that horizon; `FIREKEEP_BYPASS` is the un-TTL'd hard tier for longer).
- **`FIREKEEP_BYPASS=1` (hard, whole-session)** — set before launch. The running **shim** can't un-list tools mid-stream, so it honors this only at startup: `shim.run()` serves an inert **zero-tool** MCP server (no config resolved, nothing proxied to the HTTP service). Use when the whole session is personal from the start.

**CLI:** `firekeep personal [on|off|status|toggle]` (default `toggle`) — flips the marker; usable in any runtime via `! firekeep personal`. **`/personal` as plain chat text works in ANY runtime** (client 0.1.16): kiro has no slash-command surface, so typing `/personal` there used to do nothing — now the hook DISPATCHER (`hooks/__main__.py::_personal_text_command`) intercepts a prompt whose text is exactly `/personal [on|off|status|toggle]` and performs the toggle itself, returning the state as a systemMessage. The intercept deliberately sits BEFORE the dispatcher's bypass gate — while personal mode is ON the prompt core is short-circuited, so an in-core intercept could never toggle OFF. Both kiro's `userPromptSubmit` and Claude's `UserPromptSubmit` deliver the message as `payload["prompt"]` (kiro shape validated empirically on kiro-cli 2.12.1; opencode's bridge maps `session.idle` to the prompt core without a prompt text, so `/personal` there still goes through `! firekeep personal` or the CLI). `firekeep doctor` shows a `personal-mode` row that WARNs when bypass is active, so it's never silently left on.

**Config (client, all optional):** `FIREKEEP_BYPASS` (truthy → hard startup bypass), `FIREKEEP_PERSONAL_TTL_HOURS` (default 12; marker staleness backstop). Concurrency caveat: the marker is machine-global, so concurrent sessions share personal mode — fine for focused personal work, coarse if several run at once.

## Session Resumption
Automatic discovery and resumption of paused or crashed sessions on conversation start. No new tools or endpoints — purely hook-layer.

**How it works:**
1. During a session, the `prompt` hook core snapshots workspace state (git branch, recent commits, diff stats) to the platform cache dir every 5th prompt
2. On clean exit, the `stop` hook core captures a final snapshot
3. On next conversation start, the `session_start` hook core (via `GET /briefing`) surfaces Bridge paused sessions and Relay-presence crash detection
4. If resumable sessions found, the briefing nudges the agent to call `ctx_resume_session` which returns the full shadow context including the workspace snapshot

**Crash detection:** An "active" session with no Relay presence entry means the previous instance crashed. The briefing treats it as resumable.

**Session ownership (Bridge, `app/session.py`).** `resume_session` performed NO ownership check: it read the status, refused only `completed`/`abandoned`, then unconditionally rewrote the session's `agent_id` and pointed the CALLER's active key at it. Proven between two probe identities on the live deployment — a session owned by AND ACTIVE for one agent was taken over by another with no error, and `ctx_list_sessions()` with no filter returns every agent's session id, so the ids are trivially discoverable. Three compounding effects: the victim's `nb:active:<agent>` pointer was never cleared (both agents' `ctx_get_shadow` resolved to the same session), an ACTIVE session was stolen even though the tool documents itself as resuming a PAUSED one, and the memory distilled at completion was attributed to the thief. Fixed: `resume_session` refuses a session owned by another agent unless the caller passes the new `takeover: bool = False`, refuses an ACTIVE session outright even with `takeover` (resume picks up work that STOPPED; it never evicts a live agent), and `RESUME_SESSION_LUA` clears the previous owner's active pointer atomically with the swap, so a deliberate hand-off TRANSFERS the session rather than sharing it. The ownership check was not a new idea — `complete_session` already had one.

**`ctx_update` refuses a finished session.** `SessionManager.update` resolved the target from the agent's active pointer and dispatched on category with no status check, so after the takeover above the original owner — whose pointer still dangled — wrote a progress entry into a COMPLETED session and got `{"status": "ok", "component_count": 3}` back. The entry really landed and `ctx_get_shadow` kept serving it as live working context, but distillation had already run at completion and never runs again, so the distilled memory contained none of it: **a success response for a write that can never reach long-term memory.** It now raises `Cannot update {status} session` before any write (mirroring `resume_session`'s own `Cannot resume completed session`), and `complete_session`/`abandon_session` clear the active pointer of EVERY agent naming that session (owner AND caller) rather than only `meta["agent_id"]` — the dangling pointer was the precondition for the lost write. Guards: `bridge/tests/test_session_ownership.py`.

**Age thresholds:** Strong nudge for sessions < 72h old ("You have unfinished work"), neutral mention for older sessions.
