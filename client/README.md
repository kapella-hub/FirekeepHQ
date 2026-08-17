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
coordination) plus the Decision Board and whichever **dexes** — the domain
indexes the Keep understands — you have registered, the lifecycle hook cores,
the runtime adapters, and the `firekeep` CLI (`install`, `join`, `doctor`,
`update`, `dex`, `docdex`).

Two dexes ship today: `firekeep-symdex` (code intelligence) and
`firekeep-docdex` (folders of documents indexed into the Keep's corpus). The
managed installer always installs both wheels; `firekeep dex list|add|remove`
decides which of them actually run. Existing installs keep symdex across an
update; a fresh install opts in with `firekeep dex add symdex`.

**Firekeep is self-hosted — this package needs a server to talk to.** Each
team runs its own server on any Docker host, with per-key authentication.
There is no hosted endpoint. Server install:
[firekeep.ai/docs.html](https://firekeep.ai/docs.html#server).

## Install

The recommended path is the managed installer, which pins a private
environment under `~/.firekeep`, verifies Ed25519-signed releases, renders
every runtime adapter, and keeps itself updated:

```bash
curl -fsSL https://firekeep.ai/latest/install | sh    # macOS / Linux
irm https://firekeep.ai/latest/install.ps1 | iex         # Windows
```

It asks two things: the agent identity your memories, sessions and replay
events are attributed to, and where your server is — set one up on this
machine (Docker), redeem a join code, point at one that is already running, or
decide later. The full guide is
[firekeep.ai/docs.html](https://firekeep.ai/docs.html).

Installing from PyPI works too and gives you the same CLI and gateway:

```bash
pip install firekeep-client            # gateway + CLI + hooks + adapters
pip install "firekeep-client[symdex]"  # + the symdex code-intelligence dex
pip install firekeep-docdex            # + the docdex documents dex
firekeep join fk_join_...              # single-use code from your team's dashboard
firekeep install                       # render adapters for your agent clients
firekeep dex list                      # which dexes are registered on this machine
```

A pip install does **not** bundle the dex wheels, so install the ones you want
before registering them — `firekeep dex add <name>` refuses to register a dex
whose wheel it cannot import, rather than leaving you with a backend that fails
to start.

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
