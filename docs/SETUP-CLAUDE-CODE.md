# Setting Up Claude Code with Firekeep

## Prerequisites

- Claude Code installed and authenticated
- Firekeep deployed on VPS (run `bash install.sh` first)
- A single-use install command from Dashboard → Devices (or
  `deploy/firekeep-admin invite --json` on the server)

## Automated Setup (Recommended)

Paste the complete install command issued for this device. It carries the join
code into the bootstrap and asks no profile, host, API-key, identity, or runtime
questions. On an already-installed client, use `firekeep join <code>`.

For a checkout/development install without a join code:

```bash
# Linux / macOS
./install            # or: firekeep install --runtime claude

# Windows (PowerShell)
.\install.ps1        # or: firekeep install --runtime claude
```

With a join code, the customer path is fully non-interactive. It:

1. **Creates a versioned venv at `~/.firekeep/venvs/<version>`** (selected by the `~/.firekeep/current` link) and pip-installs `firekeep-client` (pulling `mcp`+`httpx`)
2. **Bootstraps `~/.firekeep/`** — config skeleton (`0600`), hook-core files, contract fragment, CA slot
3. **Writes `~/.claude.json`** — one user-scoped `firekeep` stdio gateway entry aggregating all four remote and two local backends
4. **Writes `~/.claude/settings.json`** — 5 hook cores + env vars (user-scoped), merging non-destructively with any existing foreign hooks/servers

Enrollment writes the one `[server]` connection and `[identity]`, then runs
`firekeep doctor`. The client-generated credential is stored at 0600 and is not
printed by default. Configuration takes effect on the next Claude Code session
start.

The Firekeep Claude integration is user-scoped and venv-relocation-proof: the hook commands are absolute paths through `~/.firekeep/current` (the link that survives every update), not into this repository.

## What Gets Configured

### MCP Servers (`~/.claude.json`)

| Server | Transport | Purpose |
|--------|-----------|---------|
| firekeep | stdio (`firekeep gateway`) | Memory, sessions, monitoring, coordination, code intelligence, and Decision Board |

The gateway starts parameterized shims for the four remote Streamable-HTTP
services, injecting TLS and auth from `[server]`, plus the local Symdex and
Decision Board processes. A failed backend removes only its tools; use
`firekeep_gateway_status` to see which one failed.

### Hooks (`.claude/settings.json`)

| Hook | Hook core | Purpose |
|------|-----------|---------|
| SessionStart | `firekeep_client.hooks.session_start` | Pre-flight briefing (thin fetch of Cortex `GET /briefing`) + presence registration |
| Stop | `firekeep_client.hooks.stop` | Guided session completion: final workspace snapshot, distill/tasks/lease reminders |
| UserPromptSubmit | `firekeep_client.hooks.prompt` | Polls Relay for new tasks and messages; periodic workspace snapshot |
| PreToolUse (Edit/Write) | `firekeep_client.hooks.pre_tool` | Checks file leases + agent-gateway pre-action gate (the only blocking hook) |
| PostToolUse | `firekeep_client.hooks.post_tool` | Agent-gateway reconcile |

Every hook is invoked via the dispatcher: `<venv>/python -m firekeep_client.hooks <core>`.

### Decision Board (`firekeep-decision`)

When a clarification needs more than a couple of questions, call `decision_board(context, draft_questions)` instead of asking the questions inline. It asks Cortex to synthesize a board (retrieved evidence + suggested answers per question), opens it in your browser, and waits for your answers.

- `decision_board(context, draft_questions=[])` — returns your answers once you submit, or `{status: "pending", board_id, next}` if you haven't answered within the poll window.
- `decision_board_check(board_id)` — call this with the `board_id` from a pending response to collect the answers once you've submitted them.

No terminal/headless environment? The board is printed as plain text to answer inline instead of opening a browser.

### Personal Mode (`/personal`)

Install also renders a `/personal` slash command (`~/.claude/commands/personal.md`). Type `/personal` in a session to toggle **personal mode** — Firekeep goes dormant (no briefing, memory, presence, or logging, and you shouldn't call `firekeep_*` tools). It takes effect live, auto-clears when the session ends, and shows a "⚠ PERSONAL MODE" banner while active. Toggle it off with `/personal` again, or run `firekeep personal off`. For a whole session that's personal from the start, launch Claude with `FIREKEEP_BYPASS=1` set.

## Verify

After setup, restart Claude Code in the project directory. You should see:

1. The pre-flight briefing on session start:
   ```
   === PRE-FLIGHT BRIEFING ===
   ENVIRONMENT: Events: 42. All collectors healthy.
   QUALITY: From 3 recent sessions: quality looks good
   ...
   === END BRIEFING ===
   ```

2. Run `/mcp` to confirm the single `firekeep` server is connected

3. Open the dashboard to verify it loads. A default install binds it to `127.0.0.1`, so
   from the machine running Firekeep that is `http://localhost:8040`; from anywhere else,
   tunnel first (`ssh -L 8040:127.0.0.1:8040 user@host`). `http://<VPS_IP>:8040` only
   resolves if you deliberately set `BIND_ADDR=0.0.0.0` — see
   [DEPLOYMENT.md](DEPLOYMENT.md#access-and-authentication).

## Multi-Agent Setup (Optional)

To run named agents for cross-session coordination:

```bash
FIREKEEP_AGENT_ID=agent-alpha claude
# In another terminal:
FIREKEEP_AGENT_ID=agent-beta claude
```

`FIREKEEP_AGENT_ID` overrides `[identity] agent_id` for that process only — useful for running differently-identified agents from one machine without editing `~/.firekeep/config`.

See [docs/MULTI-AGENT.md](MULTI-AGENT.md) for the full workflow guide.

## Manual Setup

The installer is the supported path; there is no manual alternative that reaches the same result, because the MCP entries and hook commands must point at **absolute** paths through `~/.firekeep/current` (a bare `firekeep-shim` is not on Claude Code's `PATH`, and a versioned `venvs/<X.Y.Z>` path would pin the config to a venv updates garbage-collect). If you want to see what gets rendered without guessing paths, run `firekeep install --runtime claude` and inspect `~/.claude.json` / `~/.claude/settings.json` afterward — entries look like:

```json
{
  "mcpServers": {
    "firekeep": {"type": "stdio", "command": "/absolute/path/to/.firekeep/current/bin/firekeep", "args": ["gateway"]}
  }
}
```

(On Windows the path points at `.firekeep\current\Scripts\firekeep.exe`.) Re-running `firekeep install --runtime claude` is idempotent and non-clobbering — it removes the six retired Firekeep entries, writes the one gateway entry, and leaves foreign MCP servers and hooks untouched.

## Troubleshooting

### Dashboard shows everything offline
- **Most likely: `DASHBOARD_API_KEY` is unset.** With `AUTH_ENABLED=true` (the default)
  nginx must inject that key on every `/api/*` proxy; it drops empty
  `proxy_set_header` values, so an unset key means every tab gets a 401 and renders as
  offline. Re-run `bash deploy/bootstrap-keys.sh`, then **recreate** the container
  (`docker compose up -d dashboard`) — the key is read at container start, not per request.
- Not CORS. The SPA is served by the same nginx that proxies `/api/*` and calls
  `window.location.origin`, so the browser never makes a cross-origin request and
  `CORS_ORIGINS` cannot cause this.
- Clear browser localStorage: `localStorage.removeItem('firekeep_config')`

### Claude doesn't use the MCP tools
- Run `/mcp` — is the `firekeep` gateway connected? If so, call `firekeep_gateway_status` to inspect individual backends.
- Check VPS: `docker compose ps` should show all services healthy
- Check firewall: ports 8050-8100 must be accessible from your machine

### Briefing shows "Service unreachable"
- The `host` or `base_url` in `~/.firekeep/config` `[server]` must be reachable from your machine
- Run `firekeep doctor` — it checks connectivity and auth for every service in one pass, reports the client and cortex versions, and flags a stale client against the release manifest
- Try `curl http://127.0.0.1:8100/health` **on the Firekeep host** — `/health` is pre-auth,
  so it answers without a key and tells you the service is up. From another machine that
  call only works if you set `BIND_ADDR=0.0.0.0`; on a default install, tunnel instead.
- A `401` from any other path is not a fault — it means the stack is up and enforcing auth.
  Put your key in `[server]` rather than turning auth off.

### Hooks not firing
- Hooks are user-scoped — check `~/.claude/settings.json` exists and has the `hooks` key
- Run `firekeep doctor` — it verifies the `current` link resolves to the installed version and the rendered `firekeep-shim`/hook-core paths behind it exist and are executable
- If you moved or reinstalled `~/.firekeep`, rerun `firekeep install` to refresh the absolute venv script paths in your native config
