import { describe, expect, it, vi } from "vitest";
import type {
  MissionCheck,
  MissionCheckExecution,
  MissionCheckRunner,
} from "../src/core/mission.js";
import { RuntimeRegistry } from "../src/core/runtime-registry.js";
import { MemorySessionStore } from "../src/core/session-store.js";
import { MemorySettingsStore } from "../src/core/settings-store.js";
import { StudioService, type StudioPersistedState } from "../src/core/studio-service.js";
import { FakeRuntime } from "./helpers/fake-runtime.js";

class QueueCheckRunner implements MissionCheckRunner {
  readonly calls: Array<{ readonly check: MissionCheck; readonly cwd: string }> = [];
  readonly #results: MissionCheckExecution[];

  constructor(results: MissionCheckExecution[]) {
    this.#results = [...results];
  }

  async run(check: MissionCheck, cwd: string, _signal: AbortSignal): Promise<MissionCheckExecution> {
    this.calls.push({ check, cwd });
    return this.#results.shift() ?? passed();
  }
}

function passed(stdout = "ok"): MissionCheckExecution {
  return { exitCode: 0, signal: null, stdout, stderr: "", timedOut: false, truncated: false, durationMs: 12 };
}

function failed(stderr = "expected true, received false"): MissionCheckExecution {
  return { exitCode: 1, signal: null, stdout: "", stderr, timedOut: false, truncated: false, durationMs: 15 };
}

function setup(options: {
  readonly checks?: MissionCheckExecution[];
  readonly alphaUsage?: number;
  readonly betaAnswer?: string;
  readonly confirm?: (summary: string) => Promise<boolean>;
  readonly sessions?: MemorySessionStore;
  readonly settings?: MemorySettingsStore<StudioPersistedState>;
} = {}): {
  readonly service: StudioService;
  readonly alpha: FakeRuntime;
  readonly beta: FakeRuntime;
  readonly checks: QueueCheckRunner;
} {
  const alpha = new FakeRuntime(
    { id: "alpha", displayName: "Alpha", description: "writer", transport: "test", capabilities: ["chat", "review", "usage"] },
    "implemented the requested change",
    options.alphaUsage === undefined ? undefined : { totalTokens: options.alphaUsage },
  );
  const beta = new FakeRuntime(
    { id: "beta", displayName: "Beta", description: "reviewer", transport: "test", capabilities: ["chat", "review"] },
    options.betaAnswer ?? "Review complete: inspect the evidence before accepting.",
  );
  const checks = new QueueCheckRunner(options.checks ?? [passed()]);
  let id = 0;
  const service = new StudioService({
    runtimes: new RuntimeRegistry([alpha, beta]),
    settings: options.settings ?? new MemorySettingsStore<StudioPersistedState>(),
    sessions: options.sessions ?? new MemorySessionStore(),
    missionChecks: checks,
    confirmMission: options.confirm ?? (async () => true),
    idFactory: (prefix) => `${prefix}-${++id}`,
    now: (() => {
      let tick = 0;
      return () => `2026-08-24T00:00:${String(tick++).padStart(2, "0")}.000Z`;
    })(),
  });
  return { service, alpha, beta, checks };
}

async function configureMission(service: StudioService): Promise<void> {
  await service.initialize();
  await service.setWorkspace("C:\\work\\mission");
  await service.setPrimary("alpha");
  await service.createMission("Make the behavior demonstrably correct");
  await service.addMissionCheck("npm test", { name: "tests", timeoutMs: 60_000 });
}

describe("Studio missions", () => {
  it("runs one primary writer and records deterministic success without reading prose as a grade", async () => {
    const { service, alpha, checks } = setup();
    await configureMission(service);

    const mission = await service.runMission();

    expect(alpha.runs).toHaveLength(1);
    expect(alpha.runs[0]).toMatchObject({ mode: "primary", cwd: "C:\\work\\mission" });
    expect(alpha.runs[0]?.prompt).toContain("Make the behavior demonstrably correct");
    expect(alpha.runs[0]?.prompt).toContain("Studio records the task result separately");
    expect(checks.calls).toEqual([expect.objectContaining({ cwd: "C:\\work\\mission", check: expect.objectContaining({ command: "npm test" }) })]);
    expect(mission).toMatchObject({
      phase: "succeeded",
      attempt: 1,
      verificationPassed: true,
      outcome: { taskResult: "success", taskResultSource: "verified" },
    });
  });

  it("uses failed check evidence for one bounded repair before re-verifying", async () => {
    const { service, alpha } = setup({ checks: [failed("one assertion failed"), passed("all green")] });
    await configureMission(service);

    const mission = await service.runMission();

    expect(alpha.runs).toHaveLength(2);
    expect(alpha.runs[1]?.prompt).toContain("one assertion failed");
    expect(alpha.runs[1]?.prompt).toContain("repair attempt 1 of 1");
    expect(mission).toMatchObject({ phase: "succeeded", attempt: 2, verificationPassed: true });
    expect(mission.checkReceipts.map((receipt) => receipt.passed)).toEqual([false, true]);
  });

  it("records reviewer prose as advisory evidence and waits for an explicit human result", async () => {
    const { service, beta } = setup({ betaAnswer: "CHANGES_REQUESTED: this sentence is not a machine grade." });
    await service.initialize();
    await service.setWorkspace("C:\\work\\mission");
    await service.setPrimary("alpha");
    await service.addReviewer("beta");
    await service.createMission("Review this carefully");
    await service.addMissionCheck("npm test", { name: "tests" });

    const reviewed = await service.runMission();

    expect(beta.runs).toHaveLength(1);
    expect(beta.runs[0]).toMatchObject({ mode: "review", permissionMode: "safe" });
    expect(reviewed).toMatchObject({ phase: "awaiting-approval", verificationPassed: true, outcome: null });
    expect(reviewed.reviewReceipts[0]).toMatchObject({ runtimeId: "beta", status: "completed", text: expect.stringContaining("CHANGES_REQUESTED") });

    const accepted = await service.completeMission("success", "Reviewed the independent evidence.");
    expect(accepted).toMatchObject({
      phase: "succeeded",
      outcome: { taskResult: "success", taskResultSource: "human_confirmed", note: "Reviewed the independent evidence." },
    });
  });

  it("records verified failure only after the repair allowance is exhausted", async () => {
    const { service, alpha } = setup({ checks: [failed("first failure"), failed("still failing")] });
    await configureMission(service);

    const mission = await service.runMission();

    expect(alpha.runs).toHaveLength(2);
    expect(mission).toMatchObject({
      phase: "failed",
      attempt: 2,
      verificationPassed: false,
      outcome: { taskResult: "failure", taskResultSource: "verified" },
    });
  });

  it("pauses before the next agent run at its measured token guard and resumes after an explicit increase", async () => {
    const { service, alpha } = setup({ checks: [failed(), passed()], alphaUsage: 10 });
    await configureMission(service);
    await service.setMissionTokenBudget(10);

    const paused = await service.runMission();
    expect(paused).toMatchObject({ phase: "paused", nextAction: "primary", measuredTokens: 10, outcome: null });
    expect(paused.blockReason).toMatch(/token guard/i);
    expect(alpha.runs).toHaveLength(1);

    await service.setPermissionMode("alpha", "unrestricted");
    await service.setMissionTokenBudget(30);
    const completed = await service.continueMission();
    expect(alpha.runs).toHaveLength(2);
    expect(alpha.runs[1]?.permissionMode).toBe("standard");
    expect(completed).toMatchObject({ phase: "succeeded", measuredTokens: 20 });
  });

  it("cancels a mission while a deterministic check is running", async () => {
    let started!: () => void;
    const checking = new Promise<void>((resolve) => { started = resolve; });
    const runner: MissionCheckRunner = {
      run: async (_check, _cwd, signal) => {
        started();
        return new Promise<MissionCheckExecution>((_resolve, reject) => {
          signal.addEventListener("abort", () => reject(new Error("check aborted")), { once: true });
        });
      },
    };
    const configured = setup();
    const service = new StudioService({
      runtimes: configured.service.runtimes,
      settings: new MemorySettingsStore<StudioPersistedState>(),
      sessions: new MemorySessionStore(),
      missionChecks: runner,
      confirmMission: async () => true,
      idFactory: (() => { let id = 0; return (prefix: string) => `${prefix}-${++id}`; })(),
      now: () => "2026-08-24T00:00:00.000Z",
    });
    await configureMission(service);

    const running = service.runMission();
    await checking;
    expect(service.cancelMission()).toBe(true);

    await expect(running).resolves.toMatchObject({ phase: "cancelled", outcome: null });
  });

  it("persists the mission with its session and restores it after restart", async () => {
    const sessions = new MemorySessionStore();
    const settings = new MemorySettingsStore<StudioPersistedState>();
    const first = setup({ sessions, settings });
    await configureMission(first.service);
    await first.service.runMission();
    const original = first.service.snapshot().activeSessionId;

    const second = setup({ sessions, settings });
    await second.service.initialize();

    expect(second.service.snapshot()).toMatchObject({
      activeSessionId: original,
      mission: { goal: "Make the behavior demonstrably correct", phase: "succeeded" },
    });
    expect((await second.service.listSessions())[0]).toMatchObject({ id: original, mission: { phase: "succeeded" } });
  });

  it("requires explicit execution confirmation and leaves a declined mission byte-for-byte draft", async () => {
    const confirm = vi.fn(async () => false);
    const { service, alpha } = setup({ confirm });
    await configureMission(service);
    const before = service.mission();

    const after = await service.runMission();

    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("npm test"));
    expect(after).toEqual(before);
    expect(alpha.runs).toHaveLength(0);
  });

  it("serializes native execution approval so the displayed commands cannot change underneath it", async () => {
    let approvalOpened!: () => void;
    let answerApproval!: (approved: boolean) => void;
    const opened = new Promise<void>((resolve) => { approvalOpened = resolve; });
    const confirm = vi.fn(async () => new Promise<boolean>((resolve) => {
      answerApproval = resolve;
      approvalOpened();
    }));
    const { service } = setup({ confirm });
    await configureMission(service);

    const first = service.runMission();
    await opened;

    await expect(service.runMission()).rejects.toThrow(/approval.*progress/i);
    await expect(service.addMissionCheck("npm run hidden")).rejects.toThrow(/approval.*progress/i);
    await expect(service.setWorkspace("C:\\other")).rejects.toThrow(/approval is open/i);
    answerApproval(false);
    await expect(first).resolves.toMatchObject({ phase: "draft" });
  });

  it("turns an in-flight persisted phase into an inspectable resumable pause after restart", async () => {
    const sessions = new MemorySessionStore();
    const settings = new MemorySettingsStore<StudioPersistedState>();
    const first = setup({ sessions, settings });
    await configureMission(first.service);
    const id = first.service.snapshot().activeSessionId;
    const draft = first.service.mission()!;
    await sessions.setMission(id, {
      ...draft,
      phase: "verifying",
      startedAt: "2026-08-24T00:00:05.000Z",
      executionApprovedAt: "2026-08-24T00:00:05.000Z",
      attempt: 1,
      nextAction: "verify",
      runtimeSettings: { alpha: { permissionMode: "standard" } },
    });

    const second = setup({ sessions, settings });
    await second.service.initialize();

    expect(second.service.mission()).toMatchObject({
      phase: "paused",
      nextAction: "verify",
      blockReason: expect.stringContaining("closed while this mission step was active"),
    });
  });

  it("allows a human-directed repair from review without converting reviewer prose into control flow", async () => {
    const { service, alpha, beta } = setup({ checks: [passed(), passed()] });
    await service.initialize();
    await service.setWorkspace("C:\\work\\mission");
    await service.setPrimary("alpha");
    await service.addReviewer("beta");
    await service.createMission("Ship polished behavior");
    await service.addMissionCheck("npm test");
    await service.runMission();

    const repaired = await service.repairMission("Address the review's concrete accessibility finding.");

    expect(alpha.runs).toHaveLength(2);
    expect(alpha.runs[1]?.prompt).toContain("accessibility finding");
    expect(beta.runs).toHaveLength(2);
    expect(repaired).toMatchObject({ phase: "awaiting-approval", attempt: 2, outcome: null });
  });
});
