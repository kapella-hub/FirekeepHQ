# Client Integrations

Firekeep is MCP-native. The client-specific setup varies, but the integration model is the same:

- connect the four Firekeep MCP HTTP endpoints
- keep repository-specific agent instructions in the repo (`AGENTS.md`, `CLAUDE.md`)
- use client-native hooks or startup flows only where they add value

## Supported Today

### Codex

- Project instructions: root `AGENTS.md`
- Setup: [docs/SETUP-CODEX.md](docs/SETUP-CODEX.md)
- Install: `firekeep install --runtime codex`

### Claude Code

- Project instructions: root `CLAUDE.md`
- Setup: [docs/SETUP-CLAUDE-CODE.md](docs/SETUP-CLAUDE-CODE.md)
- Install: `firekeep install --runtime claude` (or `./install`)

## Generic MCP Clients

For any MCP-capable client, register these HTTP endpoints:

> **Reachability and auth, before you register anything.** A default install binds these
> ports to `127.0.0.1` and requires an `X-API-Key` on every call. So from the Firekeep
> host use `127.0.0.1`; from another machine either tunnel
> (`ssh -L 8080:127.0.0.1:8080 …`) or set `BIND_ADDR=0.0.0.0` deliberately, and in both
> cases supply a key. The shipped kit gets one through Dashboard → Devices or
> `deploy/firekeep-admin invite`; `deploy/firekeep-admin keys create --agent
> <you>` is the manual fallback for a generic client that cannot run
> `firekeep join`. Substitute `<HOST>` below accordingly. See
> [DEPLOYMENT.md](DEPLOYMENT.md#access-and-authentication).

| Service | URL |
|---|---|
| FirekeepCortex | `http://<HOST>:8080/mcp` |
| FirekeepBridge | `http://<HOST>:8070/mcp` |
| FirekeepSentinel | `http://<HOST>:8060/mcp` |
| FirekeepRelay | `http://<HOST>:8050/mcp` |

Code intelligence (`firekeep-symdex`) and the Decision Board (`firekeep-decision`) are **not** HTTP endpoints — they are client-installed stdio-local MCP servers, registered automatically by `firekeep install`. There is no symdex server or port 8090.

Minimum useful combination:

- Cortex for memory
- Bridge for session continuity
- Relay for coordination

Full platform experience:

- add Sentinel for environment awareness
- code intelligence (`firekeep-symdex`) is already installed automatically as a client-stdio server — no HTTP endpoint to register

## Integration Tiers

### Tier 1: Plain MCP

Any client that can call MCP tools over HTTP can use Firekeep immediately.

### Tier 2: MCP + Repo Instructions

Recommended for coding agents. Keep client-specific guidance in repo-root files so setup stays project-scoped.

### Tier 3: MCP + Hooks

Best experience for clients that support:

- startup briefing
- pre-edit safety checks
- inbox polling
- completion/debrief flows

Today, Claude has the strongest hook integration. Codex now has repo-scoped setup and MCP connectivity, but hook parity is still a future improvement area.

## Recommended Next Integrations

- Cursor
- Aider
- OpenHands
- other MCP-capable terminal or IDE agents

The goal is to keep Firekeep client-agnostic at the service layer and thin at the integration layer.

## Agent Gateway: Predict-Then-Act

Any agent runtime can use the Agent Gateway. Setup varies by runtime.

### Claude Code
Already wired by `firekeep install` (the Claude adapter) — it installs both the PreToolUse (`firekeep_client.hooks.pre_tool`) and PostToolUse (`firekeep_client.hooks.post_tool`) hook cores. Adapter type is `shell-hook`; predictions are not blocking (advisory only) since hooks cannot extract agent reasoning.

For genuine predict-then-act reflection, call the MCP tools `action_before` / `action_after` explicitly from agent reasoning. These return structured advisories the agent can use to reflect and resubmit.

#### Upgrading a stale install
Re-run `firekeep install` for your runtime; it is idempotent and non-clobbering. Hook changes take effect on the next session start.

### Codex CLI
Codex is MCP-only (no hook surface), so use the client kit's stdio shim rather than raw HTTP — Codex's `~/.codex/config.toml` speaks stdio, not `type: "http"`. Run `firekeep install --runtime codex` (see [docs/SETUP-CODEX.md](docs/SETUP-CODEX.md)), which renders:
```toml
[mcp_servers.firekeep-cortex]
command = '/absolute/path/to/.firekeep/venv/bin/firekeep-shim'
args = ["--service", "cortex"]
```
(plus `firekeep-bridge`/`firekeep-sentinel`/`firekeep-relay`, each through the same `firekeep-shim` bridge, which injects TLS + auth headers from `[server]` and `[identity]` in `~/.firekeep/config`.) Then the `action_before` and `action_after` tools appear as `mcp__firekeep-cortex__action_before` etc. Restart Codex after installing.

### Kiro
Run `firekeep install --runtime kiro`, which renders `~/.kiro/agents/firekeep.json` with the Firekeep services as stdio commands through the same `firekeep-shim` bridge (kiro also gets inline hooks wired to the same lifecycle events Claude uses):
```json
{
  "mcpServers": {
    "firekeep-cortex": {
      "command": "/absolute/path/to/.firekeep/venv/bin/firekeep-shim",
      "args": ["--service", "cortex"]
    }
  }
}
```
Restart Kiro. Tools appear under the `firekeep-cortex` namespace.

### Cursor
Add to `~/.cursor/mcp.json`:
```json
{
  "mcpServers": {
    "firekeep-cortex": {
      "url": "http://<HOST>:8080/mcp"
    }
  }
}
```

### Custom Python / LangGraph

Two HTTP calls — no SDK required.

```python
import requests

CORTEX = "http://<HOST>:8100"   # 127.0.0.1 on a default install
# Required. AUTH_ENABLED defaults to true, so every call needs a key — mint one
# with `deploy/bootstrap-keys.sh` / `deploy/firekeep-admin keys create --agent
# <you>`. There is no single shared API_KEY env var; each caller gets its own
# scoped key. (Even with AUTH_ENABLED=false the admin-gated routes — vault,
# /auth/keys — refuse an anonymous caller, so disabling auth is not a way to
# skip this.)
FIREKEEP_KEY = "nxs_..."

def action_before(session_id, agent_id, action_type, target, prediction=None):
    payload = {
        "session_id": session_id,
        "agent_id": agent_id,
        "adapter": "rest",
        "action": {"type": action_type, "target": target},
    }
    if prediction:
        payload["prediction"] = prediction
    headers = {"Content-Type": "application/json"}
    if FIREKEEP_KEY:
        headers["X-API-Key"] = FIREKEEP_KEY
    return requests.post(f"{CORTEX}/agent/action/before", json=payload, headers=headers, timeout=5).json()

def action_after(action_id, success, **outcome_fields):
    payload = {
        "action_id": action_id,
        "outcome": {"success": success, **outcome_fields},
    }
    headers = {"Content-Type": "application/json"}
    if FIREKEEP_KEY:
        headers["X-API-Key"] = FIREKEEP_KEY
    return requests.post(f"{CORTEX}/agent/action/after", json=payload, headers=headers, timeout=5).json()

# Usage
decision = action_before(
    "s1", "a1", "edit_file", "src/foo.py",
    prediction={
        "intent": "add docstring",
        "expected_changes": ["src/foo.py"],
        "success_criteria": ["FILE_EXISTS:src/foo.py"],
        "confidence": 0.9,
    },
)
if decision["decision"] == "allow":
    # execute the action
    ...
    if not decision["auto_reconcile"]:
        action_after(
            decision["action_id"],
            success=True,
            actual_changes=["src/foo.py"],
            observed_criteria_met=["FILE_EXISTS:src/foo.py"],
        )
elif decision["decision"] == "rethink":
    # show suggested_questions from advisories to user or reflect, then resubmit
    for adv in decision["advisories"]:
        print(adv["message"])
        for q in adv.get("suggested_questions", []):
            print(f"  - {q}")
elif decision["decision"] == "block":
    raise RuntimeError(f"Action blocked: {decision['advisories'][0]['message']}")
```

### Decision Semantics

| Decision | Meaning | Agent obligation |
|---|---|---|
| `allow` | Proceed | Execute, then call `/action/after` (unless `auto_reconcile=true`) |
| `rethink` | Something doesn't add up | Don't execute. Reflect, optionally ask the user, resubmit with refined prediction |
| `block` | Hard policy violation | Surface to user; cannot proceed |

After 3 consecutive `rethink` verdicts on the same target, the gateway escalates to `block` with a `rethink_limit` advisory.

### Prediction Schema

```python
prediction = {
    "intent": "one-line: what this action accomplishes",       # max 512 chars
    "expected_changes": ["file/path", "directory/"],            # list of paths
    "success_criteria": ["TESTS_PASS", "BUILD_OK"],             # enum-style codes
    "confidence": 0.85,                                         # 0.0 to 1.0
}
```

Prediction is **optional** but required on elevated-risk actions (full tier). MCP and REST callers without a prediction on full tier receive `rethink: prediction_required`. Shell-hook callers are exempt (advisory recorded but not blocking).
