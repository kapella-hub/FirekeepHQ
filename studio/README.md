# Firekeep Studio

Firekeep Studio is Firekeep's runtime-neutral desktop agent console. Version `0.4.0`
is a separate Electron application: it does not replace or fork the Python Client Kit.
The kit remains the source of truth for Keep connectivity, memory, hooks, instructions,
personal mode, and runtime configuration.

Studio gives every supported agent the same product role. You explicitly choose the
primary runtime for future turns, may attach any review-capable runtime as an independent
reviewer, and can change or hand off between them without claiming that vendor-native
sessions are portable.

Session usage and its next-turn guard stay visible in the left session rail, with fresh
tokens separated from cached context and total provider traffic. The selected runtime has
an explicit **In use** state. **Runtime Center**, opened from the primary-runtime picker,
keeps every supported agent's account, reviewer, connectivity, and Keep status available
without occupying the right inspector. The inspector can be hidden when the conversation
needs more visual focus. Model refreshes replace the live provider catalog and show progress,
success, or failure. When the Client Kit has a server configured, the rail also opens its
dashboard without sending a URL through renderer IPC.

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
warning events remain visible. The **Agents** view (`Ctrl/Cmd+\\`) tiles any chat-capable
runtime into independently scrollable panes; selecting a pane routes the shared composer to
that runtime without changing the configured primary, and **Use** makes the change explicit.
Each pane keeps its provider-native continuation while sharing the Studio session and selected
workspace. Studio still serializes active runs, so the tiled view is a supervision and handoff
surface rather than permission for concurrent agents to write into one workspace.

Version 0.3.3 replaces Electron's nonfunctional hosted Web Speech recognition with bounded,
cancellable Windows dictation. The microphone is click-to-start and click-to-cancel, raw
audio remains inside the Windows recognizer, and only the resulting text returns through
Studio's typed IPC boundary. Unsupported platforms now report that honestly.

Version 0.3.4 brings the established Firekeep Beacon into every Studio brand surface and
replaces the generic welcome copy with the product's continuity promise: agents come and
go; the Keep stays.

Version 0.3.5 makes each complete session row the name-and-color editor trigger while
preserving its existing select/resume behavior. The palette icon remains a visual hint,
not a second competing target.

Version 0.3.6 restores the clearer split interaction after hands-on use: clicking a session
row only selects it, while its palette icon exclusively opens name-and-color editing.

Version 0.3.7 is a complete presentation pass: stronger contrast and typography, calmer
surface depth, larger interaction targets, richer Markdown, polished session/runtime/mission
panels, native light/dark window chrome, and a minimum-width inspector that becomes a real
drawer instead of disappearing. Three-agent workspaces now use one balanced, tmux-style row
when space permits. The packaged smoke verifies both themes, the session editor, the responsive
inspector, the command surface, and the three-pane geometry against the built executable.

Version 0.4 adds a release channel that is separate from the Python Client Kit. Packaged
Studio checks that channel shortly after launch, verifies its manifest with Firekeep's pinned
minisign public key, and accepts only the platform artifact whose version, byte length, and
SHA-256 digest match that signed manifest. Windows downloads in the background and offers a
visible **Restart to update** action; a normal app close also installs a ready update after
Studio has flushed its active session. macOS uses the same signed release evidence, but native
in-app installation is enabled only for releases signed and notarized with Apple Developer
credentials. Until those credentials are armed, Studio opens the universal DMG explicitly
instead of pretending an unsigned build can pass Apple's updater requirements.

## Runtime support

| Runtime | Structured boundary | Firekeep Client Kit surface | Authentication |
|---|---|---|---|
| Codex | `codex app-server` JSONL RPC; live `model/list` | Keep memory when the managed Codex MCP block is detected; Codex exposes no hooks | Existing account or App Server-managed browser/device/API-key login |
| Claude Code | Native `stream-json` CLI; live CLI aliases/efforts | Keep memory and automatic hooks are reported independently from the installed native configs | Existing provider-owned Claude login |
| Kiro CLI | Stable ACP v1; live account models/efforts | Keep memory and hooks when the Client Kit's named `firekeep` agent is installed | Existing provider-owned browser/device login |
| Grok | xAI Responses API with SSE; live language-model catalog | Provider-direct: no Keep memory, hooks, or briefing | xAI API key stored with Electron `safeStorage` |

Studio shows this distinction for every agent in Runtime Center. It never implies that a
provider-direct adapter has team memory merely because another installed runtime does.

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
a primary runtime from the welcome screen, choose the workspace your agents should operate
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
- `/compare all --prompt "..."` — ask each runtime independently in safe contexts.
- `/consensus codex --focus evidence` — synthesize recent answers while retaining dissent.
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
- `/voice on` — enable spoken assistant replies. Click the microphone once to dictate and
  click again to cancel; the editable transcript is never submitted automatically.

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
- API keys travel only through the secure connection dialog and OS encryption. Slash
  commands deliberately refuse API-key arguments so secrets cannot enter command history.
- Reviews, comparisons, and consensus run fresh and read-only. Review output is never
  recursively reviewed.
- Mission verification commands execute locally only after a native confirmation shows the
  exact commands. Output is bounded, every check has a timeout, and cancellation terminates
  the check's process tree rather than only its shell.
- Exactly one mission writer, reviewer, comparison, or check owns execution at a time. Studio
  never runs multiple writing agents concurrently in the same workspace.
- Provider failures never silently switch the primary runtime. Use `/handoff` explicitly.
- Sessions are local JSONL files under Electron's user-data directory. Delete requires the
  exact session ID as confirmation; exports use an OS save dialog.
- The workspace is selected through an OS folder dialog, persisted locally, and cannot be
  changed while a run is active.
- On Windows, microphone input uses the installed Windows desktop speech recognizer in a
  bounded local process; raw audio never crosses Studio IPC or reaches an agent. Electron's
  hosted Web Speech path is deliberately not used because it exposes the API but fails at
  runtime. Other platforms currently report voice input as unavailable. Spoken replies use
  the operating-system/Chromium speech synthesizer and may have different platform behavior.
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
