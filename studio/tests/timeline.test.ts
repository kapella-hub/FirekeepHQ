import { describe, expect, it } from "vitest";
import type { RuntimeEvent, RuntimeEventPayload } from "../src/core/runtime.js";
import { buildTimeline, groupTimeline } from "../src/renderer/src/timeline.js";

function event(id: string, payload: RuntimeEventPayload): RuntimeEvent {
  return { id, runId: "run-1", studioSessionId: "session-1", runtimeId: "codex", timestamp: `2026-08-24T00:00:0${id}.000Z`, payload };
}

describe("buildTimeline", () => {
  it("coalesces deltas and tool lifecycle updates without losing order", () => {
    const timeline = buildTimeline([
      event("1", { kind: "message.delta", messageId: "m", role: "assistant", text: "Hel" }),
      event("2", { kind: "message.delta", messageId: "m", role: "assistant", text: "lo" }),
      event("3", { kind: "tool.started", toolCallId: "t", name: "Shell", input: { command: "test" } }),
      event("4", { kind: "tool.completed", toolCallId: "t", name: "Shell", output: "ok" }),
      event("5", { kind: "message.completed", messageId: "m", role: "assistant", text: "Hello!" }),
    ]);

    expect(timeline).toEqual([
      expect.objectContaining({ kind: "message", text: "Hello!", complete: true }),
      expect.objectContaining({ kind: "tool", name: "Shell", summary: "Ran tests", status: "completed", input: { command: "test" }, output: "ok" }),
    ]);
  });

  it("describes completed tools without exposing a generic tool/status label", () => {
    const timeline = buildTimeline([
      event("1", { kind: "tool.started", toolCallId: "bash", name: "Bash", input: { command: "git status --short" } }),
      event("2", { kind: "tool.completed", toolCallId: "bash", name: "Bash", output: "clean" }),
      event("3", { kind: "tool.started", toolCallId: "memory", name: "mcp__firekeep__memory_recall", input: { task: "prior work" } }),
      event("4", { kind: "tool.completed", toolCallId: "memory", name: "mcp__firekeep__memory_recall", output: "found" }),
    ]);

    expect(timeline).toEqual(expect.arrayContaining([
      expect.objectContaining({ kind: "tool", summary: "Checked repository status" }),
      expect.objectContaining({ kind: "tool", summary: "Recalled team memory" }),
    ]));
  });

  it("renders failed and cancelled run terminals while keeping lifecycle starts quiet", () => {
    const timeline = buildTimeline([
      event("1", { kind: "run.started", mode: "primary", permissionMode: "standard" }),
      event("2", { kind: "run.failed", cancelled: false, error: "provider unavailable", durationMs: 12 }),
    ]);
    expect(timeline).toEqual([expect.objectContaining({ kind: "notice", level: "error", message: "Run failed", detail: "provider unavailable" })]);
  });

  it("groups each run with messages before low-priority working activity", () => {
    const runs = groupTimeline(buildTimeline([
      event("1", { kind: "tool.started", toolCallId: "t", name: "Shell", input: { command: "npm test" } }),
      event("2", { kind: "tool.completed", toolCallId: "t", name: "Shell", output: "passed" }),
      event("3", { kind: "message.completed", messageId: "m", role: "assistant", text: "Final answer" }),
      event("4", { kind: "usage.updated", usage: { inputTokens: 10, outputTokens: 5 } }),
    ]));

    expect(runs).toHaveLength(1);
    expect(runs[0]?.messages).toEqual([expect.objectContaining({ kind: "message", text: "Final answer" })]);
    expect(runs[0]?.activity).toEqual([
      expect.objectContaining({ kind: "tool", summary: "Ran tests" }),
      expect.objectContaining({ kind: "usage" }),
    ]);
  });
});
