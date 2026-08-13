# firekeep-client

The portable client kit for [Firekeep](https://firekeep.ai) — a **self-hosted
control plane for AI coding agents**: persistent team memory, session
continuity, environment awareness, agent coordination, and replayable decision
traces. Every capability is an MCP tool behind one local `firekeep` gateway;
shipped adapters configure Claude Code, Codex, Kiro, and OpenCode, and any
MCP-capable client can connect.

mcp-name: io.github.kapella-hub/firekeep

## What this package is

The client half of Firekeep: the stdio MCP gateway (`firekeep gateway`) that
aggregates your team server's services (memory, sessions, environment,
coordination) plus client-local code intelligence and the Decision Board, the
lifecycle hook cores, the runtime adapters, and the `firekeep` CLI
(`install`, `join`, `doctor`, `update`).

**Firekeep is self-hosted — this package needs a server to talk to.** Each
team runs its own server on any Docker host, with per-key authentication.
There is no hosted endpoint. Server install:
[firekeep.ai/docs.html](https://firekeep.ai/docs.html#server).

## Install

The recommended path is the managed installer, which pins a private
environment under `~/.firekeep`, verifies Ed25519-signed releases, renders
every runtime adapter, and keeps itself updated:

```bash
curl -fsSL https://firekeep.ai/latest/install.sh | sh    # macOS / Linux
irm https://firekeep.ai/latest/install.ps1 | iex         # Windows
```

Installing from PyPI works too and gives you the same CLI and gateway:

```bash
pip install firekeep-client            # gateway + CLI + hooks + adapters
pip install "firekeep-client[symdex]"  # + client-local code intelligence
firekeep join fk_join_...              # single-use code from your team's dashboard
firekeep install                       # render adapters for your agent clients
```

Note the trade-off: a pip install lives in whatever environment you put it in
and updates when you update it; the managed installer owns its environment,
verifies release signatures against a pinned key, and swaps versions without
closing agent sessions.

## Connect an MCP client manually

Any MCP client can launch the gateway over stdio once `firekeep join` has
written the connection:

```json
{
  "mcpServers": {
    "firekeep": { "command": "firekeep", "args": ["gateway"] }
  }
}
```

## Links

- Product and docs: https://firekeep.ai · https://firekeep.ai/docs.html
- Why agents need this: https://firekeep.ai/agents-md-vs-memory.html
- The instruction-compliance study: https://firekeep.ai/instruction-compliance.html
- Security contact: security@firekeep.ai

## License

BUSL-1.1 (source-available). Free for individual use — production use by one
natural person in one workspace. Teams of more than one person run on a paid
commercial subscription (sales@firekeep.ai). Each version converts to
Apache-2.0 four years after its first public release.
