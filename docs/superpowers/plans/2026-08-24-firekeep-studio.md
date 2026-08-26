# Firekeep Studio Implementation Plan

**Spec:** `docs/superpowers/specs/2026-08-24-firekeep-studio-design.md`
**Method:** TDD where behavior is portable; live smoke probes only for installed vendor binaries.
**Status:** Complete for the `0.3.2` preview.

## Completion record

All seventeen planned tasks landed under the isolated `studio/` package. The implementation
also added four product-level extensions discovered during hardening:

- fresh safe `/compare` plus evidence-preserving `/consensus`;
- provider-reported `/usage` and a next-turn `/budget` guard with deduplicated accounting;
- optional no-shell Kiro IDE launch/handoff while Kiro CLI remains the peer runtime;
- current-platform packaging, the established Firekeep Beacon app icon, OS save-dialog
  exports, exact-ID-confirmed local session deletion, and an OS-dialog-selected workspace
  shared by every runtime.

The release hardening pass also fenced session/workspace changes during active runs, waits
for cancellation to settle before flushing the JSONL queue, restricts Electron permission
handling to main-frame microphone requests, and verifies the hidden packaged renderer over
its loopback debugging endpoint instead of relying on a Windows window title.

The 0.2.1 installed-preview pass replaced frozen model/reasoning menus with live Codex,
Claude, Kiro, and Grok discovery; repaired Claude cache-aware and interrupted-run usage
including legacy-session recovery; mapped explicit unrestricted mode to Claude and Kiro's
native flags; moved the unbound cwd from the application directory to the user's home; and
made active work visible on the Firekeep logo. A genuine disposable Claude mission proved
unrestricted execution and nonzero provider usage together.

The 0.2.2 layout pass moved the complete session-usage and token-guard card into the left
session rail, and made the runtime-card list explicitly collapsible without changing runtime
selection, review, connection, or refresh behavior.

The 0.2.3 interaction pass made the selected runtime an explicit modern **In use** state,
added an obvious full-inspector hide/show control, and made live model refresh request-fenced
and visibly report loading, success, empty, or failure while replacing provider options.
Usage now separates fresh tokens from cached context while retaining total traffic as the
guard input. A parameterless typed IPC action opens the dashboard derived from the existing
Client Kit config, and completed tool rows use short deterministic descriptions while raw
details remain expandable.

The 0.3.0 visual pass reserves the full Windows caption-button safe area, gives the primary
runtime selector its own non-clipping control, and replaces the working-logo spinner with a
reduced-motion-safe flickering ember and radiating glow. Runtime-neutral rich Markdown now
lazy-renders sanitized Mermaid with zoom, copy, expansion, and source fallback. The existing
Client Kit Decision Board is presented as a native Studio form through a strict main-process
loopback bridge. Its authenticated out-of-band notification deliberately preserves the
existing MCP long poll, preventing an additional full-context polling turn; tool-result URL
detection is only the recovery path.

The 0.3.1 interaction pass replaces the native primary dropdown with an accessible,
status-aware runtime picker, changes transcript auto-follow from repeatedly restarted smooth
animations to an instant streaming tail with an explicit smooth **Latest** return, and moves
copy/paste through bounded text-only main-process IPC. Server statistics were deliberately
not added: Studio has no structured stats contract, and parsing doctor prose or duplicating
the Client Kit's endpoint/TLS resolver would create a misleading second source of truth.

The 0.3.2 orchestration pass promotes each completed answer above its subordinate folded work
log and adds a tmux-like agent grid without introducing a terminal emulator or a second runtime
path. Any chat-capable adapter can occupy a pane. The selected pane receives the shared
composer through a typed direct-message action, retains its provider-native continuation, and
does not silently become the configured primary. Panes share the selected workspace and Studio
session, while the existing single-active-run invariant continues to prevent concurrent writers.

Three plan details changed in implementation because live evidence made the safer contract
clearer: reviewer/compare runs are sequenced rather than parallel so there is one genuine
active-run/cancellation owner; provider failures never invoke any fallback automatically;
and the current primary is an explicit application default while each session stores its
own provider-native continuation IDs.

Final gates are recorded in the Studio README. Deterministic tests include core, commands,
all four adapters, JSONL/ACP transport, persistence, IPC, rendering, voice-unavailable,
permission/workspace policy, Mission Mode, visual artifacts, native Decision Boards, and
packaging helpers. The final 0.3.2 pass is 133 deterministic tests, four installed-runtime
auth/model/effort/health smoke tests, and genuine
disposable Mission runs on Codex, Claude, and Kiro, including an unrestricted Claude gate
that also requires measured usage. Only Mission runs spend provider tokens.

## 0.2 completion record — Mission Mode

The second milestone turns the console into an outcome-bounded harness without creating a
second provider execution path. A Mission is persisted with one Studio session and calls the
same runtime runner used by ordinary turns, reviews, comparisons, and handoffs. The single
active-run invariant therefore remains load-bearing.

Mission execution provides:

- one frozen primary writer, workspace, model, effort, and permission posture;
- up to 20 deterministic local commands behind one native Electron confirmation;
- bounded output, timeouts, abort, and process-tree cleanup for every check;
- a provider-measured 50k default next-run guard with pause/continue;
- one default repair (configurable from zero through three), triggered only by failed checks;
- fresh safe reviewers whose free text is retained but never parsed into a grade;
- explicit approve, partial/failure, and human-directed repair decisions;
- structured check, review, and outcome receipts persisted and exported with the session;
- an opt-in real disposable mission suite for Codex, Claude, and Kiro, separate from the
  no-inference installed-runtime health probe.

The renderer receives typed `mission.run|continue|repair|complete|cancel` actions, not a
generic process primitive. Slash commands and inspector controls call the same service
methods. On Windows, the check runner's cmd quoting is pinned by real-process tests because
ordinary `spawn()` quoting corrupts quoted executables; timeout/abort terminates the exact
child process tree.

## Global constraints

- Add the application under `studio/`; do not refactor existing services.
- Preserve `scripts/installlab/lab.py`, `docs/marketing/`, and `scripts/demo/` exactly as found.
- Provider names do not appear in core service conditionals.
- Do not read, copy, log, or migrate vendor credential stores.
- Do not override `HOME`/`USERPROFILE` for provider child processes.
- Do not use `--trust-all-tools`, `--dangerously-skip-permissions`, or an equivalent default.
- UI controls and slash commands must call the same `StudioService` methods.
- Unsupported capabilities fail with a user-facing reason.

## Task 1 — Package and contracts

Create the Electron/Vite/React/TypeScript package, split main/preload/renderer tsconfigs, and
define shared IPC types. Add a minimal smoke test and production build.

**Verify:** package install, typecheck, test, and empty-window build.

## Task 2 — Runtime-neutral core

Write tests first for:

- runtime registration and duplicate rejection;
- primary selection using two fake providers;
- capability enforcement;
- normalized event sequencing;
- cancellation;
- native session ID persistence without cross-provider assumptions.

Implement `AgentRuntime`, `RuntimeRegistry`, `StudioSession`, `EventJournal`, and
`StudioService` only to satisfy those tests.

## Task 3 — Slash command registry

Write parser/completion/dispatch tests covering quotes, escaped characters, aliases,
subcommands, flags, unknown commands, capability errors, and live completions.

Register the runtime, connectivity, account, reviewer, review, handoff, session,
workspace, model, effort, permissions, budget, Firekeep, voice, theme, export, cancel, clear, shortcuts,
and help commands from the spec. Commands delegate to `StudioService`.

## Task 4 — Local persistence and authentication broker

Add injectable settings and secret stores. Test that serialized state omits secret values,
primary/reviewer/model settings round-trip, corrupt state recovers with a warning, and
secret deletion is durable. Implement the Electron secret store with `safeStorage`.

## Task 5 — JSONL process transport

Build one bounded JSONL RPC/process primitive used by Codex and Claude. Test partial lines,
multiple lines per chunk, request correlation, timeout, abort, child exit, malformed events,
stderr bounds, and unknown notifications.

## Task 6 — Codex App Server adapter

Use stable App Server methods only. Test against a fake JSONL server for initialization,
account status/login URL, models, thread start/resume, turn streaming, approvals,
cancellation, usage, and unknown events. Add an opt-in live handshake test.

## Task 7 — Kiro ACP adapter

Use `@agentclientprotocol/sdk` stable v1. Test capability negotiation, session create/load,
message/tool/turn updates, permission requests, abort, and process exit with an in-memory ACP
agent. Add an opt-in live `kiro-cli acp` handshake.

## Task 8 — Claude and Grok adapters

For Claude, test stream-JSON normalization, auth status, session resume, model selection,
reviewer read-only arguments, usage, and malformed-line handling. For Grok, test encrypted
key presence, `/v1/language-models`, Responses streaming, prior-response continuity, HTTP errors, and
key removal using a fake HTTP server.

## Task 9 — Reviews, handoffs, and Firekeep Client Kit

Test immutable review packet bounds, fresh reviewer sessions, non-recursion, read-only mode,
explicit handoff, and no silent fallback. Add a typed wrapper over the existing `firekeep`
CLI for doctor/status/connect/install/personal operations with bounded output and timeouts.

## Task 10 — Secure Electron boundary

Expose an allowlisted preload API. Test action validation, event unsubscription, approval
resolution, and the absence of generic process/filesystem/secret methods. Register IPC in
main and add the persistent service lifecycle.

## Task 11 — Product UI

Build:

- session rail and new-session control;
- persisted workspace selector backed by the operating-system directory dialog;
- primary runtime/model selector;
- connection/account status cards;
- rich transcript with Markdown, code, tools, diffs, approvals, usage, and reviewers;
- composer with slash autocomplete and keyboard navigation;
- reviewer strip and mode control;
- command-result cards and diagnostics drawer;
- push-to-talk voice input plus optional TTS;
- responsive, accessible dark/light visual system.

Pure timeline tests cover streamed rendering, tools, and run terminals; jsdom component
tests cover bootstrap, primary selection, command completion, and unavailable voice. The
production renderer and packaged executable receive separate live window smoke checks.

## Task 12 — Iterate to green

Run unit tests, typecheck, production build, and installed-runtime smoke checks. Fix failures
without weakening assertions. Update `studio/README.md` and the repository documentation
surface. Confirm unrelated dirty paths are untouched and report any runtime capability that
could not be live-tested.

## Task 13 — Mission truth model and session persistence

Write failing tests for verified success/failure, prose-independent review, bounded repair,
token pause/continue, cancellation, native confirmation decline, and restart restoration.
Add the strict Mission snapshot and receipt types, then persist them in the existing session
index. Keep global Studio settings version 1 and preserve old sessions with no Mission.

**Verify:** focused Mission tests and a real JSON session-index round trip.

## Task 14 — Mission coordinator and local verifier

Drive primary, verify, repair, review, and finish through `StudioService`'s one runtime-run
owner. Freeze runtime settings at execution approval. Add an injected check-runner contract
and a main-process shell implementation with output limits, timeout, abort, and exact process-
tree cleanup. No check result may be inferred from output text; only exit zero without timeout
passes.

**Verify:** real child-process tests plus concurrency/cancellation tests.

## Task 15 — Commands, native approval, IPC, and UI

Add the `/mission` command family, native Electron summary confirmation, named IPC actions,
and an inspector card showing phase, checks, budget, outcome source, approval, continue, and
cancel controls. Do not expose a generic process action.

**Verify:** parser/command tests, IPC rejection tests, and jsdom discovery/control tests.

## Task 16 — Genuine installed-runtime task conformance

Add an opt-in suite that gives each selected installed runtime a disposable workspace,
requires an exact file change, runs a real local acceptance command, and asserts a verified
Mission receipt plus normalized message/tool events and native-session continuity. Keep it
separate from the zero-inference auth/health smoke and state clearly that it spends provider
tokens.

**Verify:** `npm run smoke:tasks -- <runtime-id>` for Codex, Claude, and Kiro where installed.

## Task 17 — 0.2 release gates

Update version and documentation surfaces, then run the entire deterministic suite,
typecheck, no-inference runtime smoke, real task conformance, production build, unpacked
package, current-platform installer, packaged renderer CDP smoke, and dependency audit.
Preserve every unrelated dirty path.

## Task 18 — 0.4 cross-platform release and verified updates

Add a Studio-owned updater instead of routing through the Python Client Kit. Pin Firekeep's
existing minisign public key in Electron main, verify a bounded fixed-channel manifest before
asking the native updater to download, and verify the downloaded file's signed size and
SHA-256 before installation. Expose typed update state through IPC, a compact title-bar
control, and `/update status|check|install`. Keep the renderer unable to provide URLs, paths,
keys, manifests, or signatures, and install only after orderly Studio shutdown.

Build x64 Windows NSIS assets and universal macOS DMG+ZIP assets in a tag-triggered workflow.
Publish binaries under an immutable `studio-v<version>` release, publish only signed metadata
under `studio-latest`, and verify the public bytes and signature. Gate macOS automatic updates
on actual Apple signing/notarization credentials; otherwise publish a clearly manual universal
DMG. Update both READMEs and the design contract.

**Verify:** updater/signature/IPC/renderer tests, workflow and manifest contract tests, full
deterministic suite, typecheck/build, runtime dependency audit, local Windows installer plus
packaged smoke, tag workflow on macOS and Windows, public artifact byte checks, and an installed
Windows 0.4.0 version check. Preserve unrelated dirty paths and stage explicit files only.
