# Firekeep Studio — Universal Agent Console Design

**Date:** 2026-08-24
**Status:** Implemented preview (`0.3.7`)
**Product name:** Firekeep Studio
**Package:** `studio/` (separate desktop application)

## 1. Outcome

Firekeep Studio is the one local application from which a person can use, supervise,
and compare every supported agent runtime. One runtime is the explicit **primary** for
each Studio session. Any chat-capable runtime can be primary. Any review-capable runtime
can be a reviewer. No provider receives architectural privilege.

Studio retains the existing Python Client Kit rather than reimplementing it. The Client
Kit remains responsible for installing Firekeep into vendor runtimes, rendering MCP and
hook configuration, connecting to a Keep, personal/bypass mode, doctor checks, and the
local Firekeep gateway. Studio supervises provider-native agent harnesses and renders
their output through one canonical protocol.

Studio 0.2 also provides **Missions**, a session-scoped harness that binds a goal to
one primary writer, deterministic local checks, measured-token and repair bounds,
independent review evidence, and an explicit outcome receipt. Mission results are never
inferred from agent or reviewer prose.

The first release supports these runtime families behind the same contract:

| Runtime | Native boundary | Local authentication |
|---|---|---|
| Codex | `codex app-server` JSONL RPC | App Server account APIs / existing Codex login |
| Claude Code | `claude -p` bidirectional stream JSON | Official `claude auth` flow |
| Kiro | `kiro-cli acp` (ACP v1 over stdio) | Official `kiro-cli login` flow |
| Grok | xAI Responses API | API key in the operating-system protected store |

Kiro IDE is an optional launch/handoff target. It is not a duplicate runtime entry: the
embedded Kiro agent is driven through Kiro CLI's official ACP endpoint.

## 2. Load-bearing invariants

1. **The core knows runtimes, not vendors.** Provider names may appear only in adapter
   registration, adapter code, display metadata, and provider-specific tests.
2. **Primary is explicit state.** Studio never silently switches a failed primary to a
   different provider. Recovery is an explicit retry, primary change, or `/handoff`.
3. **Commands and controls cannot drift.** Buttons and `/` commands invoke the same typed
   `StudioService` operations. Commands contain presentation and argument parsing only.
4. **Capability honesty beats false parity.** Every adapter negotiates and reports its
   capabilities. Unsupported actions are disabled with a reason, never simulated.
5. **Provider-native harnesses remain in charge.** Studio consumes official App Server,
   ACP, stream-JSON, or API boundaries; it does not scrape TUIs or replay private tokens.
6. **Credentials stay local.** Vendor-managed credentials remain vendor-managed. API
   keys stored by Studio use Electron `safeStorage` and are never sent to the Keep.
7. **Firekeep remains the continuity layer.** Every runtime receives the same Client Kit
   gateway, instructions, lifecycle, and memory affordances its capabilities permit.
8. **Reviews are independent evidence.** A reviewer starts in a fresh, read-only context
   over an immutable evidence packet. It cannot mutate the primary conversation or repo.
9. **Raw evidence is retained locally.** Normalization never destroys the original event;
   raw payloads stay in the local session JSONL until the user explicitly deletes it.
10. **Personal mode is universal.** Studio observes and controls the Client Kit bypass
    state; no runtime can continue emitting Firekeep lifecycle data while bypass is on.
11. **One active run means one active run.** Primary turns, reviewers, comparisons, and
    synthesis are sequenced through one cancellation/approval owner; no parallel child may
    overwrite the active-run pointer.
12. **Usage is provider evidence, not an estimate.** Studio keeps the latest usage report
    per run, never double-counts an adapter's final report, and labels missing cost/usage.
    Claude cache creation/read tokens and deduplicated partial usage remain measurable after
    cancellation; older raw Claude records supply the same recovery path on reload.
13. **Project workspace is explicit.** A desktop launch never treats the application install
    directory as the user's project. An ordinary unbound turn starts from the user's home;
    Missions require an OS-picker-selected workspace shared by every runtime. The persisted
    selection is fenced while a run is active.
14. **Mission truth is a separate field.** `taskResult` and `taskResultSource` are stored
    in a structured receipt; no agent answer, review sentence, or command output is parsed
    into a grade.
15. **Only checks trigger automatic repair.** A nonzero or timed-out required check may
    trigger the next bounded primary attempt. Reviewer prose is evidence for a human, never
    executable control flow.
16. **One mission has one writer.** The bound primary is the only write-capable runtime.
    Checks and fresh safe reviewers run sequentially through the same active-work owner.
17. **Execution is explicit and frozen.** Before the first run, a native Electron dialog
    shows the exact workspace, local commands, permission posture, budget, and repair bound.
    Workspace, primary, model, effort, and permission settings are then frozen for retries.
18. **Unknown stays unknown.** Passing checks plus configured reviewers produces
    `awaiting-approval`, not success. Only explicit human acceptance or a structured human
    partial/failure result closes that reviewed mission.
19. **Usage names what happened.** Total provider traffic remains the safety-guard input,
    while the UI separately reports fresh tokens and cached context. A warm cache is never
    presented as newly generated work, and no provider-reported traffic disappears.
20. **Live controls are observable.** Model refresh is request-fenced, replaces the selected
    runtime's provider catalog, and visibly reports loading, success/empty, or failure.
    Reasoning choices are derived only from that live catalog, never a Studio-owned list.
21. **Dashboard navigation is configured, not renderer-directed.** Main reads only the safe
    `[server]` fields from the existing Client Kit config, derives the dashboard URL for
    ports or paths mode, and exposes only an availability boolean plus a parameterless typed
    open action. The renderer cannot supply or receive the target URL or API key.

## 3. Process topology

```text
React renderer (untrusted)
        │ typed IPC only
Electron preload (context-isolated)
        │
Electron main / StudioService
 ├─ RuntimeRegistry ── Codex / Claude / Kiro / Grok adapters
 ├─ CommandRegistry ── /runtime /account /reviewer /doctor ...
 ├─ SessionStore ───── primary, workspace, reviewers, models, policies
 ├─ MissionCoordinator goal, checks, repair, review, outcome receipts
 ├─ CheckRunner ─────── native-confirmed, bounded local verification
 ├─ SecretStore ────── OS-encrypted API keys only
 ├─ Review/Compare Coordinator
 └─ FirekeepClient ─── existing `firekeep` Python CLI + gateway
```

The renderer never receives provider secrets, a raw environment, or a Node execution
primitive. It sends discriminated IPC actions and receives serializable state/events.

## 4. Canonical runtime contract

Every runtime implements this semantic interface:

```ts
interface AgentRuntime {
  readonly descriptor: RuntimeDescriptor;
  probe(): Promise<RuntimeConnection>;
  authStatus(): Promise<RuntimeAuthStatus>;
  login(request: LoginRequest): Promise<LoginResult>;
  logout(): Promise<void>;
  listModels(): Promise<RuntimeModel[]>;
  run(request: RunRequest, sink: RuntimeEventSink, signal: AbortSignal): Promise<RunResult>;
}
```

`RuntimeDescriptor.capabilities` is a closed set:

- `chat`
- `review`
- `streaming`
- `tools`
- `approvals`
- `resume`
- `models`
- `images`
- `audio-input`
- `usage`
- `reasoning`
- `firekeep-hooks`

An adapter may be installed but disconnected, authenticated but unhealthy, or healthy
without chat capability. These states are distinct in the UI and `/doctor` output.

## 5. Primary runtime and handoff

`primaryRuntimeId` is the user's explicit current default for future turns. A local session
records the runtime on every event and keeps separate provider-native session IDs, but
resuming a transcript never silently changes the current primary.

Changing primary has two forms:

- `/runtime use <id>` changes the primary for future turns without importing context.
- `/handoff <id>` constructs a bounded packet from completed transcript messages plus the
  user's optional handoff note. It starts a fresh target-provider session, stores that new
  native session ID, and makes the target the explicit primary.

Studio never claims vendor-native session portability. Each adapter persists its native
session ID alongside the Studio session and resumes only through that provider's protocol.

## 6. Normalized event model

All events include `id`, `runId`, `studioSessionId`, `runtimeId`, and `timestamp`.

| Event | Purpose |
|---|---|
| `run.started` | Run metadata and selected model |
| `message.delta` | Streaming assistant or reviewer text |
| `message.completed` | Stable message content |
| `reasoning.delta` | Provider-exposed reasoning summary only |
| `tool.started` / `tool.updated` / `tool.completed` | Tool lifecycle |
| `diff.updated` | Patch or file-change preview |
| `approval.requested` / `approval.resolved` | User authorization |
| `usage.updated` | Tokens, cost, duration where available |
| `notice` | Capability, warning, and diagnostic messages |
| `run.completed` / `run.failed` | Terminal state; `run.failed.cancelled` distinguishes cancellation |

Unknown provider events become `notice` events and remain in the raw log. An adapter
upgrade therefore cannot crash Studio merely because a provider added an event type.

## 7. Slash command surface

Typing `/` opens a searchable command palette. Completion is argument-aware and uses live
runtime descriptors plus command shape. Quoted arguments and `--flags` are supported. `/help` renders
the same metadata used by completion, so documentation cannot drift from the parser.

### Runtime and connectivity

- `/runtime list`
- `/runtime status [id|all]`
- `/runtime use <id>` (`/primary <id>`, `/use <id>` aliases)
- `/runtime models [id]`
- `/connect <runtime-id> [--method browser|device|console|sso]`
- `/disconnect <runtime-id>`
- `/account list`
- `/account login <runtime-id> [method]`
- `/account logout <runtime-id>`
- `/doctor [runtime-id|all]` (`/status` alias)

### Review and orchestration

- `/reviewer list`
- `/reviewer add <runtime-id>`
- `/reviewer remove <runtime-id>`
- `/reviewer clear`
- `/reviewer mode off|manual|after-turn`
- `/review [runtime-id|all] [--focus <text>]`
- `/compare [runtime-id...] --prompt <text>` (`all` selects every chat runtime)
- `/consensus [runtime-id] [--focus <text>]`
- `/handoff <runtime-id> [--note <text>]`

### Session and execution

- `/workspace show|choose|clear` (`/project` alias)
- `/session new [name]`
- `/session list`
- `/session rename <name>`
- `/session color <ember|gold|moss|teal|ocean|violet|rose|slate>`
- `/session resume <id>`
- `/session delete <id> --confirm <id>`
- `/model [runtime-id] [model-id]`
- `/effort [runtime-id] low|medium|high|xhigh|max`
- `/permissions [runtime-id] safe|standard|unrestricted`
- `/budget show|set <amount>|off`
- `/usage`
- `/cancel`
- `/clear`
- `/export markdown|json`

### Missions

- `/mission new <goal>`
- `/mission status`
- `/mission primary <runtime-id>`
- `/mission reviewer add|remove <runtime-id>`
- `/mission check add <command> [--name <label>] [--timeout <duration>]`
- `/mission check remove <check-id>`
- `/mission budget <amount|off>`
- `/mission repairs <0-3>`
- `/mission run|continue|cancel`
- `/mission repair --note <direction>`
- `/mission approve [--note <text>]`
- `/mission result partial|failure [--note <text>]`
- `/mission report`

### Firekeep and interface

- `/firekeep status|doctor|version|connect|personal|update|night-shift`
- `/kiro status|use|connect|open`
- `/voice on|off|status`
- `/theme system|dark|light`
- `/shortcuts`
- `/help [command]`

Commands return structured `CommandResult` blocks rather than printing ANSI text. A
command may request a UI action (open a login URL, focus settings, choose a folder) but
may never acquire Electron primitives directly.

## 8. Authentication and secrets

Studio presents one Accounts screen while preserving three independent trust planes:

1. Firekeep device/member identity.
2. Model-provider identity and billing.
3. Tool/action credentials exposed through MCP or local processes.

Codex, Claude, and Kiro login flows are delegated to their official clients. Studio reads
only their supported status result and never copies their credential files. Child processes
inherit the real user environment; in particular Studio never replaces `HOME`, because that
breaks Kiro's existing PKCE session on macOS.

Grok currently requires an xAI API key. Studio stores it through an injected `SecretStore`;
the Electron implementation encrypts it with `safeStorage`. The renderer receives only
`configured: true|false`, never the value. Deleting the account deletes the local ciphertext.
API-key login is deliberately unavailable in slash commands: secrets enter only through
the password input in the typed connection dialog.

## 9. Provider mappings

### Codex

Spawn `codex app-server` over local stdio JSONL. Initialize with client name
`firekeep_studio`, then use `account/read`, `account/login/start`, `model/list`,
`thread/start|resume`, `turn/start|interrupt`, item notifications, approval requests, and
token-usage notifications. Stay on the stable API surface; do not enable experimental API
unless a separately tested feature requires it.

### Claude Code

Spawn `claude -p --input-format stream-json --output-format stream-json --verbose` and
preserve the official CLI's authenticated environment. Parse init, assistant, tool-use,
partial-message, and result records. Resume with the emitted session ID. Read the installed
CLI's current model aliases and reasoning choices from `--help`; do not freeze that catalog
in Studio. Standard mode uses Claude's `auto` posture, reviewer mode uses plan/read-only
tools, and an explicit unrestricted selection maps to `bypassPermissions` plus
`--dangerously-skip-permissions`.

### Kiro

Spawn `kiro-cli acp` and use stable ACP v1 through the official TypeScript SDK. Map session
updates (`AgentMessageChunk`, `ToolCall`, `ToolCallUpdate`, `TurnEnd`) and ACP permission
requests into canonical events. When the Client Kit's global `firekeep` agent exists, launch
ACP explicitly with `--agent firekeep` so its gateway, hooks, and steering remain active;
otherwise use Kiro's provider default. Do not scrape the TUI and never pass
`--trust-all-tools` by default. Discover account-visible models through
`chat --list-models --format json` and effort levels from current ACP help. Pass
`--trust-all-tools` only for an explicit unrestricted selection.

### Grok

Use the xAI Responses API with streaming and the authenticated language-model endpoint.
Reasoning choices follow the selected model's supported Responses contract. The adapter is
chat/review capable but does not gain local filesystem or shell tools merely to match coding
CLIs. Local actions require explicitly connected MCP tools in a later capability extension.

## 10. Reviews

A review packet contains the primary's completed answer, the independent-review contract,
and an optional focus string. A comparison sends the same prompt to at least two runtimes;
a consensus packet contains the latest bounded candidate response per runtime/role.

Review runs are fresh and read-only. Findings render separately with reviewer identity and
remain visible as source evidence for a follow-up or `/consensus`. `after-turn` mode respects
a local budget ceiling and never recursively reviews reviewer output.

## 11. Mission Mode

A Mission is one session-scoped, persistable state machine:

```text
draft -> primary -> verify --fail/remaining-repair--> primary
                         \--final--> fresh safe reviews
                                      |-- verified failure
                                      |-- no reviewers: verified success
                                      \-- reviewed pass: human decision
```

The draft captures a goal, primary runtime, reviewers, up to 20 required commands, a
provider-measured token guard (50,000 by default), and zero to three repairs (one by
default). The workspace comes only from the OS folder picker. At execution approval,
Studio freezes the workspace and each involved runtime's selected model, effort, and
permission posture; reviewer permission remains `safe` regardless of stored settings.

Before execution, Electron presents a native confirmation containing the exact workspace,
primary permission mode, commands, token guard, and repair bound. The renderer cannot send
a generic process action. The main process runs only commands already stored in the locked
mission, with a per-check timeout, bounded stdout/stderr, cancellation, and process-tree
termination. A check passes only on exit code zero without timeout.

A failed verification may trigger only the next bounded primary repair. The repair prompt
contains the failed check receipt, not a guessed explanation. After final verification,
each configured reviewer receives a fresh, read-only evidence packet containing the goal,
primary report, and check states. Reviewer failures are recorded; no reviewer failure
silently replaces the primary or the deterministic result.

Outcome truth is a separate pair:

| `taskResult` | `taskResultSource` | Producer |
|---|---|---|
| `success` | `verified` | all required checks passed and no reviewers require human acceptance |
| `failure` | `verified` | required checks still failed after the bounded repair allowance |
| `success` / `partial` / `failure` | `human_confirmed` | explicit decision after review |

Configured review deliberately changes the successful terminal path to
`awaiting-approval`; review text can contain any words without altering that state. The
human may approve, record partial/failure, or request another repair if the bound allows it.
Each outcome references the exact check and review receipt IDs. Mission state and receipts
live with the Studio session, survive restart, and are included in local JSON/Markdown
exports. A paused mission retains its next action and can continue after an explicit budget
increase or recoverable runtime failure.

## 12. Voice

Voice is a Studio input/output layer, not a provider feature requirement:

```text
microphone -> typed local STT boundary -> editable Studio prompt -> primary runtime
primary text -> optional system TTS -> speakers
```

The transcript is always visible and editable before submission. Tool approvals are never
accepted from passive speech; an explicit click is required. On Windows, Studio invokes the
installed desktop speech recognizer through a strict start/stop IPC pair and a bounded,
cancellable local process. Raw audio never crosses IPC or reaches an agent. Electron's Web
Speech object is not a valid availability signal because recognition fails at runtime with
its hosted-service network error; Studio does not use that path. Other platforms report
input as unavailable until they gain a native adapter. System TTS remains platform-owned.
Runtime-native audio may be added only through capability negotiation.

## 13. Native visual artifacts and Decision Boards

Studio treats diagrams and multi-question decisions as first-class runtime-neutral
artifacts rather than provider-specific prose conventions.

For ordinary responses, fenced `mermaid` blocks are lazy-rendered in the renderer with
Mermaid `securityLevel: strict`, SVG-only labels, and a second DOMPurify pass that removes
scripts, `foreignObject`, links, and event attributes. Invalid syntax leaves a readable
source block. The native card provides zoom, reset, copy-source, and expanded views. Studio
adds the Mermaid-output instruction only when the user's prompt has explicit visual intent;
it does not add a permanent hidden system prompt or per-turn token tax.

The existing Client Kit `firekeep-decision` server remains the single board owner and keeps
its current random-id loopback `spec`, `embed/<n>`, and `answer` routes. A Studio-launched
runtime inherits three internal environment values: the `studio` presentation marker, an
ephemeral `127.0.0.1` notification endpoint, and a 256-bit bearer token. As soon as the board
is served, the Client Kit posts only its loopback URL to that receiver. Studio's main process
validates the token and exact URL shape, loads and bounds the JSON and embed documents, then
pushes a typed board document to the sandboxed renderer. The renderer never receives a
generic fetch or local-network primitive.

Critically, a successful notification does **not** return `pending` early. The original MCP
call remains in its existing bounded long poll, so a quick native answer returns inside the
same tool call with no extra full-context model turn. If notification fails, the tool returns
the existing pending envelope immediately; Studio can recover the URL from the normalized
tool result. Neither Studio path launches a browser. Runtimes launched outside Studio retain
the browser contract byte-for-byte.

The native board renders context, questions, retrieved evidence, suggested answers,
explicitly confirmable actions, skip controls, and optional rich embeds. Embed HTML is loaded
only by the main process and shown in opaque-origin script-capable frames with a no-network
CSP. A missing or malformed visual degrades independently and can never make the questions
unanswerable.

### Outcome-first transcripts and tiled agents

A completed assistant answer is the primary artifact of a run. Reasoning, tools, diffs, usage,
and informational notices are grouped under a folded **Work log** after the answer; approvals,
warnings, and errors remain visible because they may require action. The work log stays open
while a run has no completed answer and closes when the answer becomes complete.

The **Agents** view is a runtime-neutral tiled workspace, not a provider feature and not a
terminal emulator. Any chat-capable runtime may occupy a pane, each pane retains that runtime's
native continuation, and the selected pane receives the shared composer. Direct pane turns do
not silently change the configured primary; changing primary remains an explicit **Use** action.
Panes share the Studio session and selected workspace, but the existing single-active-run
invariant remains global so multiple agents cannot write concurrently into the same workspace.

## 14. Client Kit integration

Studio locates the existing `firekeep` executable and exposes its operations through a
small typed client:

- status/doctor/version;
- validated SSH connect handoff;
- update the installed Client Kit;
- personal mode;
- night shift and gateway health.

Studio does not import the Python package into Electron and does not recreate configuration
rendering. CLI output is decoded defensively as UTF-8; structured output will be preferred
when the Client Kit adds it. A missing Client Kit produces an actionable command error, not
an application crash.

## 15. Persistence, privacy, and limits

- Settings and session indexes live under Electron's per-user application-data directory.
- The selected workspace path is local settings state; an OS directory dialog is the only
  renderer-accessible way to change it.
- Secrets are encrypted separately from settings.
- Run logs are JSONL and never uploaded implicitly. A destructive session delete requires
  the exact session ID as a second token.
- Provider prompts and responses do not enter Firekeep failure reporting.
- Export is explicit and uses the operating-system save dialog.
- Process environments are never included in diagnostics.
- Review evidence is bounded before it is sent to another provider.
- Mission check output is capped locally and is never uploaded except when its bounded
  failure evidence is intentionally sent to the bound primary for repair.
- Mission outcomes remain local Studio receipts in 0.2; a future Bridge receipt route may
  ingest them only through an explicit authenticated contract.

## 16. Acceptance gates

1. Core tests prove two fake runtimes are interchangeable as primary and reviewer.
2. Command tests cover quoting, aliases, completion, invalid arguments, capability errors,
   and every command's help metadata.
3. IPC tests prove the renderer cannot request arbitrary process execution or read secrets.
4. Fake peers cover Kiro ACP and Codex App Server initialization; the installed smoke probes
   each real CLI's version and provider-owned auth without executing an inference turn.
5. Claude auth status parses on a real installed CLI; Grok remains disconnected without a
   stored key.
6. A fake streamed run renders text, tools, diffs, approval, usage, and terminal state.
7. Reviewer mode is fresh, read-only, non-recursive, and never changes primary implicitly.
8. `npm test`, type checking, the production Electron build, unpacked package, and current-
   platform installer build pass.
9. Existing Python service behavior and tests are untouched by the isolated Studio package.
10. The existing unrelated dirty files remain byte-identical and unstaged.
11. Installed-runtime smoke verifies Codex, Claude, Kiro, and Firekeep health/auth without
    executing an inference turn.
12. Token guards count one final report per run, disclose measurement coverage, and stop
    before the next run rather than claiming an exact provider billing ceiling.
13. Workspace selection is persisted, reaches every `RunRequest.cwd`, and cannot change
    during a run; the renderer receives no generic filesystem API.
14. Shutdown cancels and awaits the active runtime before flushing JSONL writes, packaged
    initial navigation succeeds, and only main-frame audio—not camera—is permissioned.
15. Mission tests cover verified success/failure, bounded repair, reviewer advisory-only
    semantics, human confirmation, human-directed repair, token pause/continue, cancellation,
    declined native approval, and restart persistence.
16. A reviewer response containing grade-like words such as `CHANGES_REQUESTED` leaves the
    task result unknown until the human acts.
17. Model, effort, permission, primary, and workspace bindings do not drift between the
    initial attempt and a resumed repair.
18. The verifier passes real-process tests for workspace binding, nonzero exit evidence,
    output truncation, timeout, abort, Windows quoted paths, and exact process-tree cleanup.
19. IPC exposes named Mission operations but no generic process action; local commands can
    enter only through mission configuration and execute only after native confirmation.
20. Session index round-trips the complete Mission snapshot and outcome receipt; local
    export includes its result source.
21. Opt-in live task conformance completes a real disposable file-writing mission and local
    verification through each installed Codex, Claude, and Kiro adapter. It is separate from
    the no-inference connectivity smoke and labels its provider-token cost.
22. Renderer tests prove live-model replacement and visible refresh state, selected-runtime
    semantics, full-inspector hide/show, fresh/cached/total usage labels, configured-dashboard
    opening, and concise completed-tool descriptions.
23. Config and IPC tests reject unsafe dashboard schemes, missing configuration, renderer-
    supplied URLs, malformed actions, and credentials embedded in a dashboard URL.
24. Renderer tests prove fenced Mermaid becomes sanitized SVG, invalid Mermaid preserves its
    source, and the visual hint appears only on explicit visual-intent prompts.
25. Decision Board tests prove exact loopback URL validation, bounded main-process loads,
    typed answer submission, bearer-protected notification, deduplication, native form
    rendering, and browser-free fallback.
26. A successful native notification keeps the original MCP call in `_poll_board`; a test
    submits through the board route and proves the same call returns the answer rather than a
    pending envelope that would force another model turn.
27. Streaming deltas follow the transcript tail without restarting smooth animations; user
    scroll-up disables auto-follow until an explicit smooth **Latest** action.
28. Clipboard read/write crosses only typed, bounded text IPC. Response and diagram copy and
    caret-aware composer paste expose no generic clipboard or OS bridge to the renderer.
29. The primary runtime picker exposes readiness and transport context, marks the selected
    runtime, and supports mouse, arrow-key, Home/End, Enter/Space, and Escape interaction.
30. Recall responses expose the exact stable memory IDs represented by returned vector and
    graph-linked results, and the MCP rendering supplies a directly callable `memory_feedback`
    footer without inventing an ID for unattributed prose.
31. Renderer tests prove completed answers precede a folded work log, while approvals,
    warnings, and errors remain visible outside the low-priority fold.
32. Agent-grid tests prove any chat-capable runtime can occupy and receive a direct pane turn,
    direct targeting preserves the configured primary, and packaged smoke validates selectable
    pane geometry without permitting concurrent active runs.
