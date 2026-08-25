import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import type { AgentRuntime, RuntimeEvent } from "../src/core/runtime.js";
import { RuntimeRegistry } from "../src/core/runtime-registry.js";
import { MemorySessionStore } from "../src/core/session-store.js";
import { MemorySettingsStore } from "../src/core/settings-store.js";
import { StudioService, type StudioPersistedState } from "../src/core/studio-service.js";
import { ProcessMissionCheckRunner } from "../src/main/mission-check-runner.js";
import { ClaudeRuntime } from "../src/main/runtime/claude-runtime.js";
import { CodexRuntime } from "../src/main/runtime/codex-runtime.js";
import { KiroRuntime } from "../src/main/runtime/kiro-runtime.js";

const live = process.env.STUDIO_LIVE_TASKS === "1";
const requested = new Set((process.env.STUDIO_LIVE_TASK_RUNTIME ?? "codex,claude,kiro").split(",").map((item) => item.trim()).filter(Boolean));
const available: Array<[string, () => AgentRuntime]> = [
  ["codex", () => new CodexRuntime()],
  ["claude", () => new ClaudeRuntime()],
  ["kiro", () => new KiroRuntime()],
];
const cases = available.filter(([id]) => requested.has(id));

describe.skipIf(!live)("installed runtime task conformance", () => {
  it.each(cases)("completes and verifies a disposable mission with %s", async (runtimeId, createRuntime) => {
    const workspace = await mkdtemp(join(tmpdir(), `firekeep-studio-${runtimeId}-`));
    const expected = `firekeep-studio-conformance:${runtimeId}`;
    const verifyPath = join(workspace, "verify.cjs");
    await writeFile(verifyPath, [
      "const fs = require('node:fs');",
      `const expected = ${JSON.stringify(expected)};`,
      "const actual = fs.readFileSync('result.txt', 'utf8').trim();",
      "if (actual !== expected) { console.error(JSON.stringify({ expected, actual })); process.exit(1); }",
      "console.log('verified');",
    ].join("\n"), "utf8");
    const runtime = createRuntime();
    const service = new StudioService({
      runtimes: new RuntimeRegistry([runtime]),
      settings: new MemorySettingsStore<StudioPersistedState>(),
      sessions: new MemorySessionStore(),
      missionChecks: new ProcessMissionCheckRunner(),
      confirmMission: async () => true,
    });
    const events: RuntimeEvent[] = [];
    service.subscribe((event) => {
      events.push(event);
      if (event.payload.kind !== "approval.requested") return;
      const allow = event.payload.options.find((option) => /accept|allow_once|allow once|approve/i.test(option));
      if (allow) service.resolveApproval(event.payload.approvalId, allow);
    });

    try {
      const connection = await runtime.probe();
      expect(connection, connection.detail).toMatchObject({ state: "ready" });
      const auth = await runtime.authStatus();
      expect(auth, auth.detail).toMatchObject({ state: "connected" });
      await service.initialize();
      await service.setWorkspace(workspace);
      await service.setPrimary(runtimeId);
      if (process.env.STUDIO_LIVE_TASK_PERMISSION === "unrestricted") await service.setPermissionMode(runtimeId, "unrestricted");
      await service.createMission(`Create result.txt containing exactly ${expected}. Do not modify any other file except result.txt.`);
      await service.addMissionCheck(`${quote(process.execPath)} ${quote(verifyPath)}`, { name: "result contract", timeoutMs: 30_000 });

      const mission = await service.runMission();

      expect(mission).toMatchObject({ phase: "succeeded", outcome: { taskResult: "success", taskResultSource: "verified" } });
      expect((await readFile(join(workspace, "result.txt"), "utf8")).trim()).toBe(expected);
      expect(events).toContainEqual(expect.objectContaining({ payload: expect.objectContaining({ kind: "message.completed" }) }));
      expect(events).toContainEqual(expect.objectContaining({ payload: expect.objectContaining({ kind: "tool.started" }) }));
      expect(service.snapshot().nativeSessionIds[runtimeId]).toBeTruthy();
      expect(service.snapshot().usage.tokens).toBeGreaterThan(0);
      expect(service.snapshot().usage.measuredRuns).toBeGreaterThan(0);
    } finally {
      await service.shutdown();
      await rm(workspace, { recursive: true, force: true });
    }
  }, 8 * 60_000);
});

function quote(value: string): string {
  return `"${value.replace(/"/g, '\\"')}"`;
}
