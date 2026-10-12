# Firekeep Studio 0.5.0 — "Window into the Keep" Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn Studio from a control panel into a workspace a new teammate understands in two minutes, one that shows the Keep working: a calm three-column layout, a Start card instead of a hero, "Done when…" instead of the Missions panel, and a Keep panel plus per-turn Keep receipts read from the kit's own gateway.

**Architecture:**
- **Cuts (Tasks 1–4).** Voice, Grok, `/compare` + `/consensus` and the Agents grid are deleted, together with their tests.
- **Pure core logic (Tasks 5–7)** lives in `src/core/`: test-command detection, the receipt model, and a receipt tracker that turns replay timelines into per-turn receipts using a server-ordered cursor.
- **Main process (Tasks 8–11).** It spawns `firekeep gateway` (toolset `studio`, agent label `<kit id>-studio-panel`) through a small hand-written MCP stdio client that reuses `spawnRuntime`/`terminateProcessTree`. It passes `FIREKEEP_SESSION_REPORT_FILE` to every runtime child and exposes typed IPC only.
- **Renderer (Tasks 12–19).** `App.tsx` is split along the new layout.
- **Smoke, docs and release (Tasks 20–22).**

**Tech Stack:** TypeScript, Electron, React, vitest (+ @testing-library/react with jsdom), electron-vite, electron-builder. No new npm dependency: the MCP client is ~150 lines over the existing `JsonLineDecoder`, so Windows process-tree kill keeps working (the kit's shims are grandchildren of the gateway).

**Spec:** `docs/superpowers/specs/2026-10-11-studio-keep-window-design.md` (commit `0cad46e`), pieces 1 and 2, §4–§11.

**Release order:** plan 1 (server `v1.6.2`) → plan 2 (client `1.8.0`) → **this plan (Studio `0.5.0`)**. This plan's code does not need either release to pass its tests. Against an older server or kit, Studio degrades to "partial" receipts (Task 7 pins this). Do not *publish* 0.5.0 before both prerequisites are published (Task 22 checks).

## Global Constraints

- Runtime ids after the cut: `codex`, `claude`, `kiro`. `RunMode` after the cut: `"primary" | "review" | "handoff"`.
- Saved settings naming `grok` load safely: primary falls back to `null`, reviewer lists drop it, and old Grok turns still render read-only.
- `voiceEnabled` is not part of `StudioPersistedState` any more. Old files that carry it load fine, and the field is dropped on the next save (`#normalizeState` rebuilds the object from `defaults`).
- The renderer gets typed IPC only: no generic "call tool" action. Keep IPC actions are exactly `keep.status`, `keep.retry`, `keep.search`, `keep.skills`, `keep.team`, `keep.highlights`, `keep.feedback` and `keep.receipt.retry`.
- Studio calls only tools in the kit's `studio` preset: `memory_recall`, `memory_feedback`, `skill_list`, `skill_recall`, `relay_who_is_online`, `relay_read`, `ctx_list_sessions`, `replay_timeline`, `corpus_sources`. Studio never starts, resumes or completes a Keep session.
- Panel agent label: `<kit agent id>-studio-panel` (constant suffix `PANEL_AGENT_SUFFIX = "-studio-panel"`). It is set as `FIREKEEP_AGENT_ID` on the panel gateway process, and passed as `agent_id` only on `memory_recall` and `memory_feedback`. Other tools either lack the parameter or, like `ctx_list_sessions`, treat `agent_id` as a filter.
- Report file path: `<userData>/sessions/<studioSessionId>.keep`, passed to every runtime child as `FIREKEEP_SESSION_REPORT_FILE`.
- Gateway call timeout: 10 s. Restart backoff: 1 s, 5 s, 30 s, then manual. A Keep failure never blocks or fails a runtime turn.
- Receipts never show a confident zero they did not measure. The statuses are exactly `measured`, `partial` and `unavailable`.
- UI copy (verbatim):
  - "Keep · recalled N · learned M"
  - "Keep · nothing recalled"
  - "partial — hook-injected recall not shown"
  - "Keep receipt unavailable · retry"
  - "Was this knowledge right?"
  - "Connect Studio to your team's Keep"
  - "I've installed it — retry"
  - "Personal mode — nothing from this session is shared"
  - "Voice was removed in Studio 0.5"
- Shortcuts: ⌘/Ctrl+K opens the command palette. ⌘/Ctrl+\\ toggles the Keep panel. ⌘/Ctrl+⇧+R keeps running review.
- Keep panel becomes a drawer below **1100 px** window width.
- Studio version: **0.5.0**.

## Review Focus

1. **Studio launched on a machine with no kit at all** (the packaged smoke's situation, and many downloaders'). The Keep panel must show "Connect Studio to your team's Keep", chat must work, and no error banner may appear. Pinned by the `KeepGateway` "missing" test (Task 10) and the smoke assertion (Task 20).
2. **Current server (Markdown `replay_timeline`) and current kit (no report file).** This is every user's situation on 0.5.0's release day. Receipts must say "partial", never "nothing recalled". Pinned by `receipt-tracker.test.ts › against today's server and kit` (Task 7).
3. **The gateway dies mid-session**, or a call hangs past 10 s. The turn completes, the receipt says "unavailable · retry", and the panel shows the unreachable state. Pinned by `mcp-stdio-client.test.ts` timeout/exit tests (Task 9) and `keep-gateway.test.ts` backoff test (Task 10).
4. **A report file containing ids from several agent sessions, or repeats, or garbage lines.** The union is de-duplicated, garbage is skipped, and cursors are tracked per id. Pinned by `receipt-tracker.test.ts › unions sessions and skips garbage` (Task 7).
5. **Old settings naming Grok, or carrying `voiceEnabled`.** Studio loads, shows the Start card, and saves a file without those fields. Pinned by `settings-migration.test.ts` (Task 2).

---

## File Structure

**Delete:**
- `src/main/voice-input.ts`, `src/main/permissions.ts`, `src/main/runtime/grok-runtime.ts`, `src/main/electron-secret-store.ts`
- `tests/voice-input.test.ts`, `tests/permissions.test.ts`, `tests/grok-runtime.test.ts`

**Create (core, pure):**
- `src/core/test-command-detect.ts` — §5.3 rules.
- `src/core/keep/receipt.ts` — receipt types, replay-reply parsing, receipt builders and label.
- `src/core/keep/receipt-tracker.ts` — per-Studio-session cursors, the union across Keep sessions, provisional/final.
- `src/shared/keep.ts` — the `KeepStatus` / `KeepItem` IPC types shared by main and renderer.

**Create (main):**
- `src/main/keep/mcp-stdio-client.ts` — minimal MCP (JSON-RPC 2.0, ndjson) client.
- `src/main/keep/keep-gateway.ts` — whoami probe, spawn, preset fallback, backoff, typed Keep operations, `KeepTimelineSource`.
- `src/main/keep/report-file.ts` — read/delete `<id>.keep`.
- `src/main/workspace-detect.ts` — `readdir` adapter for `test-command-detect`.

**Create (renderer):**
- `src/renderer/src/layout/StudioShell.tsx`, `layout/TopBar.tsx`
- `src/renderer/src/rail/SessionRail.tsx`
- `src/renderer/src/composer/Composer.tsx`, `composer/AgentChip.tsx`, `composer/DoneWhenChip.tsx`
- `src/renderer/src/conversation/KeepReceiptCard.tsx`, `conversation/MissionStepper.tsx`
- `src/renderer/src/start/StartCard.tsx`
- `src/renderer/src/keep/KeepPanel.tsx`
- `src/renderer/src/settings/SettingsSheet.tsx`
- `src/renderer/src/palette/CommandPalette.tsx`

**Modify:**
- `src/core/runtime.ts`, `src/core/studio-service.ts`, `src/core/slash-commands.ts`, `src/shared/ipc.ts`
- `src/main/ipc-controller.ts`, `src/main/index.ts`, `src/main/firekeep-client.ts`, `src/main/runtime/{index,codex-runtime,claude-runtime,kiro-runtime}.ts`
- `src/renderer/src/{App.tsx,timeline.ts,styles.css}`
- `scripts/package-smoke.mjs`, `tests/release-workflow.test.ts`
- `README.md`, root `CLAUDE.md`, `package.json`

Run all commands from `studio/`. The per-task test command is `npx vitest run <files>`. Every task ends with `npm run typecheck` passing before commit.

---

## Phase A — Cuts

### Task 1: Remove voice

**Files:**
- Delete: `src/main/voice-input.ts`, `src/main/permissions.ts`, `tests/voice-input.test.ts`, `tests/permissions.test.ts`
- Modify:
  - `src/shared/ipc.ts`: lines 18-22 (`VoiceInputOutcome`), 68-70 (voice actions), 102 (voice result)
  - `src/main/ipc-controller.ts`: lines 7, 21, 57-59, 75-78, 90, 128-132, 177
  - `src/main/index.ts`: lines 19, 22, 196, 203-218, 249
  - `src/core/studio-service.ts`: lines 56, 325-328, 1261, 1279
  - `src/core/runtime.ts`: line 10 (`"audio-input"`) and line 72 (`"audio"` modality)
  - `src/core/slash-commands.ts:547` (`/voice`)
  - `src/renderer/src/App.tsx`: imports 19, 20, 34, 35; and 57, 89, 103, 209-212, 239-242, 321, 327, 423-443, 490, 569, 573, 975
  - `src/renderer/src/styles.css`: 327-328, and the `listen` keyframes at ~423
- Tests:
  - `tests/app.test.tsx`: 12, 33, 77, 90-94, 150-163, 210-217, 219-231
  - `tests/ipc-controller.test.ts`: 7, 10, 14, 25, 44-46, 67-88
  - `tests/studio-service.test.ts:301`
  - `tests/runtime-registry.test.ts:37-38`
  - `tests/slash-commands.test.ts`

**Interfaces:**
- Produces:
  - `StudioController` constructor without the `voiceInput` parameter. Parameter order becomes: service, commands, appVersion, openExternal, selectWorkspace, dashboardUrl, decisionBoards, clipboard, updates.
  - `StudioPersistedState` without `voiceEnabled`.
  - `RUNTIME_CAPABILITIES` without `"audio-input"`.

- [ ] **Step 1: Write the replacement `/voice` test** — in `tests/slash-commands.test.ts`, add:

```ts
it("tells /voice users the feature was removed", async () => {
  const { commands } = createCommands(); // the file's existing factory
  const result = await commands.execute("/voice on");
  expect(result.tone).toBe("warning");
  expect(result.body).toContain("Voice was removed in Studio 0.5");
});
```

If the file's factory is not named `createCommands`, use the one its other tests use.

- [ ] **Step 2: Run to verify it fails**

Run: `npx vitest run tests/slash-commands.test.ts`
Expected: FAIL — `/voice on` currently toggles the voice setting.

- [ ] **Step 3: Delete and edit.**
  1. Delete the four files listed above.
  2. Remove every line listed under **Files** (each is voice-only, except as noted below).
  3. Replace the `/voice` registration at `slash-commands.ts:547` with:

```ts
    {
      name: "voice",
      summary: "Removed in Studio 0.5",
      usages: ["/voice"],
      execute: async () => ({ kind: "notice", title: "Voice", tone: "warning", body: "Voice was removed in Studio 0.5. Dictate with your operating system's voice typing instead." }),
    },
```

  4. In `App.tsx`:
     - delete `stripMarkdown` (975); it is used only by speech;
     - in the theme effect (239-242), keep the theme line and drop `voiceEnabledRef`;
     - in the composer hint (573), drop the voice branches and `" · voice replies on"`.
  5. Remove `"audio-input"` from `RUNTIME_CAPABILITIES`, and `"audio"` from `inputModalities`. Fix any runtime descriptor that declared them (grep `audio-input`).

- [ ] **Step 4: Run the suite**

Run: `npx vitest run && npm run typecheck`
Expected: PASS. Fix stale imports the compiler reports. `grep -rn "voice\|Voice\|speechSynthesis\|microphone" src tests` should return only the `/voice` removal notice and its test.

- [ ] **Step 5: Commit**

```bash
git add -A src tests
git commit -m "feat(studio)!: remove voice input and spoken replies"
```

### Task 2: Remove the Grok runtime and pin settings migration

**Files:**
- Delete: `src/main/runtime/grok-runtime.ts`, `tests/grok-runtime.test.ts`, `src/main/electron-secret-store.ts`
- Modify:
  - `src/main/runtime/index.ts` (whole file)
  - `src/main/index.ts:10, 105, 115`
  - `src/core/settings-store.ts:50-155`: keep only what `tests/settings-store.test.ts` exercises outside secrets. If the secret-store classes have no remaining users, delete them and their tests at `tests/settings-store.test.ts:49-72`.
- Create: `tests/settings-migration.test.ts`

**Interfaces:**
- Produces: `createRuntimeRegistry(appVersion: string): RuntimeRegistry`, which no longer takes a `secrets` parameter.
- **Keep** the connect dialog's API-key input (`App.tsx:609`, state 88, `openConnect` 406-410, `connect` 412-421) and IPC `runtime.login{secret}`: Codex uses them (`codex-runtime.ts:67, 102-106`).

- [ ] **Step 1: Write the failing migration tests** — create `tests/settings-migration.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { RuntimeRegistry } from "../src/core/runtime-registry.js";
import { MemorySettingsStore } from "../src/core/settings-store.js";
import { StudioService, type StudioPersistedState } from "../src/core/studio-service.js";
import { FakeRuntime } from "./helpers/fake-runtime.js";

function legacyState(): Record<string, unknown> {
  return {
    version: 1,
    activeSessionId: "session-legacy",
    workspacePath: "C:\\work",
    primaryRuntimeId: "grok",
    reviewerRuntimeIds: ["grok", "claude"],
    reviewerMode: "after-turn",
    selectedModels: { grok: "grok-4" },
    selectedEfforts: {},
    permissionModes: {},
    nativeSessionIds: {},
    tokenBudget: null,
    voiceEnabled: true,
    theme: "dark",
  };
}

describe("0.5 settings migration", () => {
  it("drops grok and voiceEnabled without crashing", async () => {
    const settings = new MemorySettingsStore<StudioPersistedState>(legacyState() as unknown as StudioPersistedState);
    const claude = new FakeRuntime({ id: "claude", displayName: "Claude", description: "", transport: "test", capabilities: ["chat", "review"] });
    const service = new StudioService({ runtimes: new RuntimeRegistry([claude]), settings });
    await service.initialize();
    const snapshot = service.snapshot();
    expect(snapshot.primaryRuntimeId).toBeNull();
    expect(snapshot.reviewerRuntimeIds).toEqual(["claude"]);
    expect(snapshot.theme).toBe("dark");
    expect("voiceEnabled" in snapshot).toBe(false);
  });

  it("registers exactly codex, claude and kiro", async () => {
    const { createRuntimeRegistry } = await import("../src/main/runtime/index.js");
    const ids = createRuntimeRegistry("0.5.0").list().map((runtime) => runtime.descriptor.id).sort();
    expect(ids).toEqual(["claude", "codex", "kiro"]);
  });
});
```

`MemorySettingsStore`'s constructor may not take an initial value. If it does not, call `await settings.save(legacyState() as never)` before constructing the service. If `snapshot()` has another name in `StudioService`, use the method `ipc-controller.ts` calls to build `{type:"state", snapshot}`.

- [ ] **Step 2: Run to verify it fails**

Run: `npx vitest run tests/settings-migration.test.ts`
Expected: the first test FAILS on `voiceEnabled` only if Task 1 is incomplete; otherwise it may already PASS (it pins migration behaviour). The second FAILS: `createRuntimeRegistry` requires `secrets` and lists `grok`.

- [ ] **Step 3: Implement.** Replace `src/main/runtime/index.ts` with:

```ts
import { RuntimeRegistry } from "../../core/runtime-registry.js";
import { ClaudeRuntime } from "./claude-runtime.js";
import { CodexRuntime } from "./codex-runtime.js";
import { KiroRuntime } from "./kiro-runtime.js";

export function createRuntimeRegistry(appVersion: string): RuntimeRegistry {
  return new RuntimeRegistry([
    new CodexRuntime({ appVersion }),
    new ClaudeRuntime(),
    new KiroRuntime({ appVersion }),
  ]);
}
```

In `src/main/index.ts`:
- delete the `ElectronSecretStore` import (10) and construction (105);
- call `createRuntimeRegistry(app.getVersion())` (115).

Delete the Grok and secret-store files.

- [ ] **Step 4: Run**

Run: `npx vitest run && npm run typecheck`
Expected: PASS. `grep -rni "grok\|safeStorage\|secrets.json" src tests` returns nothing except `settings-migration.test.ts`.

- [ ] **Step 5: Commit**

```bash
git add -A src tests
git commit -m "feat(studio)!: remove the Grok runtime; Studio stores no secrets"
```

### Task 3: Remove `/compare` and `/consensus`

**Files:**
- Modify:
  - `src/core/slash-commands.ts`: 128, 132-134, 406-430, 575
  - `src/core/studio-service.ts`: 730-779, 1114
  - `src/core/runtime.ts:21`
  - `src/main/runtime/codex-runtime.ts:259`, `kiro-runtime.ts:171`, `claude-runtime.ts:221`
  - `src/renderer/src/App.tsx:775`
- Tests:
  - `tests/slash-commands.test.ts:69, 117-128`
  - `tests/studio-service.test.ts:326-356`
  - `tests/studio-service.test.ts:358-371`: keep the `handoff` assertions at 367-370 as their own `it(...)`

**Interfaces:**
- Produces: `type RunMode = "primary" | "review" | "handoff"`. Review runs still force `"safe"` permission.

- [ ] **Step 1: Write the failing test** — in `tests/slash-commands.test.ts`:

```ts
it("no longer offers /compare or /consensus", async () => {
  const { commands } = createCommands();
  const names = (await commands.complete("/c")).map((item) => item.value);
  expect(names).not.toContain("/compare");
  expect(names).not.toContain("/consensus");
});
```

- [ ] **Step 2: Run to verify it fails.** Run: `npx vitest run tests/slash-commands.test.ts`. Expected: FAIL.

- [ ] **Step 3: Implement.**
  1. Delete the registrations, the completion branch, `"consensus"` from `firstRuntimeCommands`, `compare()`, `synthesize()` and the Welcome quick command.
  2. Change the `/budget` summary text at 575 to drop "compare, and synthesis runs".
  3. Change `RunMode`, and `#run`'s permission line to `mode === "review" ? "safe" : …`.
  4. In each runtime, delete the `mode === "compare"` branch. In Codex and Claude, the branch also covered `"review"`: keep the `review` arm (`request.mode === "review"`), so reviews stay ephemeral.

- [ ] **Step 4: Run.** Run: `npx vitest run && npm run typecheck`. Expected: PASS. `grep -rn "compare\|consensus\|synthes" src` returns only unrelated words.

- [ ] **Step 5: Commit**

```bash
git add -A src tests
git commit -m "feat(studio)!: remove /compare and /consensus"
```

### Task 4: Remove the Agents grid

**Files:**
- Modify:
  - `src/renderer/src/App.tsx`: imports 18, 21, 25; and 56, 93-97, 279-302, 313-315, 320, 327, 340-341, 473-476, 481, 537-543, 555, 561, 819-862
  - `src/renderer/src/styles.css`: 181-185, 293-310
  - `src/shared/ipc.ts:41` (`message.sendTo`)
  - `src/main/ipc-controller.ts`: 30, 143
  - `src/core/studio-service.ts:706-712` (`sendMessageTo`)
- Tests:
  - `tests/app.test.tsx:511-527`
  - `tests/studio-service.test.ts:54-69`
  - `tests/ipc-controller.test.ts:32-33`
- Smoke: `scripts/package-smoke.mjs:38, 75-82, 103, 358-450` (grid functions and their call)

**Interfaces:**
- Produces: ⌘\\ is unbound until Task 12 binds it to the Keep panel. `StudioView` is removed.

- [ ] **Step 1: Write the failing test** — in `tests/ipc-controller.test.ts`:

```ts
it("rejects the removed message.sendTo action", () => {
  expect(() => parseStudioAction({ type: "message.sendTo", runtimeId: "alpha", prompt: "x" })).toThrow();
});
```

- [ ] **Step 2: Run to verify it fails.** Expected: FAIL (the action still parses).

- [ ] **Step 3: Implement.** Delete everything listed. In `package-smoke.mjs`, delete the `agentGrid` variable, the open/assert/close calls, `agentGrid` in the JSON output, and the three grid functions. `tests/release-workflow.test.ts:44-53` asserts strings in `package-smoke.mjs`; if one of them is a grid string, delete that assertion now (Task 20 adds the new ones).

- [ ] **Step 4: Run.** Run: `npx vitest run && npm run typecheck`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A src tests scripts
git commit -m "feat(studio)!: remove the Agents tiled grid"
```

---

## Phase B — Pure core logic

### Task 5: Test-command detection

**Files:**
- Create: `src/core/test-command-detect.ts`, `tests/test-command-detect.test.ts`

**Interfaces:**
- Produces: `interface WorkspaceEntry { readonly name: string; readonly isDirectory: boolean }` and `function detectTestCommand(entries: readonly WorkspaceEntry[]): string | null`. Used by Task 11 (`workspace.detectTest`) and Task 15 (chip).

- [ ] **Step 1: Write the failing tests**

```ts
import { describe, expect, it } from "vitest";
import { detectTestCommand, type WorkspaceEntry } from "../src/core/test-command-detect.js";

const f = (name: string): WorkspaceEntry => ({ name, isDirectory: false });
const d = (name: string): WorkspaceEntry => ({ name, isDirectory: true });

describe("detectTestCommand", () => {
  it.each([
    [[f("package.json")], "npm test"],
    [[f("pyproject.toml")], "pytest"],
    [[f("pytest.ini")], "pytest"],
    [[d("tests")], "pytest"],
    [[f("Cargo.toml")], "cargo test"],
    [[f("go.mod")], "go test ./..."],
  ])("%j → %s", (entries, expected) => {
    expect(detectTestCommand(entries)).toBe(expected);
  });

  it("prefers package.json over python markers (spec order)", () => {
    expect(detectTestCommand([f("pyproject.toml"), f("package.json")])).toBe("npm test");
  });

  it("does not treat a FILE named tests as a test directory", () => {
    expect(detectTestCommand([f("tests")])).toBeNull();
  });

  it("returns null for an empty or unknown workspace", () => {
    expect(detectTestCommand([])).toBeNull();
    expect(detectTestCommand([f("README.md"), d("src")])).toBeNull();
  });
});
```

- [ ] **Step 2: Run to verify they fail.** Run: `npx vitest run tests/test-command-detect.test.ts`. Expected: FAIL (module not found).

- [ ] **Step 3: Implement** `src/core/test-command-detect.ts`:

```ts
export interface WorkspaceEntry {
  readonly name: string;
  readonly isDirectory: boolean;
}

// Spec §5.3, in this order: the first rule that matches proposes the command.
const RULES: ReadonlyArray<{ readonly matches: (entry: WorkspaceEntry) => boolean; readonly command: string }> = [
  { matches: (entry) => !entry.isDirectory && entry.name === "package.json", command: "npm test" },
  { matches: (entry) => (!entry.isDirectory && (entry.name === "pyproject.toml" || entry.name === "pytest.ini")) || (entry.isDirectory && entry.name === "tests"), command: "pytest" },
  { matches: (entry) => !entry.isDirectory && entry.name === "Cargo.toml", command: "cargo test" },
  { matches: (entry) => !entry.isDirectory && entry.name === "go.mod", command: "go test ./..." },
];

/** Propose one verification command from a workspace's top-level entries, or null. */
export function detectTestCommand(entries: readonly WorkspaceEntry[]): string | null {
  for (const rule of RULES) if (entries.some(rule.matches)) return rule.command;
  return null;
}
```

- [ ] **Step 4: Run.** Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/core/test-command-detect.ts tests/test-command-detect.test.ts
git commit -m "feat(studio): test-command detection for Done when…"
```

### Task 6: Receipt model, replay-reply parsing, builders

**Files:**
- Create: `src/core/keep/receipt.ts`, `tests/keep-receipt.test.ts`
- Modify: `src/core/runtime.ts` (add the `keep.receipt` payload)

**Interfaces:**
- Produces, from `src/core/keep/receipt.ts`:
  - `PANEL_AGENT_SUFFIX = "-studio-panel"`
  - `interface ReplayEvent { id: string; agent_id: string; event_type: string; timestamp: string; payload: Readonly<Record<string, unknown>> }`
  - `type TimelinePage = { kind: "page"; events: readonly ReplayEvent[]; total: number; hasMore: boolean } | { kind: "unsupported" } | { kind: "error"; message: string }`
  - `parseTimelineReply(text: string): TimelinePage`
  - `interface ReceiptRecall { query: string; trigger: "prompt-hook" | "agent"; resultCount: number; memoryIds: readonly string[] }`
  - `interface ReceiptWrite { summary: string; memoryId: string }`
  - `interface KeepReceipt { status: "measured" | "partial" | "unavailable"; final: boolean; recalls: readonly ReceiptRecall[]; writes: readonly ReceiptWrite[]; recalledCount: number; learnedCount: number; keepSessionIds: readonly string[]; cursorsAfter: Readonly<Record<string, number>>; detail?: string }`
  - `measuredReceipt(events: readonly ReplayEvent[], keepSessionIds: readonly string[], cursorsAfter: Readonly<Record<string, number>>, final: boolean): KeepReceipt`
  - `partialReceipt(runEvents: readonly RuntimeEventPayload[], final: boolean): KeepReceipt`
  - `unavailableReceipt(message: string, final: boolean, cursors: Readonly<Record<string, number>>): KeepReceipt`
  - `receiptLabel(receipt: KeepReceipt): string`
- Produces, in `src/core/runtime.ts`: `RuntimeEventPayload` gains `{ readonly kind: "keep.receipt"; readonly receipt: KeepReceipt }`.

- [ ] **Step 1: Write the failing tests** — `tests/keep-receipt.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { measuredReceipt, parseTimelineReply, partialReceipt, receiptLabel, unavailableReceipt, type ReplayEvent } from "../src/core/keep/receipt.js";

const read = (id: string, agent: string, ids: string[], trigger: string | null = null, query = "q"): ReplayEvent => ({
  id, agent_id: agent, event_type: "memory_read", timestamp: "t",
  payload: { query, trigger, result_count: ids.length, memory_ids: ids },
});
const write = (id: string, agent: string, summary: string): ReplayEvent => ({
  id, agent_id: agent, event_type: "memory_write", timestamp: "t",
  payload: { action_summary: summary, memory_id: `m-${id}` },
});

describe("parseTimelineReply", () => {
  it("parses the server's JSON format", () => {
    const page = parseTimelineReply(JSON.stringify({ session_id: "s", events: [read("e1", "a", ["m1"])], total: 1, has_more: false }));
    expect(page).toEqual({ kind: "page", events: [read("e1", "a", ["m1"])], total: 1, hasMore: false });
  });
  it("reads today's Markdown reply as an old server", () => {
    expect(parseTimelineReply("## Trace Timeline — s (2 events)\n- `t` **memory_read**")).toEqual({ kind: "unsupported" });
    expect(parseTimelineReply("No trace events found for session s")).toEqual({ kind: "unsupported" });
  });
  it("reads a JSON error as an error, not an old server", () => {
    expect(parseTimelineReply(JSON.stringify({ error: "403 forbidden" }))).toEqual({ kind: "error", message: "403 forbidden" });
  });
  it("reads an argument-validation failure (old server, unknown param) as unsupported", () => {
    expect(parseTimelineReply("Error executing tool replay_timeline: 1 validation error … format")).toEqual({ kind: "unsupported" });
  });
});

describe("measuredReceipt", () => {
  it("counts distinct memory ids and writes, and drops panel events", () => {
    const receipt = measuredReceipt([
      read("e1", "agent-a", ["m1", "m2"], "prompt-hook", "deploy"),
      read("e2", "agent-a", ["m2", "m3"]),
      read("e3", "agent-a-studio-panel", ["m9"]),
      write("e4", "agent-a", "fixed the tunnel"),
      { id: "e5", agent_id: "agent-a", event_type: "ctx_update", timestamp: "t", payload: {} },
    ], ["s1"], { s1: 5 }, true);
    expect(receipt.status).toBe("measured");
    expect(receipt.recalledCount).toBe(3);
    expect(receipt.learnedCount).toBe(1);
    expect(receipt.recalls.map((recall) => recall.trigger)).toEqual(["prompt-hook", "agent"]);
    expect(receipt.recalls[0]?.query).toBe("deploy");
    expect(receipt.writes).toEqual([{ summary: "fixed the tunnel", memoryId: "m-e4" }]);
    expect(receiptLabel(receipt)).toBe("Keep · recalled 3 · learned 1");
  });
  it("says nothing recalled only when it measured nothing", () => {
    expect(receiptLabel(measuredReceipt([], ["s1"], { s1: 0 }, true))).toBe("Keep · nothing recalled");
  });
});

describe("partialReceipt", () => {
  it("counts visible Firekeep tool calls and is labelled partial", () => {
    const receipt = partialReceipt([
      { kind: "tool.started", toolCallId: "1", name: "mcp__firekeep__memory_recall", input: { task: "vps" } },
      { kind: "tool.started", toolCallId: "2", name: "memory_learn", input: { action: "x" } },
      { kind: "tool.started", toolCallId: "3", name: "Read", input: {} },
    ], true);
    expect(receipt.status).toBe("partial");
    expect(receipt.recalledCount).toBe(1);
    expect(receipt.learnedCount).toBe(1);
    expect(receipt.recalls[0]?.query).toBe("vps");
    expect(receiptLabel(receipt)).toBe("Keep · recalled 1 · learned 1 · partial — hook-injected recall not shown");
  });
  it("never says nothing recalled", () => {
    expect(receiptLabel(partialReceipt([], true))).toBe("Keep · partial — hook-injected recall not shown");
  });
});

it("unavailable receipts never show a count", () => {
  expect(receiptLabel(unavailableReceipt("timeout", true, {}))).toBe("Keep receipt unavailable · retry");
});
```

- [ ] **Step 2: Run to verify they fail.** Expected: FAIL (module not found).

- [ ] **Step 3: Implement** `src/core/keep/receipt.ts`:

```ts
import type { RuntimeEventPayload } from "../runtime.js";

/** Agent label of Studio's own Keep-panel gateway (spec §7.2). */
export const PANEL_AGENT_SUFFIX = "-studio-panel";

export interface ReplayEvent {
  readonly id: string;
  readonly agent_id: string;
  readonly event_type: string;
  readonly timestamp: string;
  readonly payload: Readonly<Record<string, unknown>>;
}

export type TimelinePage =
  | { readonly kind: "page"; readonly events: readonly ReplayEvent[]; readonly total: number; readonly hasMore: boolean }
  | { readonly kind: "unsupported" }
  | { readonly kind: "error"; readonly message: string };

export interface ReceiptRecall {
  readonly query: string;
  readonly trigger: "prompt-hook" | "agent";
  readonly resultCount: number;
  readonly memoryIds: readonly string[];
}

export interface ReceiptWrite {
  readonly summary: string;
  readonly memoryId: string;
}

export interface KeepReceipt {
  readonly status: "measured" | "partial" | "unavailable";
  readonly final: boolean;
  readonly recalls: readonly ReceiptRecall[];
  readonly writes: readonly ReceiptWrite[];
  readonly recalledCount: number;
  readonly learnedCount: number;
  readonly keepSessionIds: readonly string[];
  readonly cursorsAfter: Readonly<Record<string, number>>;
  readonly detail?: string;
}

const PARTIAL_NOTE = "partial — hook-injected recall not shown";

/**
 * replay_timeline(format="json") returns the REST TimelineResponse; an older
 * server returns Markdown (or rejects the unknown argument). Anything that is
 * not JSON with an `events` array or an `error` string is an old server.
 */
export function parseTimelineReply(text: string): TimelinePage {
  let value: unknown;
  try { value = JSON.parse(text); } catch { return { kind: "unsupported" }; }
  if (!value || typeof value !== "object") return { kind: "unsupported" };
  const record = value as Record<string, unknown>;
  if (typeof record.error === "string") return { kind: "error", message: record.error };
  if (!Array.isArray(record.events)) return { kind: "unsupported" };
  const events = record.events.filter(isReplayEvent);
  return { kind: "page", events, total: typeof record.total === "number" ? record.total : events.length, hasMore: record.has_more === true };
}

export function measuredReceipt(events: readonly ReplayEvent[], keepSessionIds: readonly string[], cursorsAfter: Readonly<Record<string, number>>, final: boolean): KeepReceipt {
  const own = events.filter((event) => !event.agent_id.endsWith(PANEL_AGENT_SUFFIX));
  const recalls: ReceiptRecall[] = own.filter((event) => event.event_type === "memory_read").map((event) => ({
    query: stringField(event.payload.query),
    trigger: event.payload.trigger === "prompt-hook" ? "prompt-hook" : "agent",
    resultCount: typeof event.payload.result_count === "number" ? event.payload.result_count : 0,
    memoryIds: Array.isArray(event.payload.memory_ids) ? event.payload.memory_ids.filter((id): id is string => typeof id === "string") : [],
  }));
  const writes: ReceiptWrite[] = own.filter((event) => event.event_type === "memory_write").map((event) => ({
    summary: stringField(event.payload.action_summary),
    memoryId: stringField(event.payload.memory_id),
  }));
  const distinct = new Set(recalls.flatMap((recall) => recall.memoryIds));
  return { status: "measured", final, recalls, writes, recalledCount: distinct.size, learnedCount: writes.length, keepSessionIds: [...keepSessionIds], cursorsAfter: { ...cursorsAfter } };
}

/** Fallback: Firekeep tool calls visible in the runtime stream (spec §7.3). */
export function partialReceipt(runEvents: readonly RuntimeEventPayload[], final: boolean): KeepReceipt {
  const calls = runEvents.filter((event): event is Extract<RuntimeEventPayload, { kind: "tool.started" }> => event.kind === "tool.started");
  const recalls: ReceiptRecall[] = calls.filter((call) => toolBase(call.name) === "memory_recall").map((call) => ({
    query: stringField((call.input as Record<string, unknown> | undefined)?.task), trigger: "agent", resultCount: 0, memoryIds: [],
  }));
  const writes: ReceiptWrite[] = calls.filter((call) => toolBase(call.name) === "memory_learn").map((call) => ({
    summary: stringField((call.input as Record<string, unknown> | undefined)?.action), memoryId: "",
  }));
  return { status: "partial", final, recalls, writes, recalledCount: recalls.length, learnedCount: writes.length, keepSessionIds: [], cursorsAfter: {}, detail: PARTIAL_NOTE };
}

export function unavailableReceipt(message: string, final: boolean, cursors: Readonly<Record<string, number>>): KeepReceipt {
  return { status: "unavailable", final, recalls: [], writes: [], recalledCount: 0, learnedCount: 0, keepSessionIds: [], cursorsAfter: { ...cursors }, detail: message };
}

export function receiptLabel(receipt: KeepReceipt): string {
  if (receipt.status === "unavailable") return "Keep receipt unavailable · retry";
  const counts = receipt.recalledCount || receipt.learnedCount
    ? `recalled ${receipt.recalledCount} · learned ${receipt.learnedCount}`
    : "";
  if (receipt.status === "partial") return counts ? `Keep · ${counts} · ${PARTIAL_NOTE}` : `Keep · ${PARTIAL_NOTE}`;
  return counts ? `Keep · ${counts}` : "Keep · nothing recalled";
}

function toolBase(name: string): string {
  // Claude: mcp__firekeep__memory_recall; Codex/Kiro may report the bare name or server.tool.
  return name.split(/__|\./).pop() ?? name;
}

function stringField(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function isReplayEvent(value: unknown): value is ReplayEvent {
  if (!value || typeof value !== "object") return false;
  const event = value as Record<string, unknown>;
  return typeof event.id === "string" && typeof event.agent_id === "string" && typeof event.event_type === "string"
    && typeof event.payload === "object" && event.payload !== null;
}
```

In `src/core/runtime.ts`, add `import type { KeepReceipt } from "./keep/receipt.js";` and the union member `| { readonly kind: "keep.receipt"; readonly receipt: KeepReceipt }`. Runtimes never emit it; `StudioService` does (Task 8).

- [ ] **Step 4: Run.** Run: `npx vitest run tests/keep-receipt.test.ts && npm run typecheck`. Expected: PASS. If the exhaustiveness check in `timeline.ts` complains, that is fixed in Task 14. Until then, add a no-op `else if (payload.kind === "keep.receipt") { /* rendered in Task 14 */ }` branch in `buildTimeline`.

- [ ] **Step 5: Commit**

```bash
git add src/core/keep/receipt.ts src/core/runtime.ts src/renderer/src/timeline.ts tests/keep-receipt.test.ts
git commit -m "feat(studio): Keep receipt model and replay reply parsing"
```

### Task 7: Receipt tracker (cursor, union, provisional/final, compatibility)

**Files:**
- Create: `src/core/keep/receipt-tracker.ts`, `tests/receipt-tracker.test.ts`

**Interfaces:**
- Consumes: Task 6.
- Produces:
  - `interface KeepTimelineSource { timeline(keepSessionId: string, offset: number, limit: number): Promise<TimelinePage> }`
  - `interface ReceiptTrackerDeps { readonly source: () => KeepTimelineSource | null; readonly readReport: (studioSessionId: string) => Promise<readonly string[]> }`
  - `class ReceiptTracker { constructor(deps: ReceiptTrackerDeps); restore(studioSessionId: string, cursors: Readonly<Record<string, number>>): void; commit(studioSessionId: string, cursors: Readonly<Record<string, number>>): void; cursors(studioSessionId: string): Readonly<Record<string, number>>; collect(studioSessionId: string, runEvents: readonly RuntimeEventPayload[], final: boolean): Promise<KeepReceipt>; forget(studioSessionId: string): void }`
  - `commit` is used when a final collect times out: the provisional receipt is promoted, and its `cursorsAfter` committed, so the cursor still advances and nothing is counted twice. `cursors` returns the committed cursors (for retry).
  - Constants `PAGE_LIMIT = 200`, `MAX_PAGES = 10`.
- Semantics:
  - The cursor counts **all** events of a Keep session (the fetch has no `event_type` filter, so offsets stay stable).
  - `collect(…, false)` never commits cursors. `collect(…, true)` commits `cursorsAfter`.
  - A page `unsupported` from any session yields a partial receipt.
  - A page `error` yields unavailable.
  - An empty report file yields partial.

- [ ] **Step 1: Write the failing tests** — `tests/receipt-tracker.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { ReceiptTracker, type KeepTimelineSource } from "../src/core/keep/receipt-tracker.js";
import type { ReplayEvent, TimelinePage } from "../src/core/keep/receipt.js";

const read = (id: string, ids: string[], agent = "agent-a"): ReplayEvent => ({ id, agent_id: agent, event_type: "memory_read", timestamp: "t", payload: { query: id, trigger: null, result_count: ids.length, memory_ids: ids } });

function source(sessions: Record<string, ReplayEvent[]>): KeepTimelineSource & { calls: Array<[string, number]> } {
  const calls: Array<[string, number]> = [];
  return {
    calls,
    async timeline(id, offset, limit): Promise<TimelinePage> {
      calls.push([id, offset]);
      const all = sessions[id] ?? [];
      const events = all.slice(offset, offset + limit);
      return { kind: "page", events, total: all.length, hasMore: offset + limit < all.length };
    },
  };
}

describe("ReceiptTracker", () => {
  it("assigns events between two sends to one turn (late writes included)", async () => {
    const events: Record<string, ReplayEvent[]> = { s1: [read("e1", ["m1"])] };
    const src = source(events);
    const tracker = new ReceiptTracker({ source: () => src, readReport: async () => ["s1"] });
    const provisional = await tracker.collect("studio-1", [], false);
    expect(provisional.recalledCount).toBe(1);
    events.s1!.push(read("late", ["m2"]));            // stop-hook learn lands after the answer
    const final = await tracker.collect("studio-1", [], true);
    expect(final.recalledCount).toBe(2);
    expect(final.cursorsAfter).toEqual({ s1: 2 });
    events.s1!.push(read("e3", ["m3"]));
    const next = await tracker.collect("studio-1", [], true);
    expect(next.recalledCount).toBe(1);                // cursor advanced
  });

  it("unions sessions and skips garbage", async () => {
    const src = source({ s1: [read("a", ["m1"])], s2: [read("b", ["m1", "m2"])] });
    const tracker = new ReceiptTracker({ source: () => src, readReport: async () => ["s1", "s2", "s1", ""] });
    const receipt = await tracker.collect("studio-1", [], true);
    expect(receipt.keepSessionIds).toEqual(["s1", "s2"]);
    expect(receipt.recalledCount).toBe(2);
  });

  it("pages until has_more is false, bounded by MAX_PAGES", async () => {
    const many = Array.from({ length: 450 }, (_, i) => read(`e${i}`, [`m${i}`]));
    const src = source({ s1: many });
    const tracker = new ReceiptTracker({ source: () => src, readReport: async () => ["s1"] });
    const receipt = await tracker.collect("studio-1", [], true);
    expect(src.calls.map(([, offset]) => offset)).toEqual([0, 200, 400]);
    expect(receipt.cursorsAfter).toEqual({ s1: 450 });
  });

  it("commit() advances cursors without a fetch (promoted provisional receipt)", async () => {
    const src = source({ s1: [read("a", ["m1"]), read("b", ["m2"])] });
    const tracker = new ReceiptTracker({ source: () => src, readReport: async () => ["s1"] });
    tracker.commit("studio-1", { s1: 1 });
    expect(tracker.cursors("studio-1")).toEqual({ s1: 1 });
    expect((await tracker.collect("studio-1", [], true)).recalledCount).toBe(1);
  });

  it("restores cursors from a previous final receipt", async () => {
    const src = source({ s1: [read("old", ["m1"]), read("new", ["m2"])] });
    const tracker = new ReceiptTracker({ source: () => src, readReport: async () => ["s1"] });
    tracker.restore("studio-1", { s1: 1 });
    expect((await tracker.collect("studio-1", [], true)).recalledCount).toBe(1);
  });

  it("an error from the Keep is unavailable, and does not move the cursor", async () => {
    const tracker = new ReceiptTracker({ source: () => ({ timeline: async () => ({ kind: "error", message: "403" }) }), readReport: async () => ["s1"] });
    tracker.restore("studio-1", { s1: 7 });
    const receipt = await tracker.collect("studio-1", [], true);
    expect(receipt.status).toBe("unavailable");
    expect(receipt.cursorsAfter).toEqual({ s1: 7 });
  });

  describe("against today's server and kit (release-day compatibility, Review Focus #2)", () => {
    const toolEvents = [{ kind: "tool.started" as const, toolCallId: "1", name: "mcp__firekeep__memory_recall", input: { task: "vps" } }];

    it("old kit: no report file → partial from the tool stream, never 'nothing recalled'", async () => {
      const tracker = new ReceiptTracker({ source: () => source({}), readReport: async () => [] });
      const receipt = await tracker.collect("studio-1", toolEvents, true);
      expect(receipt.status).toBe("partial");
      expect(receipt.recalledCount).toBe(1);
    });

    it("old server: Markdown replay_timeline → partial", async () => {
      const tracker = new ReceiptTracker({ source: () => ({ timeline: async () => ({ kind: "unsupported" }) }), readReport: async () => ["s1"] });
      expect((await tracker.collect("studio-1", toolEvents, true)).status).toBe("partial");
    });

    it("no gateway at all → partial", async () => {
      const tracker = new ReceiptTracker({ source: () => null, readReport: async () => ["s1"] });
      expect((await tracker.collect("studio-1", [], true)).status).toBe("partial");
    });
  });
});
```

- [ ] **Step 2: Run to verify they fail.** Expected: FAIL (module not found).

- [ ] **Step 3: Implement** `src/core/keep/receipt-tracker.ts`:

```ts
import type { RuntimeEventPayload } from "../runtime.js";
import { measuredReceipt, partialReceipt, unavailableReceipt, type KeepReceipt, type ReplayEvent, type TimelinePage } from "./receipt.js";

export interface KeepTimelineSource {
  timeline(keepSessionId: string, offset: number, limit: number): Promise<TimelinePage>;
}

export interface ReceiptTrackerDeps {
  /** The live gateway, or null while the Keep is unavailable. */
  readonly source: () => KeepTimelineSource | null;
  /** Keep session ids the agents reported for this Studio session (kit 1.8 report file). */
  readonly readReport: (studioSessionId: string) => Promise<readonly string[]>;
}

export const PAGE_LIMIT = 200;
export const MAX_PAGES = 10;
const SESSION_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

/**
 * Turns Keep replay timelines into per-turn receipts (spec §7.3). Cursors are
 * event counts per Keep session, i.e. server ordering — Studio never compares
 * its clock with the Keep's. A provisional collect (turn just ended) never
 * commits; the final collect (made just before the next prompt leaves)
 * commits, so late writes land on the turn that caused them.
 */
export class ReceiptTracker {
  readonly #deps: ReceiptTrackerDeps;
  readonly #cursors = new Map<string, Record<string, number>>();

  constructor(deps: ReceiptTrackerDeps) {
    this.#deps = deps;
  }

  restore(studioSessionId: string, cursors: Readonly<Record<string, number>>): void {
    this.#cursors.set(studioSessionId, { ...cursors });
  }

  commit(studioSessionId: string, cursors: Readonly<Record<string, number>>): void {
    this.#cursors.set(studioSessionId, { ...cursors });
  }

  cursors(studioSessionId: string): Readonly<Record<string, number>> {
    return { ...(this.#cursors.get(studioSessionId) ?? {}) };
  }

  forget(studioSessionId: string): void {
    this.#cursors.delete(studioSessionId);
  }

  async collect(studioSessionId: string, runEvents: readonly RuntimeEventPayload[], final: boolean): Promise<KeepReceipt> {
    const committed = this.#cursors.get(studioSessionId) ?? {};
    const ids = unique((await this.#deps.readReport(studioSessionId).catch(() => [])).filter((id) => SESSION_ID.test(id)));
    const source = this.#deps.source();
    if (ids.length === 0 || !source) return partialReceipt(runEvents, final);

    const events: ReplayEvent[] = [];
    const after: Record<string, number> = { ...committed };
    for (const id of ids) {
      let offset = committed[id] ?? 0;
      for (let page = 0; page < MAX_PAGES; page += 1) {
        let reply: TimelinePage;
        try { reply = await source.timeline(id, offset, PAGE_LIMIT); }
        catch (error) { return unavailableReceipt(message(error), final, committed); }
        if (reply.kind === "unsupported") return partialReceipt(runEvents, final);
        if (reply.kind === "error") return unavailableReceipt(reply.message, final, committed);
        events.push(...reply.events);
        offset += reply.events.length;
        if (!reply.hasMore || reply.events.length === 0) break;
      }
      after[id] = offset;
    }
    if (final) this.#cursors.set(studioSessionId, after);
    return measuredReceipt(events, ids, after, final);
  }
}

function unique(values: readonly string[]): string[] {
  return [...new Set(values)];
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
```

- [ ] **Step 4: Run.** Run: `npx vitest run tests/receipt-tracker.test.ts tests/keep-receipt.test.ts`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/core/keep/receipt-tracker.ts tests/receipt-tracker.test.ts
git commit -m "feat(studio): receipt tracker — server-ordered cursors, union, old-server/old-kit fallback"
```

---

## Phase C — Main process and service integration

### Task 8: Per-run env, report file, receipts in `StudioService`

**Files:**
- Modify:
  - `src/core/runtime.ts` (`RunRequest.env`)
  - `src/main/runtime/codex-runtime.ts`: 24, 43, 53-56, 109, 187, 286
  - `src/main/runtime/claude-runtime.ts`: 25, 38, 45, 113
  - `src/main/runtime/kiro-runtime.ts`: 32, 45, 54, 139, 279-294
  - `src/core/studio-service.ts`: options 87-96, `#run` 1086+, `sendMessage` 692-704, `runMission` 559, `runReview` 714, `deleteSession` 375-381, `initialize`/`resumeSession`
- Create: `src/main/keep/report-file.ts`
- Tests:
  - `tests/studio-service.test.ts`
  - `tests/claude-runtime.test.ts`, `tests/codex-runtime.test.ts`, `tests/kiro-runtime.test.ts`
  - new `tests/report-file.test.ts`

**Interfaces:**
- Produces:
  - `RunRequest.env?: Readonly<Record<string, string>>`, merged over `process.env` by every runtime's default factory.
    - Codex: `peerFactory?: (env?: Readonly<Record<string, string>>) => RpcPeer`
    - Claude: `processFactory?: (args, cwd?, env?) => JsonlTransport`
    - Kiro: `targetFactory?: (args, cwd?, env?) => AcpTargetHandle`
  - `StudioServiceOptions.keep?: { readonly tracker: ReceiptTracker; readonly reportPath: (studioSessionId: string) => string; readonly removeReport: (studioSessionId: string) => Promise<void> }`
  - `StudioService.retryReceipt(runId: string): Promise<void>`
  - `readReportFile(path: string): Promise<string[]>` and `removeReportFile(path: string): Promise<void>` (main, both never throw).
- Turn semantics:
  - A **turn** starts at `sendMessage`, `runMission` or `runReview`.
  - Before a turn starts, the previous pending turn's receipt is finalized (`collect(…, true)`) and emitted as `keep.receipt` with the previous turn's `runId` and `runtimeId`.
  - After a turn's primary run (and any after-turn reviewers) completes, a provisional receipt (`collect(…, false)`) is emitted.
  - Reviews launched by "Second opinion" are their own turn (they finalize the answer's receipt first).
  - **The final collect is bounded to `FINALIZE_BUDGET_MS = 2000`.** On timeout or error, the turn's last provisional receipt is promoted: emitted with `final: true`, its `cursorsAfter` committed through `tracker.commit`. Events that arrive later roll into the next turn and are never double-counted. With no provisional receipt, an `unavailable` final receipt is emitted and cursors stay. A slow Keep therefore delays Send by at most 2 s.
  - The provisional collect after a turn is **fire-and-forget**: the IPC reply never waits for it, and the receipt arrives as a push event.
  - **Session switches finalize first.** `resumeSession`, `startNewSession` and `deleteSession` call `#finalizeKeepTurn()` before the active session changes, so a receipt is always written into the session that produced it.
  - `retryReceipt(runId)` works only for the pending turn (re-collected provisionally) or the most recently finalized turn (re-collected from the cursors that were committed before it). Any older run throws `"only the latest receipt can be retried"`, so a retry never takes events from later turns.

- [ ] **Step 1: Write the failing runtime env tests.** In each runtime test file, add a test with a factory spy. Claude:

```ts
it("passes RunRequest.env to the process factory", async () => {
  let seen: Readonly<Record<string, string>> | undefined;
  const runtime = new ClaudeRuntime({ processFactory: (args, cwd, env) => { seen = env; return fakeTransport(); } });
  await runtime.run({ ...baseRequest, env: { FIREKEEP_SESSION_REPORT_FILE: "/tmp/s.keep" } }, () => undefined, new AbortController().signal);
  expect(seen).toEqual({ FIREKEEP_SESSION_REPORT_FILE: "/tmp/s.keep" });
});
```

Use each file's existing fake transport and base request helpers (`claude-runtime.test.ts`, `codex-runtime.test.ts`, `kiro-runtime.test.ts`). Codex asserts `peerFactory` received `env`. Kiro asserts `targetFactory`'s third argument.

- [ ] **Step 2: Write the failing service tests** — in `tests/studio-service.test.ts`:

```ts
import { ReceiptTracker } from "../src/core/keep/receipt-tracker.js";

function createKeepService() {
  const reports: Record<string, string[]> = {};
  const timelines: Record<string, unknown[]> = {};
  const tracker = new ReceiptTracker({
    source: () => ({ timeline: async (id, offset) => { const all = (timelines[id] ?? []) as never[]; return { kind: "page", events: all.slice(offset), total: all.length, hasMore: false }; } }),
    readReport: async (studioSessionId) => reports[studioSessionId] ?? [],
  });
  const alpha = new FakeRuntime({ id: "alpha", displayName: "Alpha", description: "", transport: "test", capabilities: ["chat", "review"] });
  const service = new StudioService({
    runtimes: new RuntimeRegistry([alpha]),
    settings: new MemorySettingsStore<StudioPersistedState>(),
    sessions: new MemorySessionStore(),
    idFactory: (() => { let id = 0; return (prefix: string) => `${prefix}-${++id}`; })(),
    keep: { tracker, reportPath: (id) => `/keep/${id}.keep`, removeReport: async () => undefined },
  });
  return { service, alpha, reports, timelines };
}

it("launches every run with the session's report file", async () => {
  const { service, alpha } = createKeepService();
  await service.initialize();
  await service.setPrimary("alpha");
  await service.sendMessage("hi");
  const sessionId = service.snapshot().activeSessionId;
  expect(alpha.runs[0]?.env).toEqual({ FIREKEEP_SESSION_REPORT_FILE: `/keep/${sessionId}.keep` });
});

it("emits a provisional receipt after the turn and a final one before the next prompt", async () => {
  const { service, reports, timelines } = createKeepService();
  await service.initialize();
  await service.setPrimary("alpha");
  const sessionId = service.snapshot().activeSessionId;
  reports[sessionId] = ["k1"];
  timelines.k1 = [{ id: "e1", agent_id: "agent-a", event_type: "memory_read", timestamp: "t", payload: { query: "q", memory_ids: ["m1"] } }];
  const receipts = () => service.events().filter((event) => event.payload.kind === "keep.receipt").map((event) => event.payload as Extract<typeof event.payload, { kind: "keep.receipt" }>);
  await service.sendMessage("first");
  expect(receipts().map((receipt) => receipt.receipt.final)).toEqual([false]);
  timelines.k1.push({ id: "e2", agent_id: "agent-a", event_type: "memory_write", timestamp: "t", payload: { action_summary: "late", memory_id: "m2" } });
  await service.sendMessage("second");
  const [, finalFirst] = receipts();
  expect(finalFirst?.receipt.final).toBe(true);
  expect(finalFirst?.receipt.learnedCount).toBe(1);
});

it("a slow Keep delays Send by at most the finalize budget and promotes the provisional receipt", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const alpha = new FakeRuntime({ id: "alpha", displayName: "Alpha", description: "", transport: "test", capabilities: ["chat"] });
  let calls = 0;
  const tracker = new ReceiptTracker({
    source: () => ({ timeline: async (_id, offset) => {
      calls += 1;
      if (calls > 1) return new Promise(() => undefined);           // the finalize fetch hangs
      return { kind: "page", events: [{ id: "e1", agent_id: "a", event_type: "memory_read", timestamp: "t", payload: { memory_ids: ["m1"] } }].slice(offset), total: 1, hasMore: false };
    } }),
    readReport: async () => ["k1"],
  });
  const service = new StudioService({ runtimes: new RuntimeRegistry([alpha]), settings: new MemorySettingsStore<StudioPersistedState>(), keep: { tracker, reportPath: () => "/x.keep", removeReport: async () => undefined } });
  await service.initialize();
  await service.setPrimary("alpha");
  await service.sendMessage("first");
  await vi.waitFor(() => expect(calls).toBe(1));                    // provisional landed
  const second = service.sendMessage("second");
  await vi.advanceTimersByTimeAsync(2100);
  await second;
  const receipts = service.events().filter((event) => event.payload.kind === "keep.receipt");
  const promoted = receipts.map((event) => (event.payload as { receipt: { final: boolean; recalledCount: number } }).receipt).find((receipt) => receipt.final);
  expect(promoted?.recalledCount).toBe(1);
  expect(tracker.cursors(service.snapshot().activeSessionId)).toEqual({ k1: 1 });
  vi.useRealTimers();
});

it("finalizes into the session that produced the turn when the user switches sessions", async () => {
  const { service, reports, timelines } = createKeepService();
  await service.initialize();
  await service.setPrimary("alpha");
  const first = service.snapshot().activeSessionId;
  reports[first] = ["k1"];
  timelines.k1 = [];
  await service.sendMessage("in A");
  await service.startNewSession();
  await service.sendMessage("in B");
  const finalInA = service.eventsFor(first).filter((event) => event.payload.kind === "keep.receipt" && (event.payload as { receipt: { final: boolean } }).receipt.final);
  expect(finalInA).toHaveLength(1);
});

it("retry refuses a run older than the latest finalized turn", async () => {
  const { service } = createKeepService();
  await service.initialize();
  await service.setPrimary("alpha");
  await service.sendMessage("one");
  const firstRun = service.events().find((event) => event.payload.kind === "run.started")?.runId as string;
  await service.sendMessage("two");
  await service.sendMessage("three");
  await expect(service.retryReceipt(firstRun)).rejects.toThrow(/only the latest receipt/);
});

it("a Keep failure never fails the turn", async () => {
  const alpha = new FakeRuntime({ id: "alpha", displayName: "Alpha", description: "", transport: "test", capabilities: ["chat"] });
  const tracker = new ReceiptTracker({ source: () => ({ timeline: async () => { throw new Error("gateway gone"); } }), readReport: async () => ["k1"] });
  const service = new StudioService({ runtimes: new RuntimeRegistry([alpha]), settings: new MemorySettingsStore<StudioPersistedState>(), keep: { tracker, reportPath: () => "/x.keep", removeReport: async () => undefined } });
  await service.initialize();
  await service.setPrimary("alpha");
  await expect(service.sendMessage("hi")).resolves.toMatchObject({ finalText: "response from alpha" });
});
```

If `StudioService` exposes events under another name than `events()`, use the accessor `ipc-controller.ts` uses for `{type:"state", events}`. `eventsFor(sessionId)` means "load that session's events from the session store". Use `MemorySessionStore.load(id)` directly if the service has no such accessor, and `startNewSession` means the service method behind `session.new` (App's `startNewSession` at 335-349 calls it).

- [ ] **Step 3: Run to verify they fail.** Run: `npx vitest run tests/studio-service.test.ts tests/claude-runtime.test.ts tests/codex-runtime.test.ts tests/kiro-runtime.test.ts`. Expected: FAIL (`env` undefined; no `keep` option).

- [ ] **Step 4: Implement runtime env threading.**
  1. Add `readonly env?: Readonly<Record<string, string>>;` to `RunRequest` in `runtime.ts`.
  2. Claude: change the option and field types to `(args: readonly string[], cwd?: string, env?: Readonly<Record<string, string>>) => JsonlTransport`. Make the default factory `(args, cwd, env) => new ChildProcessJsonlTransport(this.#command, args, { ...(cwd ? { cwd } : {}), env: { ...process.env, ...env } })` and the call `this.#processFactory(args, request.cwd, request.env)`.
  3. Codex: change `peerFactory` to `(env?: Readonly<Record<string, string>>) => RpcPeer`; the default factory passes `{ env: { ...process.env, ...env } }`. `run` (187) calls `this.#peerFactory(request.env)`; `login` (109) and `#withPeer` (286) call it with no argument.
  4. Kiro: change `targetFactory` to `(args, cwd?, env?) => AcpTargetHandle`, the default to `(args, cwd, env) => createProcessTarget(this.#command, args, cwd, env)`, `createProcessTarget` to accept `env?` and spawn with `env: { ...process.env, ...env }`, and the call (139) to `this.#targetFactory(args, request.cwd, request.env)`.

- [ ] **Step 5: Implement the report file helpers** — `src/main/keep/report-file.ts`:

```ts
import { readFile, rm } from "node:fs/promises";

/** Keep session ids the kit's bridge tap appended (kit 1.8, spec §7.2). Never throws. */
export async function readReportFile(path: string): Promise<string[]> {
  try {
    return (await readFile(path, "utf8")).split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  } catch {
    return [];
  }
}

export async function removeReportFile(path: string): Promise<void> {
  await rm(path, { force: true }).catch(() => undefined);
}
```

`tests/report-file.test.ts`:
- write a temp file `"s1\r\ns2\n\n"`, expect `["s1", "s2"]`;
- a missing path returns `[]`;
- `removeReportFile` on a missing path resolves.

- [ ] **Step 6: Implement the service integration** in `studio-service.ts`:

```ts
// imports: ReceiptTracker from ./keep/receipt-tracker.js; KeepReceipt and unavailableReceipt from ./keep/receipt.js
// options
readonly keep?: {
  readonly tracker: ReceiptTracker;
  readonly reportPath: (studioSessionId: string) => string;
  readonly removeReport: (studioSessionId: string) => Promise<void>;
};

// fields
readonly #keep: StudioServiceOptions["keep"];
#keepTurn: { studioSessionId: string; runId: string; runtimeId: string; events: RuntimeEventPayload[] } | null = null;
```

- In the constructor: `this.#keep = options.keep;`.
- In `#run`, when building `request`, add:
  `...(this.#keep ? { env: { FIREKEEP_SESSION_REPORT_FILE: this.#keep.reportPath(state.activeSessionId) } } : {}),`
- In the sink wrapper, before `this.#emit(...)`, add `this.#keepTurn?.events.push(payload);`.
- In `#run`, right after `const runId = …`, when `mode === "primary"` and `this.#keepTurn` is null, start a turn:
  `this.#keepTurn = { studioSessionId: state.activeSessionId, runId, runtimeId, events: [] };`

Then add:

```ts
/** Finalize the previous turn's receipt before a new turn's prompt leaves (spec §7.3). Bounded: a slow Keep delays Send by at most FINALIZE_BUDGET_MS. */
async #finalizeKeepTurn(): Promise<void> {
  const turn = this.#keepTurn;
  this.#keepTurn = null;
  if (!turn || !this.#keep) return;
  const tracker = this.#keep.tracker;
  const before = tracker.cursors(turn.studioSessionId);
  const timeout = new Promise<null>((resolve) => setTimeout(() => resolve(null), FINALIZE_BUDGET_MS));
  const collected = await Promise.race([tracker.collect(turn.studioSessionId, turn.events, true).catch(() => null), timeout]);
  let receipt: KeepReceipt;
  if (collected && collected.status !== "unavailable") receipt = collected;
  else if (turn.provisional && turn.provisional.status === "measured") {
    receipt = { ...turn.provisional, final: true };
    tracker.commit(turn.studioSessionId, turn.provisional.cursorsAfter);
  } else receipt = collected ?? unavailableReceipt("Keep did not answer in time", true, before);
  this.#lastFinalized = { runId: turn.runId, runtimeId: turn.runtimeId, studioSessionId: turn.studioSessionId, events: turn.events, cursorsBefore: before };
  this.#emitInto(turn, receipt);
}

/** Fire-and-forget: never awaited by sendMessage, the receipt arrives by push event. */
#provisionalKeepReceipt(): void {
  const turn = this.#keepTurn;
  if (!turn || !this.#keep) return;
  void this.#keep.tracker.collect(turn.studioSessionId, turn.events, false)
    .then((receipt) => {
      if (this.#keepTurn !== turn) return;   // already finalized; the final receipt wins
      turn.provisional = receipt;
      this.#emitInto(turn, receipt);
    })
    .catch(() => undefined);              // a Keep failure never fails or blocks a turn (spec §8)
}

/** Append to the session that produced the turn, even if another session is active now. */
#emitInto(turn: KeepTurn, receipt: KeepReceipt): void {
  this.#emit(turn.runtimeId, turn.runId, { kind: "keep.receipt", receipt }, undefined, turn.studioSessionId);
}
```

Declare at module level:

```ts
const FINALIZE_BUDGET_MS = 2000;
type KeepTurn = { studioSessionId: string; runId: string; runtimeId: string; events: RuntimeEventPayload[]; provisional?: KeepReceipt };
```

Also declare a field `#lastFinalized: (Omit<KeepTurn, "provisional"> & { cursorsBefore: Readonly<Record<string, number>> }) | null = null`.

`#emit` currently appends to the active session. Give it an optional fifth parameter `studioSessionId` (default: the active session) that it uses both for the event's `studioSessionId` field and for the session-store append. The only caller passing it is `#emitInto`, and the push to the renderer still happens; the renderer ignores events for a session it is not showing.

Wire the turn boundaries:
- `sendMessage`: the first line after validation is `await this.#finalizeKeepTurn();`. After the reviewers block, add `this.#provisionalKeepReceipt();` (not awaited).
- `runMission`: first line `await this.#finalizeKeepTurn();`. When `#driveMission` settles (the mission reaches a terminal or waiting phase), call `this.#provisionalKeepReceipt();`.
- `resumeSession`, `startNewSession` and `deleteSession`: first line `await this.#finalizeKeepTurn();`, before the active session changes.
- `runReview` (Second opinion): first line `await this.#finalizeKeepTurn();`. Reviews never start a Keep turn, because `#run` only starts one for `mode === "primary"`.

Cursor restore, and cleanup on delete:
- In `initialize` and `resumeSession`, after the session's events are loaded, find the last `keep.receipt` event whose `receipt.final === true` and call `this.#keep?.tracker.restore(sessionId, thatReceipt.cursorsAfter)`.
- In `deleteSession`, after `remove`, call `await this.#keep?.removeReport(sessionId); this.#keep?.tracker.forget(sessionId);`.

Add `async retryReceipt(runId: string): Promise<void>`:
- if `this.#keepTurn?.runId === runId`, call `this.#provisionalKeepReceipt()`;
- else if `this.#lastFinalized?.runId === runId`, call `tracker.restore(sessionId, lastFinalized.cursorsBefore)`, then `collect(…, true)`, and `#emitInto` the result;
- else throw `new Error("only the latest receipt can be retried")`.

The renderer only offers retry on the newest receipt (Task 14).

- [ ] **Step 7: Run.** Run: `npx vitest run && npm run typecheck`. Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add -A src tests
git commit -m "feat(studio): per-run FIREKEEP_SESSION_REPORT_FILE and per-turn Keep receipts"
```

### Task 9: Minimal MCP stdio client

**Files:**
- Create: `src/main/keep/mcp-stdio-client.ts`, `tests/mcp-stdio-client.test.ts`, `tests/helpers/fake-mcp-server.mjs`

**Interfaces:**
- Consumes: `JsonLineDecoder` (`jsonl-rpc.ts:9`), `spawnRuntime` and `terminateProcessTree` (`process.ts:151, 96`).
- Produces:

```ts
export interface McpToolResult { readonly text: string; readonly isError: boolean }
export interface McpStdioClientOptions {
  readonly command: string;
  readonly args: readonly string[];
  readonly env: Readonly<Record<string, string>>;
  readonly timeoutMs?: number;            // default 10_000
  readonly onExit?: (code: number | null, stderrTail: string) => void; // only after a successful initialize
}
export class McpStdioClient {
  static connect(options: McpStdioClientOptions): Promise<McpStdioClient>; // spawn + initialize + notifications/initialized
  callTool(name: string, args: Readonly<Record<string, unknown>>): Promise<McpToolResult>;
  close(): Promise<void>;                                                    // process-tree kill
  readonly closed: boolean;
}
```

- [ ] **Step 1: Write the fake server** — `tests/helpers/fake-mcp-server.mjs`. It is driven by `FAKE_MCP_MODE`: `ok` | `hang` | `exit-on-call` | `unknown-preset`:

```js
import { createInterface } from "node:readline";

const mode = process.env.FAKE_MCP_MODE ?? "ok";
if (mode === "unknown-preset") {
  process.stderr.write("firekeep gateway: unknown FIREKEEP_TOOLSET 'studio' (valid: chat); refusing to start\n");
  process.exit(1);
}
const send = (message) => process.stdout.write(`${JSON.stringify(message)}\n`);
createInterface({ input: process.stdin }).on("line", (line) => {
  const message = JSON.parse(line);
  if (message.method === "initialize") send({ jsonrpc: "2.0", id: message.id, result: { protocolVersion: "2025-06-18", capabilities: {}, serverInfo: { name: "fake", version: "0" } } });
  else if (message.method === "tools/call") {
    if (mode === "hang") return;
    if (mode === "exit-on-call") process.exit(3);
    const { name, arguments: args } = message.params;
    send({ jsonrpc: "2.0", id: message.id, result: { content: [{ type: "text", text: JSON.stringify({ name, args, env: { toolset: process.env.FIREKEEP_TOOLSET ?? null, allow: process.env.FIREKEEP_TOOLS_ALLOW ?? null, agent: process.env.FIREKEEP_AGENT_ID ?? null } }) }], isError: false } });
  }
});
```

- [ ] **Step 2: Write the failing tests** — `tests/mcp-stdio-client.test.ts`:

```ts
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { McpStdioClient } from "../src/main/keep/mcp-stdio-client.js";

const server = fileURLToPath(new URL("./helpers/fake-mcp-server.mjs", import.meta.url));
const connect = (mode: string, extra: Partial<Parameters<typeof McpStdioClient.connect>[0]> = {}) =>
  McpStdioClient.connect({ command: process.execPath, args: [server], env: { FAKE_MCP_MODE: mode, FIREKEEP_TOOLSET: "studio" }, timeoutMs: 1500, ...extra });

describe("McpStdioClient", () => {
  it("initializes and calls a tool with JSON-RPC 2.0", async () => {
    const client = await connect("ok");
    const result = await client.callTool("relay_who_is_online", { include_idle: true });
    expect(JSON.parse(result.text)).toMatchObject({ name: "relay_who_is_online", args: { include_idle: true }, env: { toolset: "studio" } });
    await client.close();
    expect(client.closed).toBe(true);
  });

  it("times out a hung call without hanging the caller", async () => {
    const client = await connect("hang");
    await expect(client.callTool("memory_recall", { task: "x" })).rejects.toThrow(/timed out/);
    await client.close();
  });

  it("rejects pending calls and reports exit when the server dies", async () => {
    let exited: number | null | undefined;
    const client = await connect("exit-on-call", { onExit: (code) => { exited = code; } });
    await expect(client.callTool("memory_recall", { task: "x" })).rejects.toThrow(/exited/);
    expect(exited).toBe(3);
  });

  it("surfaces a startup refusal with its stderr", async () => {
    await expect(connect("unknown-preset")).rejects.toThrow(/unknown FIREKEEP_TOOLSET/);
  });
});
```

- [ ] **Step 3: Run to verify they fail.** Expected: FAIL (module not found).

- [ ] **Step 4: Implement** `src/main/keep/mcp-stdio-client.ts`:

```ts
import type { ChildProcessWithoutNullStreams } from "node:child_process";
import { JsonLineDecoder } from "../runtime/jsonl-rpc.js";
import { spawnRuntime, terminateProcessTree } from "../runtime/process.js";

export interface McpToolResult { readonly text: string; readonly isError: boolean }
export interface McpStdioClientOptions {
  readonly command: string;
  readonly args: readonly string[];
  readonly env: Readonly<Record<string, string>>;
  readonly timeoutMs?: number;
  readonly onExit?: (code: number | null, stderrTail: string) => void;
}

interface Pending { resolve(value: unknown): void; reject(error: Error): void; timer: NodeJS.Timeout }

const PROTOCOL_VERSION = "2025-06-18";
const STDERR_TAIL = 4096;

/**
 * Just enough MCP (JSON-RPC 2.0 over newline-delimited stdio) to talk to
 * `firekeep gateway`. Hand-written rather than the SDK so the child is spawned
 * through spawnRuntime and killed with terminateProcessTree — the gateway's
 * shim grandchildren must die with it on Windows.
 */
export class McpStdioClient {
  readonly #child: ChildProcessWithoutNullStreams;
  readonly #timeoutMs: number;
  readonly #pending = new Map<number, Pending>();
  #nextId = 1;
  #stderr = "";
  #closed = false;
  #ready = false;   // onExit fires only for a gateway that finished initialize

  private constructor(child: ChildProcessWithoutNullStreams, options: McpStdioClientOptions) {
    this.#child = child;
    this.#timeoutMs = options.timeoutMs ?? 10_000;
    const decoder = new JsonLineDecoder();
    child.stdout.on("data", (chunk: Buffer) => { for (const message of decoder.push(chunk)) this.#receive(message); });
    child.stderr.on("data", (chunk: Buffer) => { this.#stderr = (this.#stderr + chunk.toString("utf8")).slice(-STDERR_TAIL); });
    child.on("close", (code) => {
      this.#closed = true;
      const error = new Error(`firekeep gateway exited (${code ?? "signal"}): ${this.#stderr.trim().split("\n").pop() ?? ""}`);
      for (const pending of this.#pending.values()) { clearTimeout(pending.timer); pending.reject(error); }
      this.#pending.clear();
      if (this.#ready) options.onExit?.(code, this.#stderr);
    });
    child.on("error", () => undefined);
  }

  get closed(): boolean { return this.#closed; }

  static async connect(options: McpStdioClientOptions): Promise<McpStdioClient> {
    const child = spawnRuntime(options.command, options.args, { env: { ...process.env, ...options.env } });
    const client = new McpStdioClient(child, options);
    await client.#request("initialize", { protocolVersion: PROTOCOL_VERSION, capabilities: {}, clientInfo: { name: "firekeep-studio", version: "0.5.0" } });
    client.#notify("notifications/initialized");
    client.#ready = true;
    return client;
  }

  async callTool(name: string, args: Readonly<Record<string, unknown>>): Promise<McpToolResult> {
    const result = await this.#request("tools/call", { name, arguments: args }) as { content?: Array<{ type?: string; text?: string }>; isError?: boolean };
    const text = (result.content ?? []).filter((part) => part.type === "text").map((part) => part.text ?? "").join("\n");
    return { text, isError: result.isError === true };
  }

  async close(): Promise<void> {
    if (this.#closed) return;
    await terminateProcessTree(this.#child);
    this.#closed = true;
  }

  #request(method: string, params: unknown): Promise<unknown> {
    if (this.#closed) return Promise.reject(new Error("firekeep gateway exited"));
    const id = this.#nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.#pending.delete(id);
        reject(new Error(`firekeep gateway call ${method} timed out after ${this.#timeoutMs} ms`));
      }, this.#timeoutMs);
      this.#pending.set(id, { resolve, reject, timer });
      this.#child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id, method, params })}\n`);
    });
  }

  #notify(method: string): void {
    this.#child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", method })}\n`);
  }

  #receive(message: unknown): void {
    if (!message || typeof message !== "object") return;
    const { id, result, error } = message as { id?: unknown; result?: unknown; error?: { message?: string } };
    if (typeof id !== "number") return;  // notifications and server requests are ignored
    const pending = this.#pending.get(id);
    if (!pending) return;
    this.#pending.delete(id);
    clearTimeout(pending.timer);
    if (error) pending.reject(new Error(error.message ?? "MCP error"));
    else pending.resolve(result ?? {});
  }
}
```

- [ ] **Step 5: Run.** Run: `npx vitest run tests/mcp-stdio-client.test.ts`. Expected: PASS. On Windows, `spawnRuntime` refuses `.cmd` shims, and `process.execPath` is an `.exe`, so the test is fine.

- [ ] **Step 6: Commit**

```bash
git add src/main/keep/mcp-stdio-client.ts tests/mcp-stdio-client.test.ts tests/helpers/fake-mcp-server.mjs
git commit -m "feat(studio): minimal MCP stdio client with process-tree shutdown"
```

### Task 10: `KeepGateway` — whoami, spawn, fallback, backoff, Keep operations

**Files:**
- Create: `src/shared/keep.ts`, `src/main/keep/keep-gateway.ts`, `tests/keep-gateway.test.ts`
- Modify: `src/main/firekeep-client.ts` (export `defaultFirekeepCommand`; add `"whoami"` to the actions with fixed args `["--json"]`)

**Interfaces:**
- Produces in `src/shared/keep.ts`:

```ts
export type KeepStatus =
  | { readonly state: "starting" }
  | { readonly state: "missing" }
  | { readonly state: "unconfigured"; readonly detail: string }
  | { readonly state: "unreachable"; readonly detail: string; readonly retryInMs: number | null }
  | { readonly state: "connected"; readonly agentId: string; readonly personalMode: boolean; readonly kitVersion: string };
export interface KeepItem { readonly title: string; readonly body: string }
export interface KeepHighlights { readonly online: string; readonly bulletin: string; readonly paused: readonly { readonly sessionId: string; readonly goal: string }[] }
export const KEEP_INSTALL_COMMANDS = {
  unix: "curl -fsSL https://firekeep.ai/latest/install | sh",
  windows: "irm https://firekeep.ai/latest/install.ps1 | iex",
} as const;
```

- Produces in `src/main/keep/keep-gateway.ts`:

```ts
export interface KeepGatewayDeps {
  readonly command: string;                                                   // firekeep executable
  readonly whoami: () => Promise<{ ok: boolean; output: string; missing: boolean }>;
  readonly connect: (env: Readonly<Record<string, string>>) => Promise<McpLike>; // McpStdioClient.connect wrapper
  readonly sleep?: (ms: number) => Promise<void>;
  readonly onStatus?: (status: KeepStatus) => void;
}
export interface McpLike { callTool(name: string, args: Readonly<Record<string, unknown>>): Promise<McpToolResult>; close(): Promise<void>; readonly closed: boolean }
export const STUDIO_TOOLS: readonly string[];        // the nine preset names
export const BACKOFF_MS: readonly number[];          // [1000, 5000, 30000]
export class KeepGateway implements KeepTimelineSource {
  constructor(deps: KeepGatewayDeps);
  start(): Promise<KeepStatus>;
  handleExit(): void;                                 // unexpected gateway exit: start() unless stop() was called
  status(): KeepStatus;
  retry(): Promise<KeepStatus>;                       // manual retry after backoff exhausted
  search(query: string, kind: "memory" | "skill"): Promise<readonly KeepItem[]>;
  skills(): Promise<readonly KeepItem[]>;              // skill_list (active)
  team(): Promise<string>;
  highlights(): Promise<KeepHighlights>;
  feedback(memoryIds: readonly string[], useful: boolean, comment?: string): Promise<void>;
  timeline(keepSessionId: string, offset: number, limit: number): Promise<TimelinePage>;
  stop(): Promise<void>;
}
export function splitNumberedItems(text: string): KeepItem[];
```

- Behaviour:
  - **`whoami` result.** `missing` → `{state:"missing"}`. A non-zero exit or `agent_id: null` → `unconfigured` with the first output line. Otherwise, spawn with `{ FIREKEEP_TOOLSET: "studio", FIREKEEP_AGENT_ID: `${agentId}${PANEL_AGENT_SUFFIX}` }`.
  - **Old kit.** If connect fails with "unknown FIREKEEP_TOOLSET", reconnect once with `{ FIREKEEP_TOOLS_ALLOW: STUDIO_TOOLS.join(","), FIREKEEP_AGENT_ID: … }`. A kit without `whoami` gets no panel label: in that case `whoami` reports `missing: false, ok: false`, the status is `unconfigured`, and the detail says "Update the Firekeep kit (firekeep update) to connect Studio".
  - **Unexpected exit.** Status becomes `unreachable` and the gateway reconnects after `BACKOFF_MS[n]`. After the third failure it stops (`retryInMs: null`) until `retry()`.
  - **Tool arguments.** Only `memory_recall` and `memory_feedback` carry `agent_id: <panel label>` (`TAGGED_TOOLS`). `memory_recall` sends `{ task, format: "raw", top_k: 8, agent_id }`. `replay_timeline` sends `{ session_id, offset, limit, format: "json" }`.
  - **Not connected.** Operations throw `Error("Keep not connected")`. `timeline()` returns `{ kind: "error", message }` instead of throwing when the gateway is down.

- [ ] **Step 1: Write the failing tests** — `tests/keep-gateway.test.ts`:

```ts
import { describe, expect, it, vi } from "vitest";
import { BACKOFF_MS, KeepGateway, splitNumberedItems, STUDIO_TOOLS, type McpLike } from "../src/main/keep/keep-gateway.js";

function fakeMcp(reply: (name: string, args: Record<string, unknown>) => string = () => "ok"): McpLike & { calls: Array<[string, Record<string, unknown>]> } {
  const calls: Array<[string, Record<string, unknown>]> = [];
  return { calls, closed: false, async callTool(name, args) { calls.push([name, { ...args }]); return { text: reply(name, { ...args }), isError: false }; }, async close() {} };
}
const whoamiOk = async () => ({ ok: true, missing: false, output: JSON.stringify({ agent_id: "agent-a", kit_version: "1.8.0", personal_mode: false, server_configured: true }) });

describe("KeepGateway", () => {
  it("reports missing when the kit is not installed", async () => {
    const gateway = new KeepGateway({ command: "firekeep", whoami: async () => ({ ok: false, missing: true, output: "" }), connect: vi.fn() });
    expect(await gateway.start()).toEqual({ state: "missing" });
  });

  it("spawns the studio toolset under the panel label and tags every call", async () => {
    const mcp = fakeMcp();
    const connect = vi.fn(async () => mcp);
    const gateway = new KeepGateway({ command: "firekeep", whoami: whoamiOk, connect });
    expect(await gateway.start()).toMatchObject({ state: "connected", agentId: "agent-a" });
    expect(connect).toHaveBeenCalledWith({ FIREKEEP_TOOLSET: "studio", FIREKEEP_AGENT_ID: "agent-a-studio-panel" });
    await gateway.search("vps", "memory");
    expect(mcp.calls[0]).toEqual(["memory_recall", { task: "vps", format: "raw", top_k: 8, agent_id: "agent-a-studio-panel" }]);
  });

  it("falls back to FIREKEEP_TOOLS_ALLOW on a kit without the studio preset", async () => {
    const mcp = fakeMcp();
    const connect = vi.fn()
      .mockRejectedValueOnce(new Error("firekeep gateway exited (1): unknown FIREKEEP_TOOLSET 'studio'"))
      .mockResolvedValueOnce(mcp);
    const gateway = new KeepGateway({ command: "firekeep", whoami: whoamiOk, connect });
    await gateway.start();
    expect(connect).toHaveBeenLastCalledWith({ FIREKEEP_TOOLS_ALLOW: STUDIO_TOOLS.join(","), FIREKEEP_AGENT_ID: "agent-a-studio-panel" });
  });

  it("backs off 1s, 5s, 30s and then waits for a manual retry", async () => {
    const sleeps: number[] = [];
    const connect = vi.fn().mockRejectedValue(new Error("firekeep gateway exited (1): cannot reach cortex"));
    const gateway = new KeepGateway({ command: "firekeep", whoami: whoamiOk, connect, sleep: async (ms) => { sleeps.push(ms); } });
    const status = await gateway.start();
    expect(sleeps).toEqual([...BACKOFF_MS]);
    expect(status).toMatchObject({ state: "unreachable", retryInMs: null });
    expect(status.state === "unreachable" && status.detail).toContain("cannot reach cortex");
  });

  it("asks replay_timeline for JSON and parses it", async () => {
    const mcp = fakeMcp(() => JSON.stringify({ events: [], total: 0, has_more: false }));
    const gateway = new KeepGateway({ command: "firekeep", whoami: whoamiOk, connect: async () => mcp });
    await gateway.start();
    expect(await gateway.timeline("s1", 4, 200)).toEqual({ kind: "page", events: [], total: 0, hasMore: false });
    expect(mcp.calls[0]).toEqual(["replay_timeline", { session_id: "s1", offset: 4, limit: 200, format: "json" }]);
  });

  it("timeline() returns an error page instead of throwing when not connected", async () => {
    const gateway = new KeepGateway({ command: "firekeep", whoami: async () => ({ ok: false, missing: true, output: "" }), connect: vi.fn() });
    await gateway.start();
    expect(await gateway.timeline("s1", 0, 200)).toEqual({ kind: "error", message: "Keep not connected" });
  });

  it("only ever calls preset tools", async () => {
    const mcp = fakeMcp(() => "");
    const gateway = new KeepGateway({ command: "firekeep", whoami: whoamiOk, connect: async () => mcp });
    await gateway.start();
    await gateway.search("x", "skill"); await gateway.skills(); await gateway.team(); await gateway.highlights();
    await gateway.feedback(["m1"], true); await gateway.timeline("s", 0, 1);
    for (const [name] of mcp.calls) expect(STUDIO_TOOLS).toContain(name);
  });

  it("labels only memory_recall and memory_feedback (ctx_list_sessions treats agent_id as a filter)", async () => {
    const mcp = fakeMcp(() => "");
    const gateway = new KeepGateway({ command: "firekeep", whoami: whoamiOk, connect: async () => mcp });
    await gateway.start();
    await gateway.highlights(); await gateway.skills(); await gateway.feedback(["m1"], false);
    for (const [name, args] of mcp.calls) expect("agent_id" in args).toBe(name === "memory_recall" || name === "memory_feedback");
  });

  it("does not restart after stop()", async () => {
    const connect = vi.fn(async () => fakeMcp());
    const gateway = new KeepGateway({ command: "firekeep", whoami: whoamiOk, connect });
    await gateway.start();
    await gateway.stop();
    gateway.handleExit();
    await Promise.resolve();
    expect(connect).toHaveBeenCalledTimes(1);
  });
});

it("splitNumberedItems splits a raw recall list, else returns one block", () => {
  expect(splitNumberedItems("1. Alpha\nmore\n2. Beta")).toEqual([{ title: "Alpha", body: "Alpha\nmore" }, { title: "Beta", body: "Beta" }]);
  expect(splitNumberedItems("just prose")).toEqual([{ title: "just prose", body: "just prose" }]);
  expect(splitNumberedItems("")).toEqual([]);
});
```

- [ ] **Step 2: Run to verify they fail.** Expected: FAIL (module not found).

- [ ] **Step 3: Implement.**
  1. Create `src/shared/keep.ts` as specified above.
  2. In `firekeep-client.ts`:
     - export `defaultFirekeepCommand`;
     - add `"whoami"` to `FirekeepClientAction` and `isAction`;
     - in `validateArgs`, return `["--json"]` for `whoami` when `args` is empty, and throw otherwise.
  3. Create `src/main/keep/keep-gateway.ts`:

```ts
import { PANEL_AGENT_SUFFIX, parseTimelineReply, type TimelinePage } from "../../core/keep/receipt.js";
import type { KeepTimelineSource } from "../../core/keep/receipt-tracker.js";
import type { KeepHighlights, KeepItem, KeepStatus } from "../../shared/keep.js";
import type { McpToolResult } from "./mcp-stdio-client.js";

export interface McpLike {
  callTool(name: string, args: Readonly<Record<string, unknown>>): Promise<McpToolResult>;
  close(): Promise<void>;
  readonly closed: boolean;
}

export interface KeepGatewayDeps {
  readonly command: string;
  readonly whoami: () => Promise<{ readonly ok: boolean; readonly output: string; readonly missing: boolean }>;
  readonly connect: (env: Readonly<Record<string, string>>) => Promise<McpLike>;
  readonly sleep?: (ms: number) => Promise<void>;
  readonly onStatus?: (status: KeepStatus) => void;
}

/** Exactly the kit's `studio` preset (client 1.8.0). */
export const STUDIO_TOOLS: readonly string[] = [
  "memory_recall", "memory_feedback", "skill_list", "skill_recall",
  "relay_who_is_online", "relay_read", "ctx_list_sessions", "replay_timeline", "corpus_sources",
];
export const BACKOFF_MS: readonly number[] = [1000, 5000, 30_000];
/** Tools whose `agent_id` is an attribution label. ctx_list_sessions treats it as a FILTER; others lack it. */
const TAGGED_TOOLS = new Set(["memory_recall", "memory_feedback"]);

export class KeepGateway implements KeepTimelineSource {
  readonly #deps: KeepGatewayDeps;
  #status: KeepStatus = { state: "starting" };
  #mcp: McpLike | null = null;
  #label = "";
  #env: Record<string, string> = {};
  #stopped = false;

  constructor(deps: KeepGatewayDeps) {
    this.#deps = deps;
  }

  status(): KeepStatus { return this.#status; }

  async start(): Promise<KeepStatus> {
    if (this.#stopped) return this.#status;
    const who = await this.#deps.whoami().catch(() => ({ ok: false, output: "", missing: true }));
    if (who.missing) return this.#set({ state: "missing" });
    const facts = parseWhoami(who.output);
    if (!who.ok || !facts?.agent_id) {
      const detail = facts ? "This machine has no Firekeep agent identity yet. Run firekeep doctor." : "Update the Firekeep kit (firekeep update) to connect Studio.";
      return this.#set({ state: "unconfigured", detail });
    }
    this.#label = `${facts.agent_id}${PANEL_AGENT_SUFFIX}`;
    this.#env = { FIREKEEP_TOOLSET: "studio", FIREKEEP_AGENT_ID: this.#label };
    return this.#connectWithBackoff({ agentId: facts.agent_id, personalMode: facts.personal_mode === true, kitVersion: String(facts.kit_version ?? "") });
  }

  async retry(): Promise<KeepStatus> { return this.start(); }

  handleExit(): void {
    this.#mcp = null;
    if (!this.#stopped) void this.start();
  }

  async stop(): Promise<void> {
    this.#stopped = true;   // start()/handleExit() become no-ops; quit cannot respawn the gateway
    await this.#mcp?.close().catch(() => undefined);
    this.#mcp = null;
  }

  async search(query: string, kind: "memory" | "skill"): Promise<readonly KeepItem[]> {
    const text = kind === "memory"
      ? await this.#call("memory_recall", { task: query, format: "raw", top_k: 8 })
      : await this.#call("skill_recall", { task: query });
    return splitNumberedItems(text);
  }

  async skills(): Promise<readonly KeepItem[]> {
    return splitNumberedItems(await this.#call("skill_list", { status: "active", limit: 25 }));
  }

  async team(): Promise<string> { return this.#call("relay_who_is_online", { include_idle: true }); }

  async highlights(): Promise<KeepHighlights> {
    const [online, bulletin, sessions] = await Promise.all([
      this.#call("relay_who_is_online", { include_idle: false }),
      this.#call("relay_read", { limit: 3 }),
      this.#call("ctx_list_sessions", { status: "paused", limit: 5 }),
    ]);
    return { online, bulletin, paused: parsePausedSessions(sessions) };
  }

  async feedback(memoryIds: readonly string[], useful: boolean, comment?: string): Promise<void> {
    await this.#call("memory_feedback", { memory_ids: [...memoryIds], useful, ...(comment ? { comment } : {}) });
  }

  async timeline(keepSessionId: string, offset: number, limit: number): Promise<TimelinePage> {
    try {
      return parseTimelineReply(await this.#call("replay_timeline", { session_id: keepSessionId, offset, limit, format: "json" }));
    } catch (error) {
      return { kind: "error", message: error instanceof Error ? error.message : String(error) };
    }
  }

  async #call(name: string, args: Record<string, unknown>): Promise<string> {
    if (!STUDIO_TOOLS.includes(name)) throw new Error(`tool outside the studio preset: ${name}`);
    if (!this.#mcp || this.#mcp.closed) throw new Error("Keep not connected");
    const result = await this.#mcp.callTool(name, TAGGED_TOOLS.has(name) ? { ...args, agent_id: this.#label } : args);
    if (result.isError) throw new Error(result.text.split("\n")[0] || `${name} failed`);
    return result.text;
  }

  async #connectWithBackoff(facts: { agentId: string; personalMode: boolean; kitVersion: string }): Promise<KeepStatus> {
    const sleep = this.#deps.sleep ?? ((ms) => new Promise<void>((resolve) => setTimeout(resolve, ms)));
    let lastError = "";
    for (let attempt = 0; attempt <= BACKOFF_MS.length; attempt += 1) {
      if (this.#stopped) break;
      try {
        this.#mcp = await this.#connectOnce();
        return this.#set({ state: "connected", ...facts });
      } catch (error) {
        lastError = error instanceof Error ? error.message : String(error);
        const delay = BACKOFF_MS[attempt];
        this.#set({ state: "unreachable", detail: firstLine(lastError), retryInMs: delay ?? null });
        if (delay === undefined) break;
        await sleep(delay);
      }
    }
    return this.#set({ state: "unreachable", detail: firstLine(lastError), retryInMs: null });
  }

  async #connectOnce(): Promise<McpLike> {
    try {
      return await this.#deps.connect(this.#env);
    } catch (error) {
      if (!/unknown FIREKEEP_TOOLSET/.test(String(error))) throw error;
      // Kit older than 1.8.0: same tools, named explicitly.
      this.#env = { FIREKEEP_TOOLS_ALLOW: STUDIO_TOOLS.join(","), FIREKEEP_AGENT_ID: this.#label };
      return this.#deps.connect(this.#env);
    }
  }

  #set(status: KeepStatus): KeepStatus {
    this.#status = status;
    this.#deps.onStatus?.(status);
    return status;
  }
}

export function splitNumberedItems(text: string): KeepItem[] {
  const trimmed = text.trim();
  if (!trimmed) return [];
  const items: KeepItem[] = [];
  let current: string[] | null = null;
  for (const line of trimmed.split(/\r?\n/)) {
    const match = /^\s*\d+\.\s+(.*)$/.exec(line);
    if (match) { if (current) items.push(toItem(current)); current = [match[1] ?? ""]; }
    else if (current) current.push(line);
  }
  if (current) items.push(toItem(current));
  return items.length ? items : [{ title: firstLine(trimmed), body: trimmed }];
}

function toItem(lines: readonly string[]): KeepItem {
  return { title: firstLine(lines[0] ?? ""), body: lines.join("\n").trim() };
}

function firstLine(text: string): string {
  return text.trim().split(/\r?\n/)[0]?.slice(0, 160) ?? "";
}

function parseWhoami(output: string): { agent_id?: string | null; kit_version?: unknown; personal_mode?: unknown } | null {
  try {
    const value = JSON.parse(output.trim().split(/\r?\n/).pop() ?? "");
    return value && typeof value === "object" ? value : null;
  } catch {
    return null;
  }
}

/** ctx_list_sessions returns prose; pull "[<id>] \"<goal>\"" rows (format seen in the session-start briefing). */
function parsePausedSessions(text: string): KeepHighlights["paused"] {
  const rows: Array<{ sessionId: string; goal: string }> = [];
  for (const match of text.matchAll(/\[([A-Za-z0-9._:-]{4,128})\][^"\n]*"([^"\n]*)"/g)) rows.push({ sessionId: match[1] ?? "", goal: match[2] ?? "" });
  return rows.slice(0, 5);
}
```

Before relying on `parsePausedSessions`, run `ctx_list_sessions(status="paused")` through any agent session and compare its real output with the regex. If the format differs, adjust the regex, and add that real output as a fixture to `keep-gateway.test.ts`.

- [ ] **Step 4: Run.** Run: `npx vitest run tests/keep-gateway.test.ts tests/firekeep-client.test.ts`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/shared/keep.ts src/main/keep/keep-gateway.ts src/main/firekeep-client.ts tests/keep-gateway.test.ts tests/firekeep-client.test.ts
git commit -m "feat(studio): KeepGateway — whoami probe, studio toolset, fallback, backoff"
```

### Task 11: IPC, settings (recent workspaces, panel state) and main wiring

**Files:**
- Modify:
  - `src/shared/ipc.ts` (actions 33-73, results 87-102)
  - `src/main/ipc-controller.ts` (schema 22-63, dispatch 103-181)
  - `src/core/studio-service.ts` (state type 44-58, normalize, setters)
  - `src/main/index.ts` (construction ~105-200, quit ~249)
- Create: `src/main/workspace-detect.ts`
- Tests: `tests/ipc-controller.test.ts`, `tests/studio-service.test.ts`

**Interfaces:**
- Produces, in `StudioPersistedState`:
  - `recentWorkspaces: readonly string[]` — most recent first, max 8, set by `setWorkspace`;
  - `keepPanelOpen: boolean` — default `true`.
- New `StudioAction` members (all `.strict()` in zod):
  - `{ type: "keep.status" }` → `{ type: "keep-status"; status: KeepStatus }`
  - `{ type: "keep.retry" }` → `{ type: "keep-status"; status: KeepStatus }`
  - `{ type: "keep.search"; query: string (1..500); kind: "memory" | "skill" }` → `{ type: "keep-items"; items: KeepItem[] }`
  - `{ type: "keep.skills" }` → `{ type: "keep-items"; items: KeepItem[] }`
  - `{ type: "keep.team" }` → `{ type: "keep-team"; markdown: string }`
  - `{ type: "keep.highlights" }` → `{ type: "keep-highlights"; highlights: KeepHighlights }`
  - `{ type: "keep.feedback"; memoryIds: string[] (1..50, each matching /^[A-Za-z0-9._:-]{1,128}$/); useful: boolean; comment?: string (≤500) }` → `{ type: "keep-feedback" }`
  - `{ type: "keep.receipt.retry"; runId: id }` → `state`
  - `{ type: "workspace.detectTest" }` → `{ type: "test-command"; command: string | null }`
  - `{ type: "workspace.set"; path: string }` → `state`. Accepted only if `path` is in `recentWorkspaces`, so the renderer cannot point agents at an arbitrary path without the native dialog.
  - `{ type: "settings.keepPanel"; open: boolean }` → `state`
  - `{ type: "mission.quick"; goal: string (1..4000); checks: string[] (1..8, each 1..1000) }` → `state`. It calls `createMission(goal)`, then `addMissionCheck(c)` for each check, then `runMission()`; the native confirmation dialog still gates execution.
- Push event: `StudioPushEvent` gains `{ type: "keep-status"; status: KeepStatus }`, broadcast by `KeepGateway.onStatus`.

- [ ] **Step 1: Write the failing tests** — `tests/ipc-controller.test.ts`:

```ts
it.each([
  [{ type: "keep.search", query: "vps", kind: "memory" }],
  [{ type: "keep.feedback", memoryIds: ["m1"], useful: true }],
  [{ type: "mission.quick", goal: "fix it", checks: ["npm test"] }],
  [{ type: "workspace.set", path: "C:\\work" }],
])("parses %j", (action) => {
  expect(parseStudioAction(action)).toEqual(action);
});

it.each([
  [{ type: "keep.call", tool: "vault_retrieve", args: {} }],               // no generic tool IPC
  [{ type: "keep.search", query: "", kind: "memory" }],
  [{ type: "keep.feedback", memoryIds: ["bad id\n"], useful: true }],
  [{ type: "keep.search", query: "x", kind: "memory", extra: 1 }],
])("rejects %j", (action) => {
  expect(() => parseStudioAction(action)).toThrow();
});
```

In `tests/studio-service.test.ts`:

```ts
it("keeps recent workspaces most-recent-first, capped at 8, and only switches to known ones", async () => {
  const { service } = createService();
  await service.initialize();
  for (let i = 0; i < 10; i += 1) await service.setWorkspace(`C:\\w${i}`);
  expect(service.snapshot().recentWorkspaces).toEqual(["C:\\w9", "C:\\w8", "C:\\w7", "C:\\w6", "C:\\w5", "C:\\w4", "C:\\w3", "C:\\w2"]);
  await expect(service.switchToRecentWorkspace("C:\\elsewhere")).rejects.toThrow(/not a recent workspace/);
  await service.switchToRecentWorkspace("C:\\w5");
  expect(service.snapshot().workspacePath).toBe("C:\\w5");
});
```

- [ ] **Step 2: Run to verify they fail.** Expected: FAIL.

- [ ] **Step 3: Implement.**

**`studio-service.ts`:**
- Add `recentWorkspaces` and `keepPanelOpen` to the state type, `#defaultState` (`[]`, `true`) and `#normalizeState`:
  `recentWorkspaces: Array.isArray(stored.recentWorkspaces) ? stored.recentWorkspaces.filter((p): p is string => typeof p === "string" && p.trim().length > 0 && p.length <= 4096).slice(0, 8) : []`,
  `keepPanelOpen: stored.keepPanelOpen !== false`.
- In `setWorkspace(path)`, after the existing checks, when `path` is non-null:
  `recentWorkspaces: [path, ...state.recentWorkspaces.filter((p) => p !== path)].slice(0, 8)`.
- Add `switchToRecentWorkspace(path)` (throws `"not a recent workspace"` unless listed, then calls `setWorkspace`), `setKeepPanelOpen(open)`, and `quickMission(goal, checks)`. `quickMission` creates the mission, adds the checks and runs it, and calls `cancelMission()` if any `addMissionCheck` throws.

**`ipc.ts` and `ipc-controller.ts`:** add the actions, the results and the push event exactly as above. The controller receives two new constructor parameters at the end: `keep: KeepGateway | null` and `detectTest: () => Promise<string | null>`. Each `keep.*` dispatch:
- calls the gateway;
- converts a thrown error into `{ type: "error", message }`, via the existing `invoke` wrapper;
- for `keep.receipt.retry`, calls `service.retryReceipt(runId)`.

**`src/main/workspace-detect.ts`:**

```ts
import { readdir } from "node:fs/promises";
import { detectTestCommand } from "../core/test-command-detect.js";

export async function detectWorkspaceTestCommand(workspace: string | null): Promise<string | null> {
  if (!workspace) return null;
  try {
    const entries = await readdir(workspace, { withFileTypes: true });
    return detectTestCommand(entries.map((entry) => ({ name: entry.name, isDirectory: entry.isDirectory() })));
  } catch {
    return null;
  }
}
```

**`src/main/index.ts`** — construct, then start without awaiting:

```ts
const sessionsRoot = join(userData, "sessions");
const firekeep = new FirekeepClient();
const keep = new KeepGateway({
  command: defaultFirekeepCommand(),
  whoami: async () => {
    try {
      const result = await firekeep.execute("whoami", []);
      return { ok: result.ok, output: result.output, missing: false };
    } catch (error) {
      return { ok: false, output: "", missing: isMissingExecutable(error) };
    }
  },
  connect: (env) => McpStdioClient.connect({ command: defaultFirekeepCommand(), args: ["gateway"], env, onExit: () => keep.handleExit() }),
  onStatus: (status) => broadcast({ type: "keep-status", status }),
});
const tracker = new ReceiptTracker({
  source: () => (keep.status().state === "connected" ? keep : null),
  readReport: (id) => readReportFile(join(sessionsRoot, `${id}.keep`)),
});
// pass to StudioService:
keep: { tracker, reportPath: (id) => join(sessionsRoot, `${id}.keep`), removeReport: (id) => removeReportFile(join(sessionsRoot, `${id}.keep`)) },
// after the window is created:
void keep.start();
// on quit, next to the existing teardown at ~249:
await keep.stop();
```

`isMissingExecutable(error)` returns true when `error.code === "ENOENT"`, or when the message contains "ENOENT" or "not recognized". Add it to `firekeep-client.ts` and unit-test it with both shapes.

`onExit` fires only for a gateway that had initialized. A child that dies during startup is handled by the backoff loop itself, so failures cannot storm. It calls `keep.handleExit()`, which restarts through `start()` → `#connectWithBackoff` (1 s / 5 s / 30 s), unless `stop()` ran first on quit.

- [ ] **Step 4: Run.** Run: `npx vitest run && npm run typecheck`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A src tests
git commit -m "feat(studio): typed Keep IPC, recent workspaces, quick mission, gateway wiring"
```

---

## Phase D — Renderer

All renderer tests live in `tests/app.test.tsx` (jsdom) unless stated otherwise.

Each new action is mocked in `installBridge`:
- `keep.status` → `{ type: "keep-status", status: options.keepStatus ?? { state: "missing" } }`
- `keep.highlights` → `{ type: "keep-highlights", highlights: { online: "", bulletin: "", paused: [] } }`
- `workspace.detectTest` → `{ type: "test-command", command: options.testCommand ?? null }`

Add `keepStatus`, `testCommand` and `keepItems` to the `installBridge` options type in the first renderer task that needs them (Task 12).

### Task 12: Shell, top bar, session rail, Keep panel slot, ⌘\\

**Files:**
- Create: `src/renderer/src/layout/StudioShell.tsx`, `src/renderer/src/layout/TopBar.tsx`, `src/renderer/src/rail/SessionRail.tsx`
- Modify:
  - `src/renderer/src/App.tsx`: titlebar 482-494 → `TopBar`; rail 496-526 → `SessionRail`; inspector 577-595 → removed (the Keep panel slot replaces it)
  - `src/renderer/src/styles.css`: grid at 63-64; the drawer media query at 432-438 changes to `max-width: 1100px` and targets `.keep-panel`
- Tests: `tests/app.test.tsx`

**Interfaces:**
- `StudioShell({ topBar, rail, conversation, keepPanel, keepPanelOpen }: { topBar: ReactNode; rail: ReactNode; conversation: ReactNode; keepPanel: ReactNode; keepPanelOpen: boolean })` renders `<div className="studio" data-keep-open={String(keepPanelOpen)}>`. The four regions use classes `titlebar`, `session-rail`, `conversation`, and `keep-panel` (the last rendered only when open).
- `TopBar({ workspacePath, recentWorkspaces, onChooseFolder, onSwitchWorkspace, onOpenSettings, update })`. The repo picker is a `<button className="repo-picker" aria-label="Workspace: <basename or Home>">` that opens a menu listing the recent workspaces plus "Open folder…". The Settings button has `aria-label="Settings"`. `StudioUpdateButton` (App.tsx 741-772) moves here unchanged. There is no Needs-you pill (piece 4).
- `SessionRail({ sessions, activeSessionId, onNew, onResume, onEdit, keepWarning })` holds the session list and `SessionEditor` (moved from App 498-510 and 778-789). `keepWarning` (a string or null) renders one `.keep-warning-row` button at the bottom; clicking it opens Settings → Keep connection.
- The rail usage card (512-518), workspace card (519-522), kit block (523-524), the inspector `compact-info` (594) and `LaunchScreen`'s marketing lines (737-739; keep "Loading…" only) are deleted.

- [ ] **Step 1: Write the failing tests**

```ts
it("renders three columns with the Keep panel and toggles it with Ctrl+\\", async () => {
  const invoke = installBridge();
  render(<App />);
  expect(await screen.findByRole("complementary", { name: "The Keep" })).toBeInTheDocument();
  fireEvent.keyDown(window, { key: "\\", code: "Backslash", ctrlKey: true });
  await waitFor(() => expect(screen.queryByRole("complementary", { name: "The Keep" })).not.toBeInTheDocument());
  expect(invoke).toHaveBeenCalledWith({ type: "settings.keepPanel", open: false });
});

it("shows no usage card, workspace card or marketing lines in the rail", async () => {
  installBridge();
  render(<App />);
  await screen.findByRole("button", { name: /Workspace:/ });
  expect(screen.queryByText("Session usage")).not.toBeInTheDocument();
  expect(screen.queryByText("Runtime-neutral core")).not.toBeInTheDocument();
  expect(screen.queryByText(/Agents come and go/)).not.toBeInTheDocument();
});

it("switches to a recent workspace from the repo picker", async () => {
  const invoke = installBridge({ snapshot: { ...state("alpha"), recentWorkspaces: ["C:\\work\\firekeep", "C:\\work\\site"], workspacePath: "C:\\work\\firekeep", keepPanelOpen: true } as never });
  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "Workspace: firekeep" }));
  fireEvent.click(screen.getByRole("menuitem", { name: /site/ }));
  expect(invoke).toHaveBeenCalledWith({ type: "workspace.set", path: "C:\\work\\site" });
});
```

Update the shared `state()` helper to include `recentWorkspaces: []` and `keepPanelOpen: true`, and remove `voiceEnabled`.

- [ ] **Step 2: Run to verify they fail.** Run: `npx vitest run tests/app.test.tsx`. Expected: the three new tests FAIL.

- [ ] **Step 3: Implement.** Create the three components with the props above. The Keep panel slot in this task is a placeholder `<aside className="keep-panel" aria-label="The Keep"><KeepPanelSlot /></aside>` whose content arrives in Task 17; for now it renders `<h2>The Keep</h2>`.

In `App.tsx`:
- Replace the old ⌘\\ grid binding in the keyboard effect with:

```ts
} else if ((event.ctrlKey || event.metaKey) && event.code === "Backslash") {
  event.preventDefault();
  const open = !(snapshot?.keepPanelOpen ?? true);
  void invoke({ type: "settings.keepPanel", open });
}
```

- Replace `rightOpen` state with `snapshot.keepPanelOpen`.

CSS:
- Change `.studio` to `grid-template: 44px minmax(0, 1fr) / 232px minmax(520px, 1fr) 320px; grid-template-areas: "title title title" "rail conversation keep";`.
- When `[data-keep-open="false"]`, use two columns.
- Rename the `inspector` area to `keep`.
- Move the 1120 px media block to `@media (max-width: 1100px)`, with `.keep-panel` taking the fixed drawer rules previously on `.inspector`.

- [ ] **Step 4: Run.** Run: `npx vitest run && npm run typecheck`. Expected: PASS. Delete old tests that asserted the inspector toggle and rail usage (`app.test.tsx` 135, 318-334, 470-481); their behaviour moves to Tasks 13 and 18, whose tests replace them.

- [ ] **Step 5: Commit**

```bash
git add -A src/renderer tests
git commit -m "feat(studio): three-column shell, repo picker, Keep panel slot (Ctrl+\\)"
```

### Task 13: Composer, Agent chip, usage line

**Files:**
- Create: `src/renderer/src/composer/Composer.tsx`, `src/renderer/src/composer/AgentChip.tsx`
- Modify:
  - `src/renderer/src/App.tsx`: composer zone 558-574 → `Composer`; toolbar `RuntimePicker` 530 and the Turn controls 586-593 → `AgentChip`
  - Delete `src/renderer/src/RuntimePicker.tsx` once nothing imports it
- Tests: `tests/app.test.tsx` (the `openRuntimeCenter` helper changes, Task 18)

**Interfaces:**
- `AgentChip({ runtimes, diagnostics, primaryRuntimeId, models, selectedModel, selectedEffort, permissionMode, onPrimary, onModel, onEffort, onPermission })` renders a `<button className="agent-chip" aria-label="Agent: Claude · Fable · high">`, using the display name, the model display name and the effort. It opens a popover (`role="dialog"`, `aria-label="Agent settings"`) with four `<select>`s labelled Agent, Model, Reasoning and Permissions, which reuse the existing Turn-controls option lists and IPC (`primary.set`, `model.set`, `effort.set`, `permission.set` — the exact action names used at App.tsx 586-593). With no primary, the label is "Choose an agent".
- `Composer({ value, onChange, onSubmit, busy, completions, chips, usage, onExpandUsage })` holds the textarea, the completion menu, and the Paste and Send buttons (moved unchanged from 559-571). `chips` is a ReactNode row: `[<AgentChip/>, <DoneWhenChip/>]`, with the Done-when chip arriving in Task 15. `usage` renders a single `.usage-line` button, "12k fresh · 40k cached", formatted with the existing `formatUsage`/usage summary helpers. Clicking it toggles an inline expansion that contains the old breakdown and the "Set guard" control, which still inserts `/budget `.

- [ ] **Step 1: Write the failing tests**

```ts
it("sets model and effort from the Agent chip popover", async () => {
  const invoke = installBridge({ snapshot: state("alpha"), models: [{ id: "fable", displayName: "Fable", isDefault: true, efforts: ["low", "high"] }] });
  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: /^Agent: Alpha/ }));
  const dialog = screen.getByRole("dialog", { name: "Agent settings" });
  fireEvent.change(within(dialog).getByLabelText("Reasoning"), { target: { value: "high" } });
  expect(invoke).toHaveBeenCalledWith(expect.objectContaining({ runtimeId: "alpha", effort: "high" }));
});

it("shows one usage line that expands to the breakdown", async () => {
  installBridge({ snapshot: { ...state("alpha"), usage: { tokens: 52_000, freshTokens: 12_000, cachedTokens: 40_000, costUsd: 0, runs: 1, totalRuns: 1, measuredRuns: 1, byRuntime: {} } } as never });
  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: /12k fresh · 40k cached/ }));
  expect(screen.getByRole("button", { name: "Set guard" })).toBeInTheDocument();
});
```

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** the two components. Move the Turn-controls `<select>` markup and handlers from App 586-593 into `AgentChip` unchanged except for the wrapper. Remove the toolbar's reviewer strip (531-536); Task 14 replaces it with Second opinion, and Task 18 moves reviewer settings.
- [ ] **Step 4: Run.** Run: `npx vitest run && npm run typecheck`. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add -A src/renderer tests
git commit -m "feat(studio): composer with Agent chip and one-line usage"
```

### Task 14: Conversation — Keep receipt card, Second opinion, inline mission stepper

**Files:**
- Create: `src/renderer/src/conversation/KeepReceiptCard.tsx`, `src/renderer/src/conversation/MissionStepper.tsx`
- Modify:
  - `src/renderer/src/timeline.ts`: the `receipt` item, and `TimelineRun.receipt`
  - `src/renderer/src/App.tsx`: `RunTimeline` 797-812; `MissionPanel` 679-735 is deleted and its mount at 578-585 is gone
- Tests: `tests/timeline.test.ts`, `tests/app.test.tsx`

**Interfaces:**
- `timeline.ts`:
  - `TimelineItem` gains `TimelineBase & { kind: "receipt"; receipt: KeepReceipt }`, upserted with key `receipt:${runId}` (so the latest receipt for the run wins);
  - `groupTimeline` sets `run.receipt` instead of pushing it to `activity`;
  - `TimelineRun` gains `readonly receipt?: KeepReceipt`.
- `KeepReceiptCard({ receipt, runId, onFeedback, onRetry })`:
  - collapsed, a `<button className="keep-receipt" aria-expanded>` showing `receiptLabel(receipt)`;
  - for `measured` with 0/0, the class `keep-receipt muted`;
  - for `unavailable`, clicking calls `onRetry(runId)` → `keep.receipt.retry`, but only on the newest receipt in the session. Older unavailable receipts show the label without the retry action.
  - Expanded, it shows a list:
    - each recall shows its query, "via prompt hook" or "asked by the agent", and the result count, with 👍 and 👎 buttons (`aria-label="Knowledge was right"` / `"Knowledge was wrong"`, `title="Was this knowledge right?"`);
    - each write shows its summary.
  - A thumb calls `onFeedback(memoryIds, useful)` → `keep.feedback`, then disables both thumbs for that recall (one-shot). On failure it shows an inline "retry" for that recall.
  - Thumbs are hidden for recalls with no `memoryIds` (partial receipts).
- **Second opinion.** The last completed assistant message (and only that one, because `/review` reviews the last answer) gets a hover action, `<button aria-label="Second opinion">`. It opens a one-line chooser: an agent `<select>` over review-capable runtimes, an optional focus `<input placeholder="Focus (optional), e.g. security">`, and Run. Run sends `review.run { runtimeId, focus }`. Reviewer runs (assistant role `reviewer`) render inside a `.second-opinion` card nested under the preceding primary run, not as a new top-level turn.
- `MissionStepper({ mission, onAccept, onPartial, onFailure, onCancel })` renders when `snapshot.mission` is non-null:
  - The steps are Write → Check → Repair n/N → Review → outcome, derived from `mission.phase`, `mission.attempt` and `mission.maxRepairAttempts`, using the same mapping `missionStatus` uses in `slash-commands.ts:694-712`.
  - The buttons are Accept (`mission.complete taskResult:"success"`), Partial (`taskResult:"partial"`), Failure (`taskResult:"failure"`) and Cancel. They appear when the phase awaits human acceptance, the same condition the old `MissionPanel` Approve button used (`App.tsx:728`).
  - It renders at the bottom of the conversation, after the last run.

- [ ] **Step 1: Write the failing tests** — `tests/timeline.test.ts`:

```ts
it("attaches the latest keep.receipt to its run", () => {
  const base = { studioSessionId: "s", runtimeId: "alpha", timestamp: "t" };
  const receipt = (final: boolean) => ({ status: "measured", final, recalls: [], writes: [], recalledCount: final ? 2 : 1, learnedCount: 0, keepSessionIds: [], cursorsAfter: {} });
  const runs = groupTimeline(buildTimeline([
    { ...base, id: "1", runId: "r1", payload: { kind: "message.completed", messageId: "m", role: "assistant", text: "hi" } },
    { ...base, id: "2", runId: "r1", payload: { kind: "keep.receipt", receipt: receipt(false) } },
    { ...base, id: "3", runId: "r1", payload: { kind: "keep.receipt", receipt: receipt(true) } },
  ] as never));
  expect(runs[0]?.receipt?.recalledCount).toBe(2);
  expect(runs[0]?.activity).toHaveLength(0);
});
```

`tests/app.test.tsx`:

```ts
it("renders the receipt and sends one-shot knowledge feedback", async () => {
  const receipt = { status: "measured", final: true, recalls: [{ query: "deploy vps", trigger: "prompt-hook", resultCount: 2, memoryIds: ["m1", "m2"] }], writes: [], recalledCount: 2, learnedCount: 0, keepSessionIds: ["k1"], cursorsAfter: { k1: 3 } };
  const invoke = installBridge({ snapshot: state("alpha"), events: [
    { id: "1", runId: "r1", studioSessionId: "session-1", runtimeId: "alpha", timestamp: "t", payload: { kind: "message.completed", messageId: "a", role: "assistant", text: "Done." } },
    { id: "2", runId: "r1", studioSessionId: "session-1", runtimeId: "alpha", timestamp: "t", payload: { kind: "keep.receipt", receipt } },
  ] as never });
  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "Keep · recalled 2 · learned 0" }));
  expect(screen.getByText("deploy vps")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Knowledge was right" }));
  expect(invoke).toHaveBeenCalledWith({ type: "keep.feedback", memoryIds: ["m1", "m2"], useful: true });
  await waitFor(() => expect(screen.getByRole("button", { name: "Knowledge was right" })).toBeDisabled());
});

it("an unavailable receipt retries", async () => {
  const receipt = { status: "unavailable", final: true, recalls: [], writes: [], recalledCount: 0, learnedCount: 0, keepSessionIds: [], cursorsAfter: {}, detail: "timeout" };
  const invoke = installBridge({ snapshot: state("alpha"), events: [{ id: "2", runId: "r1", studioSessionId: "session-1", runtimeId: "alpha", timestamp: "t", payload: { kind: "keep.receipt", receipt } }] as never });
  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "Keep receipt unavailable · retry" }));
  expect(invoke).toHaveBeenCalledWith({ type: "keep.receipt.retry", runId: "r1" });
});

it("asks for a second opinion on the last answer", async () => {
  const invoke = installBridge({ snapshot: state("alpha"), events: [{ id: "1", runId: "r1", studioSessionId: "session-1", runtimeId: "alpha", timestamp: "t", payload: { kind: "message.completed", messageId: "a", role: "assistant", text: "Answer" } }] as never });
  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "Second opinion" }));
  fireEvent.change(screen.getByPlaceholderText(/Focus \(optional\)/), { target: { value: "security" } });
  fireEvent.click(screen.getByRole("button", { name: "Run" }));
  expect(invoke).toHaveBeenCalledWith({ type: "review.run", runtimeId: "beta", focus: "security" });
});

it("renders mission progress inline and accepts on the turn", async () => {
  const mission = { ...draftMission([{ id: "c1", name: "npm test", command: "npm test", timeoutMs: 60_000 }] as never), phase: "awaiting_acceptance" };
  const invoke = installBridge({ snapshot: { ...state("alpha"), mission } as never });
  render(<App />);
  expect(await screen.findByRole("list", { name: "Mission progress" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Accept" }));
  expect(invoke).toHaveBeenCalledWith(expect.objectContaining({ type: "mission.complete", taskResult: "success" }));
});
```

Use the real awaiting-acceptance phase name from `src/core/mission.ts`. `"awaiting_acceptance"` is illustrative; check `MissionSnapshot["phase"]` and the old `MissionPanel` Approve condition. The `review.run` action's exact field names are at `ipc.ts:52`; match them.

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** `timeline.ts` changes, the two components, the Second opinion chooser inside `RunTimeline`, and delete `MissionPanel`. `/mission …` commands keep working (no change to `slash-commands.ts`).
- [ ] **Step 4: Run.** Run: `npx vitest run && npm run typecheck`. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add -A src/renderer tests
git commit -m "feat(studio): Keep receipts, Second opinion and inline mission stepper"
```

### Task 15: "Done when…" chip

**Files:**
- Create: `src/renderer/src/composer/DoneWhenChip.tsx`
- Modify: `src/renderer/src/App.tsx` (`submit` 329-349)
- Tests: `tests/app.test.tsx`

**Interfaces:**
- `DoneWhenChip({ checks, onChange, detect })`:
  - `checks: readonly string[]`, renderer-local state in App, cleared after Send;
  - `detect: () => Promise<string | null>`, which calls `workspace.detectTest`.
  - Collapsed label: "Done when…" when there are no checks, otherwise "Done when: npm test ✓" (the first check, plus "+n").
  - Clicking opens a popover (`aria-label="Done when"`) with:
    - an input pre-filled from `detect()`, or empty with placeholder `e.g. npm test`;
    - "Add check";
    - a list of the current checks with remove buttons;
    - a "More" `<details>` holding the name and timeout fields. These only label the check for now: `mission.quick` takes commands only; the fields are kept for parity and passed through when `mission.quick` grows them.
- **Submit.** With `checks.length > 0`, Send calls `mission.quick { goal: composer, checks }` instead of `message.send`. With checks set but none confirmed, Send is disabled. Pressing Enter with an empty confirmed list behaves as normal chat.

- [ ] **Step 1: Write the failing tests**

```ts
it("proposes the detected test command and sends a quick mission", async () => {
  const invoke = installBridge({ snapshot: state("alpha"), testCommand: "npm test" });
  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "Done when…" }));
  const popover = screen.getByRole("dialog", { name: "Done when" });
  await waitFor(() => expect(within(popover).getByRole("textbox")).toHaveValue("npm test"));
  fireEvent.click(within(popover).getByRole("button", { name: "Add check" }));
  fireEvent.change(screen.getByRole("textbox", { name: /Message/i }), { target: { value: "Fix the flaky test" } });
  fireEvent.click(screen.getByRole("button", { name: /Send/i }));
  expect(invoke).toHaveBeenCalledWith({ type: "mission.quick", goal: "Fix the flaky test", checks: ["npm test"] });
});

it("leaves the field empty with a hint when nothing is detected", async () => {
  installBridge({ snapshot: state("alpha"), testCommand: null });
  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "Done when…" }));
  expect(screen.getByPlaceholderText("e.g. npm test")).toHaveValue("");
});
```

Use the composer textarea's accessible name from the existing App (`getByRole("textbox", …)` matching its `aria-label`).

- [ ] **Step 2–4:** Run (FAIL), implement, run (PASS), then `npm run typecheck`.
- [ ] **Step 5: Commit**

```bash
git add -A src/renderer tests
git commit -m "feat(studio): Done when… chip replaces the Missions panel"
```

### Task 16: Start card (first run / empty session)

**Files:**
- Create: `src/renderer/src/start/StartCard.tsx`
- Modify: `src/renderer/src/App.tsx` (`Welcome` 774-776 is deleted; its mount at 549 renders `StartCard`)
- Tests: `tests/app.test.tsx` (replace the Welcome assertions at 127-129, 143, 153)

**Interfaces:**
- `StartCard({ runtimes, diagnostics, primaryRuntimeId, recentWorkspaces, workspacePath, highlights, keepStatus, onPrimary, onConnect, onChooseFolder, onSwitchWorkspace, onOpenSession })`.
- **Agent row**, from the runtime probes:
  - exactly one ready runtime that is not yet primary: Studio calls `onPrimary` once, automatically, then shows "Using Alpha · change";
  - several ready: one card per runtime (`role="button"`, name "Use Alpha");
  - none ready: each card shows "Connect" → `onConnect(runtimeId)` (the existing connect dialog).
- **Workspace row.** Chips for `recentWorkspaces` → `onSwitchWorkspace`, plus "Open folder…" → `onChooseFolder`. The current workspace chip is `aria-pressed="true"`.
- **Keep highlights.** When `keepStatus.state === "connected"`, it shows:
  - "Online now" (`highlights.online` rendered through `RichMarkdown`);
  - "From the bulletin" (`highlights.bulletin`);
  - "Pick up where you left off": a list of `highlights.paused`, where each item is a button whose `onOpenSession(goal)` puts `Continue: <goal>` into the composer. The plan deliberately does not map Keep sessions to Studio sessions; that is out of scope.
- In any other Keep state, this area shows nothing. The Keep panel carries the connect card (Task 17).

- [ ] **Step 1: Write the failing tests**

```ts
it("preselects the only ready runtime and shows 'Using … · change'", async () => {
  const invoke = installBridge({ snapshot: state(null), readyRuntimeIds: ["alpha"] });
  render(<App />);
  expect(await screen.findByText(/Using Alpha/)).toBeInTheDocument();
  expect(invoke).toHaveBeenCalledWith({ type: "primary.set", runtimeId: "alpha" });
});

it("offers one-click cards when several runtimes are ready", async () => {
  const invoke = installBridge({ snapshot: state(null) });
  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "Use Beta" }));
  expect(invoke).toHaveBeenCalledWith({ type: "primary.set", runtimeId: "beta" });
});

it("shows Keep highlights only when connected", async () => {
  installBridge({ snapshot: state("alpha"), keepStatus: { state: "connected", agentId: "a", personalMode: false, kitVersion: "1.8.0" }, highlights: { online: "codex@laptop — release notes", bulletin: "Deployed v2.3", paused: [{ sessionId: "k1", goal: "Fix recall timeout" }] } });
  render(<App />);
  expect(await screen.findByText("Online now")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /Fix recall timeout/ }));
  expect(screen.getByRole("textbox", { name: /Message/i })).toHaveValue("Continue: Fix recall timeout");
});
```

Extend `installBridge`:
- `readyRuntimeIds?: string[]`: `runtime.probe` marks the others `{ state: "missing" }`;
- `highlights?: KeepHighlights`.

- [ ] **Step 2–4:** Run (FAIL), implement, run (PASS), typecheck.
- [ ] **Step 5: Commit**

```bash
git add -A src/renderer tests
git commit -m "feat(studio): Start card replaces the hero"
```

### Task 17: Keep panel (Memory / Skills / Team, states)

**Files:**
- Create: `src/renderer/src/keep/KeepPanel.tsx`
- Modify: `src/renderer/src/App.tsx` (the panel slot from Task 12, and the `keep-status` push subscription in the effect at 195-237)
- Tests: `tests/app.test.tsx`

**Interfaces:**
- `KeepPanel({ status, receipts, invoke, onInsert, onOpenKeepSettings })`:
  - `receipts` is this session's `KeepReceipt[]`, newest first;
  - `invoke` is the App's typed `invoke`;
  - `onInsert(text)` appends a quoted block (`> ` per line) to the composer.
- **States** (spec §7.5):
  - `missing`: the heading "Connect Studio to your team's Keep" and one sentence: "The Keep is your team's shared memory: what agents learned, the skills they built, and who is working on what." Then both `KEEP_INSTALL_COMMANDS` with `CopyButton`s (from App 948-962, moved to a shared module if needed), and "I've installed it — retry" → `keep.retry`.
  - `unconfigured` or `unreachable`: the `detail` line, then "Run doctor" → `onOpenKeepSettings()`. For `unreachable` with a non-null `retryInMs`, show "Retrying…".
  - `connected` with `personalMode`: the banner "Personal mode — nothing from this session is shared" above the tabs.
  - `starting`: a skeleton.
- **Tabs** (`role="tablist"`, labelled Memory, Skills and Team). The open tab persists in `localStorage` under `"firekeep.keepTab"`, wrapped in try/catch.
  - **Memory.**
    - Default list: the recalls from `receipts` (query + count). Clicking one fills the search box and runs it.
    - Search box (`aria-label="Search team memory"`): debounced 300 ms, calls `keep.search { kind: "memory" }`.
    - Results: `KeepItem` titles. Clicking a result opens a read-only detail popover with the body (`RichMarkdown`) and "Insert into composer".
  - **Skills.**
    - Default: when the tab first opens, `keep.skills` (the kit's `skill_list`, active skills). An item whose body contains `[TRIAL]` gets a `TRIAL` badge.
    - Search: debounced, `kind: "skill"`.
  - **Team.** `keep.team` markdown via `RichMarkdown`, refreshed when the tab opens. No search box.

- [ ] **Step 1: Write the failing tests**

```ts
it("shows the connect card when the kit is missing, and retries", async () => {
  const invoke = installBridge({ keepStatus: { state: "missing" } });
  render(<App />);
  expect(await screen.findByRole("heading", { name: "Connect Studio to your team's Keep" })).toBeInTheDocument();
  expect(screen.getByText(/irm https:\/\/firekeep.ai\/latest\/install.ps1/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "I've installed it — retry" }));
  expect(invoke).toHaveBeenCalledWith({ type: "keep.retry" });
});

it("searches team memory after a 300 ms debounce and inserts a result", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const invoke = installBridge({ keepStatus: { state: "connected", agentId: "a", personalMode: false, kitVersion: "1.8.0" }, keepItems: [{ title: "VPS is on the tailnet", body: "VPS is on the tailnet\nuse srv1143982" }] });
  render(<App />);
  fireEvent.change(await screen.findByRole("searchbox", { name: "Search team memory" }), { target: { value: "vps" } });
  expect(invoke).not.toHaveBeenCalledWith(expect.objectContaining({ type: "keep.search" }));
  await act(async () => { vi.advanceTimersByTime(310); });
  expect(invoke).toHaveBeenCalledWith({ type: "keep.search", query: "vps", kind: "memory" });
  fireEvent.click(await screen.findByRole("button", { name: "VPS is on the tailnet" }));
  fireEvent.click(screen.getByRole("button", { name: "Insert into composer" }));
  expect(screen.getByRole("textbox", { name: /Message/i })).toHaveValue("> VPS is on the tailnet\n> use srv1143982\n");
  vi.useRealTimers();
});

it("shows the personal-mode banner", async () => {
  installBridge({ keepStatus: { state: "connected", agentId: "a", personalMode: true, kitVersion: "1.8.0" } });
  render(<App />);
  expect(await screen.findByText("Personal mode — nothing from this session is shared")).toBeInTheDocument();
});

it("updates live from keep-status push events", async () => {
  installBridge({ keepStatus: { state: "starting" } });
  render(<App />);
  act(() => pushStudioEvent?.({ type: "keep-status", status: { state: "unreachable", detail: "cortex unreachable at 100.91.3.51", retryInMs: null } }));
  expect(await screen.findByText(/cortex unreachable/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Run doctor" })).toBeInTheDocument();
});
```

Extend `installBridge`:
- `keepItems?: KeepItem[]`, returned for `keep.search`;
- `keep.team` → `{ type: "keep-team", markdown: "" }`;
- `keep.skills` → `{ type: "keep-items", items: options.keepItems ?? [] }`;
- `keep.retry` → `{ type: "keep-status", status: options.keepStatus ?? { state: "missing" } }`.

- [ ] **Step 2–4:** Run (FAIL), implement, run (PASS), typecheck.
- [ ] **Step 5: Commit**

```bash
git add -A src/renderer tests
git commit -m "feat(studio): Keep panel — memory, skills, team, connection states"
```

### Task 18: Settings sheet

**Files:**
- Create: `src/renderer/src/settings/SettingsSheet.tsx`
- Modify: `src/renderer/src/App.tsx`. `RuntimeManagerDialog` (619-677) moves into the sheet as the "Agents & accounts" section, keeping its internals. The theme button (titlebar) moves to Appearance. `StudioUpdateButton` remains in the top bar, and its detail also renders under "Studio updates".
- Tests: `tests/app.test.tsx`. Update the `openRuntimeCenter` helper to: click `Settings` → click the tab "Agents & accounts" → return `getByRole("region", { name: "Agents & accounts" })`. Every existing Runtime Center test keeps passing through the helper.

**Interfaces:**
- `SettingsSheet({ open, section, onClose, ...sectionProps })` is a modal `role="dialog"` with `aria-label="Settings"` and a left `role="tablist"` of five sections:
  - **Agents & accounts**: the former Runtime Center, including connect and disconnect.
  - **Keep connection**: `KeepStatus` details, plus buttons that run the existing kit commands through `command.run` (the action the composer uses). "Run doctor" → `/firekeep doctor`; Personal mode toggle → `/firekeep personal toggle`; "Update kit" → `/firekeep update`. The output renders as a `CommandResultCard` inside the section. The Dashboard link uses the existing open-external action when `bootstrap.dashboardAvailable`.
  - **Reviews**: the after-turn mode `<select>` (`off | manual | after-turn`, the existing `reviewer-mode` IPC) and the default reviewer list (add/remove, the existing `reviewer.add`/`reviewer.remove` IPC).
  - **Appearance**: theme (`system | dark | light`).
  - **Studio updates**: the `StudioUpdateButton` state and its "Check now" action.
- `section` can be set externally: the rail warning row and the Keep panel's "Run doctor" open it on "Keep connection".

- [ ] **Step 1: Write the failing tests**

```ts
it("opens Settings on Keep connection from the panel and runs doctor", async () => {
  const invoke = installBridge({ keepStatus: { state: "unreachable", detail: "401 unauthorised", retryInMs: null } });
  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "Run doctor" }));
  const sheet = screen.getByRole("dialog", { name: "Settings" });
  expect(within(sheet).getByRole("tab", { name: "Keep connection", selected: true })).toBeInTheDocument();
  fireEvent.click(within(sheet).getByRole("button", { name: "Run doctor" }));
  expect(invoke).toHaveBeenCalledWith(expect.objectContaining({ type: "command.run", input: "/firekeep doctor" }));
});

it("moves after-turn review to Settings → Reviews", async () => {
  const invoke = installBridge({ snapshot: state("alpha") });
  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "Settings" }));
  fireEvent.click(screen.getByRole("tab", { name: "Reviews" }));
  fireEvent.change(screen.getByLabelText("After each turn"), { target: { value: "after-turn" } });
  expect(invoke).toHaveBeenCalledWith(expect.objectContaining({ mode: "after-turn" }));
});
```

Match `command.run`'s exact field name (`input` or `text`) to `ipc.ts`.

- [ ] **Step 2–4:** Run (FAIL), implement, run (PASS), typecheck. Existing Runtime Center tests pass through the updated `openRuntimeCenter` helper.
- [ ] **Step 5: Commit**

```bash
git add -A src/renderer tests
git commit -m "feat(studio): Settings sheet (agents, Keep connection, reviews, appearance, updates)"
```

### Task 19: ⌘K command palette

**Files:**
- Create: `src/renderer/src/palette/CommandPalette.tsx`
- Modify: `src/renderer/src/App.tsx` (keyboard effect 304-327: ⌘K opens the palette instead of inserting `/`)
- Tests: `tests/app.test.tsx`, plus a new `tests/command-palette.test.ts` for ranking

**Interfaces:**
- `rankCommands(query: string, items: readonly CommandCompletion[]): CommandCompletion[]`, exported from `CommandPalette.tsx`. It is a case-insensitive subsequence match on `label`. Score: a prefix match beats a word-start match, which beats a subsequence match; ties sort alphabetically. An empty query returns all items alphabetically.
- `CommandPalette({ open, onClose, completions, onRun, onSearchKeep })` is a dialog with `aria-label="Command palette"`. Its source is `command.complete { input: "/" }`, the full list. The first row is always "Search the Keep…": it focuses the Keep panel's search box with the typed query (opening the panel if closed, through `settings.keepPanel`). Enter on a command puts its `value` plus a space into the composer, so arguments can be typed. Escape closes.

- [ ] **Step 1: Write the failing tests** — `tests/command-palette.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { rankCommands } from "../src/renderer/src/palette/CommandPalette.js";

const items = ["/doctor", "/mission", "/review", "/reviewer", "/session"].map((value) => ({ value, label: value, description: "" }));

describe("rankCommands", () => {
  it("ranks prefix over word-start over subsequence", () => {
    expect(rankCommands("rev", items).map((item) => item.value)).toEqual(["/review", "/reviewer"]);
    expect(rankCommands("ms", items).map((item) => item.value)).toEqual(["/mission", "/session"]);
  });
  it("returns everything alphabetically for an empty query", () => {
    expect(rankCommands("", items).map((item) => item.value)).toEqual(["/doctor", "/mission", "/review", "/reviewer", "/session"]);
  });
});
```

`tests/app.test.tsx`:

```ts
it("Ctrl+K opens the palette with Search the Keep first", async () => {
  installBridge({ snapshot: state("alpha") });
  render(<App />);
  await screen.findByRole("button", { name: /^Agent:/ });
  fireEvent.keyDown(window, { key: "k", ctrlKey: true });
  const palette = await screen.findByRole("dialog", { name: "Command palette" });
  expect(within(palette).getAllByRole("option")[0]).toHaveTextContent("Search the Keep…");
});
```

- [ ] **Step 2–4:** Run (FAIL), implement, run (PASS), typecheck.
- [ ] **Step 5: Commit**

```bash
git add -A src/renderer tests
git commit -m "feat(studio): Ctrl+K command palette with Search the Keep"
```

---

## Phase E — Smoke, docs, release

### Task 20: Packaged smoke for the new shell

**Files:**
- Modify: `scripts/package-smoke.mjs` (layout 228-285, responsive inspector 487-520, flow 42-103), `tests/release-workflow.test.ts:44-53`

**Interfaces:**
- New smoke steps, replacing the grid and inspector steps:
  1. **Layout.** `.titlebar`, `.session-rail`, `.conversation` and `.keep-panel` are all visible at 1440×900. `.keep-panel` is to the right of `.conversation`, at least 280 px wide.
  2. **Keep panel collapse.** Press Ctrl+\\: `.keep-panel` is gone and `.conversation` grows by at least 250 px. Press Ctrl+\\ again: it is back.
  3. **Kit-not-installed state.** The panel heading text equals "Connect Studio to your team's Keep". The smoke runs with `PATH` stripped of `firekeep` and `USERPROFILE`/`HOME` pointed at a temp dir, so no kit is found.
  4. **Start card.** `.start-card` is visible in an empty session.
  5. **Palette.** Ctrl+K: `[role=dialog][aria-label="Command palette"]` appears; Escape closes it.
  6. **Responsive drawer.** At 1000×760, `.keep-panel` is `position: fixed`, flush right, at least 280 px wide. This replaces `assertResponsiveInspector`, keeping its tolerance logic.
  7. **Both themes and the `/help` command card**, kept as today.
- Screenshot env vars:
  - keep `FIREKEEP_STUDIO_SMOKE_SCREENSHOT`, `_LIGHT_SCREENSHOT` and `_COMMAND_SCREENSHOT`;
  - drop `_GRID_SCREENSHOT` and `_SESSION_SCREENSHOT` only if the session editor step was removed (keep it if the editor still exists in `SessionRail`).

- [ ] **Step 1: Update `tests/release-workflow.test.ts:44-53`** to assert the new strings: `"keep-panel"`, `"Connect Studio to your team's Keep"`, `"Command palette"`, and that none of `"agent-grid"` or `"inspector-toggle"` remain.

```ts
it("package smoke covers the 0.5 shell", () => {
  const smoke = readFileSync(new URL("../scripts/package-smoke.mjs", import.meta.url), "utf8");
  for (const text of ["keep-panel", "Connect Studio to your team's Keep", "Command palette", "start-card"]) expect(smoke).toContain(text);
  for (const text of ["agent-grid", "inspector-toggle"]) expect(smoke).not.toContain(text);
});
```

- [ ] **Step 2: Run to verify it fails.** Run: `npx vitest run tests/release-workflow.test.ts`. Expected: FAIL.
- [ ] **Step 3: Rewrite the smoke steps** listed above in `package-smoke.mjs`, reusing its existing CDP helpers (`evaluate`, viewport setters and `assert*` style). Selector changes: `.runtime-picker` → `.agent-chip`; `.inspector-toggle` → keyboard toggle; `.session-row`/`.session-editor` stay if `SessionRail` kept the editor (it does, Task 12).
- [ ] **Step 4: Run**

```bash
npx vitest run tests/release-workflow.test.ts
npm run package && npm run smoke:package
```

Expected: PASS. The smoke prints its JSON summary with `keepPanel`, `startCard`, `palette` and `drawer` fields.

- [ ] **Step 5: Commit**

```bash
git add scripts/package-smoke.mjs tests/release-workflow.test.ts
git commit -m "test(studio): packaged smoke asserts the 0.5 shell and kit-missing state"
```

### Task 21: Live smoke — Keep probe

**Files:**
- Modify: `tests/live-runtimes.test.ts` (64 lines; runs only with `STUDIO_LIVE_RUNTIMES=1`)

**Interfaces:**
- Consumes: `KeepGateway`, `McpStdioClient`, `FirekeepClient`.

- [ ] **Step 1: Add the test**

```ts
it.runIf(process.env.STUDIO_LIVE_RUNTIMES === "1")("connects to the Keep through the studio toolset", async () => {
  const client = new FirekeepClient();
  const gateway = new KeepGateway({
    command: defaultFirekeepCommand(),
    whoami: async () => { const r = await client.execute("whoami", []); return { ok: r.ok, output: r.output, missing: false }; },
    connect: (env) => McpStdioClient.connect({ command: defaultFirekeepCommand(), args: ["gateway"], env }),
    sleep: async () => undefined,
  });
  const status = await gateway.start();
  expect(status.state).toBe("connected");
  expect(await gateway.team()).toBeTypeOf("string");
  const page = await gateway.timeline("nonexistent-session", 0, 1);
  expect(["page", "unsupported"]).toContain(page.kind);   // "unsupported" until server v1.6.2 is deployed
  await gateway.stop();
}, 60_000);
```

- [ ] **Step 2: Run on this workstation** (it has kit 1.8.0 once plan 2 has shipped):

Run: `npm run smoke:runtimes`
Expected: PASS. If `timeline` returns `unsupported`, the server is older than v1.6.2. That is expected before plan 1 has deployed, and it blocks Task 22's publish step.

- [ ] **Step 3: Verify every runtime forwards the report variable (manual).** The receipt mechanism assumes each runtime passes its environment into the MCP servers it starts. Codex does so through the kit's `env_vars`; Claude and Kiro are assumed to inherit. With kit 1.8.0 installed, run `npm start`. Then, for each of Codex, Claude and Kiro in turn: make it the agent, send one prompt that makes it call `ctx_start_session` (for example "start a Keep session for: smoke test"), and check `<userData>/sessions/<studioSessionId>.keep` (`%APPDATA%/Firekeep Studio/sessions/` on Windows).
   Expected: one new line per runtime. If a runtime adds no line, its MCP launch drops the environment. Record which one, and treat receipts for that runtime as "partial" in the README until the kit renders an explicit pass-through for it.

- [ ] **Step 4: Commit**

```bash
git add tests/live-runtimes.test.ts
git commit -m "test(studio): live smoke probes the Keep gateway"
```

### Task 22: Docs, version 0.5.0, release

**Files:**
- Modify:
  - `studio/README.md`: the 0.5 section; runtime table row 109 (delete Grok); lines 53-67, 79-81, 163-164, 175-187, 227, 232, 239-243 (voice, grid, compare, usage card, hero)
  - `studio/package.json:3`
  - root `CLAUDE.md` (the Studio row in the Architecture table)

**Interfaces:**
- Produces: the Studio 0.5.0 release through `.github/workflows/studio-release.yml`, the existing tag flow. Use the tag pattern of the last release (`studio-v0.4.1`).

- [ ] **Step 1: README.** Add a "0.5 — Window into the Keep" section at the top of the feature history with:
  - Layout: sessions rail, conversation, Keep panel (Ctrl+\\), the drawer below 1100 px, and the Ctrl+K palette.
  - Start card, Agent chip, "Done when…" (mission from the composer; same confirmation dialog, receipt and human acceptance), and Second opinion.
  - The Keep link: the `firekeep gateway` `studio` toolset; the `<agent>-studio-panel` label; per-turn receipts from replay, with the partial and unavailable meanings; feedback through `memory_feedback`.
  - Requirements: client kit ≥ 1.8.0 and server ≥ v1.6.2 for full receipts; older versions give "partial".
  - Removed: voice, Grok, `/compare`, `/consensus`, the Agents grid. Studio stores no secrets.
  - Security bullets: Studio never reads `~/.firekeep/config`; typed IPC only; Studio calls only the nine preset tools.

  Delete the stale sections listed above.

- [ ] **Step 2: Root `CLAUDE.md`.** Replace the Studio row's description with:

```markdown
| Firekeep Studio | `studio/` | none (local desktop) | Window into the Keep: a three-column desktop console (sessions · conversation · Keep panel) over Codex App Server, Claude stream JSON and Kiro ACP. It reads the Keep through the kit's own `firekeep gateway` (`FIREKEEP_TOOLSET=studio`, panel label `<agent>-studio-panel`) and shows per-turn Keep receipts built from replay (`replay_timeline` `format="json"`, server ≥ v1.6.2; kit ≥ 1.8.0 reports agent Keep sessions through `FIREKEEP_SESSION_REPORT_FILE`). "Done when…" runs Missions from the composer. Studio stores no secrets; the Python Client Kit remains the Keep and dashboard connection source. |
```

- [ ] **Step 3: Version.** Set `package.json` `"version": "0.5.0"`, and update every test that pins `0.4.x` strings where it means "current version" (`grep -rn "0\.4\.[01]" tests src`). Leave historical strings, such as the update-state fixtures in `app.test.tsx` that model an older installed version.

- [ ] **Step 4: Full verification**

```bash
npm test
npm run typecheck
npm run package && npm run smoke:package
npm run smoke:runtimes
```

Expected: all PASS. Record the smoke JSON summary in the PR description.

- [ ] **Step 5: Final review before release.** Dispatch a fresh reviewer on the whole branch diff against `main`. Check in particular:
  - no IPC action accepts a tool name;
  - `KeepGateway` cannot call a tool outside `STUDIO_TOOLS`;
  - every Keep failure path leaves turns working;
  - the README matches the behaviour.

- [ ] **Step 6: Prerequisite gate.** Confirm that:
  - server `v1.6.2` is deployed: Task 21's `timeline` returned `page`, not `unsupported`;
  - client `1.8.0` is published: `firekeep update` installs it and `firekeep whoami --json` works.

  If either is missing, stop: merging is fine, publishing is not.

- [ ] **Step 7: Commit, PR, merge, tag**

```bash
git add -A studio CLAUDE.md
git commit -m "release(studio): 0.5.0 — window into the Keep"
git push -u origin HEAD
gh pr create --title "Studio 0.5.0 — window into the Keep" --body "Plan docs/superpowers/plans/2026-10-11-studio-05-p3-studio.md; spec docs/superpowers/specs/2026-10-11-studio-keep-window-design.md. Requires server v1.6.2 and client 1.8.0 (both published).

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
gh pr checks --watch
gh pr merge --rebase
git checkout main && git pull --ff-only
git tag studio-v0.5.0 && git push origin studio-v0.5.0
```

Expected: `studio-release.yml` publishes installers, `SHA256SUMS` and the signed update manifest. Verify the signed manifest the same way 0.4.1 was verified: pinned Ed25519 signature and version, then compare digests against the GitHub assets.
