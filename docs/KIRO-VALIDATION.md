# kiro-cli Adapter Validation

Empirical validation of the `firekeep_client.adapters.kiro` adapter against a real kiro-cli.
Prior to this, the adapter's assumptions were unverified ("kiro-cli has never been run against
this file"). This document records what was actually observed and the resulting corrections.

- **Tool:** kiro-cli **2.12.1** (`kiro-cli-chat 2.12.1`), Windows.
- **Binary:** `%LOCALAPPDATA%\Kiro-Cli\kiro-cli.exe`
- **Method:** `kiro-cli agent validate --path`, `kiro-cli agent create`, and scripted
  `kiro-cli chat --agent <a> --no-interactive [--trust-all-tools]` turns against a throwaway
  `firekeepval` agent whose preToolUse hook wrote a marker + chosen exit code, plus the official
  docs (kiro.dev/docs/cli/hooks, .../custom-agents/configuration-reference).

## Per-assumption verdict

| # | Assumption | Verdict | Evidence |
|---|------------|---------|----------|
| 1 | File `~/.kiro/agents/firekeep.json`; `mcpServers` + `hooks` top-level keys | **CONFIRMED** | `agent validate --path firekeep.json` → exit 0; `agent list` shows `firekeep` as a Global agent. |
| 2 | Events `agentSpawn`/`userPromptSubmit`/`preToolUse`/`postToolUse`/`stop` | **CONFIRMED** | Docs + `agent create` template; all five are valid hook events. |
| 3 | `matcher` value | **WRONG → FIXED** | matcher is an EXACT tool-name/alias match, **not a regex**. `.*` and `Edit\|Write` fired on nothing; `write`/`fs_write`/no-matcher fired. kiro's file tool is **`fs_write`** (alias `write`). Adapter's `Edit\|Write`/`Edit\|Write\|MultiEdit` → **the hook never fired at all.** Fixed to `fs_write`. |
| 4 | Blocking semantics | **PARTIAL → documented gap** | Docs: preToolUse exit 0=allow, 2=block, other=warn+allow (same as Claude). EMPIRICAL (2.12.1): the hook fires and kiro waits for it, but exit 2 does **not** veto the tool — kiro prints `✗ 0 of 1 hooks finished` and proceeds. Blocking is not enforced in 2.12.1. |
| 5 | Per-entry non-clobbering merge; flat `hooks: {event: [{command, matcher?}]}` | **CONFIRMED** | Schema matches; `upsert_flat_hook` keys on the firekeep marker in the command. |
| 6 | `"env": {...}` on an `mcpServers` entry is accepted AND passed to the spawned process | **CONFIRMED (2026-07-13, kiro-cli 2.12.1)** | `agent validate` rc=0 with env dicts on every entry. Live spawn probe: a throwaway agent's server command wrapped in a script dumping `$FIREKEEP_PROFILE` before exec'ing the real shim; `kiro-cli chat --agent firekeepprobe --no-interactive` spawned it and the dump read `personal` — kiro passes the env dict through. The shim `--profile` args fallback stays implemented but is not needed on 2.12.1. |

| 7 | `stop` fires once per SESSION (implied by the "1:1 with Claude" mapping) | **PER TURN — same as Claude** | Probe 2026-07-28, kiro-cli 2.12.1: throwaway `fkprobe` agent logging every hook invocation; three prompts piped into ONE interactive `kiro-cli chat` session. Counts: `agentSpawn` **1**, `userPromptSubmit` **3**, `stop` **3**. So kiro carried the identical turn-1 presence bug as Claude, and it is fixed by the same change (the deregister leaving the shared `stop` core). |
| 8 | Hook stdin payload carries a per-session id | **NO — no id of any kind** | Same probe. Payload keys are exactly `cwd`, `hook_event_name`, and `prompt` (agentSpawn/userPromptSubmit) or `assistant_response` (stop). Zero keys matching `*session*` or `*id`. Note kiro *does* have sessions at the CLI surface (`chat --resume-id <SESSION_ID>`, `--list-sessions`) — it simply does not pass the id to hooks. |

## Consequence: kiro cannot deregister presence, by any mechanism

Rows 7 and 8 together close the question the `session_end` work left open:

- kiro has **no session-end event**. Its five events are `agentSpawn`, `userPromptSubmit`,
  `preToolUse`, `postToolUse`, `stop` — and `stop` is per-turn (row 7). There is nothing to
  hang a deregister on.
- kiro passes **no session id** (row 8), so the per-session scratch-marker pattern that would
  otherwise let a per-turn event fire once per session cannot be keyed.

Therefore kiro is deliberately **not** wired to the `session_end` core, and this is the final
answer rather than an interim one. The consequence is bounded and benign: presence is one key
per `agent_id` with idempotent overwrite (`relay/app/presence.py:27-29`), so kiro leaves at most
**one** idle record per agent, reclaimed by that agent's next `agentSpawn` and filtered out of
`who_is_online` by computed status. That is strictly better than the previous behaviour, where
presence was deleted at the end of turn 1 and could never return.

## The two bugs found

1. **Matcher never matched (gate silently dead).** The adapter rendered Claude's tool names
   (`Edit|Write`). kiro matchers are exact kiro tool names, so the preToolUse hook **never
   fired** — the agent-gateway before-call did not run on kiro edits at all. Fix: matcher
   `fs_write` (kiro's create/edit tool; the stdin event confirms `"tool_name":"fs_write"`).
2. **Missing `--block-exit 2` remap.** kiro's documented contract blocks only on exit 2;
   `pre_tool.run()` returns 1 for a gateway block. Without the remap a block (rc=1) is
   "warn+allow". Fix: render `--block-exit 2` on the pre_tool command, mirroring the claude
   adapter.

## Empirical blocking caveat (kiro-cli 2.12.1)

With the correct matcher, the preToolUse hook fires and kiro **synchronously waits** for it
(so the before-call runs and records), but a hook exit of 2 does **not** prevent the tool:

```
✗ 0 of 1 hooks finished in 0.24 s
Creating: C:\Users\mogan\kiro_probe5\blocked2.txt   ← written despite exit 2 + stderr
```

Observed under `--no-interactive --trust-all-tools`. Without `--trust-all-tools`, kiro
auto-denies *all* tools in non-interactive mode (a trust decision, not the hook), which
confounds a clean blocking test — so the clean signal is the trust-all run above.

**Conclusion:** on 2.12.1 the firekeep pre-edit hook on kiro is **advisory** — it fires and the
agent-gateway before-call runs, but it is not a hard block. `contract.matrix` rates it
`advisory (fires, non-blocking on 2.12.1)` (not `guaranteed`, as Claude is). The
`--block-exit 2` remap is rendered anyway so the gate becomes real the moment kiro honors its
own documented exit-2 contract.

## Legacy migration

kiro's `render()` (`adapters/kiro.py`, `_migrate_legacy`) migrates pre-kit artifacts on
every render, mirroring the claude adapter's `LEGACY_HOOK_MARKERS` precedent: it drops every
firekeep-owned entry (exact key match or `<key>_`-prefixed, e.g. `firekeep-cortex_DISABLED`) from
`~/.kiro/settings/mcp.json`, and flips `~/.kiro/settings/cli.json`'s `chat.defaultAgent`
to `"firekeep"` when and only when it names the pre-kit agent. Best-effort (a
missing/malformed file never fails `render()` or the install) and one-way. The
legacy-artifact **archival step was removed**: `_migrate_legacy` used to move the pre-kit
agent file and env file aside to `.bak`, but after the product rename that path collided
with the adapter's *own* output (`~/.kiro/agents/firekeep.json`), so every render archived
the user's live config — it was removed rather than re-pointed (see the long comment at
the former site in `adapters/kiro.py`). Live-validated on the first migrated machine
(2026-07-13, as the code was then): the mcp.json sweep removed 5 legacy entries (including
a `_DISABLED` one carrying a plaintext credential), both pre-kit artifacts archived, and
the sweep surfaced the `chat.defaultAgent` follow-up — the value pointed at the archived
pre-kit agent, so kiro errored "user defined default ... not found" on every chat start.
The env-dict validation is row 6 in the table above — CONFIRMED via live spawn probe.

## Re-validation checklist (run on a kiro-cli upgrade)

1. `python client/scripts/validate_kiro.py` — asserts `agent validate --path` on the rendered
   firekeep.json passes and prints `mcp list` + the rendered servers.
2. Blocking probe (manual, ~1 min): point a throwaway agent's `preToolUse` hook (matcher
   `fs_write`) at a script that appends to a marker and `sys.exit(2)`; run
   `kiro-cli chat --agent <a> --no-interactive --trust-all-tools "create a file X with hi"`;
   check whether X was created. If it is **not** created, kiro now enforces the block — flip
   `contract.matrix` kiro `pre_edit_block` to `guaranteed` and update this doc.
