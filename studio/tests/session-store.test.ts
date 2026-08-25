import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import type { MissionSnapshot } from "../src/core/mission.js";
import type { RuntimeEvent } from "../src/core/runtime.js";
import { JsonlSessionStore } from "../src/main/session-store.js";

const event: RuntimeEvent = {
  id: "event-1",
  runId: "run-1",
  studioSessionId: "session-1",
  runtimeId: "codex",
  timestamp: "2026-08-24T00:00:00.000Z",
  payload: { kind: "message.completed", messageId: "m1", role: "assistant", text: "hello" },
};

const mission: MissionSnapshot = {
  version: 1,
  id: "mission-1",
  goal: "Prove persistence",
  phase: "draft",
  createdAt: "2026-08-24T00:00:00.000Z",
  updatedAt: "2026-08-24T00:00:00.000Z",
  startedAt: null,
  completedAt: null,
  workspacePath: "C:\\work",
  primaryRuntimeId: "codex",
  reviewerRuntimeIds: [],
  runtimeSettings: {},
  checks: [],
  tokenBudget: 50_000,
  maxRepairAttempts: 1,
  attempt: 0,
  nextAction: null,
  nextReviewerIndex: 0,
  measuredTokensAtStart: 0,
  measuredTokens: 0,
  lastPrimaryText: "",
  manualRepairNote: null,
  verificationPassed: null,
  checkReceipts: [],
  reviewReceipts: [],
  outcome: null,
  blockReason: null,
  executionApprovedAt: null,
};

describe("JsonlSessionStore", () => {
  it("persists session metadata, native runtime ids, and events", async () => {
    const root = await mkdtemp(join(tmpdir(), "firekeep-studio-sessions-"));
    const store = new JsonlSessionStore(root);

    await store.ensure("session-1", "2026-08-24T00:00:00.000Z");
    await store.append(event);
    await store.rename("session-1", "Important work");
    await store.setNativeSessionIds("session-1", { codex: "thread-1" });
    await store.setMission("session-1", mission);
    await store.flush();

    expect(await store.load("session-1")).toEqual([event]);
    expect(await store.list()).toEqual([expect.objectContaining({ id: "session-1", name: "Important work", eventCount: 1, nativeSessionIds: { codex: "thread-1" }, mission })]);

    await store.remove("session-1");
    expect(await store.list()).toEqual([]);
    expect(await store.load("session-1")).toEqual([]);
  });

  it("rejects traversal-shaped session ids", async () => {
    const root = await mkdtemp(join(tmpdir(), "firekeep-studio-sessions-"));
    const store = new JsonlSessionStore(root);
    await expect(store.ensure("../escape", "2026-08-24T00:00:00.000Z")).rejects.toThrow(/invalid session id/i);
  });
});
