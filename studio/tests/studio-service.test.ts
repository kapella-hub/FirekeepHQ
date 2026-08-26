import { describe, expect, it, vi } from "vitest";
import { RuntimeRegistry } from "../src/core/runtime-registry.js";
import { MemorySessionStore } from "../src/core/session-store.js";
import { MemorySettingsStore } from "../src/core/settings-store.js";
import { StudioService, type StudioPersistedState } from "../src/core/studio-service.js";
import { FakeRuntime } from "./helpers/fake-runtime.js";

function createService(): {
  service: StudioService;
  alpha: FakeRuntime;
  beta: FakeRuntime;
} {
  const alpha = new FakeRuntime({
    id: "alpha",
    displayName: "Alpha",
    description: "Alpha agent",
    transport: "test",
    capabilities: ["chat", "review", "streaming", "models", "usage"],
  });
  const beta = new FakeRuntime({
    id: "beta",
    displayName: "Beta",
    description: "Beta agent",
    transport: "test",
    capabilities: ["chat", "review", "streaming", "models"],
  });
  const service = new StudioService({
    runtimes: new RuntimeRegistry([alpha, beta]),
    settings: new MemorySettingsStore<StudioPersistedState>(),
    idFactory: (() => {
      let id = 0;
      return (prefix: string) => `${prefix}-${++id}`;
    })(),
    now: () => "2026-08-24T00:00:00.000Z",
  });
  return { service, alpha, beta };
}

describe("StudioService", () => {
  it("lets any chat-capable runtime become primary", async () => {
    const { service, alpha, beta } = createService();
    await service.initialize();

    await service.setPrimary("alpha");
    await service.sendMessage("first");
    await service.setPrimary("beta");
    await service.sendMessage("second");

    expect(alpha.runs.map((run) => run.prompt)).toEqual(["first"]);
    expect(beta.runs.map((run) => run.prompt)).toEqual(["second"]);
    expect(service.snapshot().primaryRuntimeId).toBe("beta");
  });

  it("targets a pane runtime without changing the configured primary", async () => {
    const { service, alpha, beta } = createService();
    await service.initialize();
    await service.setPrimary("alpha");

    await service.sendMessageTo("beta", "inspect independently");

    expect(alpha.runs).toHaveLength(0);
    expect(beta.runs).toHaveLength(1);
    expect(beta.runs[0]).toMatchObject({ prompt: "inspect independently", mode: "primary" });
    expect(service.snapshot().primaryRuntimeId).toBe("alpha");
    expect(service.events()).toContainEqual(expect.objectContaining({
      runtimeId: "beta",
      payload: expect.objectContaining({ kind: "message.completed", role: "user", text: "inspect independently" }),
    }));
  });

  it("asks for Mermaid only on visual turns without changing the visible user message", async () => {
    const { service, alpha } = createService();
    await service.initialize();
    await service.setPrimary("alpha");

    await service.sendMessage("Create an example graph of the memory flow");

    expect(alpha.runs[0]?.prompt).toContain("fenced `mermaid` block");
    const userEvent = service.events().find((event) => event.payload.kind === "message.completed" && event.payload.role === "user");
    expect(userEvent?.payload).toMatchObject({ text: "Create an example graph of the memory flow" });
  });

  it("uses one explicit persisted workspace for every runtime", async () => {
    const { service, alpha } = createService();
    await service.initialize();
    await service.setWorkspace("C:\\work\\firekeep");
    await service.setPrimary("alpha");

    await service.sendMessage("inspect this workspace");

    expect(alpha.runs[0]?.cwd).toBe("C:\\work\\firekeep");
    expect(service.snapshot().workspacePath).toBe("C:\\work\\firekeep");
  });

  it("resets reasoning to the provider default whenever the model changes", async () => {
    const { service } = createService();
    await service.initialize();

    await service.setEffort("alpha", "high");
    await service.setModel("alpha", "alpha-default");

    expect(service.snapshot().selectedModels.alpha).toBe("alpha-default");
    expect(service.snapshot().selectedEfforts.alpha).toBeUndefined();
    await service.setModel("alpha", "");
    expect(service.snapshot().selectedModels.alpha).toBeUndefined();
  });

  it("manages reviewers independently of the primary", async () => {
    const { service, beta } = createService();
    await service.initialize();
    await service.setPrimary("alpha");
    await service.addReviewer("beta");
    await service.setReviewerMode("after-turn");

    await service.sendMessage("implement it");

    expect(beta.runs).toHaveLength(1);
    expect(beta.runs[0]?.mode).toBe("review");
    expect(beta.runs[0]?.prompt).toContain("independent reviewer");
    expect(service.snapshot().primaryRuntimeId).toBe("alpha");
  });

  it("persists primary and reviewer choices without secrets", async () => {
    const settings = new MemorySettingsStore<StudioPersistedState>();
    const first = createService();
    const service = new StudioService({
      runtimes: first.service.runtimes,
      settings,
      idFactory: (prefix) => `${prefix}-one`,
      now: () => "2026-08-24T00:00:00.000Z",
    });
    await service.initialize();
    await service.setWorkspace("C:\\work\\persisted");
    await service.setPrimary("beta");
    await service.addReviewer("alpha");
    await service.setModel("beta", "beta-pro");

    const reloaded = new StudioService({
      runtimes: first.service.runtimes,
      settings,
      idFactory: (prefix) => `${prefix}-two`,
      now: () => "2026-08-24T00:00:00.000Z",
    });
    await reloaded.initialize();

    expect(reloaded.snapshot()).toMatchObject({
      workspacePath: "C:\\work\\persisted",
      primaryRuntimeId: "beta",
      reviewerRuntimeIds: ["alpha"],
      selectedModels: { beta: "beta-pro" },
    });
    expect(JSON.stringify(settings.value)).not.toMatch(/secret|api.?key|access.?token|refresh.?token/i);
  });

  it("routes runtime approvals through one resolvable Studio event", async () => {
    const { service, alpha } = createService();
    await service.initialize();
    await service.setPrimary("alpha");
    const originalRun = alpha.run.bind(alpha);
    alpha.run = async (request, sink, signal) => {
      const decision = await request.requestApproval?.({
        id: "approval-1",
        title: "Write settings",
        detail: "Change one file",
        options: ["accept", "decline"],
      });
      expect(decision).toBe("accept");
      return originalRun(request, sink, signal);
    };

    const running = service.sendMessage("do it");
    await vi.waitFor(() => expect(service.events().some((event) => event.payload.kind === "approval.requested")).toBe(true));
    expect(service.resolveApproval("approval-1", "unknown")).toBe(false);
    expect(service.resolveApproval("approval-1", "accept")).toBe(true);
    await running;
    expect(service.events().some((event) => event.payload.kind === "approval.resolved")).toBe(true);
  });

  it("cancels an active provider run during orderly shutdown", async () => {
    const { service, alpha } = createService();
    await service.initialize();
    await service.setPrimary("alpha");
    alpha.run = async (_request, _sink, signal) => new Promise((_resolve, reject) => {
      signal.addEventListener("abort", () => setTimeout(() => reject(new Error("aborted")), 10), { once: true });
    });
    const running = service.sendMessage("long task");
    const observed = running.then(() => null, (error: unknown) => error);
    await vi.waitFor(() => expect(service.snapshot().activeRunId).not.toBeNull());

    await service.shutdown();

    await expect(observed).resolves.toMatchObject({ message: "aborted" });
    expect(service.snapshot().activeRunId).toBeNull();
    expect(service.events().at(-1)?.payload.kind).toBe("run.failed");
  });

  it("refuses to switch transcripts while a provider run is active", async () => {
    const { service, alpha } = createService();
    await service.initialize();
    await service.setPrimary("alpha");
    alpha.run = async (_request, _sink, signal) => new Promise((_resolve, reject) => {
      signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
    });
    const originalSessionId = service.snapshot().activeSessionId;
    const running = service.sendMessage("stay in this transcript");
    const observed = running.then(() => null, (error: unknown) => error);
    await vi.waitFor(() => expect(service.snapshot().activeRunId).not.toBeNull());

    await expect(service.startNewSession()).rejects.toThrow(/run is active/i);
    await expect(service.setWorkspace("C:\\other")).rejects.toThrow(/run is active/i);
    expect(service.snapshot().activeSessionId).toBe(originalSessionId);

    expect(service.cancel()).toBe(true);
    await expect(observed).resolves.toMatchObject({ message: "aborted" });
  });

  it("persists, names, and resumes local Studio sessions", async () => {
    const sessions = new MemorySessionStore();
    const first = createService();
    const service = new StudioService({
      runtimes: first.service.runtimes,
      settings: new MemorySettingsStore<StudioPersistedState>(),
      sessions,
      idFactory: (() => { let value = 0; return (prefix) => `${prefix}-${++value}`; })(),
      now: () => "2026-08-24T00:00:00.000Z",
    });
    await service.initialize();
    await service.setPrimary("alpha");
    const original = service.snapshot().activeSessionId;
    await service.sendMessage("remember this");
    await service.updateSession(original, { name: "First task", color: "ocean" });
    await service.startNewSession();
    await service.resumeSession(original);

    expect(service.events().some((item) => item.payload.kind === "message.completed")).toBe(true);
    expect(await service.listSessions()).toContainEqual(expect.objectContaining({ id: original, name: "First task", color: "ocean" }));
    expect(service.exportSession("markdown")).toContain("response from alpha");
  });

  it("requires exact confirmation before deleting a local session and replaces an active one", async () => {
    const sessions = new MemorySessionStore();
    const first = createService();
    const service = new StudioService({
      runtimes: first.service.runtimes,
      settings: new MemorySettingsStore<StudioPersistedState>(),
      sessions,
      idFactory: (() => { let value = 0; return (prefix) => `${prefix}-${++value}`; })(),
      now: () => "2026-08-24T00:00:00.000Z",
    });
    await service.initialize();
    const original = service.snapshot().activeSessionId;

    await expect(service.deleteSession(original, "wrong")).rejects.toThrow(/exact session id/i);
    await service.deleteSession(original, original);

    expect(service.snapshot().activeSessionId).not.toBe(original);
    expect(await service.listSessions()).not.toContainEqual(expect.objectContaining({ id: original }));
  });

  it("records each runtime's final usage once and enforces a session token budget before the next run", async () => {
    const alpha = new FakeRuntime({
      id: "alpha",
      displayName: "Alpha",
      description: "Alpha agent",
      transport: "test",
      capabilities: ["chat", "review", "usage"],
    }, "budgeted answer", { inputTokens: 7, outputTokens: 3, totalTokens: 10, costUsd: 0.01 });
    const service = new StudioService({
      runtimes: new RuntimeRegistry([alpha]),
      settings: new MemorySettingsStore<StudioPersistedState>(),
      idFactory: (() => { let value = 0; return (prefix) => `${prefix}-${++value}`; })(),
      now: () => "2026-08-24T00:00:00.000Z",
    });
    await service.initialize();
    await service.setPrimary("alpha");
    await service.setTokenBudget(10);

    await service.sendMessage("one turn");

    expect(service.events().filter((event) => event.payload.kind === "usage.updated")).toHaveLength(1);
    expect(service.snapshot().usage).toMatchObject({ tokens: 10, freshTokens: 10, cachedTokens: 0, costUsd: 0.01, measuredRuns: 1, totalRuns: 1 });
    await expect(service.sendMessage("over budget")).rejects.toThrow(/token budget reached/i);
    expect(alpha.runs).toHaveLength(1);
  });

  it("recovers measured Claude usage from legacy raw events after an interrupted run", async () => {
    const sessions = new MemorySessionStore();
    const settings = new MemorySettingsStore<StudioPersistedState>();
    settings.value = {
      version: 1,
      activeSessionId: "session-existing",
      workspacePath: null,
      primaryRuntimeId: "alpha",
      reviewerRuntimeIds: [],
      reviewerMode: "off",
      selectedModels: {},
      selectedEfforts: {},
      permissionModes: {},
      nativeSessionIds: {},
      tokenBudget: null,
      voiceEnabled: false,
      theme: "system",
    };
    const timestamp = "2026-08-24T00:00:00.000Z";
    await sessions.ensure("session-existing", timestamp);
    await sessions.append({ id: "event-start", runId: "run-claude", studioSessionId: "session-existing", runtimeId: "claude", timestamp, payload: { kind: "run.started", mode: "primary", permissionMode: "standard" } });
    const firstRaw = { type: "assistant", message: { id: "message-1", usage: { input_tokens: 2, cache_creation_input_tokens: 10, cache_read_input_tokens: 20, output_tokens: 3 } } };
    await sessions.append({ id: "event-one", runId: "run-claude", studioSessionId: "session-existing", runtimeId: "claude", timestamp, payload: { kind: "tool.started", toolCallId: "tool-1", name: "Read" }, raw: firstRaw });
    await sessions.append({ id: "event-duplicate", runId: "run-claude", studioSessionId: "session-existing", runtimeId: "claude", timestamp, payload: { kind: "tool.updated", toolCallId: "tool-1", update: "running" }, raw: firstRaw });
    await sessions.append({ id: "event-two", runId: "run-claude", studioSessionId: "session-existing", runtimeId: "claude", timestamp, payload: { kind: "tool.started", toolCallId: "tool-2", name: "Bash" }, raw: { type: "assistant", message: { id: "message-2", usage: { input_tokens: 1, cache_read_input_tokens: 30, output_tokens: 4 } } } });
    await sessions.append({ id: "event-failed", runId: "run-claude", studioSessionId: "session-existing", runtimeId: "claude", timestamp, payload: { kind: "run.failed", cancelled: true, error: "cancelled", durationMs: 1 } });
    const { service: prior } = createService();
    const service = new StudioService({
      runtimes: prior.runtimes,
      settings,
      sessions,
      idFactory: (prefix) => `${prefix}-new`,
      now: () => timestamp,
    });

    await service.initialize();

    expect(service.snapshot().usage).toMatchObject({ tokens: 70, freshTokens: 20, cachedTokens: 50, measuredRuns: 1, totalRuns: 1, byRuntime: { claude: { tokens: 70, freshTokens: 20, cachedTokens: 50, runs: 1 } } });
  });

  it("runs comparisons and reviewers sequentially in isolated safe contexts", async () => {
    const { service, alpha, beta } = createService();
    await service.initialize();
    await service.setPrimary("alpha");
    await service.addReviewer("alpha");
    await service.addReviewer("beta");
    await service.sendMessage("seed answer");
    let concurrent = 0;
    let maximum = 0;
    for (const runtime of [alpha, beta]) {
      const original = runtime.run.bind(runtime);
      runtime.run = async (...args) => {
        concurrent += 1;
        maximum = Math.max(maximum, concurrent);
        await new Promise((resolve) => setTimeout(resolve, 5));
        try { return await original(...args); }
        finally { concurrent -= 1; }
      };
    }

    await service.runReview();
    const compared = await service.compare("independent question", ["alpha", "beta"]);

    expect(compared).toHaveLength(2);
    expect(maximum).toBe(1);
    expect(alpha.runs.at(-1)).toMatchObject({ mode: "compare", permissionMode: "safe" });
    expect(alpha.runs.at(-1)?.nativeSessionId).toBeUndefined();
    expect(beta.runs.at(-1)).toMatchObject({ mode: "compare", permissionMode: "safe" });
    expect(beta.runs.at(-1)?.nativeSessionId).toBeUndefined();
    expect(service.events().filter((event) => event.payload.kind === "message.completed" && event.payload.role === "user").at(-1)?.payload).toMatchObject({ text: "independent question" });
  });

  it("builds a fresh consensus packet and keeps an explicit handoff's new native session", async () => {
    const { service, alpha, beta } = createService();
    await service.initialize();
    await service.compare("compare this", ["alpha", "beta"]);

    await service.synthesize("alpha", "prefer evidence");
    expect(alpha.runs.at(-1)?.prompt).toContain("## beta");
    expect(alpha.runs.at(-1)?.prompt).toContain("prefer evidence");

    await service.handoff("beta", "continue carefully");
    expect(service.snapshot()).toMatchObject({ primaryRuntimeId: "beta", nativeSessionIds: { beta: "beta-session" } });
    expect(beta.runs.at(-1)).toMatchObject({ mode: "handoff" });
    expect(beta.runs.at(-1)?.nativeSessionId).toBeUndefined();
  });
});
