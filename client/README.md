# firekeep-client

The portable client kit for [Firekeep](https://firekeep.ai) — the **self-hosted
operating layer for connected AI agents**. It carries durable knowledge,
working context, procedures, coordination, and replayable evidence across
sessions, models, machines, and teammates. Agent-facing MCP tools are exposed
through one local `firekeep` gateway. Shipped adapters configure Claude Code,
Claude Desktop (auto-detected when the app's config dir exists), Codex, Kiro,
and OpenCode; other MCP clients can connect through the generic
configuration path, without hook-driven lifecycle automation.

mcp-name: io.github.kapella-hub/firekeep

## What this package is

The client half of Firekeep: the stdio MCP gateway (`firekeep gateway`) that
aggregates your team server's services (memory, sessions, environment,
coordination) plus the Decision Board and whichever **dexes** — the domain
indexes the Keep understands — you have registered, the lifecycle hook cores,
the runtime adapters, and the `firekeep` CLI (`install`, `join`, `doctor`,
`update`, `dex`, `docdex`, `maildex`).

Three dexes ship today: `firekeep-symdex` (code intelligence),
`firekeep-docdex` (folders of documents indexed into the Keep's corpus), and
`firekeep-maildex` (email over read-only IMAP, always member-private,
registered with `firekeep maildex add`). The managed installer always installs
all three wheels; `firekeep dex list|add|remove` decides which of them
actually run. Symdex and docdex are registered by default — since client 1.2.0
an absent registry is seeded with both (default-on), `firekeep dex remove` is
the off-switch, and removals stick across updates.

**Firekeep is self-hosted — this package needs a server to talk to.** A person
or team runs its own server, with per-key authentication. Current server images
target `linux/amd64`: use an x86-64 Linux host, or Docker Desktop with amd64
container support on Windows or Mac. There is no hosted endpoint. Server install:
[firekeep.ai/docs.html](https://firekeep.ai/docs.html#server).

## Install

The recommended path is the managed installer, which pins a private
environment under `~/.firekeep`, verifies Ed25519-signed releases, renders
every runtime adapter, and keeps itself updated:

```bash
curl -fsSL https://firekeep.ai/latest/install | sh    # macOS / Linux
irm https://firekeep.ai/latest/install.ps1 | iex         # Windows
```

It asks two required questions: the agent identity your memories, sessions and
replay events are attributed to, and where your server is — set one up on this
machine, redeem a join code, point at one that is already running, or decide
later. It then offers a skippable prompt for another MCP client's rules file.
The full guide is
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

BUSL-1.1 (source-available). The Additional Use Grant permits free self-hosted
production use by individuals and teams while Firekeep is in early access.
Commercial licensing covers Firekeep Enterprise and hosted or managed use
outside that grant (sales@firekeep.ai). Each version converts to Apache-2.0
four years after its first public release.
