# ChatGPT → the Keep, via OpenAI Secure MCP Tunnel

Connects ChatGPT (developer-mode connector) to this machine's Firekeep gateway
**without exposing the Keep**: `tunnel-client` makes outbound-only HTTPS to
OpenAI's control plane and forwards MCP requests to a local
`firekeep gateway --runtime chatgpt` running the curated **chat toolset**
(recall, learn, feedback, skills, sessions — no vault, no corpus ingest, no
relay, no backup). Design record:
`docs/superpowers/specs/2026-08-19-chatgpt-tunnel-design.md`.

**Trust statement:** requests transit OpenAI's control plane — a third party in
the request path, chosen deliberately over opening a public port on a box that
holds all team memory and the vault key. ChatGPT acts as the member this
machine is enrolled as; every call is attributed `runtime: chatgpt` in replay,
so chatgpt-authored memories are auditable (and purgeable) as a class.

## Prerequisites

- This machine runs an enrolled Firekeep client kit (`firekeep doctor` green)
  at client ≥ 1.4.0 (the toolset filter).
- ChatGPT workspace with **developer mode** permission.
- OpenAI Platform account: create a **tunnel** in Platform settings — it yields
  a `tunnel_id` and a runtime **API key**.

## Install (once, as root on the Keep host)

```bash
# 1. tunnel-client binary (see github.com/openai/tunnel-client/releases)
#    Verify the checksum the release page publishes, then:
install -m 0755 tunnel-client /usr/local/bin/tunnel-client
tunnel-client --version

# 2. Config home + secrets
mkdir -p /etc/firekeep/chatgpt-tunnel
install -m 0755 /opt/Firekeep/deploy/chatgpt-tunnel/run-gateway.sh /etc/firekeep/chatgpt-tunnel/
printf 'CONTROL_PLANE_API_KEY=%s\n' '<runtime api key>' > /etc/firekeep/chatgpt-tunnel/tunnel.env
chmod 0600 /etc/firekeep/chatgpt-tunnel/tunnel.env

# 3. Tunnel profile (creates ./profiles/firekeep.yaml)
cd /etc/firekeep/chatgpt-tunnel
tunnel-client init --sample sample_mcp_stdio_local --profile firekeep \
  --tunnel-id <tunnel_id> \
  --mcp-command "/etc/firekeep/chatgpt-tunnel/run-gateway.sh"

# 4. Validate, then install the service
CONTROL_PLANE_API_KEY=<runtime api key> tunnel-client doctor --profile firekeep --explain
cp /opt/Firekeep/deploy/chatgpt-tunnel/firekeep-chatgpt-tunnel.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now firekeep-chatgpt-tunnel
systemctl status firekeep-chatgpt-tunnel
```

## Connect in ChatGPT (once)

1. In OpenAI Platform settings, associate the tunnel with your ChatGPT
   workspace.
2. In ChatGPT developer mode, create the connector and select the tunnel by
   its `tunnel_id`.
3. Prove it end-to-end: ask ChatGPT something only team memory knows
   ("what's our VPS address?") and confirm the answer comes from
   `memory_recall` — then check replay shows `runtime: chatgpt`.

## Operations

- `systemctl status firekeep-chatgpt-tunnel` / `journalctl -u firekeep-chatgpt-tunnel`
- `tunnel-client doctor --profile firekeep --explain` (run from
  `/etc/firekeep/chatgpt-tunnel`)
- The gateway process is LONG-LIVED here (unlike per-session coding hosts). If
  a backend degrades and stays degraded, `systemctl restart
  firekeep-chatgpt-tunnel` restarts the gateway and its backends —
  `Restart=always` already covers crashes.
- Off switch: `systemctl disable --now firekeep-chatgpt-tunnel` (the Keep
  never had a public port to close).

## Changing the tool surface

The chat toolset is set inside `run-gateway.sh` (`FIREKEEP_TOOLSET=chat`).
An explicit list can replace it (`FIREKEEP_TOOLS_ALLOW=name,name,...` — wins
over the preset). The gateway's `firekeep_gateway_status` tool reports the
active toolset and how many tools it filtered; an unknown toolset name refuses
to start rather than serving everything.
