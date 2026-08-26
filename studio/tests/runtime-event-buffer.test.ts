import { describe, expect, it } from "vitest";
import type { RuntimeEvent, RuntimeEventPayload } from "../src/core/runtime.js";
import { coalesceRuntimeEvents } from "../src/renderer/src/runtime-event-buffer.js";

function event(id: string, payload: RuntimeEventPayload): RuntimeEvent {
  return { id, runId: "run-1", studioSessionId: "session-1", runtimeId: "codex", timestamp: `2026-08-25T00:00:0${id}.000Z`, payload };
}

describe("coalesceRuntimeEvents", () => {
  it("collapses streaming bursts while preserving final messages and tool inputs", () => {
    const events = coalesceRuntimeEvents([], [
      event("1", { kind: "message.delta", messageId: "m", role: "assistant", text: "Hel" }),
      event("2", { kind: "message.delta", messageId: "m", role: "assistant", text: "lo" }),
      event("3", { kind: "reasoning.delta", itemId: "r", text: "Check " }),
      event("4", { kind: "reasoning.delta", itemId: "r", text: "types" }),
      event("5", { kind: "tool.started", toolCallId: "t", name: "Bash", input: { command: "npm test" } }),
      event("6", { kind: "tool.completed", toolCallId: "t", name: "Bash", output: "ok" }),
      event("7", { kind: "message.completed", messageId: "m", role: "assistant", text: "Hello" }),
    ]);

    expect(events).toHaveLength(4);
    expect(events.map((item) => item.payload.kind)).toEqual(["message.completed", "reasoning.delta", "tool.started", "tool.completed"]);
    expect(events[1]?.payload).toMatchObject({ kind: "reasoning.delta", text: "Check types" });
    expect(events[2]?.payload).toMatchObject({ kind: "tool.started", input: { command: "npm test" } });
  });

  it("drops duplicate event ids without merging independent runs", () => {
    const first = event("1", { kind: "message.delta", messageId: "m", role: "assistant", text: "one" });
    const otherRun = { ...event("2", { kind: "message.delta", messageId: "m", role: "assistant", text: "two" }), runId: "run-2" };

    expect(coalesceRuntimeEvents([first], [first, otherRun])).toEqual([first, otherRun]);
  });
});
