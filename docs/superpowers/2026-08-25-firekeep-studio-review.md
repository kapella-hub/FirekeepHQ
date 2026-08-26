# Firekeep Studio — architecture & security review (v0.3.2)

**Date:** 2026-08-25 · **Reviewer:** read-only, 5-dimension review (no files edited) · **Status of subject:** active WIP.

> This is a review of another session's in-progress work. Nothing here was changed. It credits what's
> genuinely strong and flags what's worth attention, with file:line evidence. Distinguish real defects
> from WIP-incompleteness where noted.

## Bottom line

Well-built software, not scaffolding — above the median for an Electron app. The runtime-neutral
architecture is real, the Electron security posture is excellent, and the Mission truth-contract is
honored in code. Weaknesses are real but concentrated. **One overclaim matters most:** the "preserves the
complete Client Kit capability surface" framing is not accurate across all four runtimes, and Grok reaches
the Keep not at all.

## Genuinely strong (credit where due)

- **Electron security — standout.** `contextIsolation:true` + `nodeIntegration:false` + `sandbox:true`
  (`src/main/index.ts:36-41`); a minimal typed IPC allowlist — one Zod discriminated-union channel,
  `.strict()` on every arm, no `ipcRenderer` passthrough, no Node primitives exposed
  (`src/preload/index.ts`, `src/main/ipc-controller.ts:18-58`); untrusted agent output defended in depth
  (no `rehype-raw` in `RichMarkdown.tsx`; mermaid `securityLevel:"strict"` + a second DOMPurify pass in
  `MermaidDiagram.tsx:22-37`; decision-board embeds in `<iframe sandbox="allow-scripts">` **without**
  `allow-same-origin` + injected `connect-src 'none'` CSP at `App.tsx:720-728`); secrets via `safeStorage`,
  never in argv/renderer/logs (`electron-secret-store.ts`, `settings-store.ts`); refuses to spawn
  `.cmd`/`.bat` shims (`process.ts:98-99`, BatBadBut CVE-2024-27980 class). **No high-severity findings.**
- **Runtime-neutral abstraction is real.** One `AgentRuntime` interface (`src/core/runtime.ts:147-159`);
  a subprocess runtime (Claude) and an HTTP/SSE runtime (Grok) both map onto the same event union with
  zero core changes. Capability-gated fan-out, not runtime-id branching (`runtime-registry.ts:29-39`).
  Would survive a 5th runtime.
- **Mission truth-contract — exemplary + tested.** `taskResult` stored separately from all prose,
  deterministic checks (not reviewer text) drive automatic repair, reviewer output explicitly never
  parsed as a grade (`studio-service.ts:1329`, `mission.ts:119`). Layered cancellation
  (`AbortController` parent→child, mission+run abort, process-tree kill). `mission.test.ts` drives the
  whole state machine with real assertions. Adapter tests parse realistic provider events, not "no throw".

## Findings, ranked by what matters

### 1. [HIGH — accuracy] "Complete Client Kit surface" is an overstatement; Grok reaches the Keep not at all
Studio wires the Keep by **inheritance, not active wiring** — it does NOT shell out to `firekeep gateway`
(`firekeep-client.ts` wraps only status/doctor/version/personal/connect/update/night-shift). It launches
provider CLIs with inherited env (HOME not overridden), so each CLI reads its own kit-rendered config.
- **Claude** — full *if the kit is installed*. But `claude-runtime.ts:36` declares `firekeep-hooks`
  **unconditionally**, with no check the kit exists — unlike Kiro. This is the one *dishonest* spot: it
  advertises a capability that may be wired to nothing.
- **Kiro** — full, honestly gated: `firekeep-hooks` only when `~/.kiro/agents/firekeep.json` exists
  (`kiro-runtime.ts:59,65,148`), tested both ways.
- **Codex** — partial: inherits MCP (config.toml), correctly does NOT claim `firekeep-hooks`.
- **Grok** — **empty**: pure xAI Responses API, no CLI, no native config, no MCP → zero Keep surface. A
  Grok primary or reviewer runs with **no team memory, no hooks, no briefing**, and nothing in the UI
  surfaces that.
**So what:** the README is more careful than a blanket "complete surface" claim, and per-runtime
descriptors are honest — but the blanket phrasing isn't true, and Grok's total absence of Keep memory is
invisible to the user. **Fix:** gate Claude's `firekeep-hooks` on evidence (mirror Kiro); surface in the
UI when a runtime has no Keep memory.

### 2. [HIGH — correctness] Codex login is likely broken
`login()` runs inside `#withPeer`, which `peer.close()`s — killing the `codex app-server` process the
instant `account/login/start` returns the auth URL / device code (`codex-runtime.ts:85-103` + `238-246`).
Browser/device OAuth completion is delivered out-of-band to that same live connection, which is now dead.
Only `api-key` (synchronous write) is safe. Same teardown also negates the graceful `turn/interrupt`
(`196-199` vs `230-235`) — cancellation only ever kills the process. **Needs live verification; as written
the browser/device flows look non-functional.** Codex is also the riskiest adapter generally (largest
protocol surface, tightest coupling to an evolving local protocol).

### 3. [HIGH — verification] The headline integration is entirely untested
Nothing in the suite verifies a Studio-launched turn actually reaches `memory_recall` or fires a
briefing/hook. `smoke:runtimes` (`live-runtimes.test.ts`) probes auth/models + calls `firekeep status`,
but never runs a turn that touches the Keep. The product's core promise rests on descriptor strings and
README prose. **Run/cancel paths are also untested against any real server** — all confidence rests on
in-memory fakes (`tests/live-runtimes.test.ts` skips turns and skips Grok entirely). **Fix:** one
integration test proving a Studio turn reaches the Keep (and, ideally, a live turn+abort per adapter).

### 4. [HIGH — runtime perf/robustness] Renderer is O(n²) over a session, and has no error boundary
Every `message.delta` is a distinct broadcast event; the renderer appends to an unbounded array (O(n)
dedup), rebuilds the whole timeline via `useMemo(buildTimeline, [events])`, and re-renders the **entire**
transcript with **no `React.memo`** and no virtualization (`App.tsx:153,470,658`; `timeline.ts:49`). A
single long answer is O(m²) (string concat), and every already-completed message re-parses its markdown on
every token of the current one. Invisible on short transcripts; will jank in exactly the long, fast
missions Studio targets — the token-stream half of "backpressure" the (good) tail-follow work didn't
address. Paired with **no React `ErrorBoundary`** (`main.tsx`) — one render-time throw from untrusted
markdown/mermaid blanks the whole window. **Fix:** delta coalescing/throttling + `React.memo` on
`RunTimeline`/`TimelineCard`/`RichMarkdown` (+ eventually virtualization); wrap message/diagram rendering
in an error boundary.

### 5. [MEDIUM — correctness] Cancel orphans process trees
All transports terminate with a bare `child.kill()` (SIGTERM, no tree) — `jsonl-rpc.ts:244`,
`kiro-runtime.ts:283`, Claude via `transport.kill()` — even though `process.ts:76-91` has `killTree`
/ `taskkill /t` and doesn't use it here. An aborted run leaks the shells/dev-servers/compilers a coding
agent spawned, most acutely on Windows. **Fix:** route adapter teardown through `killTree`.

### 6. [MEDIUM — parity] Grok is a degraded tier, not a fourth peer
Descriptor advertises chat/review/streaming/resume/models/images/usage/reasoning — **no tools, no
approvals, no hooks** (`grok-runtime.ts:53`). It's a text/reasoning streamer, not an agentic coding
runtime. "Four runtimes at parity" is inaccurate — it's three agentic + one conversational behind the same
interface. Also: no stall watchdog (unlike Codex's 30-min guard; `reader.read()` can block forever), a
minor reader/body leak on throw paths (`156-158`, no `try/finally`), and `store:true` unconditional
(`121`) so ephemeral review/compare turns are still persisted server-side — inconsistent with Claude's
`--no-session-persistence` and Codex's `ephemeral:true`.

### 7. [MEDIUM — maintainability] Duplicated enum literals + no migration seam
- Effort `["low","medium","high","xhigh","max"]` is re-hardcoded at ~6 sites despite `RUNTIME_EFFORTS`
  being exported; permission `["safe","standard","unrestricted"]` at ~3 sites with no exported const.
  Adding/renaming a value silently diverges validation — the repo's own change-consistency smell.
- Every persisted shape is `version:1` with **reject-on-mismatch and no migration function**
  (`parseMissionSnapshot`→null silently drops a mission; `#normalizeState` resets all settings). The first
  bump to `2` silently discards users' missions/settings. **Add a real migration seam before 1.0.**

### 8. [LOW] Assorted
- Accessibility: `aria-live="polite"` on the *entire* transcript (`App.tsx:468`) → screen-reader flood
  during token streaming (worse than no live region); custom comboboxes lack `aria-activedescendant`;
  modals inconsistently apply `role="dialog"`/focus-trap. Pervasive 8–9px `--faint` text.
- UX: Enter submits the literal text instead of the highlighted slash completion (`App.tsx:482-485`) → a
  highlighted `/mission` + Enter throws "unknown command: mis"; Escape cancels the active run even from the
  composer; mermaid renders dark under the default "system" theme (`MermaidDiagram.tsx:16`).
- Security (defense-in-depth, none exploitable): agent-triggered loopback fetch + **auto-open** of decision
  boards with no user gesture (`App.tsx:156-160`) — contained by loopback normalization + the embed
  sandbox, but consider a click gate; `will-navigate` attached post-load (`index.ts:54-66`); `style-src
  'unsafe-inline'` in the renderer CSP.
- Tests: `MermaidDiagram` SVG sanitization (security-load-bearing, `dangerouslySetInnerHTML`) has no test;
  `src/main/index.ts` (env injection, IPC wiring) untested; coverage report scoped to `src/core` +
  `src/main/runtime` only, so the measured number omits the untested main glue and is easy to over-read.

## Suggested priority

1. Honest-up the surface: evidence-gate Claude's `firekeep-hooks`; surface "no Keep memory" for Grok.
2. Fix/verify Codex login (peer teardown) against a live server.
3. One integration test that proves a Studio turn reaches the Keep.
4. Renderer perf (delta coalescing + `React.memo`) + an error boundary, before real long-mission use.
5. `killTree` on cancel; the migration seam and the enum-literal consolidation when convenient.

Security needs almost nothing. This is impressive WIP that isn't done — the honesty gaps (#1, the Claude
flag) are the most important to close because they're the kind of confident-claim-exceeds-reality this
project's whole memory/outcome layer exists to refuse.

_(Note: a reviewer's tooling flagged `--dangerously-skip-permissions`/`bypassPermissions` strings — those
are legitimate Claude CLI flags in the adapter, not a finding.)_
