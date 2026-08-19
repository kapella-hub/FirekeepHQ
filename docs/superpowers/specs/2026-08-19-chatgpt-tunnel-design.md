# ChatGPT via Secure MCP Tunnel — design

Decided on the decision board 2026-08-19 (board 9c0786778e31): **tunnel** (not a
public endpoint), **existing member identity**, **curated read-mostly toolset**,
owner has ChatGPT developer mode. The public streamable-HTTP `/mcp` + OAuth
endpoint is explicitly out of scope — it remains its own future decision, and
Claude web/mobile custom connectors stay unsupported until it is taken.

## Architecture

OpenAI's Secure MCP Tunnel (shipped May 2026) runs a customer-side
`tunnel-client` that makes **outbound-only** HTTPS to OpenAI's control plane and
forwards MCP requests to a private MCP server — stdio command or internal URL.
The Keep therefore stays tailnet-only: no public port, no reverse proxy, no
OAuth server.

```
ChatGPT (developer-mode connector, selects tunnel_id)
  ⇅ OpenAI tunnel control plane
tunnel-client on the VPS  — outbound HTTPS only, systemd service
  ↓ spawns (stdio, long-lived)
firekeep gateway --runtime chatgpt          FIREKEEP_TOOLSET=chat
  ↓ existing shims/backends, existing enrollment (the owner's member identity)
cortex / bridge / relay — unchanged
```

No new server component. The VPS already runs an enrolled client kit; the
shipped gateway IS the MCP server the tunnel forwards to.

**Trust statement (docs must carry it):** requests transit OpenAI's control
plane. That is a third party in the request path, chosen deliberately over
public exposure of a box holding all team memory and the vault key.

**Identity:** the VPS enrollment — ChatGPT sees exactly what the owner's coding
agents see, member-private docdex/maildex included. All calls are attributed
`runtime: chatgpt` in replay (the `--runtime` flag is already free-form
pass-through; zero server changes).

**Session semantics:** all ChatGPT chats share the one enrolled agent identity,
so `ctx_start_session` from a new chat auto-pauses the previous session — the
existing single-active-session-per-agent behavior, documented, not changed.

**Bonus for free:** the same tunnel is selectable from OpenAI Codex cloud and
the Responses API — anything on OpenAI's supported-surface list.

## The toolset filter (the only new code — client wheel, 1.4.0)

`gateway.py` gains two env vars, read once in `Gateway.__init__`:

- `FIREKEEP_TOOLSET=<preset>` — named preset. Round 1 ships exactly one:
  `chat` = `memory_recall`, `memory_learn`, `memory_feedback`, `skill_recall`,
  `skill_list`, `ctx_start_session`, `ctx_update`, `ctx_complete_session`,
  `ctx_abandon_session`, `ctx_list_sessions`, `ctx_resume_session`,
  `ctx_get_shadow`. (Prior art rides `ctx_start_session`.)
- `FIREKEEP_TOOLS_ALLOW=<comma,list>` — explicit allowlist; **wins over the
  preset** when both are set.

Rules, all load-bearing:

1. **Filter at the routing layer.** `Gateway.discover()` skips excluded tools
   when building BOTH the advertised list and `self.routes` — an excluded tool
   is invisible AND uncallable (`tools/call` → -32601 unknown tool), never
   decoratively hidden.
2. **Unknown preset fails closed.** `FIREKEEP_TOOLSET=chta` must not silently
   serve ~90 tools through a tunnel: the gateway exits non-zero at startup with
   the valid preset names in the message. A typo is a refusal, not a fallback.
3. **Unset env = byte-identical today.** No filtering, full surface, every
   existing runtime unaffected. Pinned by test.
4. **Disclosure.** `firekeep_gateway_status` (always present, never filtered)
   gains `"toolset"` (preset name, allowlist marker, or null) and
   `"tools_filtered"` (count excluded). Narrowing is visible, never silent.
5. **Instructions match the surface.** When a toolset is active, the
   `initialize` handshake serves `CHAT_INSTRUCTIONS` — a trimmed variant
   covering recall/learn/feedback/sessions only — instead of
   `GATEWAY_INSTRUCTIONS`, which instructs agents to call `vault_retrieve` and
   `decision_board` (not in the preset; an instruction to call a tool that
   errors is worse than no instruction). Built by the same derive-don't-duplicate
   discipline as `GENERIC_INSTRUCTIONS` where practical; its own hash constant
   for serverInfo.version attribution.

**Why memory_learn stays in (and the risk, named):** a chat session that cannot
save a decision loses half its value. The cost is that ChatGPT retrieved
content is prompt-injection-rich and the threat model's largest OPEN finding is
memory poisoning by a valid key — this surface extends it. Round-1 mitigation
is attribution, not prevention: every write carries `runtime: chatgpt` in
replay, so chatgpt-authored memories are auditable and purgeable as a class.
Excluded outright: vault, corpus ingest, relay, backup, dex/code tools.

## Long-lived gateway (verify during implementation)

Every existing host spawns the gateway per session; tunnel-client keeps ONE
stdio process alive indefinitely. Implementation must verify backend-death
behavior (kill a shim mid-run; observe whether `handle`'s re-discover path
respawns it or the backend stays `unavailable:` forever). Whatever is found is
DOCUMENTED, and the systemd unit carries `Restart=always` as the backstop —
tunnel-client restarting restarts the gateway. If recovery turns out absent,
that is a disclosed limitation of round 1, not a blocker.

## Deployment recipe (`deploy/chatgpt-tunnel/`)

- `firekeep-chatgpt-tunnel.service` — systemd unit: runs `tunnel-client run
  --profile firekeep`, `Restart=always`, `EnvironmentFile=/etc/firekeep/chatgpt-tunnel.env`
  (0600 root) holding `CONTROL_PLANE_API_KEY`. The key is NOT in the repo and
  NOT in Firekeep's vault (the tunnel must start before the Keep's own auth
  matters, and the vault is inside the thing being exposed).
- `README.md` — the owner's one-time OpenAI steps: create tunnel in Platform
  settings (yields `tunnel_id` + runtime API key), `tunnel-client init` with
  `--mcp-command "<home>/.firekeep/shims/firekeep gateway --runtime chatgpt"`
  and env `FIREKEEP_TOOLSET=chat`, `tunnel-client doctor`, associate the tunnel
  with the ChatGPT workspace, add the connector in developer mode.
- Exact tunnel-client flags verified against github.com/openai/tunnel-client at
  implementation time (the fetched docs summary is not authoritative).

## Validation gates (in order; the site claims nothing before the last)

1. Unit: preset pinned, routing-layer enforcement, allowlist override, unset =
   unfiltered, unknown preset exits non-zero, status discloses, chat
   instructions served when toolset active.
2. Local: `FIREKEEP_TOOLSET=chat firekeep gateway --runtime chatgpt` piped an
   initialize + tools/list → exactly the preset + status tool; a vault call →
   -32601.
3. VPS: tunnel-client doctor green; service up.
4. ChatGPT: real connector round-trip — "what's our VPS address?" answered from
   recall; `runtime: chatgpt` visible in replay. Owner performs the OpenAI-side
   setup; this gate blocks on it.
5. Then: site automation-table row + mirrors, guide section, CLAUDE.md.

## Out of scope

Public `/mcp` + OAuth endpoint (own future decision); Claude web/mobile
connectors (need that endpoint); multi-member tunnels (per-member identity
mapping — a real teams feature, after demand); doctor integration for the
tunnel service (round 2; `tunnel-client doctor` covers it manually).
