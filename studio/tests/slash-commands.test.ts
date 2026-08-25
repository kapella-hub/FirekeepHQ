import { describe, expect, it, vi } from "vitest";
import { createCommandRegistry, parseSlashCommand } from "../src/core/slash-commands.js";
import { RuntimeRegistry } from "../src/core/runtime-registry.js";
import { MemorySessionStore } from "../src/core/session-store.js";
import { MemorySettingsStore } from "../src/core/settings-store.js";
import { StudioService } from "../src/core/studio-service.js";
import { FakeRuntime } from "./helpers/fake-runtime.js";

function setup(): { service: StudioService; commands: ReturnType<typeof createCommandRegistry> } {
  const runtimes = new RuntimeRegistry([
    new FakeRuntime({ id: "alpha", displayName: "Alpha", description: "A", transport: "test", capabilities: ["chat", "review", "models"] }),
    new FakeRuntime({ id: "beta", displayName: "Beta", description: "B", transport: "test", capabilities: ["chat", "review", "models"] }),
  ]);
  const service = new StudioService({
    runtimes,
    settings: new MemorySettingsStore(),
    idFactory: (prefix) => `${prefix}-1`,
    now: () => "2026-08-24T00:00:00.000Z",
  });
  return { service, commands: createCommandRegistry(service) };
}

describe("slash command parsing", () => {
  it("parses quotes, escapes, and flags", () => {
    expect(parseSlashCommand('/review beta --focus "security and auth" --note a\\ b')).toEqual({
      name: "review",
      args: ["beta"],
      flags: { focus: "security and auth", note: "a b" },
      raw: '/review beta --focus "security and auth" --note a\\ b',
    });
  });

  it("rejects unterminated quotes", () => {
    expect(() => parseSlashCommand('/review --focus "security')).toThrow(/unterminated quote/i);
  });

  it("preserves Windows path separators while still allowing escaped spaces", () => {
    expect(parseSlashCommand('/kiro open "C:\\Users\\me\\project"').args).toEqual(["open", "C:\\Users\\me\\project"]);
    expect(parseSlashCommand("/session rename one\\ two").args).toEqual(["rename", "one two"]);
  });
});

describe("slash command registry", () => {
  it("dispatches primary and reviewer aliases through StudioService", async () => {
    const { service, commands } = setup();
    await service.initialize();

    await commands.execute("/primary beta");
    await commands.execute("/reviewer add alpha");
    await commands.execute("/reviewer mode after-turn");

    expect(service.snapshot()).toMatchObject({
      primaryRuntimeId: "beta",
      reviewerRuntimeIds: ["alpha"],
      reviewerMode: "after-turn",
    });
  });

  it("provides state-aware completion", async () => {
    const { service, commands } = setup();
    await service.initialize();

    expect(commands.complete("/rev").map((item) => item.value)).toContain("/reviewer");
    expect(commands.complete("/runtime use ").map((item) => item.value)).toEqual([
      "/runtime use alpha",
      "/runtime use beta",
    ]);
    expect(commands.complete("/use a").map((item) => item.value)).toEqual(["/use alpha"]);
    expect(commands.complete("/compare alpha ").map((item) => item.value)).toEqual(["/compare alpha beta"]);
    expect(commands.complete("/connect alpha ").map((item) => item.value)).toContain("/connect <runtime-id> [--method browser|device|api-key]");
  });

  it("renders help from the registered metadata", async () => {
    const { service, commands } = setup();
    await service.initialize();
    const result = await commands.execute("/help reviewer");

    expect(result.kind).toBe("markdown");
    expect(result.title).toBe("/reviewer");
    expect(result.body).toContain("/reviewer add <runtime-id>");
  });

  it("returns a useful error for unknown commands", async () => {
    const { service, commands } = setup();
    await service.initialize();
    await expect(commands.execute("/warp alpha")).rejects.toThrow(/unknown command.*warp/i);
  });

  it("routes Firekeep Client Kit commands through the typed integration", async () => {
    const { service } = setup();
    await service.initialize();
    const execute = vi.fn(async () => ({ ok: true, output: "Keep healthy", exitCode: 0 }));
    const commands = createCommandRegistry(service, { firekeep: { execute } });

    const result = await commands.execute("/firekeep doctor --report");

    expect(execute).toHaveBeenCalledWith("doctor", ["--report"]);
    expect(result).toMatchObject({ title: "Firekeep doctor", body: "Keep healthy", tone: "success" });
  });

  it("runs compare and consensus workflows through runtime-neutral commands", async () => {
    const { service, commands } = setup();
    await service.initialize();
    await service.setPrimary("alpha");
    await service.addReviewer("beta");

    const compared = await commands.execute('/compare --prompt "Which option is safer?"');
    const consensus = await commands.execute("/consensus alpha --focus evidence");

    expect(compared).toMatchObject({ title: "Comparison complete", tone: "success" });
    expect(consensus).toMatchObject({ title: "Consensus complete", tone: "success" });
  });

  it("supports honest token guards and usage reporting", async () => {
    const { service, commands } = setup();
    await service.initialize();

    await commands.execute("/budget set 12.5k");
    const budget = await commands.execute("/budget show");
    const usage = await commands.execute("/usage");

    expect(service.snapshot().tokenBudget).toBe(12_500);
    expect(budget.body).toContain("12,500");
    expect(usage).toMatchObject({ title: "Session usage", rows: [] });
  });

  it("reports live reasoning choices and can return control to the provider", async () => {
    const { service, commands } = setup();
    await service.initialize();
    await service.setPrimary("alpha");

    await expect(commands.execute("/effort")).resolves.toMatchObject({ title: "Reasoning options", body: expect.stringContaining("low, medium, high, xhigh, max") });
    await commands.execute("/effort high");
    expect(service.snapshot().selectedEfforts.alpha).toBe("high");
    await commands.execute("/effort default");
    expect(service.snapshot().selectedEfforts.alpha).toBeUndefined();
  });

  it("opens provider login from slash commands and keeps API keys out of command history", async () => {
    const { service } = setup();
    await service.initialize();
    const login = vi.fn(async () => ({ state: "external" as const, message: "Provider login opened" }));
    const commands = createCommandRegistry(service, { login });

    await expect(commands.execute("/connect alpha --method browser")).resolves.toMatchObject({ body: "Provider login opened" });
    expect(login).toHaveBeenCalledWith("alpha", { method: "browser" });
    await expect(commands.execute("/connect alpha --method api-key")).rejects.toThrow(/secure Connect dialog/i);
    await expect(commands.execute("/connect alpha --method magic")).rejects.toThrow(/invalid login method/i);
  });

  it("provides a typed Kiro IDE handoff command", async () => {
    const { service } = setup();
    await service.initialize();
    const open = vi.fn(async () => ({ message: "Kiro opened" }));
    const probe = vi.fn(async () => ({ available: true, detail: "Installed", executable: "kiro" }));
    const commands = createCommandRegistry(service, { kiroIde: { open, probe } });

    await expect(commands.execute('/kiro open "C:\\workspace"')).resolves.toMatchObject({ title: "Kiro IDE", body: "Kiro opened" });
    expect(open).toHaveBeenCalledWith("C:\\workspace");
    await service.setWorkspace("C:\\shared-workspace");
    await commands.execute("/kiro open");
    expect(open).toHaveBeenLastCalledWith("C:\\shared-workspace");
  });

  it("routes explicit exports through a save integration instead of a generic filesystem bridge", async () => {
    const { service } = setup();
    await service.initialize();
    const exportSession = vi.fn(async () => ({ saved: true, detail: "Saved locally" }));
    const commands = createCommandRegistry(service, { exportSession });

    await expect(commands.execute("/export json")).resolves.toMatchObject({ title: "Session exported", body: "Saved locally" });
    expect(exportSession).toHaveBeenCalledWith("json", expect.stringContaining('"session"'), "Current session");
  });

  it("chooses and reports one shared runtime workspace through a typed picker", async () => {
    const { service } = setup();
    await service.initialize();
    const selectWorkspace = vi.fn(async () => "C:\\work\\project");
    const commands = createCommandRegistry(service, { selectWorkspace });

    await expect(commands.execute("/workspace show")).resolves.toMatchObject({ body: "No workspace selected." });
    await expect(commands.execute("/workspace choose")).resolves.toMatchObject({ title: "Workspace", body: "C:\\work\\project" });
    await expect(commands.execute("/project show")).resolves.toMatchObject({ body: "C:\\work\\project" });
    expect(service.snapshot().workspacePath).toBe("C:\\work\\project");
    expect(selectWorkspace).toHaveBeenCalledOnce();
  });

  it("requires an exact confirmation token for destructive session commands", async () => {
    const sessions = new MemorySessionStore();
    const runtimes = new RuntimeRegistry([new FakeRuntime({ id: "alpha", displayName: "Alpha", description: "A", transport: "test", capabilities: ["chat"] })]);
    const service = new StudioService({ runtimes, settings: new MemorySettingsStore(), sessions, idFactory: (() => { let value = 0; return (prefix) => `${prefix}-${++value}`; })(), now: () => "2026-08-24T00:00:00.000Z" });
    await service.initialize();
    const id = service.snapshot().activeSessionId;
    const commands = createCommandRegistry(service);

    await expect(commands.execute(`/session delete ${id}`)).rejects.toThrow(/--confirm/i);
    await expect(commands.execute(`/session delete ${id} --confirm ${id}`)).resolves.toMatchObject({ title: "Session deleted", tone: "warning" });
  });

  it("configures, runs, and reports an evidence-backed mission from slash commands", async () => {
    const runtimes = new RuntimeRegistry([
      new FakeRuntime({ id: "alpha", displayName: "Alpha", description: "A", transport: "test", capabilities: ["chat", "review", "usage"] }),
    ]);
    const service = new StudioService({
      runtimes,
      settings: new MemorySettingsStore(),
      sessions: new MemorySessionStore(),
      missionChecks: {
        run: async () => ({ exitCode: 0, signal: null, stdout: "green", stderr: "", timedOut: false, truncated: false, durationMs: 4 }),
      },
      confirmMission: async () => true,
      idFactory: (() => { let value = 0; return (prefix) => `${prefix}-${++value}`; })(),
      now: () => "2026-08-24T00:00:00.000Z",
    });
    await service.initialize();
    await service.setWorkspace("C:\\work\\mission");
    await service.setPrimary("alpha");
    const commands = createCommandRegistry(service);

    await commands.execute('/mission new "Prove the feature works"');
    await commands.execute('/mission check add "npm test" --name suite --timeout 2m');
    await commands.execute("/mission budget 20k");
    await commands.execute("/mission repairs 2");
    const status = await commands.execute("/mission status");
    const run = await commands.execute("/mission run");
    const report = await commands.execute("/mission report");

    expect(status).toMatchObject({ title: "Mission · draft", rows: expect.arrayContaining([["suite", "npm test", "pending"]]) });
    expect(run).toMatchObject({ title: "Mission · succeeded", tone: "success" });
    expect(report.body).toContain("source: `verified`");
    expect(service.mission()).toMatchObject({ tokenBudget: 20_000, maxRepairAttempts: 2 });
    expect(commands.complete("/mission ").map((item) => item.value)).toContain("/mission status");
  });
});
