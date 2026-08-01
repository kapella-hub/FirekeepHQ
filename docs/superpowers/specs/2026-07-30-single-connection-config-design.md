# Single-Connection Config — Removing the Profile Taxonomy

**Status:** design, approved 2026-07-30
**Sequencing:** lands **before** `2026-07-30-client-enrollment-join-codes-design.md`. Join
codes write into the config shape defined here; building enrollment against
`[personal]`/`[office]` and then collapsing them means writing that code twice.

## Problem

`profile` currently names three unrelated things, and only one of them is real.

1. **Connection shape.** `personal` is `kind=ports` (a host plus fixed service ports);
   `office` is `kind=paths` (a `base_url` path-routed behind a TLS proxy) plus
   `verify_tls` and `ca_path`. These are two deployment topologies wearing identity
   labels. They were never symmetric: `_configure_office` (`wizard.py:187-218`) probes
   TLS and prefills from org defaults, while `_configure_personal`
   (`wizard.py:120-133`) validates nothing at all — so the profile everybody actually
   uses is the one with no verification.
2. **The taxonomy.** `[active]`, `[pins]`, `firekeep profile use|show|pin|unpin`,
   `FIREKEEP_PROFILE`, `--profile`, and the `@{profile}` qualifier on cache keys.
3. **Dormancy** — `/personal`, `firekeep personal`, `~/.firekeep/personal`,
   `FIREKEEP_BYPASS`. Unrelated to (1) and (2); it merely shares a word.

The first user-facing question a new install asks is
`Configure which profile? [1] personal [2] office [3] both` (`wizard.py:110`) — a
question about deployment topology, posed to someone who has just run a one-line
installer and has no way to answer it. On 2026-07-29 that prompt sent a teammate down
the office branch, so their API key landed in `[office]` while `[active]` was
`personal`, and `resolver.py:295` reads `api_key` from the active profile only. The
client reported "cannot connect".

## Goal

One connection. No taxonomy, no switching, nothing to choose. The connection shape
survives as a **derived** field, written by the installer (and, once enrollment lands,
by the join code) — never named, never prompted.

**Dormancy is explicitly out of scope and unchanged.** `firekeep personal`,
`/personal`, the marker file, the TTL backstop, `FIREKEEP_BYPASS`, and every hook and
sidecar gate that consults `resolver.is_bypassed()` stay exactly as they are.

**`personal` is reserved for dormancy, product-wide.** Deleting the `[personal]` section
removes one of three claimants, not all of them: the entitlements design named its
free tier Personal in prose
(`2026-07-30-workspace-entitlements-and-onboarding-design.md:136, 147, 269`) and, worse,
in a payload literal that can reach client-visible output (`"plan": "personal|team"`,
`:127`). That collision is resolved there, by renaming the free plan to **Solo** — not here.
After both changes `personal` denotes exactly one thing — the temporary dormancy toggle
at `cli.py:1074-1081`, its `doctor` row at `cli.py:889-899`, and the marker at
`resolver.py:19-26, 68-72` — and no plan, profile or config section may reclaim the word.

---

## 1. The config

```ini
[identity]
agent_id = bob-thinkpad

[server]
kind = ports              ; ports | paths — derived, never prompted
scheme = http
host = firekeep-host.example ; kind=ports
; base_url = https://…    ; kind=paths
verify_tls = false
; ca_path = …             ; required when scheme=https
api_key = nxs_…

[dist]
base_url = https://…
auto_update = true
```

`[identity]` is a new section rather than a key inside `[server]`: identity is a
property of the machine, not of the connection, and separating them keeps a future
multi-config setup from duplicating the agent id.

Everything `resolver.resolve()` does is unchanged except for where it reads from. The
TLS invariants in `_verify_for` (`resolver.py:237-265`) — `https` requires both
`verify_tls=true` and a `ca_path`, `ca_path=os` means the OS trust store — are
preserved verbatim; they are the one piece of the profile machinery that was
protecting something.

Every `ConfigError` raised on the resolve path names the **resolved config file**, not a
profile. Today all of them name the section — `agent_id` (`resolver.py:219, 221, 224`),
`_require` (`:230, 233`), `_verify_for` (`:241, 247, 253`) and `resolve` itself
(`:285, 315-318, 322-324`) — because the profile name was the only identifier available.
With one section there is nothing to disambiguate and the useful identifier is the path,
which `_config_path` already resolves from `FIREKEEP_CONFIG` (`resolver.py:52-58`) while
`load_config` reads it and discards it (`:152-166`). Threading the path through to the
error strings is therefore required, not cosmetic: `~/.firekeep/config` hardcoded in a
message is wrong for every `FIREKEEP_CONFIG` user, including the ones §3 sends there.

---

## 2. Deletions

| Deleted | Location |
|---|---|
| `active_profile()` | `resolver.py:172-197` |
| `pinned_profile()`, `PIN_NAME_RE` | `resolver.py:169, 200-210` |
| `_ask_profiles()` | `wizard.py:107-118` |
| `_configure_personal()` / `_configure_office()` | `wizard.py:120-133, 187-218` |
| `_PERSONAL_DEFAULTS` / `_OFFICE_DEFAULTS` | `wizard.py:24-38` |
| `firekeep profile use\|show\|pin\|unpin` | `cli.py:1032-1046` |
| `--profile` on `install` and `connect` | `cli.py:1062, 1087` |
| `FIREKEEP_PROFILE` handling | `cli.py:95-111`, `resolver.py:181-183`, `hooklog.py:26`, `shim.py:441` |
| `FIREKEEP_PROFILE` injection into rendered MCP entries | `adapters/claude.py:183`, `codex.py:47`, `kiro.py:228`, `opencode.py:194` |
| `@{profile}` qualifier on cache keys | `state.py:60-71` and the session-stash keys |
| doctor's pin-hygiene checks and per-pinned-profile api-key/CA checks | `cli.py:728-756` |
| `_write_profile()` and the `[personal]`/`[active]` writes it performs | `connect.py:171-192, 224, 231-232` — the second writer of `~/.firekeep/config` |
| `read_pin()` (render's only config dependency) | `adapters/base.py:137-153` |
| `active_profile()` call sites in every hook core and the sidecar | `session_start.py:74`, `stop.py:48`, `session_end.py:73`, `prompt.py:60`, `pre_tool.py:64`, `precompact.py:41`, `sidecar.py:142, 162, 186, 218, 238, 255` |
| `_shadow_cursor_key`'s `@{profile}` qualifier | `state.py:408-409` — a third `@{profile}` key beyond the two §5 names |
| `--profile` parsing and `FIREKEEP_PROFILE` export in the hook dispatcher | `hooks/__main__.py:95-100, 205, 210` |
| `FIREKEEP_PROFILE` in hook log context | `hooklog.py:25-29` |
| the `profile_pin` capability row and its label | `contract/matrix.py:46-56, 74` |
| the module docstring naming `firekeep profile use` | `sidecar.py:6-7` |
| `profile '<name>'` in every resolver error string | `resolver.py:219, 221, 224, 230, 233, 241, 247, 253, 285, 315-318, 322-324` (see §1) |

A repo-wide grep for `active_profile|pinned_profile|read_pin|PIN_NAME_RE` returns 38
non-test hits across 17 modules; the table above names the ones that are structural
rather than mechanical. `connect.py`'s `--profile` flag (`cli.py:1087`) was already
listed — what was missing is the module that *writes* the config.

**One carve-out from the table above.** The one-shot migration (§4.3) is the last reader
of `[active]` and `[pins]`. `active_profile()`'s section resolution, `pinned_profile()`,
and `_check_pins`' dangling-pin taxonomy move into `migrate.py` as private helpers —
stripped of the `FIREKEEP_PROFILE` branch (`resolver.py:181-183`), which §4.1 requires be
neutralized — and are deleted from `resolver.py`/`cli.py`. Nothing outside `migrate.py`
may call them, and they retire with the §2.1 stubs.

`_fetch_org_defaults` (`wizard.py:136-160`) and `_probe_os_trust`
(`wizard.py:163-184`) **survive** — they prefill and probe a connection, and neither
depends on there being two of them. `_probe_os_trust` in particular should now run for
every `scheme=https` connection rather than only the office branch, which is half the
reason the surviving profile was the unvalidated one.

### 2.1 Deprecation stubs

`firekeep profile …` stays registered for two releases as a stub that exits 2 with:

```
`firekeep profile` was removed — there is now exactly one server connection.
Your config was migrated to [server]; see <resolved config path>.
To point this machine at a different server: re-run the installer, or set
FIREKEEP_CONFIG=<path> to use a separate config file.
```

An unrecognised subcommand printing bare argparse usage would tell a user their
muscle memory is wrong without telling them what replaced it.

The "re-run the installer" sentence carries the same cross-spec obligation as §4.4's: it
becomes `firekeep join <code>` when
`2026-07-30-client-enrollment-join-codes-design.md` lands, and
`test_migration_message_handoff.py` covers both messages.

---

## 3. Multiple servers

Already solved, and the profile machinery was redundant with it:
`resolver._config_path()` (`resolver.py:52-58`) reads `FIREKEEP_CONFIG`.

```bash
FIREKEEP_CONFIG=~/.firekeep/client-b.conf firekeep doctor
```

This is the same mechanism `[pins]` used — baking an env var into rendered MCP entries
— with one fewer concept, no name validation to get wrong (`PIN_NAME_RE` exists solely
because a pin name is interpolated into a rendered command string), and no possibility
of a pin naming a section that does not exist.

A machine needing two servers in two runtimes sets `FIREKEEP_CONFIG` in that runtime's
rendered env, exactly where `FIREKEEP_PROFILE` used to go. This is a documented
escape hatch, not a first-class feature; the supported case is one server per machine.

`FIREKEEP_CONFIG` is no longer only an escape hatch — §4.4 makes it the **mandated
destination** of a migration failure, so the full recipe belongs here rather than as a
gesture. A machine that genuinely needs two servers:

```bash
cp ~/.firekeep/config ~/.firekeep/office.conf   # then edit it to the [server] shape
FIREKEEP_CONFIG=~/.firekeep/office.conf firekeep doctor
```

To bind a runtime to it, add `FIREKEEP_CONFIG` to that runtime's rendered MCP entry —
exactly where `FIREKEEP_PROFILE` used to go.

**A re-render clobbers it, and that is the same property §5 relies on.** `merge_owned`
replaces each firekeep-owned MCP entry dict whole (`adapters/base.py:193-196`), which is
what makes a stale `FIREKEEP_PROFILE` self-clean and what makes a hand-added
`FIREKEEP_CONFIG` disappear on the next `firekeep install`. This is the one sharp edge of
the escape hatch and it must be documented rather than discovered: nothing in the
deletion set replaces `[pins]` as a *managed* per-runtime mechanism. The affected
population is real — `docs/DEPLOYMENT-OFFICE.md:151-154` and `docs/SETUP-CLAUDE-CODE.md:29`
both instruct users to set an active profile plus a pin, and under §4 every one of those
machines now meets a hard failure at first config load.

---

## 4. Migration

Runs once, at config load, before any other read. It is one-way, and it either produces
a complete `[server]` or refuses and writes nothing.

### 4.1 What "resolves to one endpoint" means

A legacy config resolves to one endpoint iff every candidate section produces an
identical **resolution fingerprint**:

    (kind, scheme, host | base_url, verify, api_key)

`verify` is `_verify_for`'s return value (`resolver.py:237-265`) — `False`, the
`OS_TRUST` sentinel, or the expanded `ca_path` — not the raw key, because
`ca_path = os`, `ca_path = ~/ca.crt` and an absolute path to the same file are three
spellings of two distinct trust decisions. `api_key` is the value `resolver.py:294-298`
would send as `X-API-Key`. `agent_id` is **excluded**: `wizard.prompt_config` writes one
identity into every section it configures (`wizard.py:255-257`), so it never
distinguishes two servers.

The comparison is on **resolved endpoints, never raw keys**. Two sections spelled
differently that `resolve()` turns into the same URL, trust decision and credential are
one endpoint and must migrate; two sections that differ only in a key `resolve()` ignores
are not a conflict.

The fingerprint is computed from the config file alone. `FIREKEEP_AGENT_ID` and
`FIREKEEP_PROFILE` are neutralized for the duration of the computation: `agent_id()`
returns the env var without reading the section at all (`resolver.py:213-217`), so a set
`FIREKEEP_AGENT_ID` would make a section that otherwise raises `ConfigError` resolve
cleanly — the same file would migrate or fail depending on which shell happened to
trigger config load. Migration must be a pure function of the config file.

A candidate that raises `ConfigError` under those conditions is **not live** and is
dropped: it is a section that could never have served a request.

### 4.2 What "unconfigured" means

Three shipped skeletons define the unconfigured state: `_CONFIG_SKELETON`
(`cli.py:201-222`), `_PERSONAL_DEFAULTS` (`wizard.py:24-29`) and `_OFFICE_DEFAULTS`
(`wizard.py:30-37`). A section is **configured** if either

(a) it carries a non-empty `api_key`, or
(b) its endpoint key differs from every skeleton value for its kind — `host` other than
    `127.0.0.1` for `kind=ports`, `base_url` other than `https://firekeep.office.example`
    for `kind=paths`.

`api_key` is decisive because `_configure_personal` removes the option entirely on a
blank answer (`wizard.py:128-133`): absent or empty means no credential was ever
supplied. Note that the skeleton `[office]` *resolves* — `https` plus `verify_tls=true`
plus a non-empty `ca_path` plus `agent_id=CHANGEME` passes every check — so "it resolves"
cannot substitute for this predicate.

**Unconfigured never decides which server survives.** Dropping an unconfigured section is
permitted only when the drop cannot change the surviving fingerprint. A tunnel install
(`firekeep install --host 127.0.0.1`, `docs/DEPLOYMENT.md:198-212`) against an auth-off
server is a *working* connection with the skeleton host and no key; dropping it while a
differently-configured section survives would silently repoint the machine at the other
server — the exact outcome §4.3 step 6 exists to prevent. A candidate set containing an
unconfigured section **and** a section with a different fingerprint therefore fails; it
does not resolve by dropping.

### 4.3 The algorithm

1. `[server]` exists → nothing to do.
2. Candidates = the `[active]` profile plus every `[pins]` value, read through
   `migrate.py`'s private copies of that resolution (§2 carve-out) — never through
   `resolver`, which no longer has it. A pin naming a section that does not exist is
   recorded as a dangling pin, reusing the dangling-pin taxonomy relocated from
   `_check_pins`; it is never silently ignored. An `[active]` naming a missing section is
   likewise recorded, and fails per §4.4 naming the section (§6 matrix).
3. Compute each candidate's fingerprint (§4.1). Drop non-live candidates. Drop
   unconfigured candidates only under §4.2's restriction.
4. **Exactly one distinct fingerprint survives** → migrate it: copy its keys into
   `[server]`, lift `agent_id` into `[identity]`, preserve `[dist]` byte-for-byte,
   discard `[pins]` with a line naming each pin that existed.
5. **Zero survive** → fail per §4.4, reason "no configured connection found".
6. **Two or more distinct fingerprints survive** → **fail per §4.4 and write nothing.**
   No backup file, no partial `[server]`, no `[active]` edit.

Step 6 replaces the previous "migrate `[active]`, warn about the other" rule. A printed
warning was the wrong instrument: migration runs inside `firekeep-shim`, the hook
dispatcher and the sidecar, where stderr reaches a log nobody reads, and the cost of not
reading it is a machine silently talking to the wrong server. Silently selecting one of
two configured endpoints is worse than refusing, because the user learns about it from
memories that went to the wrong place.

Behavioural consequence, stated plainly: an **unconfigured `[active]` with a configured
pin now migrates the pin.** The old rule would have discarded the only working
connection.

### 4.4 Failure — `ConfigMigrationConflict`, exit 3

Every entry point raises `ConfigMigrationConflict` (a `ConfigError` subclass) and renders
it on the channel its process actually has:

- **CLI** (`doctor`, `install`, `update`, later `join`): stderr, exit **3**. 3 is
  unclaimed — every existing failure path in `cli.py` returns 1 (`:37`, `:44`, `:921`,
  `:1019`) and argparse's own usage error is 2.
- **Hooks** (dispatcher and all six cores): a `systemMessage`
  (`hooks/__main__.py:186, 199`; the `precompact.py:80` precedent), never a block.
- **stdio MCP servers** (`firekeep-shim`, `firekeep-decision`): fail startup with the
  message on stderr. A shim that cannot resolve one connection must not guess.

Message text:

```
firekeep config migration refused: <resolved config path> defines more than one
server connection, and this version supports exactly one.

  [personal]  http://100.91.3.51:8100        (from [active])
  [office]    https://fk.corp/api/cortex     (from [pins] kiro)

Nothing was changed. Pick one and re-run the installer, or keep both by giving each
its own file:
  cp <config> ~/.firekeep/office.conf   # then edit it to the [server] shape
  FIREKEEP_CONFIG=~/.firekeep/office.conf firekeep doctor
```

Step 5's zero-candidate case renders a second message under the same exception, since
the sentence above asserts a plurality that is not true there:

```
firekeep config migration refused: <resolved config path> has no [server] section and
no configured connection to migrate from.
  [active] names 'office', which the file does not define
Nothing was changed. Run: firekeep install --host <h>   (or: firekeep join <code>)
```

Only cortex's `mcp_url` is shown. The `api_key` is never printed and never hashed into
the message; each line is labelled with **where the candidate came from**, so the user
can tell which of their own settings is which. The path shown is the resolved one, not a
hardcoded `~/.firekeep/config` — the users most likely to hit this are the ones §3 sent
to `FIREKEEP_CONFIG`.

Once `2026-07-30-client-enrollment-join-codes-design.md` lands, "re-run the installer"
becomes `firekeep join <code>`. That swap is a cross-spec obligation, not a polish item:
this spec ships first and `join` does not exist yet.

### 4.5 Migration writes concurrently and must be atomic

`load_config` (`resolver.py:152-166`) is read-only today. Making it write turns config
load into a concurrent writer, and the concurrency is immediate: four `firekeep-shim`
processes spawn simultaneously at session start (`shim.py:475`), alongside the hook
dispatcher and all six hook cores, the sidecar's per-cycle reload (`sidecar.py:141, 161,
185, 217, 237, 254`), every adapter render via `read_pin` (`adapters/base.py:137`),
`nightshift`, `cli` and `connect`. (`firekeep-decision` reaches it only when a board is
opened, `decision/server.py:608`; `firekeep-symdex` ships as a separate package and never
reads this config.)

- Serialize on an `O_CREAT|O_EXCL` lock file beside the config. A process that loses the
  lock waits for the winner and re-reads; it does not migrate.
- **The lock needs stale-owner recovery**, because `O_CREAT|O_EXCL` alone converts a
  crash into a permanent outage: a process killed between create and unlink leaves a lock
  no one owns, and every shim, hook core and sidecar cycle then blocks on config load
  forever — the client bricks itself on the next session start. The lock file records the
  owner pid and an ISO timestamp, written and `fsync`ed immediately after creation; a
  waiter that observes a lock older than
  `MIGRATION_LOCK_STALE_SECONDS` (default 30 — migration is a few file operations, so any
  longer means the owner is gone) and whose pid is not alive breaks it and retries once,
  logging that it did. **A lock file that is empty or unparseable is treated as stale on
  the same age threshold**, because `O_CREAT|O_EXCL` yields an empty file and an owner
  killed between the create and the pid write leaves exactly that — on which "is the pid
  alive" is unevaluable. That is precisely the crash class this recovery exists to close,
  so the recovery must not depend on the very field the crash prevents being written.
  Breaking is safe precisely because the write itself is atomic: the worst case is two
  processes racing `os.replace`, and both write byte-identical content derived from the
  same source file.
- Write via a temp file plus `os.replace`, so no reader ever sees a half-written INI.
- Derive the backup name from the source **content**, not the clock, so four simultaneous
  migrations produce one backup rather than four.
- Apply `state._private` (`state.py:117-135`) to the new config **and to the backup**.
  The backup carries the `api_key`; every other credential-bearing file the client writes
  is 0600 (`cli.py:50, 152, 173, 434, 974`), and a migration artifact is not an exception.

Stale `…@{profile}`-qualified keys in the platform cache dir are left to expire. They are
cache entries with a TTL, not state, and rewriting them would be more risk than orphaning
them.

---

## 5. Consequences elsewhere

- **`FIREKEEP_PROFILE` cleanup is mechanical and per-surface, not a flag day.** Every
  render site is replace-in-place, so a re-render removes the variable with no migration
  step:

  | Surface | Mechanism |
  |---|---|
  | MCP entries, all four adapters | `merge_owned` replaces each firekeep entry dict whole (`adapters/base.py:193-196`), called from `claude.py:195`, `kiro.py:231`, `opencode.py:197` |
  | Claude hook groups | `upsert_hook_group` collapses **all** firekeep groups for an event into the one rendered group (`base.py:216-232`) |
  | kiro inline hooks | `upsert_flat_hook` replaces in place (`base.py:245-252`) |
  | opencode plugin JS | `upsert_block` replaces the marker region (`base.py:264-275`) |
  | `~/.claude/settings.json` `env` | never carried it — `claude.py:198-205` writes only `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` and drops `LEGACY_ENV_KEYS` |

  Until a runtime is re-rendered its entries keep exporting a variable nothing reads,
  which is harmless: the dispatcher scans for `--profile` without validating it
  (`hooks/__main__.py:85-100`).

- **The one surface no re-render can clean is a shell rc.** `_env_profile_notice`
  (`cli.py:94-111`) is therefore converted from an install-time print into a
  `firekeep doctor` row that warns while `FIREKEEP_PROFILE` is set in the environment and
  names the file to edit. A notice printed at install time is seen once, by the process
  that no longer needs it.

- **The shim keeps `--profile` as an accepted-and-ignored stub for two releases.**
  `shim.run()` uses `parser.parse_args` (`shim.py:52-67`), which exits 2 on an
  unrecognised argument. No adapter renders `--profile` into a shim entry today — the MCP
  carrier is the env dict (`claude.py:183`, `kiro.py:228`, `opencode.py:194`,
  `codex.py:47`) and `--profile` goes only onto hook commands (`claude.py:213`,
  `kiro.py:263`, `opencode.py:77, 241`) — but `docs/KIRO-VALIDATION.md:23` documents the
  shim's `--profile` fallback as implemented, so hand-written and third-party entries may
  pass it. The failure mode is a dead MCP server; two releases of accept-and-ignore is
  cheap insurance, not a flag day.

- **Session stash and presence keys** lose their `@{profile}` qualifier
  (`state.py:60-71`), becoming `session_current_{agent}` and
  `presence_registered_{agent}`. This does not change the known concurrency
  limitation — the stash is one slot per identity per machine either way, and the
  supported partition for genuinely concurrent work remains a distinct
  `FIREKEEP_AGENT_ID`.
- **`firekeep doctor`** loses its pin rows and gains exactly one — the
  `FIREKEEP_PROFILE`-is-set warning converted from `_env_profile_notice` in the bullet
  above. Its health, versions,
  agent-id, api-key, CA-expiry, config-perms and personal-mode checks all operate on
  the single connection unchanged.
- **`cortex/` and `dashboard/` are untouched** — 11 and 1 references respectively, none
  structural. The profile concept never crossed the wire.

---

## 6. Testing

- `test_resolver_single_connection.py` — `resolve()` reads `[server]`; a config with no
  `[server]` and no `[active]` fails with a message naming the file; the `_verify_for`
  TLS invariants are re-asserted verbatim against the new shape (`https` without
  `verify_tls` refused; `https` without `ca_path` refused; `ca_path=os` honoured).
- `test_config_migration.py` — a case matrix over §4.1/§4.2, not a warning assertion:
  | config | outcome |
  |---|---|
  | `[active]=personal`, no pins | migrate |
  | `[active]=personal` + pin → `office`, **same** fingerprint | migrate, one `[server]` |
  | `[active]=personal` + pin → `office`, **different** fingerprint | **exit 3**, nothing written |
  | `[active]` unconfigured + configured pin | migrate the **pin** |
  | unconfigured section + differently-configured section | **exit 3** (§4.2's restriction) |
  | fresh `_CONFIG_SKELETON`, nothing configured | migrate the skeleton — must not brick a dev checkout |
  | `[active]` names a missing section | fail loudly, naming the section |
  | two spellings resolving to one endpoint (`ca_path` relative vs absolute) | migrate |
  | pin names a missing section, `[active]` configured | migrate `[active]`; report the dangling pin by name |
  | pin names a missing section, no other candidate | fail loudly, naming the pin |
  | `FIREKEEP_PROFILE` set to either section | identical outcome to unset, for every row above — it must not change the candidate set |
  | `FIREKEEP_AGENT_ID` set | identical outcome to unset, for every row above |
  `[dist]` survives byte-for-byte in every migrating row; migration is idempotent and a
  second run is a no-op.
- `test_migration_no_write_on_conflict.py` — **new**: on a conflict the config file is
  unchanged byte-for-byte, **no `.bak-profiles-*` file is created**, and the exit code is
  3. Step 4 backs up before writing, so a conflict path that has already produced a
  backup leaves ambiguous state for the next run.
- `test_migration_concurrent.py` — **new**: four simultaneous `load_config()` calls
  produce one migration, one backup, and a config that is valid INI at every observable
  moment; the backup is 0600. **Plus stale-owner recovery**: a lock file left by a dead
  pid older than the threshold is broken and migration proceeds; a lock held by a *live*
  pid is waited on, never broken; an empty or truncated lock file older than the
  threshold is also broken.
- `test_migration_active_unconfigured_pin_configured.py` — **new**, called out because it
  is the one case whose outcome §4 deliberately inverts: an unconfigured `[active]` with a
  configured pin migrates **the pin**. The previous rule would have discarded the only
  working connection.
- `test_migration_message_handoff.py` — **new**: §4.4's conflict message says "re-run the
  installer" while `join` does not exist, and must say `firekeep join <code>` once it
  does. The test asserts whichever sentence is correct for the shipped command set, so the
  cross-spec swap cannot be silently forgotten.
- `test_adapters_no_profile_env.py` → an **upgrade-path** test. A fresh render containing
  no `FIREKEEP_PROFILE` passes trivially once the injection is deleted; the regression
  that matters is a machine carrying pre-collapse artifacts. Seed each of the four
  runtimes' config with `FIREKEEP_PROFILE` and a `--profile` argument at every render
  site (`claude.py:213`, `kiro.py:263`, `opencode.py:77` and `:241`'s `PROFILE_ARGS`,
  `codex.py:46-47`), re-render, and assert both are gone and foreign entries are
  untouched.

- `test_profile_stub.py` — `firekeep profile use x` exits 2, names the **resolved** config
  path and `FIREKEEP_CONFIG`, and its "point at a different server" sentence matches the
  shipped command set (shared fixture with `test_migration_message_handoff.py`).
- `test_bypass_unchanged.py` — **guard**: `resolver.is_bypassed()`, the marker path, the
  TTL backstop and `FIREKEEP_BYPASS` behave identically after the collapse. Dormancy
  shares a word with the deleted section and must not be collateral damage.
- Existing suites: ~738 test references to `profile` across 46 files need mechanical
  updating. Most are fixtures constructing a two-profile config; a shared
  `single_connection_config()` fixture replaces them.

---

## 7. Scope and the same-change checklist

~1,300 references across the client and its tests, ~198 in docs, and essentially nothing
server-side. Large but mechanical, and contained in one package.

It deletes a class of bug rather than instances: "key in the wrong section", "pin names a
profile that does not exist", "`FIREKEEP_PROFILE` silently overrode `[active]`", and "the
profile everyone uses is the one with no validation" all stop being expressible.

**All of the following land in the same commit.** A stale `firekeep profile use` after
the command is gone is the documentation-drift failure the repo's own Change Consistency
Checklist exists to prevent.

| Surface | Files |
|---|---|
| Resolver, config, state | `resolver.py`, `state.py`, `connect.py`, `contract/matrix.py`, `migrate.py` (new — §4.3) |
| Tests | ~738 `profile` references across 46 files, replaced by the shared `single_connection_config()` fixture (§6) |
| Wizard, CLI, runtime entry points | `wizard.py`, `cli.py`, `hooks/__main__.py`, all six hook cores, `hooklog.py`, `shim.py`, `sidecar.py`, `nightshift.py` |
| Adapters | `adapters/base.py`, `claude.py`, `codex.py`, `kiro.py`, `opencode.py` |
| Docs | `CLAUDE.md:185, 187`; `docs/MULTI-AGENT.md:33, 97, 174, 176, 182, 183, 188, 190`; `docs/SETUP-CLAUDE-CODE.md:29, 46, 104, 146, 152`; `docs/DEPLOYMENT-OFFICE.md:91, 148, 151, 153-154, 157`; `docs/SETUP-CODEX.md:31, 136, 138`; `docs/DEPLOYMENT.md:205, 213, 216, 311`; `docs/INTEGRATIONS.md:104`; `README.md:116, 142-143`; `docs/DESIGN.md:318` |

`CLAUDE.md:187` is not optional alongside `:185`: it carries the wizard description that
dies with `_ask_profiles` — "which profile to configure (personal / office / both)", the
per-profile ports/paths shape, the "A ports-style profile is deliberately not offered a
TLS toggle" rationale, and "`--agent-id` / `--host` / `--profile` seed the prompts".
`docs/MULTI-AGENT.md:183` is a whole `FIREKEEP_PROFILE` table row, not a mention.

Deliberately **excluded**, and the exclusions are as much a part of the checklist as the
inclusions:

- **Both bootstrap scripts.** Neither `client/bootstrap/install.sh` nor `install.ps1`
  references a profile; both hand off with `--dist-base` / `--runtime` /
  `--non-interactive` only (`install.sh:114, 116, 240, 243`; `install.ps1:99, 233`).
- **`docs/KIRO-VALIDATION.md:23` and `docs/OPENCODE-VALIDATION.md`.** Dated empirical
  records (2026-07-13) of what a specific runtime version did. Rewriting tokens inside a
  validation record makes it describe a run that never happened;
  `adapters/base.py:35-40` already treats legacy tokens this way.
- **`docs/CONFIGURATION.md:107, 198`** — false positives describing the `[dist]` and
  `kind` keys, not the taxonomy.
