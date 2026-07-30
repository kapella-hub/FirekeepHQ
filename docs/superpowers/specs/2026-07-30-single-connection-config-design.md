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
sidecar gate that consults `resolver.is_bypassed()` stay exactly as they are. Once
`[personal]` the config section no longer exists, `personal` means precisely one thing
and the naming collision resolves without a rename.

---

## 1. The config

```ini
[identity]
agent_id = bob-thinkpad

[server]
kind = ports              ; ports | paths — derived, never prompted
scheme = http
host = srv1143982         ; kind=ports
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
| `FIREKEEP_PROFILE` injection into rendered MCP entries | `adapters/claude.py:133`, `codex.py:47`, `kiro.py:228`, `opencode.py:194` |
| `@{profile}` qualifier on cache keys | `state.py:60-71` and the session-stash keys |
| doctor's pin-hygiene checks and per-pinned-profile api-key/CA checks | `cli.py:728-756` |

`_fetch_org_defaults` (`wizard.py:136-160`) and `_probe_os_trust`
(`wizard.py:163-184`) **survive** — they prefill and probe a connection, and neither
depends on there being two of them. `_probe_os_trust` in particular should now run for
every `scheme=https` connection rather than only the office branch, which is half the
reason the surviving profile was the unvalidated one.

### 2.1 Deprecation stubs

`firekeep profile …` stays registered for two releases as a stub that exits 2 with:

```
`firekeep profile` was removed — there is now exactly one server connection.
Your config was migrated to [server]; see ~/.firekeep/config.
To point this machine at a different server: firekeep join <code>, or set
FIREKEEP_CONFIG=<path> to use a separate config file.
```

An unrecognised subcommand printing bare argparse usage would tell a user their
muscle memory is wrong without telling them what replaced it.

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

---

## 4. Migration

Runs once, at config load, before any other read. It is one-way.

1. If `[server]` exists → nothing to do.
2. If `[active]` exists, resolve the named section. Missing section → **fail loudly**
   with the section name; never silently pick one.
3. Copy that section's keys into `[server]`, lift `agent_id` into `[identity]`,
   preserve `[dist]` untouched.
4. Back up the original to `~/.firekeep/config.bak-profiles-<UTC>` before writing, and
   print the path.
5. **If the other profile was also configured** (its values differ from the shipped
   skeleton), print a warning naming it, the backup path, and the exact recipe to keep
   it:

   ```
   note: [office] was also configured and is NOT carried over. Its settings are in
   ~/.firekeep/config.bak-profiles-20260730T2214Z. To keep using it:
     cp <backup> ~/.firekeep/office.conf   # then edit it to the [server] shape
     FIREKEEP_CONFIG=~/.firekeep/office.conf firekeep doctor
   ```

   Dropping a working connection silently is the one unacceptable outcome here.
6. `[pins]` is discarded with a line naming each pin that existed, since a pinned
   runtime's behaviour changes.

Stale `…@{profile}`-qualified keys in the platform cache dir are left to expire. They
are cache entries with a TTL, not state, and rewriting them would be more risk than
orphaning them.

---

## 5. Consequences elsewhere

- **Adapters re-render** without `FIREKEEP_PROFILE`. Existing rendered entries carrying
  it keep working (the env var is simply ignored once `active_profile` is gone), so
  there is no flag-day; `firekeep install` cleans them up on the next render.
- **Session stash and presence keys** lose their `@{profile}` qualifier
  (`state.py:60-71`), becoming `session_current_{agent}` and
  `presence_registered_{agent}`. This does not change the known concurrency
  limitation — the stash is one slot per identity per machine either way, and the
  supported partition for genuinely concurrent work remains a distinct
  `FIREKEEP_AGENT_ID`.
- **`firekeep doctor`** loses its pin rows and gains nothing; its health, versions,
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
- `test_config_migration.py` — `[active]=personal` migrates and backs up; `[active]`
  naming a missing section fails loudly; a **configured** second profile produces the
  warning with the backup path (guard against silent data loss); `[dist]` survives
  byte-for-byte; migration is idempotent and a second run is a no-op.
- `test_profile_stub.py` — `firekeep profile use x` exits 2 and names both `join` and
  `FIREKEEP_CONFIG`.
- `test_adapters_no_profile_env.py` — no rendered MCP entry contains
  `FIREKEEP_PROFILE`, across all four adapters.
- `test_bypass_unchanged.py` — **guard**: `resolver.is_bypassed()`, the marker path, the
  TTL backstop and `FIREKEEP_BYPASS` behave identically after the collapse. Dormancy
  shares a word with the deleted section and must not be collateral damage.
- Existing suites: ~738 test references to `profile` across 46 files need mechanical
  updating. Most are fixtures constructing a two-profile config; a shared
  `single_connection_config()` fixture replaces them.

---

## 7. Scope

~1,300 references across the client and its tests, ~198 in docs, and essentially
nothing server-side. Large but mechanical, and contained in one package.

It deletes a class of bug rather than instances: "key in the wrong section", "pin names
a profile that does not exist", "`FIREKEEP_PROFILE` silently overrode `[active]`", and
"the profile everyone uses is the one with no validation" all stop being expressible.

`CLAUDE.md`'s Local setup section, `docs/MULTI-AGENT.md`, and both bootstrap scripts
must be updated in the same change — a stale `firekeep profile use` in the docs after
the command is gone is the documentation-drift failure the repo's own Change
Consistency Checklist exists to prevent.
