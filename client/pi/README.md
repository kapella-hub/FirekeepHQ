# firekeep-pi

Firekeep hook bridge for the [Pi coding agent](https://pi.dev).

Gives Pi a pre-flight briefing **in the model's context window**, a
**hard-blocking** pre-edit gate, presence, and turn/session lifecycle — all
backed by your self-hosted [Firekeep](https://firekeep.ai) Keep.

## Install

With the Firekeep client kit (recommended — it also wires the interpreter path):

```
firekeep install --runtime pi
```

Standalone, for a Pi user who already has a Keep reachable:

```
pi install npm:firekeep-pi
```

The bridge shells out to `python -m firekeep_client.hooks <core>`. It finds the
interpreter in this order:

1. `FIREKEEP_PYTHON`
2. `~/.firekeep/pi-extension.json` (written by `firekeep install --runtime pi`)
3. `python3` / `python` on `PATH`

If none of those can import `firekeep_client`, every hook degrades to a no-op —
availability over enforcement. A broken bridge never breaks your session.

## What it wires

| Pi event | Firekeep core | Effect |
|---|---|---|
| `session_start` | `session_start` | briefing, presence register |
| `before_agent_start` | `prompt` | inbox poll, proactive recall, **briefing → `systemPrompt`** |
| `tool_call` | `pre_tool` | **blocks** on policy or a lease held by another agent |
| `tool_result` | `post_tool` | reconcile |
| `agent_settled` | `stop` | turn end |
| `session_before_compact` | `precompact` | *(wired, unvalidated)* |
| `session_shutdown` | `session_end` | presence deregister |

`edit`/`write` map to Claude-shaped `Edit`/`Write` with `path` → `file_path`;
`bash`/`powershell` map to `Bash`. Ungated tools are never forwarded, so they
cost no subprocess.

## Scope

This is the **hook** surface. Pi ships no MCP client, so an agent cannot call
`memory_recall` or `ctx_update` through this package — that needs one of the
community MCP extensions for Pi, which Firekeep neither ships nor controls.

## Validation

`node validation/run.mjs` runs a real Pi agent session against a stub dispatcher
and Pi's own faux provider — no credential, no network, no spend — and asserts
16 behaviours including that the briefing reaches the model and that a blocked
write never touches disk. See [`docs/PI-VALIDATION.md`](https://github.com/kapella-hub/FirekeepHQ/blob/main/docs/PI-VALIDATION.md).

## Licence

Source-available under BUSL-1.1; each release converts to Apache-2.0 four years
after publication. See [`LICENSE`](https://github.com/kapella-hub/FirekeepHQ/blob/main/LICENSE).
