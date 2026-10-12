import { describe, expect, it } from "vitest";
import { measuredReceipt, parseTimelineReply, partialReceipt, receiptLabel, unavailableReceipt, type ReplayEvent } from "../src/core/keep/receipt.js";

const read = (id: string, agent: string, ids: string[], trigger: string | null = null, query = "q"): ReplayEvent => ({
  id, agent_id: agent, event_type: "memory_read", timestamp: "t",
  payload: { query, trigger, result_count: ids.length, memory_ids: ids },
});
const write = (id: string, agent: string, summary: string): ReplayEvent => ({
  id, agent_id: agent, event_type: "memory_write", timestamp: "t",
  payload: { action_summary: summary, memory_id: `m-${id}` },
});

describe("parseTimelineReply", () => {
  it("parses the server's JSON format", () => {
    const page = parseTimelineReply(JSON.stringify({ session_id: "s", events: [read("e1", "a", ["m1"])], total: 1, has_more: false }));
    expect(page).toEqual({ kind: "page", events: [read("e1", "a", ["m1"])], total: 1, hasMore: false });
  });
  it("reads today's Markdown reply as an old server", () => {
    expect(parseTimelineReply("## Trace Timeline — s (2 events)\n- `t` **memory_read**")).toEqual({ kind: "unsupported" });
    expect(parseTimelineReply("No trace events found for session s")).toEqual({ kind: "unsupported" });
  });
  it("reads a JSON error as an error, not an old server", () => {
    expect(parseTimelineReply(JSON.stringify({ error: "403 forbidden" }))).toEqual({ kind: "error", message: "403 forbidden" });
  });
  it("reads an argument-validation failure (old server, unknown param) as unsupported", () => {
    expect(parseTimelineReply("Error executing tool replay_timeline: 1 validation error … format")).toEqual({ kind: "unsupported" });
  });
  it("filters invalid events from JSON events array", () => {
    const page = parseTimelineReply(JSON.stringify({
      session_id: "s",
      events: [
        read("e1", "a", ["m1"]),
        { id: "e2", event_type: "memory_read", timestamp: "t", payload: {} }, // missing agent_id
      ],
      total: 2,
      has_more: false,
    }));
    expect(page).toEqual({ kind: "page", events: [read("e1", "a", ["m1"])], total: 2, hasMore: false });
  });
});

describe("measuredReceipt", () => {
  it("counts distinct memory ids and writes, and drops panel events", () => {
    const receipt = measuredReceipt([
      read("e1", "agent-a", ["m1", "m2"], "prompt-hook", "deploy"),
      read("e2", "agent-a", ["m2", "m3"]),
      read("e3", "agent-a-studio-panel", ["m9"]),
      write("e4", "agent-a", "fixed the tunnel"),
      { id: "e5", agent_id: "agent-a", event_type: "ctx_update", timestamp: "t", payload: {} },
    ], ["s1"], { s1: 5 }, true);
    expect(receipt.status).toBe("measured");
    expect(receipt.recalledCount).toBe(3);
    expect(receipt.learnedCount).toBe(1);
    expect(receipt.recalls.map((recall) => recall.trigger)).toEqual(["prompt-hook", "agent"]);
    expect(receipt.recalls[0]?.query).toBe("deploy");
    expect(receipt.writes).toEqual([{ summary: "fixed the tunnel", memoryId: "m-e4" }]);
    expect(receiptLabel(receipt)).toBe("Keep · recalled 3 · learned 1");
  });
  it("says nothing recalled only when it measured nothing", () => {
    expect(receiptLabel(measuredReceipt([], ["s1"], { s1: 0 }, true))).toBe("Keep · nothing recalled");
  });
  it("counts resultCount for id-less recalls instead of showing confident zero", () => {
    const receipt = measuredReceipt([
      { id: "e1", agent_id: "agent-a", event_type: "memory_read", timestamp: "t", payload: { query: "q", trigger: null, result_count: 2, memory_ids: [] } },
    ], ["s1"], { s1: 0 }, true);
    expect(receipt.recalledCount).toBe(2);
    expect(receiptLabel(receipt)).toBe("Keep · recalled 2 · learned 0");
  });
});

describe("partialReceipt", () => {
  it("counts visible Firekeep tool calls and is labelled partial", () => {
    const receipt = partialReceipt([
      { kind: "tool.started", toolCallId: "1", name: "mcp__firekeep__memory_recall", input: { task: "vps" } },
      { kind: "tool.started", toolCallId: "2", name: "memory_learn", input: { action: "x" } },
      { kind: "tool.started", toolCallId: "3", name: "Read", input: {} },
    ], true);
    expect(receipt.status).toBe("partial");
    expect(receipt.recalledCount).toBe(1);
    expect(receipt.learnedCount).toBe(1);
    expect(receipt.recalls[0]?.query).toBe("vps");
    expect(receiptLabel(receipt)).toBe("Keep · recalled 1 · learned 1 · partial — hook-injected recall not shown");
  });
  it("never says nothing recalled", () => {
    expect(receiptLabel(partialReceipt([], true))).toBe("Keep · partial — hook-injected recall not shown");
  });
  it("recognizes memory_recall with dot notation (Codex/Kiro)", () => {
    const receipt = partialReceipt([
      { kind: "tool.started", toolCallId: "1", name: "firekeep.memory_recall", input: { task: "deploy" } },
    ], true);
    expect(receipt.recalledCount).toBe(1);
    expect(receipt.recalls[0]?.query).toBe("deploy");
  });
});

it("unavailable receipts never show a count", () => {
  expect(receiptLabel(unavailableReceipt("timeout", true, {}))).toBe("Keep receipt unavailable · retry");
});
