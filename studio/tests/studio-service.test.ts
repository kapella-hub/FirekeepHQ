import { describe, expect, it, vi } from "vitest";
import { RuntimeRegistry } from "../src/core/runtime-registry.js";
import { MemorySessionStore } from "../src/core/session-store.js";
import { MemorySettingsStore } from "../src/core/settings-store.js";
import { StudioService, type StudioPersistedState } from "../src/core/studio-service.js";
import { FakeRuntime } from "./helpers/fake-runtime.js";
import { ReceiptTracker } from "../src/core/keep/receipt-tracker.js";
import type { KeepReceipt } from "../src/core/keep/receipt.js";
import type { RuntimeEvent } from "../src/core/runtime.js";

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

  it("runs reviewers sequentially in isolated safe contexts", async () => {
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

    expect(maximum).toBe(1);
    expect(alpha.runs.at(-1)).toMatchObject({ mode: "review", permissionMode: "safe" });
    expect(alpha.runs.at(-1)?.nativeSessionId).toBeUndefined();
    expect(beta.runs.at(-1)).toMatchObject({ mode: "review", permissionMode: "safe" });
    expect(beta.runs.at(-1)?.nativeSessionId).toBeUndefined();
  });

  it("keeps an explicit handoff's new native session", async () => {
    const { service, beta } = createService();
    await service.initialize();
    await service.handoff("beta", "continue carefully");
    expect(service.snapshot()).toMatchObject({ primaryRuntimeId: "beta", nativeSessionIds: { beta: "beta-session" } });
    expect(beta.runs.at(-1)).toMatchObject({ mode: "handoff" });
    expect(beta.runs.at(-1)?.nativeSessionId).toBeUndefined();
  });
});

function createKeepService() {
  const reports: Record<string, string[]> = {};
  const timelines: Record<string, unknown[]> = {};
  const tracker = new ReceiptTracker({
    source: () => ({ timeline: async (id, offset) => { const all = (timelines[id] ?? []) as never[]; return { kind: "page", events: all.slice(offset), total: all.length, hasMore: false }; } }),
    readReport: async (studioSessionId) => reports[studioSessionId] ?? [],
  });
  const alpha = new FakeRuntime({ id: "alpha", displayName: "Alpha", description: "", transport: "test", capabilities: ["chat", "review"] });
  const sessions = new MemorySessionStore();
  const removed: string[] = [];
  const service = new StudioService({
    runtimes: new RuntimeRegistry([alpha]),
    settings: new MemorySettingsStore<StudioPersistedState>(),
    sessions,
    idFactory: (() => { let id = 0; return (prefix: string) => `${prefix}-${++id}`; })(),
    keep: {
      tracker,
      reportPath: (id, role) => role === "review" ? `/keep/${id}.review.keep` : `/keep/${id}.keep`,
      removeReport: async (id) => { removed.push(id); },
    },
  });
  return { service, alpha, reports, timelines, tracker, sessions, removed };
}

function keepReceipts(events: readonly RuntimeEvent[]): Array<{ runId: string; receipt: KeepReceipt }> {
  return events.flatMap((event) => event.payload.kind === "keep.receipt" ? [{ runId: event.runId, receipt: event.payload.receipt }] : []);
}

function firstRunId(service: StudioService): string {
  return service.events().find((event) => event.payload.kind === "run.started")?.runId as string;
}

const RECALL_EVENT = { id: "e1", agent_id: "agent-a", event_type: "memory_read", timestamp: "t", payload: { query: "q", memory_ids: ["m1"] } };

describe("StudioService Keep receipts", () => {
  it("launches every run with the session's report file", async () => {
    const { service, alpha } = createKeepService();
    await service.initialize();
    await service.setPrimary("alpha");
    await service.sendMessage("hi");
    const sessionId = service.snapshot().activeSessionId;
    expect(alpha.runs[0]?.env).toEqual({ FIREKEEP_SESSION_REPORT_FILE: `/keep/${sessionId}.keep` });
  });

  it("launches a Second opinion review with the session's separate review report file", async () => {
    const { service, alpha } = createKeepService();
    await service.initialize();
    await service.setPrimary("alpha");
    await service.sendMessage("hi");
    await service.runReview("alpha");
    const sessionId = service.snapshot().activeSessionId;
    expect(alpha.runs[1]?.mode).toBe("review");
    expect(alpha.runs[1]?.env).toEqual({ FIREKEEP_SESSION_REPORT_FILE: `/keep/${sessionId}.review.keep` });
  });

  it("gives after-turn reviewers the review report file too", async () => {
    const { service, alpha } = createKeepService();
    await service.initialize();
    await service.setPrimary("alpha");
    await service.addReviewer("alpha");
    await service.setReviewerMode("after-turn");
    await service.sendMessage("hi");
    const sessionId = service.snapshot().activeSessionId;
    expect(alpha.runs.map((run) => run.env?.FIREKEEP_SESSION_REPORT_FILE)).toEqual([`/keep/${sessionId}.keep`, `/keep/${sessionId}.review.keep`]);
  });

  it("emits a provisional receipt after the turn and a final one before the next prompt", async () => {
    const { service, reports, timelines } = createKeepService();
    await service.initialize();
    await service.setPrimary("alpha");
    const sessionId = service.snapshot().activeSessionId;
    reports[sessionId] = ["k1"];
    timelines.k1 = [RECALL_EVENT];
    const receipts = () => keepReceipts(service.events()).map((item) => item.receipt);
    await service.sendMessage("first");
    await vi.waitFor(() => expect(receipts().map((receipt) => receipt.final)).toEqual([false]));
    timelines.k1.push({ id: "e2", agent_id: "agent-a", event_type: "memory_write", timestamp: "t", payload: { action_summary: "late", memory_id: "m2" } });
    await service.sendMessage("second");
    const [, finalFirst] = receipts();
    expect(finalFirst?.final).toBe(true);
    expect(finalFirst?.recalledCount).toBe(1);
    expect(finalFirst?.learnedCount).toBe(1);
  });

  it("a slow Keep delays Send by at most the finalize budget and promotes the provisional receipt", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const alpha = new FakeRuntime({ id: "alpha", displayName: "Alpha", description: "", transport: "test", capabilities: ["chat"] });
      let calls = 0;
      const tracker = new ReceiptTracker({
        source: () => ({ timeline: async (_id, offset) => {
          calls += 1;
          if (calls > 1) return new Promise(() => undefined);
          return { kind: "page", events: [{ id: "e1", agent_id: "a", event_type: "memory_read", timestamp: "t", payload: { memory_ids: ["m1"] } }].slice(offset), total: 1, hasMore: false };
        } }),
        readReport: async () => ["k1"],
      });
      const service = new StudioService({ runtimes: new RuntimeRegistry([alpha]), settings: new MemorySettingsStore<StudioPersistedState>(), keep: { tracker, reportPath: () => "/x.keep", removeReport: async () => undefined } });
      await service.initialize();
      await service.setPrimary("alpha");
      await service.sendMessage("first");
      await vi.waitFor(() => expect(keepReceipts(service.events())).toHaveLength(1));
      const second = service.sendMessage("second");
      await vi.advanceTimersByTimeAsync(2100);
      await second;
      const promoted = keepReceipts(service.events()).map((item) => item.receipt).find((receipt) => receipt.final);
      expect(promoted).toMatchObject({ status: "measured", recalledCount: 1 });
      expect(tracker.cursors(service.snapshot().activeSessionId)).toEqual({ k1: 1 });
    } finally {
      vi.useRealTimers();
    }
  });

  it("finalizes into the session that produced the turn when the user switches sessions", async () => {
    const { service, reports, timelines, sessions } = createKeepService();
    await service.initialize();
    await service.setPrimary("alpha");
    const first = service.snapshot().activeSessionId;
    reports[first] = ["k1"];
    timelines.k1 = [];
    await service.sendMessage("in A");
    await service.startNewSession();
    const second = service.snapshot().activeSessionId;
    await service.sendMessage("in B");
    expect(keepReceipts(await sessions.load(first)).filter((item) => item.receipt.final)).toHaveLength(1);
    expect(keepReceipts(service.events()).every((item) => !item.receipt.final)).toBe(true);
    expect((await sessions.load(second)).every((event) => event.studioSessionId === second)).toBe(true);
  });

  it("restores committed cursors from the last final receipt when a session is resumed", async () => {
    const { service, reports, timelines, tracker } = createKeepService();
    await service.initialize();
    await service.setPrimary("alpha");
    const first = service.snapshot().activeSessionId;
    reports[first] = ["k1"];
    timelines.k1 = [RECALL_EVENT];
    await service.sendMessage("in A");
    await service.startNewSession();
    tracker.forget(first);
    await service.resumeSession(first);
    expect(tracker.cursors(first)).toEqual({ k1: 1 });
  });

  it("retry refuses a run older than the latest finalized turn", async () => {
    const { service } = createKeepService();
    await service.initialize();
    await service.setPrimary("alpha");
    await service.sendMessage("one");
    const firstRun = firstRunId(service);
    await service.sendMessage("two");
    await service.sendMessage("three");
    await expect(service.retryReceipt(firstRun)).rejects.toThrow(/only the latest receipt/);
  });

  it("refuses to retry the finalized turn while a newer turn is pending, so a retry never absorbs later events", async () => {
    const { service } = createKeepService();
    await service.initialize();
    await service.setPrimary("alpha");
    await service.sendMessage("one");
    const firstRun = firstRunId(service);
    await service.sendMessage("two");
    await expect(service.retryReceipt(firstRun)).rejects.toThrow(/only the latest receipt/);
  });

  it("retries the last finalized turn from the cursors committed before it", async () => {
    const { service, reports, timelines, tracker, sessions } = createKeepService();
    await service.initialize();
    await service.setPrimary("alpha");
    const first = service.snapshot().activeSessionId;
    reports[first] = ["k1"];
    timelines.k1 = [RECALL_EVENT];
    await service.sendMessage("one");
    const runId = firstRunId(service);
    await service.startNewSession();
    expect(tracker.cursors(first)).toEqual({ k1: 1 });
    const pushed: RuntimeEvent[] = [];
    service.subscribe((event) => pushed.push(event));

    await service.retryReceipt(runId);

    const finals = keepReceipts(await sessions.load(first)).filter((item) => item.receipt.final);
    expect(finals).toHaveLength(2);
    expect(finals[1]).toMatchObject({ runId, receipt: { status: "measured", recalledCount: 1 } });
    expect(tracker.cursors(first)).toEqual({ k1: 1 });
    // The renderer shows the active session only, so a receipt for another session is stored, not pushed.
    expect(pushed).toEqual([]);
    expect(keepReceipts(service.events())).toEqual([]);
  });

  it("a failed retry leaves committed cursors where they were", async () => {
    let fail = false;
    const tracker = new ReceiptTracker({
      source: () => ({ timeline: async (_id, offset) => {
        if (fail) throw new Error("gateway gone");
        return { kind: "page", events: [RECALL_EVENT].slice(offset), total: 1, hasMore: false };
      } }),
      readReport: async () => ["k1"],
    });
    const alpha = new FakeRuntime({ id: "alpha", displayName: "Alpha", description: "", transport: "test", capabilities: ["chat"] });
    const service = new StudioService({ runtimes: new RuntimeRegistry([alpha]), settings: new MemorySettingsStore<StudioPersistedState>(), sessions: new MemorySessionStore(), keep: { tracker, reportPath: () => "/x.keep", removeReport: async () => undefined } });
    await service.initialize();
    await service.setPrimary("alpha");
    const first = service.snapshot().activeSessionId;
    await service.sendMessage("one");
    const runId = firstRunId(service);
    await service.startNewSession();
    expect(tracker.cursors(first)).toEqual({ k1: 1 });
    fail = true;
    await service.retryReceipt(runId);
    expect(tracker.cursors(first)).toEqual({ k1: 1 });
  });

  it("deleting a session removes its report and never resurrects it with a late receipt", async () => {
    const { service, sessions, removed, tracker } = createKeepService();
    await service.initialize();
    await service.setPrimary("alpha");
    const first = service.snapshot().activeSessionId;
    await service.sendMessage("in A");
    const runId = firstRunId(service);
    await service.startNewSession();
    await service.deleteSession(first, first);
    expect(removed).toEqual([first]);
    expect(tracker.cursors(first)).toEqual({});
    await expect(service.retryReceipt(runId)).rejects.toThrow(/only the latest receipt/);
    expect((await sessions.list()).map((item) => item.id)).not.toContain(first);
  });

  it("a Second opinion review is its own turn with its own receipt", async () => {
    const { service } = createKeepService();
    await service.initialize();
    await service.setPrimary("alpha");
    await service.sendMessage("answer");
    await vi.waitFor(() => expect(keepReceipts(service.events())).toHaveLength(1));
    await service.runReview("alpha");
    const primaryRun = firstRunId(service);
    const reviewRun = service.events().filter((event) => event.payload.kind === "run.started").at(-1)?.runId as string;
    expect(keepReceipts(service.events()).filter((item) => item.runId === primaryRun).map((item) => item.receipt.final)).toEqual([false, true]);
    await vi.waitFor(() => expect(keepReceipts(service.events()).some((item) => item.runId === reviewRun && !item.receipt.final)).toBe(true));
  });

  it("a mission is one turn: it finalizes the chat turn first, and a repair finalizes the mission turn", async () => {
    const tracker = new ReceiptTracker({ source: () => null, readReport: async () => [] });
    const alpha = new FakeRuntime({ id: "alpha", displayName: "Alpha", description: "", transport: "test", capabilities: ["chat", "review"] });
    const beta = new FakeRuntime({ id: "beta", displayName: "Beta", description: "", transport: "test", capabilities: ["chat", "review"] });
    const service = new StudioService({
      runtimes: new RuntimeRegistry([alpha, beta]),
      settings: new MemorySettingsStore<StudioPersistedState>(),
      sessions: new MemorySessionStore(),
      missionChecks: { run: async () => ({ exitCode: 0, signal: null, stdout: "ok", stderr: "", timedOut: false, truncated: false, durationMs: 1 }) },
      confirmMission: async () => true,
      idFactory: (() => { let id = 0; return (prefix: string) => `${prefix}-${++id}`; })(),
      keep: { tracker, reportPath: (id, role) => `/keep/${id}.${role}`, removeReport: async () => undefined },
    });
    await service.initialize();
    await service.setWorkspace("C:\\work\\mission");
    await service.setPrimary("alpha");
    await service.sendMessage("chat first");
    const chatRun = firstRunId(service);
    await service.createMission("Make it correct");
    await service.addMissionReviewer("beta");
    await service.addMissionCheck("npm test", { name: "tests", timeoutMs: 60_000 });

    await service.runMission();

    const runStarts = () => service.events().filter((event) => event.payload.kind === "run.started").map((event) => event.runId);
    const missionRun = runStarts()[1] as string;
    const byRun = (runId: string) => keepReceipts(service.events()).filter((item) => item.runId === runId).map((item) => item.receipt.final);
    expect(service.mission()?.phase).toBe("awaiting-approval");
    expect(beta.runs[0]?.env).toEqual({ FIREKEEP_SESSION_REPORT_FILE: `/keep/${service.snapshot().activeSessionId}.review` });
    expect(byRun(chatRun)).toEqual([false, true]);
    await vi.waitFor(() => expect(byRun(missionRun)).toEqual([false]));
    expect(keepReceipts(service.events()).filter((item) => item.runId === runStarts()[2])).toEqual([]);

    await service.repairMission("tighten the edge case");

    expect(byRun(missionRun)).toEqual([false, true]);
  });

  it("a Keep failure never fails the turn", async () => {
    const alpha = new FakeRuntime({ id: "alpha", displayName: "Alpha", description: "", transport: "test", capabilities: ["chat"] });
    const tracker = new ReceiptTracker({ source: () => ({ timeline: async () => { throw new Error("gateway gone"); } }), readReport: async () => ["k1"] });
    const service = new StudioService({ runtimes: new RuntimeRegistry([alpha]), settings: new MemorySettingsStore<StudioPersistedState>(), keep: { tracker, reportPath: () => "/x.keep", removeReport: async () => undefined } });
    await service.initialize();
    await service.setPrimary("alpha");
    await expect(service.sendMessage("hi")).resolves.toMatchObject({ finalText: "response from alpha" });
    await expect(service.sendMessage("again")).resolves.toMatchObject({ finalText: "response from alpha" });
  });
});
