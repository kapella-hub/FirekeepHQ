# OpenCode Adapter Validation

Empirical validation of the `firekeep_client.adapters.opencode` adapter against a real
OpenCode. Prior to this, the adapter's assumptions were documented-but-unvalidated
(wired from https://opencode.ai/docs/plugins/ and /docs/mcp-servers/, never executed).
This document records what was actually observed and the resulting corrections —
the same standard as `docs/KIRO-VALIDATION.md`.

- **Tool:** opencode **1.14.22**, macOS (Apple Silicon).
- **Model under the agent:** `qwen/qwen3.6-35b-a3b` (MLX) served by LM Studio on
  `127.0.0.1:1234` — i.e. a fully local stack; the firekeep servers were the live VPS.
- **Method:** scripted `opencode run "<prompt>"` turns in throwaway project dirs
  containing a seeded `.env` (`SECRET_KEY=do-not-touch`), a `notes.txt`, and a
  project-local `opencode.jsonc` granting `permission: {edit/bash/read: allow}` so
  the ONLY gate in front of the write tool was the firekeep bridge. Observed: process
  stdout (`[firekeep]` lines), `~/.local/share/opencode/log/*.log` bus traces, Relay
  `/presence`, `~/.firekeep/logs/hooks.log`, and on-disk file state after each run.

## Per-assumption verdict

| # | Assumption | Verdict | Evidence |
|---|------------|---------|----------|
| 1 | Global config `~/.config/opencode/opencode.json`, `mcp` key, `{type: "local", command: [...], environment}` entries | **CONFIRMED** | opencode loaded the config with the six `firekeep-*` entries merged alongside a pre-existing foreign `provider`/`model`/`permission` config (byte-identical after render); sessions ran normally. |
| 2 | Global plugins auto-load from `~/.config/opencode/plugins/*.js` | **CONFIRMED** | `[firekeep]` output appeared in `opencode run` stdout with no `plugin` config key — the file was picked up by location alone. |
| 3 | `tool.execute.before` throw blocks the tool call | **CONFIRMED — hard gate** | Write to `.env` aborted: `✗ write failed` / `Error: [firekeep] [firekeep pre_tool] block: [path_deny] File matches deny pattern '.env'…`; file untouched across every attempt. This is a REAL block (contrast kiro 2.12.1, where exit 2 is advisory). |
| 4 | `input.tool` is the lowercase opencode name; `output.args.filePath` | **CONFIRMED** | The `edit`/`write`→`Edit`/`Write` + `filePath`→`file_path` translation produced correct pre_tool decisions (block on `.env`, allow on `notes.txt`, which was written successfully). |
| 5 | `session.created` → session_start | **WRONG in `run` mode → FIXED** | The bus log shows `type=session.created publishing` with no subscriber yet — in `opencode run`, the session is created BEFORE plugins subscribe, so a created-only wiring never fires headless. Fix: the bridge fires `session_start` from the FIRST hook of any kind it sees (`ensureStarted`), once. After the fix the full PRE-FLIGHT BRIEFING printed in run mode. TUI sessions don't have this race (plugins load at app start), and `session.created` remains handled for them. |
| 6 | `session.idle` → prompt core (heartbeat + inbox) | **CONFIRMED** | `[firekeep] === RELAY INBOX (Alex) ===` printed at turn end on every run. |
| 7 | `session.deleted` → stop core | **CONFIRMED** | Bus log: `type=session.deleted unsubscribing` at run end; Relay `/presence` showed the agent deregistered after the run (the stop core's race-guarded deregister). |
| 8 | Headless permission behavior | **Documented caveat** | With the user's global `permission: {edit: "ask"}`, `opencode run` AUTO-REJECTS the ask before our hook is reached ("permission requested … auto-rejecting"). The firekeep gate is only exercised where opencode's own permission layer allows the tool. Also observed: writes addressed under `/tmp/...` (a symlink to `/private/tmp`) trip opencode's `external_directory` permission — unrelated to firekeep. |

## The bug found

**session_start never fired in `opencode run` mode (silent).** `session.created` is
published before plugin subscription, so the briefing/presence-register core was
skipped entirely in headless runs — no error anywhere, because nothing was invoked.
Fixed in the rendered bridge with a `started` latch: the first `event`/`tool.execute.*`
hook to arrive runs `session_start` exactly once, with `session.created` still the
preferred (and TUI-correct) trigger.

## Notes

- Briefing/inbox text lands in opencode's **console log**, not model context —
  opencode has no systemMessage channel. The model does not see the briefing.
- `stop` runs on `session.deleted`, not every turn end (deviation from Claude's
  per-turn Stop, documented in the adapter docstring); a hard quit is covered by
  the briefing's crash detection, same as a Claude crash.
- No new entries in `~/.firekeep/logs/hooks.log` during any validation run — every
  dispatcher invocation completed cleanly.
