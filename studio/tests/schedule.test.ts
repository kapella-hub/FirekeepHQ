import { describe, expect, it, vi } from "vitest";
import { RuntimeRegistry } from "../src/core/runtime-registry.js";
import { cleanScheduleChecks, nextOccurrence, parseSchedules, settleMissed, type MissionSchedule } from "../src/core/schedule.js";
import { MemorySessionStore } from "../src/core/session-store.js";
import { MemorySettingsStore } from "../src/core/settings-store.js";
import { StudioService, type StudioPersistedState } from "../src/core/studio-service.js";
import { FakeRuntime } from "./helpers/fake-runtime.js";

const schedule = (extra: Partial<MissionSchedule> = {}): MissionSchedule => ({
  id: "schedule-1", goal: "Keep the build green", checks: ["npm test"], repeat: "daily", nextRunAt: "2026-11-10T09:00:00.000Z",
  workspacePath: "C:\\repo", primaryRuntimeId: "alpha", reviewerRuntimeIds: [], runtimeSettings: { alpha: { permissionMode: "standard" } },
  worktree: false, createdAt: "2026-11-09T00:00:00.000Z", approvedAt: "2026-11-09T00:00:00.000Z", lastRun: null, ...extra,
});

describe("schedule rules", () => {
  it("finds the next occurrence at the same local time, skipping weekends for weekdays", () => {
    const friday = new Date(2026, 10, 13, 9, 30);   // local time, Friday 13 November 2026
    const after = friday.getTime();
    expect(nextOccurrence(friday.toISOString(), "once", after)).toBeNull();
    const daily = new Date(nextOccurrence(friday.toISOString(), "daily", after) as string);
    expect([daily.getDate(), daily.getHours(), daily.getMinutes()]).toEqual([14, 9, 30]);
    const weekday = new Date(nextOccurrence(friday.toISOString(), "weekdays", after) as string);
    expect([weekday.getDate(), weekday.getDay(), weekday.getHours()]).toEqual([16, 1, 9]);
    const weekly = new Date(nextOccurrence(friday.toISOString(), "weekly", after) as string);
    expect(weekly.getDate()).toBe(20);
    // Long overdue: the next occurrence is still in the future, never a backlog.
    const late = new Date(nextOccurrence(friday.toISOString(), "daily", after + 40 * 86_400_000) as string);
    expect(late.getTime()).toBeGreaterThan(after + 40 * 86_400_000);
    expect(late.getTime()).toBeLessThanOrEqual(after + 41 * 86_400_000 + 3_600_000);
  });

  it("does not fire late: a missed time is noted and moved on", () => {
    const [daily, once, future] = settleMissed([
      schedule(),
      schedule({ id: "schedule-2", repeat: "once" }),
      schedule({ id: "schedule-3", nextRunAt: "2026-11-12T09:00:00.000Z" }),
    ], "2026-11-11T12:00:00.000Z");
    expect(Date.parse(daily?.nextRunAt as string)).toBeGreaterThan(Date.parse("2026-11-11T12:00:00.000Z"));
    expect(daily?.lastRun).toMatchObject({ sessionId: null, error: expect.stringContaining("Missed") });
    expect(once?.nextRunAt).toBeNull();
    expect(future?.lastRun).toBeNull();
  });

  it("drops stored entries that are incomplete or were never approved", () => {
    const { approvedAt: _approvedAt, ...unapproved } = schedule({ id: "schedule-x" });
    const parsed = parseSchedules([schedule(), unapproved, { ...schedule({ id: "schedule-y" }), checks: [] }, "junk", { ...schedule({ id: "schedule-z" }), runtimeSettings: { alpha: { permissionMode: "anything" } } }]);
    expect(parsed.map((entry) => entry.id)).toEqual(["schedule-1", "schedule-z"]);
    expect(parsed[1]?.runtimeSettings).toEqual({});
    expect(parseSchedules(null)).toEqual([]);
    expect(() => cleanScheduleChecks(["  ", ""])).toThrow("at least one check");
    expect(cleanScheduleChecks([" npm test ", ""])).toEqual(["npm test"]);
  });
});

describe("scheduled missions in StudioService", () => {
  function setup(confirm: (summary: string) => Promise<boolean> = async () => true) {
    const alpha = new FakeRuntime({ id: "alpha", displayName: "Alpha", description: "writer", transport: "test", capabilities: ["chat", "review", "usage"] }, "implemented the requested change");
    const beta = new FakeRuntime({ id: "beta", displayName: "Beta", description: "other", transport: "test", capabilities: ["chat"] }, "beta answer");
    const sessions = new MemorySessionStore();
    const settings = new MemorySettingsStore<StudioPersistedState>();
    const clock = { now: "2026-11-10T08:00:00.000Z" };
    const commands: Array<{ command: string; cwd: string }> = [];
    const confirmMission = vi.fn(confirm);
    let id = 0;
    const service = new StudioService({
      runtimes: new RuntimeRegistry([alpha, beta]),
      settings,
      sessions,
      missionChecks: { run: async (check, cwd) => { commands.push({ command: check.command, cwd }); return { exitCode: 0, signal: null, stdout: "ok", stderr: "", timedOut: false, truncated: false, durationMs: 5 }; } },
      confirmMission,
      idFactory: (prefix) => `${prefix}-${++id}`,
      now: () => clock.now,
    });
    return { service, alpha, beta, sessions, settings, clock, commands, confirmMission };
  }

  async function ready(service: StudioService): Promise<void> {
    await service.initialize();
    await service.setPrimary("alpha");
    await service.setWorkspace("C:\\repo");
    await service.setPermissionMode("alpha", "safe");
  }

  it("asks once, at schedule time, and stores nothing when declined", async () => {
    const declined = setup(async () => false);
    await ready(declined.service);
    expect(await declined.service.scheduleMission({ goal: "Keep the build green", checks: ["npm test"], firstRunAt: "2026-11-10T09:00:00.000Z", repeat: "daily" })).toBeNull();
    expect(declined.service.snapshot().schedules).toEqual([]);

    const { service, confirmMission } = setup();
    await ready(service);
    await expect(service.scheduleMission({ goal: "Late", checks: ["npm test"], firstRunAt: "2026-11-10T07:00:00.000Z", repeat: "once" })).rejects.toThrow("in the future");
    await expect(service.scheduleMission({ goal: "No checks", checks: [" "], firstRunAt: "2026-11-10T09:00:00.000Z", repeat: "once" })).rejects.toThrow("at least one check");
    expect(confirmMission).not.toHaveBeenCalled();
    const stored = await service.scheduleMission({ goal: "Keep the build green", checks: ["npm test"], firstRunAt: "2026-11-10T09:00:00.000Z", repeat: "daily" });
    expect(confirmMission).toHaveBeenCalledTimes(1);
    const summary = confirmMission.mock.calls[0]?.[0] as string;
    expect(summary).toContain("SCHEDULED MISSION");
    expect(summary).toContain("starts without asking again");
    expect(summary).toContain("Primary permission: safe");
    expect(summary).toContain("npm test");
    expect(service.snapshot().schedules).toEqual([stored]);
  });

  it("starts a due mission in its own session with the settings that were approved, without asking again", async () => {
    const { service, alpha, beta, sessions, clock, commands, confirmMission } = setup();
    await ready(service);
    const open = service.snapshot().activeSessionId;
    await service.scheduleMission({ goal: "Keep the build green", checks: ["npm test"], firstRunAt: "2026-11-10T09:00:00.000Z", repeat: "daily" });
    // The human moves on: another folder, another agent, looser permissions.
    await service.setWorkspace("C:\\elsewhere");
    await service.setPrimary("beta");
    await service.setPermissionMode("alpha", "unrestricted");

    await service.runDueSchedules();
    expect(alpha.runs).toHaveLength(0);

    clock.now = "2026-11-10T09:00:20.000Z";
    await service.runDueSchedules();
    expect(confirmMission).toHaveBeenCalledTimes(1);
    expect(beta.runs).toHaveLength(0);
    expect(alpha.runs.at(0)).toMatchObject({ cwd: "C:\\repo", permissionMode: "safe" });
    expect(commands).toEqual([{ command: "npm test", cwd: "C:\\repo" }]);

    const [stored] = service.snapshot().schedules;
    expect(Date.parse(stored?.nextRunAt as string)).toBeGreaterThan(Date.parse(clock.now));
    expect(stored?.lastRun).toMatchObject({ error: null });
    const ran = stored?.lastRun?.sessionId as string;
    expect(ran).not.toBe(open);
    expect(service.snapshot().activeSessionId).toBe(open);
    const summary = (await sessions.list()).find((item) => item.id === ran);
    expect(summary?.mission).toMatchObject({ goal: "Keep the build green", workspacePath: "C:\\repo", primaryRuntimeId: "alpha" });
    expect(summary?.mission?.executionApprovedAt).not.toBeNull();

    // The same time never fires twice.
    await service.runDueSchedules();
    expect(alpha.runs).toHaveLength(1);
  });

  it("retires a one-time schedule, records why a run could not start, and deletes on request", async () => {
    const { service, alpha, clock } = setup();
    await ready(service);
    const once = await service.scheduleMission({ goal: "One time", checks: ["npm test"], firstRunAt: "2026-11-10T09:00:00.000Z", repeat: "once" });
    await service.scheduleMission({ goal: "Needs isolation", checks: ["npm test"], firstRunAt: "2026-11-10T09:00:00.000Z", repeat: "once", worktree: true }).then(
      () => { throw new Error("expected a refusal"); },
      (error: Error) => expect(error.message).toContain("worktree sessions are unavailable"),
    );
    alpha.run = async () => { throw new Error("agent crashed"); };
    clock.now = "2026-11-10T09:01:00.000Z";
    await service.runDueSchedules();
    const [stored] = service.snapshot().schedules;
    expect(stored).toMatchObject({ id: once?.id, nextRunAt: null });
    expect(stored?.lastRun?.sessionId).not.toBeNull();
    await service.deleteSchedule(once?.id as string);
    expect(service.snapshot().schedules).toEqual([]);
    await expect(service.deleteSchedule("schedule-missing")).rejects.toThrow("unknown scheduled mission");
  });

  it("does not replay a time that passed while Studio was closed", async () => {
    const first = setup();
    await ready(first.service);
    await first.service.scheduleMission({ goal: "Nightly", checks: ["npm test"], firstRunAt: "2026-11-10T09:00:00.000Z", repeat: "daily" });

    const later = setup();
    later.settings.value = first.settings.value;
    later.clock.now = "2026-11-12T12:00:00.000Z";
    await later.service.initialize();
    const [stored] = later.service.snapshot().schedules;
    expect(stored?.lastRun?.error).toContain("Missed");
    await later.service.runDueSchedules();
    expect(later.alpha.runs).toHaveLength(0);
  });
});
