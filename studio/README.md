# Firekeep Studio

Firekeep Studio is Firekeep's runtime-neutral desktop agent console. Version `0.5.0`
is a separate Electron application: it does not replace or fork the Python Client Kit.
The kit remains the source of truth for Keep connectivity, memory, hooks, instructions,
personal mode, and runtime configuration.

## 0.5 — Window into the Keep

Studio 0.5 reorganizes the app around the Keep: a three-column layout of **sessions rail**,
**conversation**, and **Keep panel**.

- **Layout.** Toggle the Keep panel with `Ctrl/Cmd+\` or the top-bar button. At window widths
  of 1100 px and below the panel becomes a drawer over the conversation. `Ctrl/Cmd+K` opens the
  command palette, which also searches the Keep. `Ctrl/Cmd+Shift+R` starts a review, `Enter`
  sends and `Shift+Enter` adds a newline, and `Escape` closes the open surface or cancels the
  active run. Settings is a sheet (it replaced the Runtime Center) with the sections
  **Agents & accounts**, **Keep connection**, **Reviews**, **Appearance**, and **Studio updates**.
- **Start card, Agent chip, "Done when…", Second opinion.** The Start card replaces the
  Welcome hero. The composer's Agent chip picks the primary runtime. **Done when…** turns the
  composer's prompt into a Mission with deterministic checks; it uses the same confirmation
  dialog, outcome receipt, and human acceptance as before. **Second opinion** asks another
  agent for a fresh, read-only review of an answer.
- **The Keep link.** Studio reaches the Keep only by launching the kit's own `firekeep gateway`
  with `FIREKEEP_TOOLSET=studio`, the kit's preset of nine tools, under the agent label
  `<agent>-studio-panel`. The Keep panel searches memory and skills and shows who is online,
  the bulletin board, and paused sessions. Each turn can end with a **Keep receipt** built from
  replay: what the agent recalled and what it wrote. Receipts carry thumbs-up/down feedback,
  sent through `memory_feedback`.
- **Receipt states.** *Measured* is a full receipt. *Partial* means Studio could not read the
  Keep's timeline for the turn: the Keep server is older than v1.6.2 (it rejects the JSON
  timeline request or answers in Markdown), the kit is older than 1.8.0 (it does not write
  the report file that names the agent's Keep sessions), or the agent's runtime did not pass
  that file through. Studio then shows what it saw locally and notes that hook-injected recall
  is not shown. *Unavailable* means the Keep could not be reached while collecting that turn;
  the newest receipt offers a retry.
- **Requirements.** Client kit 1.8.0 or later and Keep server v1.6.2 or later give full
  receipts. With older versions Studio still works; receipts read "partial".
- **Runtime coverage.** Studio hands each agent a `FIREKEEP_SESSION_REPORT_FILE` path. Codex
  forwards it through the kit's `env_vars` setting, verified with codex-cli 0.153.4. Claude and
  Kiro are expected to inherit it from the process environment but are not yet verified, so
  their receipts may show "partial" until they are.
- **Shutdown.** Closing Studio while a turn's receipt is still pending does not finalize that
  receipt.
- **Removed.** Voice input and spoken replies (`/voice` now only explains this), the Grok
  runtime, `/compare`, `/consensus`, the Agents grid, and the Welcome hero. Studio stores no
  secrets.

## Earlier versions

Studio gives every supported agent the same product role. You explicitly choose the
primary runtime for future turns, may attach any review-capable runtime as an independent
reviewer, and can change or hand off between them without claiming that vendor-native
sessions are portable.

Session usage and its next-turn guard stay visible in the composer, with fresh tokens
separated from cached context and total provider traffic. **Settings → Agents & accounts**
keeps every supported agent's account, reviewer, connectivity, and Keep status available; the
selected runtime has an explicit **In use** state. Model refreshes replace the live provider
catalog and show progress, success, or failure. When the Client Kit has a server configured,
Settings → Keep connection also opens its dashboard without sending a URL through renderer IPC.

Sessions can be named and color-coded from the palette icon on their left-rail row—including
inactive sessions—or from `/session rename ...` and `/session color ...`. The fixed palette is
stored with session metadata; older indexes migrate to the ember default when loaded.

Version 0.2 adds **Missions**: an outcome-bounded harness that gives one primary writer a
goal, explicit workspace, frozen runtime settings, local acceptance checks, a measured-token
guard, bounded repair attempts, and optional independent reviewers. Checks—not prose—drive
automatic repair. Reviewer responses remain advisory evidence, and a reviewed success stays
unknown until the human accepts it.

Version 0.3 adds native visual artifacts and Decision Boards. When a prompt asks for a graph,
diagram, flowchart, topology, or chart, Studio adds a compact Mermaid-format hint to that turn
only; ordinary turns pay no visual-instruction token cost. Fenced `mermaid` blocks render as
sanitized, zoomable, expandable diagrams with a readable source fallback. When any supported
local runtime calls Firekeep's existing `decision_board` tool, Studio opens its questions,
retrieved evidence, suggestions, explicit actions, and sandboxed visual embeds in a native
panel. The Client Kit pushes the random loopback board URL to an ephemeral bearer-protected
Studio receiver while retaining the existing long poll, so a quick answer returns within the
same agent tool call instead of spending another full-context polling turn. If that push
fails, Studio can still recover the board from the ordinary tool result. No provider gets a
special visual or Decision Board path.

Version 0.3.1 replaces the browser-native primary selector with a compact, status-aware
agent picker that supports mouse and keyboard selection without changing the runtime-neutral
contract. Streaming responses follow the tail without restarting a smooth-scroll animation
for every delta; scrolling upward pauses follow mode and exposes an explicit **Latest**
control. Response and diagram copy plus caret-aware composer paste use bounded text-only
main-process clipboard operations, so the sandboxed renderer receives no general OS API.

Version 0.3.2 makes completed answers the visual result of a run and folds low-priority
reasoning, tool, diff, usage, and status events into a subordinate **Work log**. Approval and
warning events remain visible. (Its tiled Agents view was removed in 0.5.)

Version 0.3.3 added Windows dictation and spoken replies; both were removed in 0.5.

Version 0.3.4 brings the established Firekeep Beacon into every Studio brand surface with
the product's continuity promise: agents come and go; the Keep stays.

Version 0.3.5 makes each complete session row the name-and-color editor trigger while
preserving its existing select/resume behavior. The palette icon remains a visual hint,
not a second competing target.

Version 0.3.6 restores the clearer split interaction after hands-on use: clicking a session
row only selects it, while its palette icon exclusively opens name-and-color editing.

Version 0.3.7 is a complete presentation pass: stronger contrast and typography, calmer
surface depth, larger interaction targets, richer Markdown, polished session/runtime/mission
panels, and native light/dark window chrome. The packaged smoke verifies both themes and the
session editor against the built executable.

Version 0.4 adds a release channel that is separate from the Python Client Kit. Packaged
Studio checks that channel shortly after launch, verifies its manifest with Firekeep's pinned
minisign public key, and accepts only the platform artifact whose version, byte length, and
SHA-256 digest match that signed manifest. Windows downloads in the background and offers a
visible **Restart to update** action; a normal app close also installs a ready update after
Studio has flushed its active session. macOS uses the same signed release evidence, but native
in-app installation is enabled only for releases signed and notarized with Apple Developer
credentials. Until those credentials are armed, Studio opens the universal DMG explicitly
instead of pretending an unsigned build can pass Apple's updater requirements.

Version 0.4.1 prevents incomplete Mission and Review actions before they reach Electron's
handler and returns every other operational action failure through Studio's typed IPC result.
Draft Missions identify missing workspace, primary, or deterministic-check prerequisites and
offer a direct check setup path; manual Review stays unavailable until a primary answer exists.
Backend validation remains authoritative, while expected user errors stay inside the interface
instead of escaping as Electron handler stack traces. Codex and Kiro process trees now settle
before a run resolves, preventing Windows workspace-handle races. Live conformance also keeps
Kiro's optional ACP usage honest when the provider omits token counts instead of inventing them.

## Runtime support

| Runtime | Structured boundary | Firekeep Client Kit surface | Authentication |
|---|---|---|---|
| Codex | `codex app-server` JSONL RPC; live `model/list` | Keep memory when the managed Codex MCP block is detected; Codex exposes no hooks | Existing account or App Server-managed browser/device/API-key login |
| Claude Code | Native `stream-json` CLI; live CLI aliases/efforts | Keep memory and automatic hooks are reported independently from the installed native configs | Existing provider-owned Claude login |
| Kiro CLI | Stable ACP v1; live account models/efforts | Keep memory and hooks when the Client Kit's named `firekeep` agent is installed | Existing provider-owned browser/device login |

Studio shows each agent's Keep status in Settings → Agents & accounts and never implies an
agent has team memory merely because another installed runtime does.

Kiro CLI is the Kiro runtime inside Studio. The optional Kiro IDE is an explicit external
handoff via `/kiro open`; it is not a second, privileged agent implementation. When the
Client Kit's global `firekeep` Kiro agent is installed, Studio selects it explicitly so the
same gateway, hooks, and steering remain active inside ACP sessions.

## Run it from this checkout

```bash
cd studio
npm install
npm start
```

Build an unpacked application or an installer for the current operating system:

```bash
npm run package
npm run dist
```

Artifacts land in `studio/release/`. Windows builds an x64 NSIS installer. macOS builds a
universal DMG for installation and a ZIP used by the native updater. Local builds are unsigned
unless the operating system's release-signing identity is configured. On first launch, choose
a primary runtime on the Start card, choose the workspace your agents should operate
in, then connect any account whose provider CLI is not already signed in. Ordinary turns
without an explicit workspace start from the user's home folder, never the installed
application's launch directory; Missions still require an explicit workspace.

## Essential commands

Type `/` in the composer for live completion, or `/help` for the canonical inventory.
Useful starting points:

- `/doctor` or `/status` — verify every installed runtime and its provider-owned auth.
- `/mission new "..."` — create a mission in the current session using the selected primary,
  workspace, and reviewers.
- `/mission check add "npm test" --name tests --timeout 10m` — add a local acceptance check.
- `/mission run`, `/mission continue`, and `/mission cancel` — operate the bounded workflow.
- `/mission repair --note "..."` and `/mission approve` — make reviewer-driven repair and the
  final human decision explicit.
- `/mission status` and `/mission report` — inspect evidence and the separately stored result.
- `/workspace choose` — select the one working folder passed to every provider runtime.
- `/model [runtime]` and `/effort [runtime]` — inspect the current runtime's live model and
  reasoning options; `/effort default` returns control to the provider.
- `/permissions unrestricted` — explicitly enable the selected runtime's native unrestricted
  posture. This can modify anything the provider process can access; reviewers remain safe.
- `/use codex` — choose any chat-capable runtime as primary.
- `/reviewer add claude` and `/reviewer mode after-turn` — add automatic fresh reviews.
- `/review all --focus security` — run configured reviewers manually.
- `/handoff kiro --note "continue from the verified tests"` — explicitly transfer the work.
- `/connect codex --method device` — launch supported non-secret provider auth.
- `/kiro status` and `/kiro open` — inspect Kiro CLI plus the optional IDE handoff.
- `/budget set 50k` and `/usage` — stop before the next turn after a measured session limit.
- `/session rename ...`, `/session color ember|gold|moss|teal|ocean|violet|rose|slate`,
  `/session resume ...`, and `/session delete <id> --confirm <id>`.
- `/export markdown` or `/export json` — choose a local file; Studio uploads nothing.
- `/firekeep status`, `/firekeep doctor`, `/firekeep personal ...`, and `/firekeep update`.
- `/update status`, `/update check`, and `/update install` — inspect or act on the separate
  Firekeep Studio release channel.

The token guard uses total provider-reported traffic, including cached context. The usage
card separately shows **fresh** tokens (non-cached input, cache writes, output, and other
reported work), **cached** reads, and their total, so a large warm context is not presented
as newly generated work. It is a local next-turn guard, not a provider
billing limit: a provider may omit usage, and Studio cannot interrupt a turn at an exact
token boundary. Claude cache creation/read tokens and partial usage from interrupted runs are
counted; older saved Claude streams are recovered from their deduplicated provider records.
Reported cost is shown only when the provider supplies it. Completed tool rows use short,
deterministic descriptions such as “Ran tests” or “Checked repository status”; raw tool
names, inputs, and outputs remain available in the expandable details.

## Mission truth contract

A mission requires an explicit workspace, primary runtime, and at least one deterministic
check. On first run, Electron shows a native confirmation containing the exact workspace,
primary permission mode, repair limit, token guard, and local commands that will execute.
The mission then follows one state machine:

```text
primary writer -> deterministic checks -> bounded repair (if needed)
               -> fresh safe reviewers -> human acceptance -> outcome receipt
```

The local outcome receipt stores `taskResult` separately from all free-text agent output.
An all-green mission without reviewers records `success / verified`; exhausted failing
checks record `failure / verified`. If reviewers are configured, their prose is retained but
never parsed as a grade or control signal. The result remains unknown until `/mission approve`
or `/mission result partial|failure`. A human can request a remaining bounded repair with an
explicit note. Reports include the check and review receipt IDs supporting the result.

Mission token budgets count only usage actually reported by providers and stop before the
next agent run. They do not claim to be provider billing limits. Runtime model, effort, and
permission settings are frozen when execution begins; reviewer runs are always fresh and safe.

## Security and privacy boundaries

- The renderer is sandboxed, context-isolated, and receives a discriminated IPC allowlist.
  It has no generic process, filesystem, environment, or secret API.
- Child agents inherit the user's real environment; Studio never substitutes `HOME` or
  `USERPROFILE`, and it never copies vendor credential stores.
- Studio-launched local runtimes inherit an ephemeral Decision Board callback URL and random
  bearer token. Both endpoints bind to `127.0.0.1`; the renderer cannot fetch arbitrary local
  URLs, and the main process accepts only exact random-id board routes.
- Mermaid SVG is generated with strict security, sanitized again before insertion, and cannot
  retain scripts, links, event handlers, or `foreignObject`. Decision Board HTML visuals run in
  script-capable opaque-origin iframes with a no-network CSP; a failed visual never prevents
  answering the board.
- Studio persists no secrets. An API key typed into the Connect dialog is passed straight to
  the provider's own login flow and is not kept; slash commands deliberately refuse API-key
  arguments so secrets cannot enter command history.
- Reviews run fresh and read-only. Review output is never recursively reviewed.
- Mission verification commands execute locally only after a native confirmation shows the
  exact commands. Output is bounded, every check has a timeout, and cancellation terminates
  the check's process tree rather than only its shell.
- Exactly one mission writer, reviewer, or check owns execution at a time. Studio
  never runs multiple writing agents concurrently in the same workspace.
- Provider failures never silently switch the primary runtime. Use `/handoff` explicitly.
- Sessions are local JSONL files under Electron's user-data directory. Delete requires the
  exact session ID as confirmation; exports use an OS save dialog.
- The workspace is selected through an OS folder dialog, persisted locally, and cannot be
  changed while a run is active.
- From the kit installation Studio reads exactly one thing, the dashboard URL, from
  `~/.firekeep/config` (or the path in `FIREKEEP_CONFIG`). It never reads or stores Keep
  credentials. Everything else about the Keep comes from running the kit's own executable
  (on Windows `~/.firekeep/current` first, then `PATH`), chiefly `firekeep gateway`.
- IPC is typed: the renderer sends a schema-validated, discriminated action allowlist, and no
  action accepts a tool name.
- Studio calls only the nine tools of the kit's `studio` preset. This is enforced twice: by
  the gateway's toolset (`FIREKEEP_TOOLSET=studio`) and by Studio's own `STUDIO_TOOLS`
  allowlist in `src/main/keep/`, which refuses any other tool before it is sent.
- Studio's generic update URL is fixed in the main process. The renderer cannot supply an
  update URL, release manifest, local path, or signature. A downloaded installer is never run
  unless both the signed manifest and the artifact's exact size and SHA-256 digest verify.

## Publishing Studio

The `studio-release` workflow publishes a release only from a `studio-vMAJOR.MINOR.PATCH`
tag whose package version matches and whose commit is on `main`. It builds and smoke-tests an
x64 Windows NSIS installer plus a universal Intel/Apple-Silicon macOS DMG and updater ZIP.
Large binaries live once in the immutable `studio-v<version>` release in
`kapella-hub/firekeep-dist`; the small `studio-latest` release is only the signed mutable
channel pointer. Publishing is byte-idempotent and verifies the public channel before the job
can pass.

The workflow requires `FIREKEEP_DIST_RELEASE_TOKEN` and `FIREKEEP_SIGNING_KEY`. Native Windows
signing is used when `WIN_CSC_LINK` and `WIN_CSC_KEY_PASSWORD` are configured. Native macOS
automatic updating requires all of `MAC_CSC_LINK`, `MAC_CSC_KEY_PASSWORD`, `APPLE_ID`,
`APPLE_APP_SPECIFIC_PASSWORD`, and `APPLE_TEAM_ID`; without that complete set the workflow
publishes an explicitly unsigned universal installer and marks macOS updates as manual.

## Validation

```bash
npm test                 # deterministic tests; live probes are skipped
npm run typecheck
npm run smoke:runtimes   # real Codex/Claude/Kiro auth, models, efforts + Firekeep health; no agent turn
npm run smoke:tasks      # spends provider tokens; real disposable Codex/Claude/Kiro missions
npm run smoke:tasks -- codex  # select one or more runtime ids
npm run build
npm run package
npm run smoke:package    # proves the packaged renderer loaded from app.asar
```

`npm run render:icon` regenerates `resources/icon.png` from the checked-in Beacon SVG.

The product contract and implementation record are:

- `../docs/superpowers/specs/2026-08-24-firekeep-studio-design.md`
- `../docs/superpowers/plans/2026-08-24-firekeep-studio.md`
