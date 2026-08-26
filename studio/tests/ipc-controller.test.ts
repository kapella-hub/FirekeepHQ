import { describe, expect, it, vi } from "vitest";
import { RuntimeRegistry } from "../src/core/runtime-registry.js";
import { MemorySessionStore } from "../src/core/session-store.js";
import { MemorySettingsStore } from "../src/core/settings-store.js";
import { createCommandRegistry } from "../src/core/slash-commands.js";
import { StudioService, type StudioPersistedState } from "../src/core/studio-service.js";
import type { VoiceInputOutcome } from "../src/shared/ipc.js";
import { StudioController, parseStudioAction } from "../src/main/ipc-controller.js";
import type { DecisionBoardTransport } from "../src/main/decision-board-client.js";
import type { VoiceInput } from "../src/main/voice-input.js";
import { FakeRuntime } from "./helpers/fake-runtime.js";

async function controller(openExternal: (url: string) => Promise<void> = async () => undefined, dashboardUrl: string | null = "http://keep.example:8040/", decisionBoards?: DecisionBoardTransport, clipboard?: { readText(): string; writeText(text: string): void }, voiceInput?: VoiceInput): Promise<StudioController> {
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
  return new StudioController(service, createCommandRegistry(service), "0.3.3", openExternal, async () => "C:\\workspace", dashboardUrl, decisionBoards, clipboard, voiceInput);
}

describe("Studio IPC controller", () => {
  it("rejects unknown, oversized, and extra-field actions", () => {
    expect(() => parseStudioAction({ type: "process.run", command: "rm" })).toThrow();
    expect(() => parseStudioAction({ type: "message.send", text: "x", command: "hidden" })).toThrow();
    expect(parseStudioAction({ type: "message.sendTo", runtimeId: "alpha", text: "hello" })).toEqual({ type: "message.sendTo", runtimeId: "alpha", text: "hello" });
    expect(() => parseStudioAction({ type: "message.sendTo", runtimeId: "alpha", text: "hello", command: "hidden" })).toThrow();
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
    expect(parseStudioAction({ type: "voice.input.start", language: "en-US" })).toEqual({ type: "voice.input.start", language: "en-US" });
    expect(parseStudioAction({ type: "voice.input.stop" })).toEqual({ type: "voice.input.stop" });
    expect(() => parseStudioAction({ type: "voice.input.start", language: "bad language", command: "hidden" })).toThrow();
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

  it("delegates only typed voice start and stop operations", async () => {
    const voiceInput: VoiceInput = {
      transcribe: vi.fn(async (): Promise<VoiceInputOutcome> => ({ state: "complete", text: "dictated text", detail: "local" })),
      cancel: vi.fn(() => true),
    };
    const studio = await controller(undefined, null, undefined, undefined, voiceInput);

    await expect(studio.dispatch({ type: "voice.input.start", language: "en-US" })).resolves.toEqual({
      type: "voice-input",
      state: "complete",
      text: "dictated text",
      detail: "local",
    });
    await expect(studio.dispatch({ type: "voice.input.stop" })).resolves.toEqual({
      type: "voice-input",
      state: "cancelled",
      text: "",
      detail: "Voice input stopped.",
    });
    expect(voiceInput.transcribe).toHaveBeenCalledWith("en-US");
    expect(voiceInput.cancel).toHaveBeenCalledOnce();
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
    await studio.dispatch({ type: "message.sendTo", runtimeId: "alpha", text: "pane hello" });
    expect(studio.service.events()).toContainEqual(expect.objectContaining({ payload: expect.objectContaining({ role: "user", text: "pane hello" }) }));
  });

  it("opens only the configured dashboard URL", async () => {
    const openExternal = vi.fn(async () => undefined);
    const studio = await controller(openExternal);

    await studio.dispatch({ type: "dashboard.open" });

    expect(openExternal).toHaveBeenCalledWith("http://keep.example:8040/");
    await expect((await controller(openExternal, null)).dispatch({ type: "dashboard.open" })).rejects.toThrow(/dashboard.*not configured/i);
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
});
