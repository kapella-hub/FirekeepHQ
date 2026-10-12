import { describe, expect, it, vi } from "vitest";
import { RuntimeRegistry } from "../src/core/runtime-registry.js";
import { MemorySessionStore } from "../src/core/session-store.js";
import { MemorySettingsStore } from "../src/core/settings-store.js";
import { createCommandRegistry } from "../src/core/slash-commands.js";
import { StudioService, type StudioPersistedState } from "../src/core/studio-service.js";
import { StudioController, parseStudioAction, type KeepPanelSource } from "../src/main/ipc-controller.js";
import type { KeepStatus } from "../src/shared/keep.js";
import type { DecisionBoardTransport } from "../src/main/decision-board-client.js";
import type { StudioUpdateControl } from "../src/main/studio-updater.js";
import { FakeRuntime } from "./helpers/fake-runtime.js";

async function controller(openExternal: (url: string) => Promise<void> = async () => undefined, dashboardUrl: string | null = "http://keep.example:8040/", decisionBoards?: DecisionBoardTransport, clipboard?: { readText(): string; writeText(text: string): void }, updates?: StudioUpdateControl, keep: KeepPanelSource | null = null, detectTest: () => Promise<string | null> = async () => null): Promise<StudioController> {
  const service = new StudioService({
    runtimes: new RuntimeRegistry([new FakeRuntime({ id: "alpha", displayName: "Alpha", description: "test", transport: "test", capabilities: ["chat", "review", "models"] })]),
    settings: new MemorySettingsStore<StudioPersistedState>(),
    sessions: new MemorySessionStore(),
    missionChecks: { run: async () => ({ exitCode: 0, signal: null, stdout: "ok", stderr: "", timedOut: false, truncated: false, durationMs: 1 }) },
    confirmMission: async () => true,
    idFactory: (prefix) => `${prefix}-1`,
    now: () => "2026-08-24T00:00:00.000Z",
  });
  await service.initialize();
  return new StudioController(service, createCommandRegistry(service), "0.3.7", openExternal, async () => "C:\\workspace", dashboardUrl, decisionBoards, clipboard, updates, keep, detectTest);
}

describe("Studio IPC controller", () => {
  it("rejects the removed message.sendTo action", () => {
    expect(() => parseStudioAction({ type: "message.sendTo", runtimeId: "alpha", text: "x" })).toThrow();
  });

  it("rejects unknown, oversized, and extra-field actions", () => {
    expect(() => parseStudioAction({ type: "process.run", command: "rm" })).toThrow();
    expect(() => parseStudioAction({ type: "message.send", text: "x", command: "hidden" })).toThrow();
    expect(() => parseStudioAction({ type: "command.execute", input: "x".repeat(20_001) })).toThrow();
    expect(parseStudioAction({ type: "model.set", runtimeId: "alpha", modelId: "" })).toEqual({ type: "model.set", runtimeId: "alpha", modelId: "" });
    expect(parseStudioAction({ type: "effort.set", runtimeId: "alpha", effort: null })).toEqual({ type: "effort.set", runtimeId: "alpha", effort: null });
    expect(parseStudioAction({ type: "dashboard.open" })).toEqual({ type: "dashboard.open" });
    expect(() => parseStudioAction({ type: "dashboard.open", url: "https://attacker.example" })).toThrow();
    expect(parseStudioAction({ type: "decision.load", url: "http://127.0.0.1:43123/board/abc" })).toEqual({ type: "decision.load", url: "http://127.0.0.1:43123/board/abc" });
    expect(() => parseStudioAction({ type: "decision.submit", url: "x", answers: { q0: { answer: "yes", actions_confirmed: [], skipped: false, extra: true } } })).toThrow();
    expect(parseStudioAction({ type: "clipboard.read" })).toEqual({ type: "clipboard.read" });
    expect(() => parseStudioAction({ type: "clipboard.read", path: "hidden" })).toThrow();
    expect(() => parseStudioAction({ type: "clipboard.write", text: "x".repeat(1_000_001) })).toThrow();
    expect(parseStudioAction({ type: "session.update", sessionId: "session-1", name: "Release plan", color: "violet" })).toEqual({ type: "session.update", sessionId: "session-1", name: "Release plan", color: "violet" });
    expect(() => parseStudioAction({ type: "session.update", sessionId: "session-1", name: "Release plan", color: "ultraviolet" })).toThrow();
    expect(parseStudioAction({ type: "update.check" })).toEqual({ type: "update.check" });
    expect(parseStudioAction({ type: "update.install" })).toEqual({ type: "update.install" });
    expect(() => parseStudioAction({ type: "update.install", url: "https://attacker.example" })).toThrow();
  });

  it("exposes only bounded text clipboard operations", async () => {
    const clipboard = { readText: vi.fn(() => "x".repeat(1_000_001)), writeText: vi.fn() };
    const studio = await controller(undefined, null, undefined, clipboard);

    const read = await studio.dispatch({ type: "clipboard.read" });
    expect(read).toMatchObject({ type: "clipboard-read" });
    if (read.type !== "clipboard-read") throw new Error("unexpected clipboard response");
    expect(read.text).toHaveLength(1_000_000);
    await expect(studio.dispatch({ type: "clipboard.write", text: "copy me" })).resolves.toEqual({ type: "clipboard-written" });
    expect(clipboard.readText).toHaveBeenCalledOnce();
    expect(clipboard.writeText).toHaveBeenCalledWith("copy me");
  });

  it("returns a complete bootstrap and delegates actions", async () => {
    const studio = await controller();
    const bootstrap = await studio.dispatch({ type: "bootstrap" });
    expect(bootstrap).toMatchObject({ type: "bootstrap", appName: "Firekeep Studio", dashboardAvailable: true, runtimes: [expect.objectContaining({ id: "alpha" })] });
    expect(bootstrap).not.toHaveProperty("dashboardUrl");
    await studio.dispatch({ type: "primary.set", runtimeId: "alpha" });
    await studio.dispatch({ type: "workspace.choose" });
    const result = await studio.dispatch({ type: "message.send", text: "hello" });
    expect(result).toMatchObject({ type: "state", snapshot: { primaryRuntimeId: "alpha", workspacePath: "C:\\workspace" } });
    expect(studio.service.events()).toContainEqual(expect.objectContaining({ payload: expect.objectContaining({ role: "user", text: "hello" }) }));
  });

  it("exposes updater state and only typed check/install operations", async () => {
    const current = { phase: "idle", currentVersion: "0.3.7", availableVersion: null, progressPercent: null, automatic: true, detail: "Automatic updates are on." } as const;
    const ready = { phase: "ready", currentVersion: "0.3.7", availableVersion: "0.4.0", progressPercent: 100, automatic: true, detail: "Ready." } as const;
    const updates: StudioUpdateControl = {
      snapshot: vi.fn(() => current),
      check: vi.fn(async () => ready),
      install: vi.fn(async () => ready),
    };
    const studio = await controller(undefined, null, undefined, undefined, updates);

    await expect(studio.dispatch({ type: "bootstrap" })).resolves.toMatchObject({ type: "bootstrap", update: current });
    await expect(studio.dispatch({ type: "update.check" })).resolves.toEqual({ type: "update", state: ready });
    await expect(studio.dispatch({ type: "update.install" })).resolves.toEqual({ type: "update", state: ready });
    expect(updates.check).toHaveBeenCalledOnce();
    expect(updates.install).toHaveBeenCalledOnce();
  });

  it("updates a named session's persisted title and color through typed IPC", async () => {
    const studio = await controller();
    const sessionId = studio.service.snapshot().activeSessionId;

    const result = await studio.dispatch({ type: "session.update", sessionId, name: "Release plan", color: "violet" });

    expect(result).toMatchObject({ type: "state", sessions: [expect.objectContaining({ id: sessionId, name: "Release plan", color: "violet" })] });
  });

  it("opens only the configured dashboard URL", async () => {
    const openExternal = vi.fn(async () => undefined);
    const studio = await controller(openExternal);

    await studio.dispatch({ type: "dashboard.open" });

    expect(openExternal).toHaveBeenCalledWith("http://keep.example:8040/");
    await expect((await controller(openExternal, null)).dispatch({ type: "dashboard.open" })).rejects.toThrow(/dashboard.*not configured/i);
  });

  it("returns typed IPC errors instead of rejecting the Electron handler", async () => {
    const studio = await controller(undefined, null);
    await studio.service.addReviewer("alpha");

    await expect(studio.invoke({ type: "review.run" })).resolves.toEqual({
      type: "error",
      message: "there is no primary response to review",
    });

    await studio.service.createMission("Prove the mission path");
    await expect(studio.invoke({ type: "mission.run" })).resolves.toEqual({
      type: "error",
      message: "choose an explicit workspace before running a mission",
    });
    await studio.service.setWorkspace("C:\\workspace");
    await expect(studio.invoke({ type: "mission.run" })).resolves.toEqual({
      type: "error",
      message: "choose a mission primary before running a mission",
    });
    await studio.service.setPrimary("alpha");
    await expect(studio.invoke({ type: "mission.run" })).resolves.toEqual({
      type: "error",
      message: "add at least one deterministic mission check before running",
    });
    await expect(studio.invoke({ type: "mission.run", command: "hidden" })).resolves.toMatchObject({
      type: "error",
      message: expect.stringMatching(/unrecognized key/i),
    });
  });

  it("delegates typed Decision Board loads and submissions without renderer fetch access", async () => {
    const url = "http://127.0.0.1:43123/board/abc";
    const decisionBoards: DecisionBoardTransport = {
      load: vi.fn(async () => ({ url, spec: { boardId: "abc", questions: [], degraded: false, boardEmbeds: [], embedsByQuestion: {} }, embeds: [] })),
      submit: vi.fn(async () => undefined),
    };
    const studio = await controller(undefined, null, decisionBoards);

    await expect(studio.dispatch({ type: "decision.load", url })).resolves.toMatchObject({ type: "decision", board: { url } });
    const answers = { q0: { answer: "yes", actions_confirmed: [], skipped: false } };
    await expect(studio.dispatch({ type: "decision.submit", url, answers })).resolves.toEqual({ type: "decision-submitted", url });
    expect(decisionBoards.load).toHaveBeenCalledWith(url);
    expect(decisionBoards.submit).toHaveBeenCalledWith(url, answers);
  });

  it("validates and delegates mission controls without exposing a generic process action", async () => {
    const studio = await controller();
    expect(parseStudioAction({ type: "mission.run" })).toEqual({ type: "mission.run" });
    expect(() => parseStudioAction({ type: "mission.run", command: "hidden" })).toThrow();
    await studio.service.setWorkspace("C:\\workspace");
    await studio.service.setPrimary("alpha");
    await studio.service.createMission("Prove the mission path");
    await studio.service.addMissionCheck("npm test");

    const result = await studio.dispatch({ type: "mission.run" });

    expect(result).toMatchObject({
      type: "state",
      snapshot: { mission: { phase: "succeeded" } },
      sessions: [expect.objectContaining({ mission: expect.objectContaining({ phase: "succeeded" }) })],
    });
  });

  it.each([
    [{ type: "keep.status" }],
    [{ type: "keep.retry" }],
    [{ type: "keep.search", query: "vps", kind: "memory" }],
    [{ type: "keep.skills" }],
    [{ type: "keep.team" }],
    [{ type: "keep.highlights" }],
    [{ type: "keep.feedback", memoryIds: ["m1"], useful: true }],
    [{ type: "keep.feedback", memoryIds: ["mem:1.a-b_c"], useful: false, comment: "stale" }],
    [{ type: "keep.receipt.retry", runId: "run-1" }],
    [{ type: "workspace.detectTest" }],
    [{ type: "settings.keepPanel", open: false }],
    [{ type: "mission.quick", goal: "fix it", checks: ["npm test"] }],
    [{ type: "workspace.set", path: "C:\\work" }],
  ])("parses %j", (action) => {
    expect(parseStudioAction(action)).toEqual(action);
  });

  it.each([
    [{ type: "keep.call", tool: "vault_retrieve", args: {} }],
    [{ type: "keep.search", query: "", kind: "memory" }],
    [{ type: "keep.search", query: "   \t\n", kind: "memory" }],
    [{ type: "keep.search", query: "x".repeat(501), kind: "memory" }],
    [{ type: "keep.search", query: "x", kind: "vault" }],
    [{ type: "keep.feedback", memoryIds: ["bad id\n"], useful: true }],
    [{ type: "keep.feedback", memoryIds: [], useful: true }],
    [{ type: "keep.feedback", memoryIds: Array.from({ length: 51 }, (_, i) => `m${i}`), useful: true }],
    [{ type: "keep.feedback", memoryIds: ["m1"], useful: true, comment: "x".repeat(501) }],
    [{ type: "keep.search", query: "x", kind: "memory", extra: 1 }],
    [{ type: "keep.status", tool: "memory_recall" }],
    [{ type: "mission.quick", goal: "fix it", checks: [] }],
    [{ type: "mission.quick", goal: "fix it", checks: Array.from({ length: 9 }, () => "npm test") }],
    [{ type: "mission.quick", goal: "", checks: ["npm test"] }],
    [{ type: "mission.quick", goal: "fix it", checks: ["x".repeat(1001)] }],
    [{ type: "workspace.set", path: "" }],
    [{ type: "settings.keepPanel", open: "yes" }],
  ])("rejects %j", (action) => {
    expect(() => parseStudioAction(action)).toThrow();
  });

  it("routes keep actions to the gateway and turns failures into error results", async () => {
    const status: KeepStatus = { state: "connected", agentId: "agent", personalMode: false, kitVersion: "1.8.0" };
    const keep: KeepPanelSource = {
      status: vi.fn(() => status),
      retry: vi.fn(async () => status),
      refreshIdentity: vi.fn(async () => status),
      search: vi.fn(async () => [{ title: "t", body: "b" }]),
      skills: vi.fn(async () => [{ title: "s", body: "sb" }]),
      team: vi.fn(async () => "# team"),
      highlights: vi.fn(async () => ({ online: "o", bulletin: "b", paused: [] })),
      feedback: vi.fn(async () => { throw new Error("Keep not connected"); }),
    };
    const studio = await controller(undefined, null, undefined, undefined, undefined, keep);

    await expect(studio.dispatch({ type: "keep.status" })).resolves.toEqual({ type: "keep-status", status });
    await expect(studio.dispatch({ type: "keep.retry" })).resolves.toEqual({ type: "keep-status", status });
    expect(keep.retry).not.toHaveBeenCalled();   // a connected gateway is never respawned by Retry
    await expect(studio.dispatch({ type: "keep.search", query: "vps", kind: "skill" })).resolves.toEqual({ type: "keep-items", items: [{ title: "t", body: "b" }] });
    expect(keep.search).toHaveBeenCalledWith("vps", "skill");
    await expect(studio.dispatch({ type: "keep.skills" })).resolves.toEqual({ type: "keep-items", items: [{ title: "s", body: "sb" }] });
    await expect(studio.dispatch({ type: "keep.team" })).resolves.toEqual({ type: "keep-team", markdown: "# team" });
    await expect(studio.dispatch({ type: "keep.highlights" })).resolves.toEqual({ type: "keep-highlights", highlights: { online: "o", bulletin: "b", paused: [] } });
    await expect(studio.invoke({ type: "keep.feedback", memoryIds: ["m1"], useful: true, comment: "helped" })).resolves.toEqual({ type: "error", message: "Keep not connected" });
    expect(keep.feedback).toHaveBeenCalledWith(["m1"], true, "helped");
    await expect(studio.invoke({ type: "keep.receipt.retry", runId: "run-1" })).resolves.toEqual({ type: "error", message: "the Keep link is unavailable" });
  });

  it.each([["/firekeep personal toggle", true], ["/firekeep update", true], ["/firekeep personal status", true], ["/firekeep doctor", false], ["/doctor", false]])("re-probes the Keep identity after %s: %s", async (input, expected) => {
    const status: KeepStatus = { state: "connected", agentId: "agent", personalMode: false, kitVersion: "1.8.0" };
    const keep: KeepPanelSource = {
      status: vi.fn(() => status), retry: vi.fn(async () => status), refreshIdentity: vi.fn(async () => status),
      search: vi.fn(), skills: vi.fn(), team: vi.fn(), highlights: vi.fn(), feedback: vi.fn(),
    };
    const studio = await controller(undefined, null, undefined, undefined, undefined, keep);
    const withKit = new StudioController(studio.service, createCommandRegistry(studio.service, { firekeep: { execute: async () => ({ ok: true, exitCode: 0, output: "done" }) } as never }), "0.3.7", undefined, undefined, null, undefined, undefined, undefined, keep);
    await withKit.invoke({ type: "command.execute", input });
    expect(keep.refreshIdentity).toHaveBeenCalledTimes(expected ? 1 : 0);
  });

  it("re-probes the Keep identity even when the kit command rejects", async () => {
    const status: KeepStatus = { state: "connected", agentId: "agent", personalMode: false, kitVersion: "1.8.0" };
    const keep: KeepPanelSource = {
      status: vi.fn(() => status), retry: vi.fn(async () => status), refreshIdentity: vi.fn(async () => status),
      search: vi.fn(), skills: vi.fn(), team: vi.fn(), highlights: vi.fn(), feedback: vi.fn(),
    };
    const studio = await controller(undefined, null, undefined, undefined, undefined, keep);
    const withKit = new StudioController(studio.service, createCommandRegistry(studio.service, { firekeep: { execute: async () => { throw new Error("timed out"); } } as never }), "0.3.7", undefined, undefined, null, undefined, undefined, undefined, keep);
    await expect(withKit.invoke({ type: "command.execute", input: "/firekeep update" })).resolves.toMatchObject({ type: "error" });
    expect(keep.refreshIdentity).toHaveBeenCalledTimes(1);
  });

  it("retries a Keep that is not connected", async () => {
    const unreachable: KeepStatus = { state: "unreachable", detail: "Keep gateway exited", retryInMs: null };
    const connected: KeepStatus = { state: "connected", agentId: "agent", personalMode: false, kitVersion: "1.8.0" };
    const keep: KeepPanelSource = {
      status: vi.fn(() => unreachable),
      retry: vi.fn(async () => connected), refreshIdentity: vi.fn(async () => connected),
      search: vi.fn(), skills: vi.fn(), team: vi.fn(), highlights: vi.fn(), feedback: vi.fn(),
    };
    const studio = await controller(undefined, null, undefined, undefined, undefined, keep);
    await expect(studio.dispatch({ type: "keep.retry" })).resolves.toEqual({ type: "keep-status", status: connected });
    expect(keep.retry).toHaveBeenCalledOnce();
  });

  it("reports a missing Keep without a gateway and errors on Keep queries", async () => {
    const studio = await controller();
    await expect(studio.dispatch({ type: "keep.status" })).resolves.toEqual({ type: "keep-status", status: { state: "missing" } });
    await expect(studio.dispatch({ type: "keep.retry" })).resolves.toEqual({ type: "keep-status", status: { state: "missing" } });
    await expect(studio.invoke({ type: "keep.team" })).resolves.toMatchObject({ type: "error" });
  });

  it("detects the workspace test command, switches only to recent workspaces and stores the panel state", async () => {
    const detectTest = vi.fn(async () => "npm test");
    const studio = await controller(undefined, null, undefined, undefined, undefined, null, detectTest);
    await expect(studio.dispatch({ type: "workspace.detectTest" })).resolves.toEqual({ type: "test-command", command: "npm test" });

    await expect(studio.invoke({ type: "workspace.set", path: "C:\\anywhere" })).resolves.toMatchObject({ type: "error", message: expect.stringMatching(/not a recent workspace/) });
    await studio.dispatch({ type: "workspace.choose" });
    await studio.service.setWorkspace("C:\\other");
    await expect(studio.dispatch({ type: "workspace.set", path: "C:\\workspace" })).resolves.toMatchObject({ type: "state", snapshot: { workspacePath: "C:\\workspace" } });

    await expect(studio.dispatch({ type: "settings.keepPanel", open: false })).resolves.toMatchObject({ type: "state", snapshot: { keepPanelOpen: false } });
  });

  it("runs a quick mission through create, checks and the confirmed run", async () => {
    const studio = await controller();
    await studio.service.setWorkspace("C:\\workspace");
    await studio.service.setPrimary("alpha");

    const result = await studio.dispatch({ type: "mission.quick", goal: "Fix the bug", checks: ["npm test", "npm run lint"] });

    expect(result).toMatchObject({
      type: "state",
      snapshot: { mission: { goal: "Fix the bug", phase: "succeeded", checks: [{ command: "npm test" }, { command: "npm run lint" }] } },
      sessions: expect.any(Array),
    });
  });
});
