# Setting Up Codex with Firekeep

## What Codex Needs

Codex does not use the Claude-specific hook wiring; the client kit's Codex adapter renders one user-scoped `~/.codex/config.toml` MCP gateway entry plus Firekeep guidance in `~/.codex/AGENTS.md`. Session-lifecycle automation (presence, heartbeat, snapshots) is not wired up for Codex at all today — `firekeep-sidecar` is the intended mechanism for MCP-only runtimes like Codex, but nothing starts it automatically; you have no presence path unless you run it by hand. Workspace/member attribution comes from the verified credential; only the runtime `agent_id` label is self-reported.

For Firekeep, the Codex path is:

1. `AGENTS.md` in the repo root for project instructions
2. A Firekeep-owned block in `~/.codex/AGENTS.md` for user-scoped tool-use guidance
3. One MCP gateway entry in `~/.codex/config.toml` so Codex can use every Firekeep backend

OpenAI documents both behaviors:

- Codex reads repository `AGENTS.md` files automatically: https://developers.openai.com/codex/guides/agents-md
- Codex MCP servers are configured in `~/.codex/config.toml` or project-scoped `.codex/config.toml`: https://developers.openai.com/codex/mcp

## Prerequisites

- Codex CLI or Codex IDE extension installed and authenticated
- Nothing else. No Firekeep server yet? The installer offers to set one up on
  this machine (that path needs Docker).

## Recommended: install once, render every adapter

The install guide is [firekeep.ai/docs.html](https://firekeep.ai/docs.html) — the
single source. The one command:

```bash
curl -fsSL https://firekeep.ai/latest/install | sh    # macOS / Linux
irm https://firekeep.ai/latest/install.ps1 | iex         # Windows
```

It asks your agent identity, then where your Firekeep server is (set one up here
/ join code / already running / not yet), and renders Claude Code, Codex, Kiro
and OpenCode together. A single-use install command from Dashboard → Devices
(or `deploy/firekeep-admin invite --json` on the server) can be pasted instead —
it carries the join code into the same bootstrap and redeems it without prompts.
If the kit is already present, run:

```bash
firekeep join fk_join_...
```

This writes `~/.firekeep/config`, renders `~/.codex/config.toml` with one local
stdio gateway, and upserts a Firekeep-owned guidance block in
`~/.codex/AGENTS.md`. The gateway connects to the four Streamable-HTTP services
with TLS/auth from `[server]`, and fronts local Symdex and Decision Board
processes. Use `firekeep install --runtime codex` only to repair or re-render the
Codex adapter afterward.

If you prefer to configure it manually (or want to see what the installer renders), the entries look like:

```toml
[mcp_servers.firekeep]
command = '/absolute/path/to/.firekeep/current/bin/firekeep'
args = ["gateway"]
```

(On Windows, the command points at `.firekeep\current\Scripts\firekeep.exe`. The
`current` link selects the installed version's venv under `venvs/<version>` —
never hand-write a versioned path; updates garbage-collect old venvs.)

Notes:

- If `.codex` already exists as a file on your machine, remove or rename it first, then create the directory.
- Re-running `firekeep install --runtime codex` is idempotent and non-clobbering — it merges only Firekeep-owned keys, so any other `[mcp_servers.*]` entries you've added by hand survive.
- The same non-clobbering rule applies to `~/.codex/AGENTS.md`: only the Firekeep-owned marker block is replaced.

## Alternative: Project-Scoped MCP Config

The installer deliberately uses Codex's user-scoped config so Firekeep is available in every repository. If you need a project-only manual setup instead, put the same gateway entry in that repository's `.codex/config.toml`; the installer does not manage that file.

## Verify

From any repository:

```bash
codex mcp list
```

You should see one entry: `firekeep`.

Then restart Codex and check:

- `~/.codex/AGENTS.md` contains the Firekeep-owned instruction block
- the repository's `AGENTS.md` is being applied for project guidance, when present
- `/mcp` shows the `firekeep` gateway as available

## What Works Today

With the MCP config above, Codex can use:

- Cortex for memory, replay, patterns, vault, and corpus tools
- Bridge for session persistence
- Sentinel for environment awareness
- Relay for coordination
- Symdex for code intelligence (stdio-local; the wheel is always installed, and it is mounted when registered as a dex — `firekeep dex add symdex`, see [guides/dexes.md](guides/dexes.md))
- Decision Board (`firekeep-decision`, stdio-local, always installed — it is core infrastructure, not a dex) for structured human clarification — see below

Docdex (documents) is available here too, but note the coverage: Codex has no
hook surface, so **nothing syncs automatically** — run `firekeep docdex sync`
yourself after `firekeep docdex add <folder>`. See [guides/dexes.md](guides/dexes.md).

## Decision Board (`firekeep-decision`)

> When a clarification needs more than a couple of questions, call `decision_board(context, draft_questions)` instead of asking the questions inline.

`firekeep-decision` is a local backend behind the gateway, like Symdex. Both are always installed; Decision is core and always mounted, while Symdex mounts only when registered as a dex (which it is by default). Two tools:

- `decision_board(context, draft_questions=[])` — asks Cortex to synthesize a board (retrieved evidence + suggested answers per question) and waits for the human's answers. Firekeep Studio renders it natively; standalone Codex uses the local browser. Returns answers (markdown) if submitted in time, else `{status: "pending", board_id, next}`.
- `decision_board_check(board_id)` — call with the `board_id` from a pending response to collect the answers once submitted; `{status: "pending", ...}` if still waiting, `{status: "unknown"}` if the id isn't recognized.

The installer writes this trigger into `~/.codex/AGENTS.md`, and the gateway also returns a compact version in its MCP `initialize` response. Codex still has no deterministic hook that can force the call, so launching the board remains model-directed rather than guaranteed.

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

- Run `firekeep doctor` — it checks the gateway, Decision Board and Symdex executables; validates the exact Codex MCP and instruction blocks as `codex-mcp` / `codex-instructions`; checks connectivity and auth for `[server]`; reports client and cortex versions; and flags a lingering `CHANGEME` agent_id
- Run `codex mcp list` and confirm the `firekeep` entry exists
- Check that the `host` or `base_url` in `~/.firekeep/config` `[server]` is reachable from your machine
- Verify `docker compose ps` on the VPS shows services healthy
- Confirm ports `8050`, `8060`, `8070`, `8080`, and `8100` are reachable as intended (`8090` is not used; Symdex stays local behind the gateway)

### Codex does not use Firekeep proactively

- Run `firekeep doctor` and check the `codex-mcp` and `codex-instructions` rows
- Confirm `~/.codex/AGENTS.md` contains the Firekeep-owned instruction block
- Re-run `firekeep install --runtime codex`, then restart Codex so it reloads both MCP registration and guidance
- Repository-specific guidance is separate: confirm that repository has its own `AGENTS.md` when the task depends on project rules

### Symdex tools fail in Codex

- Confirm `firekeep dex list` shows `symdex [registered]` — the wheel is always installed, but the gateway mounts it only when it is registered (`firekeep dex add symdex`, effective on the next session)
- Confirm you ran `firekeep install --runtime codex` (Symdex is stdio-local — it is not routed through `firekeep-shim` or exposed over HTTP)
- Run the `firekeep_gateway_status` MCP tool; it reports Symdex separately if that backend failed
