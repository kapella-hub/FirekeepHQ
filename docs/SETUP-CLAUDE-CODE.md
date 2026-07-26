# Setting Up Claude Code with Firekeep

## Prerequisites

- Claude Code installed and authenticated
- Firekeep deployed on VPS (run `bash install.sh` first)
- Your VPS IP address
- Python 3.10+ installed locally (the installer creates its own `~/.firekeep/venv`)

## Automated Setup (Recommended)

Unpack the `firekeep-client` tarball and run the installer from wherever you extracted it:

```bash
# Linux / macOS
./install            # or: firekeep install --runtime claude

# Windows (PowerShell)
.\install.ps1        # or: firekeep install --runtime claude
```

This is non-interactive — no prompts. It:

1. **Creates `~/.firekeep/venv`** and pip-installs `firekeep-client` (pulling `mcp`+`httpx`)
2. **Bootstraps `~/.firekeep/`** — config skeleton (`0600`), hook-core files, contract fragment, CA slot
3. **Writes `~/.claude.json`** — all 4 HTTP-backed MCP servers as stdio entries through `firekeep-shim` (user-scoped), plus the always-on stdio-local `firekeep-symdex` and `firekeep-decision`
4. **Writes `~/.claude/settings.json`** — 5 hook cores + env vars (user-scoped), merging non-destructively with any existing foreign hooks/servers

Afterward, edit `~/.firekeep/config`: set `agent_id` (it ships as the placeholder `CHANGEME`) and, for the office profile, `api_key`/`base_url`/`ca_path`. Then run `firekeep doctor` to verify. Profile changes (`firekeep profile use personal|office`) take effect on the next Claude Code session start. Per-runtime pins: `firekeep profile pin kiro office` points kiro at the office profile while other runtimes follow `[active]`.

The Firekeep Claude integration is user-scoped and venv-relocation-proof: the hook commands are absolute paths into `~/.firekeep/venv`, not into this repository.

## What Gets Configured

### MCP Servers (`~/.claude.json`)

| Server | Transport | Purpose |
|--------|-----------|---------|
| firekeep-cortex | stdio (`firekeep-shim --service cortex`) | Long-term memory (semantic + graph RAG) |
| firekeep-bridge | stdio (`firekeep-shim --service bridge`) | Session context persistence |
| firekeep-sentinel | stdio (`firekeep-shim --service sentinel`) | Environment observer |
| firekeep-relay | stdio (`firekeep-shim --service relay`) | Agent coordination |
| firekeep-symdex | stdio (local, always-on) | Code intelligence |
| firekeep-decision | stdio (local, always-on) | Decision Board — clarification via `decision_board`/`decision_board_check` |

Every HTTP service is reached through `firekeep-shim` — a stdio↔Streamable-HTTP bridge that terminates TLS and injects `X-API-Key`/`X-Agent-Id` from the active `~/.firekeep/config` profile. `firekeep-symdex` and `firekeep-decision` are never routed through the shim; they stay stdio-local. Both are always installed — no flag needed.

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

2. Run `/mcp` to confirm all 6 servers show connected (`firekeep-symdex` is always installed)

3. Open `http://<VPS_IP>:8040` to verify the dashboard loads

## Multi-Agent Setup (Optional)

To run named agents for cross-session coordination:

```bash
FIREKEEP_AGENT_ID=agent-alpha claude
# In another terminal:
FIREKEEP_AGENT_ID=agent-beta claude
```

`FIREKEEP_AGENT_ID` overrides the active profile's `agent_id` for that process only — useful for running differently-identified agents from one machine without editing `~/.firekeep/config`.

See [docs/MULTI-AGENT.md](MULTI-AGENT.md) for the full workflow guide.

## Manual Setup

The installer is the supported path; there is no manual alternative that reaches the same result, because the MCP entries and hook commands must point at **absolute** paths inside `~/.firekeep/venv` (a bare `firekeep-shim` is not on Claude Code's `PATH`). If you want to see what gets rendered without guessing paths, run `firekeep install --runtime claude` and inspect `~/.claude.json` / `~/.claude/settings.json` afterward — entries look like:

```json
{
  "mcpServers": {
    "firekeep-cortex": {"type": "stdio", "command": "/absolute/path/to/.firekeep/venv/bin/firekeep-shim", "args": ["--service", "cortex"]},
    "firekeep-bridge": {"type": "stdio", "command": "/absolute/path/to/.firekeep/venv/bin/firekeep-shim", "args": ["--service", "bridge"]},
    "firekeep-sentinel": {"type": "stdio", "command": "/absolute/path/to/.firekeep/venv/bin/firekeep-shim", "args": ["--service", "sentinel"]},
    "firekeep-relay": {"type": "stdio", "command": "/absolute/path/to/.firekeep/venv/bin/firekeep-shim", "args": ["--service", "relay"]},
    "firekeep-symdex": {"type": "stdio", "command": "/absolute/path/to/.firekeep/venv/bin/firekeep-symdex", "args": []},
    "firekeep-decision": {"type": "stdio", "command": "/absolute/path/to/.firekeep/venv/bin/firekeep-decision", "args": []}
  }
}
```

(On Windows the paths point at `.firekeep\venv\Scripts\firekeep-shim.exe` etc.) Re-running `firekeep install --runtime claude` is idempotent and non-clobbering — it merges only Firekeep-owned keys, so any other MCP servers or hooks you've added by hand survive.

## Troubleshooting

### Dashboard shows everything offline
- Check CORS: `CORS_ORIGINS` in `.env` must include `http://<VPS_IP>:8040`
- Clear browser localStorage: `localStorage.removeItem('firekeep_config')`

### Claude doesn't use the MCP tools
- Run `/mcp` — are servers connected?
- Check VPS: `docker compose ps` should show all services healthy
- Check firewall: ports 8050-8100 must be accessible from your machine

### Briefing shows "Service unreachable"
- The host/base_url in your active `~/.firekeep/config` profile must be reachable from your machine
- Run `firekeep doctor` — it checks connectivity, auth, and version-skew for every service in one pass
- Try: `curl http://YOUR_VPS_IP:8100/health`

### Hooks not firing
- Hooks are user-scoped — check `~/.claude/settings.json` exists and has the `hooks` key
- Run `firekeep doctor` — it verifies the rendered `firekeep-shim`/hook-core paths in `~/.firekeep/venv` exist and are executable
- If you moved or reinstalled `~/.firekeep`, rerun `firekeep install` to refresh the absolute venv script paths in your native config
