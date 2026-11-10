import { describe, expect, it } from "vitest";
import { attentionForDecision, attentionForEvent, attentionForMission, type AttentionNotice } from "../src/core/attention.js";
import type { MissionSnapshot } from "../src/core/mission.js";
import type { RuntimeEvent, RuntimeEventPayload } from "../src/core/runtime.js";
import type { StudioSnapshot } from "../src/core/studio-service.js";
import { AttentionNotifier } from "../src/main/attention-notifier.js";
import type { DecisionBoardDocument } from "../src/shared/decision-board.js";

let sequence = 0;
function event(payload: RuntimeEventPayload, runId = "run-1"): RuntimeEvent {
  sequence += 1;
  return { id: `e-${sequence}`, runId, studioSessionId: "s", runtimeId: "alpha", timestamp: "2026-11-10T00:00:00.000Z", payload } as RuntimeEvent;
}
const mission = (phase: MissionSnapshot["phase"], extra: Partial<MissionSnapshot> = {}): MissionSnapshot =>
  ({ id: "m1", goal: "Ship the inbox", phase, attempt: 0, blockReason: null, ...extra }) as MissionSnapshot;
const snap = (value: MissionSnapshot | null): StudioSnapshot => ({ activeSessionId: "s", mission: value, background: [] }) as unknown as StudioSnapshot;
const board = { url: "http://127.0.0.1:1/b/1", spec: { context: "Scope of the release", questions: [{}, {}] } } as unknown as DecisionBoardDocument;

describe("attention rules", () => {
  it("notices approvals, finished runs and real failures, but not cancellations", () => {
    expect(attentionForEvent(event({ kind: "approval.requested", approvalId: "a1", title: "Run npm test", detail: "", options: ["Allow"] }))).toMatchObject({ key: "approval:a1", kind: "approval", body: "Run npm test" });
    expect(attentionForEvent(event({ kind: "run.completed", durationMs: 5 }))).toMatchObject({ key: "run:run-1", kind: "run" });
    expect(attentionForEvent(event({ kind: "run.failed", cancelled: false, error: "boom", durationMs: 5 }))).toMatchObject({ title: "The run failed", body: "boom" });
    expect(attentionForEvent(event({ kind: "run.failed", cancelled: true, error: "stopped", durationMs: 5 }))).toBeNull();
    expect(attentionForEvent(event({ kind: "notice", level: "info", message: "hi" }))).toBeNull();
  });

  it("treats the runs inside a mission as steps, while approvals still interrupt", () => {
    expect(attentionForEvent(event({ kind: "run.completed", durationMs: 5 }), "running")).toBeNull();
    expect(attentionForEvent(event({ kind: "run.failed", cancelled: false, error: "x", durationMs: 5 }), "repairing")).toBeNull();
    expect(attentionForEvent(event({ kind: "approval.requested", approvalId: "a2", title: "Write file", detail: "", options: [] }), "running")).not.toBeNull();
    expect(attentionForEvent(event({ kind: "run.completed", durationMs: 5 }), "succeeded")).not.toBeNull();
  });

  it("notices a mission only when it starts waiting or ends", () => {
    expect(attentionForMission("reviewing", mission("awaiting-approval"))).toMatchObject({ kind: "mission", title: "Mission is waiting for your sign-off", body: "Ship the inbox" });
    expect(attentionForMission("running", mission("paused", { blockReason: "Token budget reached" }))?.body).toContain("Token budget reached");
    expect(attentionForMission("verifying", mission("succeeded"))?.title).toBe("Mission succeeded");
    expect(attentionForMission("running", mission("verifying"))).toBeNull();
    expect(attentionForMission("running", mission("cancelled"))).toBeNull();
    expect(attentionForMission("paused", mission("paused"))).toBeNull();
    expect(attentionForMission(null, mission("awaiting-approval"))).toBeNull();
    expect(attentionForMission("running", null)).toBeNull();
  });

  it("describes a Decision Board and clips long text", () => {
    expect(attentionForDecision(board)).toMatchObject({ key: "decision:http://127.0.0.1:1/b/1", body: "2 questions · Scope of the release" });
    const long = attentionForEvent(event({ kind: "run.failed", cancelled: false, error: "x".repeat(900), durationMs: 1 }));
    expect(long?.body.length).toBeLessThanOrEqual(180);
  });
});

describe("AttentionNotifier", () => {
  function harness(initial: { enabled?: boolean; focused?: boolean } = {}) {
    const state = { enabled: initial.enabled ?? true, focused: initial.focused ?? false };
    const shown: AttentionNotice[] = [];
    const pending: number[] = [];
    const notifier = new AttentionNotifier({ enabled: () => state.enabled, focused: () => state.focused, show: (notice) => shown.push(notice), setPending: (count) => pending.push(count) });
    return { state, shown, pending, notifier };
  }

  it("notifies once per moment while Studio is in the background", () => {
    const { shown, pending, notifier } = harness();
    const request = event({ kind: "approval.requested", approvalId: "a1", title: "Run npm test", detail: "", options: [] });
    notifier.event(request);
    notifier.event(request);
    notifier.decision(board);
    expect(shown.map((notice) => notice.kind)).toEqual(["approval", "decision"]);
    expect(pending).toEqual([1, 2]);
    notifier.focus();
    notifier.focus();
    expect(pending).toEqual([1, 2, 0]);
  });

  it("stays quiet when focused or switched off, and does not replay what was missed", () => {
    const { state, shown, notifier } = harness({ focused: true });
    const done = event({ kind: "run.completed", durationMs: 1 });
    notifier.event(done);
    state.focused = false;
    notifier.event(done);
    state.enabled = false;
    notifier.event(event({ kind: "run.completed", durationMs: 1 }, "run-2"));
    expect(shown).toEqual([]);
  });

  it("follows mission phases across snapshots and resets on another mission", () => {
    const { shown, notifier } = harness();
    notifier.snapshot(snap(mission("awaiting-approval")));
    expect(shown).toEqual([]);
    notifier.snapshot(snap(mission("running")));
    notifier.event(event({ kind: "run.completed", durationMs: 1 }));
    notifier.snapshot(snap(mission("awaiting-approval")));
    notifier.snapshot(snap(mission("awaiting-approval")));
    expect(shown.map((notice) => notice.title)).toEqual(["Mission is waiting for your sign-off"]);
    notifier.snapshot(snap(mission("succeeded", { id: "m2" })));
    expect(shown).toHaveLength(1);
  });

  it("notices a mission that starts waiting in a background session, once", () => {
    const { shown, notifier } = harness();
    const stored = (phase: MissionSnapshot["phase"]) => [{ id: "other", mission: mission(phase, { id: "m9" }) }] as never;
    notifier.sessions(stored("running"));
    notifier.sessions(stored("awaiting-approval"));
    notifier.sessions(stored("awaiting-approval"));
    expect(shown.map((notice) => notice.title)).toEqual(["Mission is waiting for your sign-off"]);
  });

  it("treats a background mission's runs as steps, using that session's own phase", () => {
    const { shown, notifier } = harness();
    notifier.snapshot({ activeSessionId: "s", mission: null, background: [{ sessionId: "other", running: true, missionPhase: "running", approvals: [] }] } as unknown as StudioSnapshot);
    notifier.event({ ...event({ kind: "run.completed", durationMs: 1 }, "run-bg"), studioSessionId: "other" } as RuntimeEvent);
    expect(shown).toEqual([]);
    notifier.event(event({ kind: "run.completed", durationMs: 1 }, "run-fg"));
    expect(shown.map((notice) => notice.key)).toEqual(["run:run-fg"]);
  });
});
