# Setting Up Codex with Firekeep

## What Codex Needs

Codex does not use the Claude-specific hook wiring; the client kit's Codex adapter renders one `.codex/config.toml` MCP gateway entry plus AGENTS.md guidance. Session-lifecycle automation (presence, heartbeat, snapshots) is not wired up for Codex at all today — `firekeep-sidecar` is the intended mechanism for MCP-only runtimes like Codex, but nothing starts it automatically; you have no presence path unless you run it by hand. Workspace/member attribution comes from the verified credential; only the runtime `agent_id` label is self-reported.

For Firekeep, the Codex path is:

1. `AGENTS.md` in the repo root for project instructions
2. One MCP gateway entry in Codex config so Codex can use every Firekeep backend

OpenAI documents both behaviors:

- Codex reads repository `AGENTS.md` files automatically: https://developers.openai.com/codex/guides/agents-md
- Codex MCP servers are configured in `~/.codex/config.toml` or project-scoped `.codex/config.toml`: https://developers.openai.com/codex/mcp

## Prerequisites

- Codex CLI or Codex IDE extension installed and authenticated
- Firekeep already deployed on a VPS
- A single-use install command from Dashboard → Devices (or
  `deploy/firekeep-admin invite --json` on the server)

## Recommended: Join once, render every adapter

Paste the complete install command issued for this device. It installs the kit,
redeems the code without prompts, and renders Claude Code, Codex, Kiro, and
OpenCode together. If the kit is already present, run:

```bash
firekeep join fk_join_...
```

This writes `~/.firekeep/config` and renders `.codex/config.toml` with one local
stdio gateway. The gateway connects to the four Streamable-HTTP services with
TLS/auth from `[server]`, and fronts local Symdex and Decision Board processes.
Use `firekeep install --runtime codex` only to repair or re-render the Codex
adapter afterward.

If you prefer to configure it manually (or want to see what the installer renders), the entries look like:

```toml
[mcp_servers.firekeep]
command = '/absolute/path/to/.firekeep/venv/bin/firekeep'
args = ["gateway"]
```

(On Windows, the command points at `.firekeep\venv\Scripts\firekeep.exe`.)

Notes:

- `.codex` is ignored by git in this repo because it is machine-local config.
- If `.codex` already exists as a file on your machine, remove or rename it first, then create the directory.
- Re-running `firekeep install --runtime codex` is idempotent and non-clobbering — it merges only Firekeep-owned keys, so any other `[mcp_servers.*]` entries you've added by hand survive.

## Alternative: User-Scoped MCP Config

If you want Firekeep available in every repo, add the same entries to `~/.codex/config.toml` instead.

## Verify

From the repo root:

```bash
codex mcp list
```

You should see one entry: `firekeep`.

Then start Codex in this repository and check:

- `AGENTS.md` is being applied for repository guidance
- `/mcp` shows the Firekeep servers as available

## What Works Today

With the MCP config above, Codex can use:

- Cortex for memory, replay, patterns, vault, and corpus tools
- Bridge for session persistence
- Sentinel for environment awareness
- Relay for coordination
- Symdex for code intelligence (stdio-local, always installed)
- Decision Board (`firekeep-decision`, stdio-local, always installed) for structured human clarification — see below

## Decision Board (`firekeep-decision`)

> When a clarification needs more than a couple of questions, call `decision_board(context, draft_questions)` instead of asking the questions inline.

`firekeep-decision` is a local backend behind the gateway, like Symdex; both are always installed. Two tools:

- `decision_board(context, draft_questions=[])` — asks Cortex to synthesize a board (retrieved evidence + suggested answers per question), opens it in the browser, and waits for the human's answers. Returns the answers (markdown) if submitted in time, else `{status: "pending", board_id, next}`.
- `decision_board_check(board_id)` — call with the `board_id` from a pending response to collect the answers once submitted; `{status: "pending", ...}` if still waiting, `{status: "unknown"}` if the id isn't recognized.

Codex has no hook surface (see below), so this trigger is doc-instruction only — there is no hook that injects it into the conversation. Codex has to know the convention (from this doc, or repo guidance) and decide to call `decision_board` on its own.

## Personal / Bypass Mode

Codex has no `/personal` slash command (that's Claude-only), but the bypass gate works the same way. Run `firekeep personal on` (or `toggle` / `off` / `status`) to make Firekeep dormant for private work — the sidecar stops sending presence, the decision board suppresses itself, and (under `FIREKEEP_BYPASS=1` at launch) the MCP servers serve zero tools. `firekeep doctor` shows a WARN row while bypass is active. Since Codex is MCP-only with no session-end hook, the marker does **not** auto-clear on exit — turn it off with `firekeep personal off` (or rely on the 12h `FIREKEEP_PERSONAL_TTL_HOURS` backstop). For a whole personal session, launch with `FIREKEEP_BYPASS=1`.

## What Is Not Yet Wired for Codex

Codex has no hook surface, so the lifecycle automation other runtimes get via hooks isn't available here:

- Session start briefing (Claude/kiro get this as a hook; Codex agents call `memory_recall`/`GET /briefing` manually)
- Session debrief
- Multi-agent inbox polling
- Guaranteed pre-edit lease/policy blocking (self-reported only)

Presence/heartbeat/snapshot/exit lifecycle is *intended* to be owned by the `firekeep-sidecar` daemon for MCP-only runtimes like Codex rather than by hooks, but nothing spawns `firekeep-sidecar` automatically — you must start it yourself (`firekeep-sidecar`) to get a presence entry at all. Codex can still use the MCP tools directly; those automations are just not mirrored in a Codex hook/config flow.

## Troubleshooting

### Codex cannot see the servers

- Run `firekeep doctor` — it verifies the rendered `firekeep-shim` paths exist, checks connectivity and auth for `[server]`, reports client and cortex versions, and flags a lingering `CHANGEME` agent_id
- Run `codex mcp list` and confirm the `firekeep` entry exists
- Check that the `host` or `base_url` in `~/.firekeep/config` `[server]` is reachable from your machine
- Verify `docker compose ps` on the VPS shows services healthy
- Confirm ports `8050`, `8060`, `8070`, `8080`, and `8100` are reachable as intended (`8090` is not used; Symdex stays local behind the gateway)

### Codex starts without repo guidance

- Confirm you started Codex from the Firekeep repo root or a subdirectory of it
- Confirm `AGENTS.md` exists in the repo root

### Symdex tools fail in Codex

- Confirm you ran `firekeep install --runtime codex` (Symdex is always installed and stdio-local — it is not routed through `firekeep-shim` or exposed over HTTP)
- Run the `firekeep_gateway_status` MCP tool; it reports Symdex separately if that backend failed
