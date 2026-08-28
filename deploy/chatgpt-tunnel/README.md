# ChatGPT → the Keep, via OpenAI Secure MCP Tunnel

Connect ChatGPT (developer-mode connector) to an enrolled Firekeep Client Kit
without opening a public inbound port to the Keep. OpenAI's `tunnel-client`
makes outbound HTTPS connections to OpenAI's control plane and forwards MCP
requests to a local `firekeep gateway --runtime chatgpt`. The gateway uses the
curated **chat** toolset: recall, learning, feedback, skills, and sessions — no
vault, corpus ingest, Relay, backups, dex/code tools, policy actions, or decision
boards.

**Trust boundary:** MCP tool names, arguments, and returned content carried by
the tunnel transit OpenAI's control plane. The Keep does not need a public
listener, but this is still a third-party data path.

**Identity boundary:** the gateway uses the member credential and agent identity
from the Client Kit installed for the service account. Every ChatGPT user and
conversation using this connection shares that Firekeep identity. Enroll a
dedicated member for the connection when you need a separate identity boundary.

`--runtime chatgpt` supplies observability attribution to server requests and is
retained on sessions created through `ctx_start_session`. It does **not** tag
every learned memory, and Firekeep does not provide purge-by-runtime retention.

## Use the maintained setup guide

The step-by-step procedure lives at
[firekeep.ai/chatgpt-self-hosted-mcp-memory.html](https://firekeep.ai/chatgpt-self-hosted-mcp-memory.html).
It covers Client Kit enrollment, the current OpenAI tunnel binary and
permissions, profile creation, the service-owned `0600` runtime-key file,
systemd installation, and ChatGPT connection. Also read OpenAI's
[current Secure MCP Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
before enabling the route.

The Firekeep Client Kit must be **1.4.0 or later** and `firekeep doctor` must be
green for the Linux account that will run the service. Firekeep installation and
enrollment have one canonical guide at
[firekeep.ai/docs.html](https://firekeep.ai/docs.html); the install command is:

```bash
curl -fsSL https://firekeep.ai/latest/install | sh
```

## Use the checked-in deployment assets

The maintained guide is self-contained and does not require a source checkout.
If you are deploying from a FirekeepHQ checkout, this directory provides the
same fail-closed wrapper and a reviewable systemd template. The unit deliberately
contains `__FIREKEEP_OS_USER__`, `__FIREKEEP_OS_GROUP__`, and
`__FIREKEEP_HOME__` placeholders; render those with the enrolled account's exact
values before installation. Do not install the unrendered template. Follow the
maintained setup guide for paths, ownership, validation, enablement, and the
end-to-end proof.

The wrapper clears any inherited `FIREKEEP_TOOLS_ALLOW` immediately before
selecting `FIREKEEP_TOOLSET=chat`. An ambient explicit allowlist therefore
cannot replace the curated surface.

## What this route can return

With every backend healthy, the `chat` preset exposes up to twelve Firekeep work
tools plus the always-present `firekeep_gateway_status` diagnostic. A degraded
backend can reduce the work-tool count; status remains available:

- Memory: `memory_recall`, `memory_learn`, `memory_feedback`.
- Skills: `skill_recall`, `skill_list`.
- Sessions: `ctx_start_session`, `ctx_update`, `ctx_complete_session`,
  `ctx_abandon_session`, `ctx_list_sessions`, `ctx_resume_session`,
  `ctx_get_shadow`.

Corpus ingestion is excluded, but `memory_recall` can still return existing
corpus content visible to the enrolled member, including that member's private
Docdex or Maildex material. The toolset is a capability allowlist, not a
per-memory or per-document content filter.

ChatGPT has no Firekeep lifecycle hooks. Recall, learning, feedback, and session
updates happen only when ChatGPT calls the corresponding tool. All conversations
also share the Client Kit's configured Firekeep agent identity. Starting a
session in one conversation can automatically pause the previous active session
for that identity. Use separate enrolled configurations and agent identities
when independent session boundaries are required.

## Prove and operate the route

In a new ChatGPT conversation with the connection enabled, call
`firekeep_gateway_status`, then use `memory_recall` for a known, non-secret
fact. For a cross-runtime proof, save a harmless test memory explicitly with
`memory_learn` and recall it from another enrolled client.

An interactive shell does not load the service's tunnel credential
automatically. Re-run tunnel diagnostics as the enrolled service account:

```bash
(
  cd /etc/firekeep/chatgpt-tunnel
  set -a
  . ./tunnel.env
  set +a
  tunnel-client doctor --profile firekeep --explain
)
```

- Check: `sudo systemctl status firekeep-chatgpt-tunnel` and `sudo journalctl -u
  firekeep-chatgpt-tunnel`.
- Restart a persistently degraded gateway with `sudo systemctl restart
  firekeep-chatgpt-tunnel`.
- Stop the route with `sudo systemctl disable --now
  firekeep-chatgpt-tunnel`. No public Keep port needs closing.

Changing `run-gateway.sh` changes the connection's security boundary. After a
deliberate change, restart the service and inspect `firekeep_gateway_status`;
an unknown preset refuses to start rather than serving the full gateway.
