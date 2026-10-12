import { describe, expect, it } from "vitest";
import { ReceiptTracker, type KeepTimelineSource } from "../src/core/keep/receipt-tracker.js";
import type { ReplayEvent, TimelinePage } from "../src/core/keep/receipt.js";

const read = (id: string, ids: string[], agent = "agent-a"): ReplayEvent => ({ id, agent_id: agent, event_type: "memory_read", timestamp: "t", payload: { query: id, trigger: null, result_count: ids.length, memory_ids: ids } });

function source(sessions: Record<string, ReplayEvent[]>): KeepTimelineSource & { calls: Array<[string, number]> } {
  const calls: Array<[string, number]> = [];
  return {
    calls,
    async timeline(id, offset, limit): Promise<TimelinePage> {
      calls.push([id, offset]);
      const all = sessions[id] ?? [];
      const events = all.slice(offset, offset + limit);
      return { kind: "page", events, total: all.length, hasMore: offset + limit < all.length };
    },
  };
}

describe("ReceiptTracker", () => {
  it("assigns events between two sends to one turn (late writes included)", async () => {
    const events: Record<string, ReplayEvent[]> = { s1: [read("e1", ["m1"])] };
    const src = source(events);
    const tracker = new ReceiptTracker({ source: () => src, readReport: async () => ["s1"] });
    const provisional = await tracker.collect("studio-1", [], false);
    expect(provisional.recalledCount).toBe(1);
    events.s1!.push(read("late", ["m2"]));            // stop-hook learn lands after the answer
    const final = await tracker.collect("studio-1", [], true);
    expect(final.recalledCount).toBe(2);
    expect(final.cursorsAfter).toEqual({ s1: 2 });
    events.s1!.push(read("e3", ["m3"]));
    const next = await tracker.collect("studio-1", [], true);
    expect(next.recalledCount).toBe(1);                // cursor advanced
  });

  it("unions sessions and skips garbage", async () => {
    const src = source({ s1: [read("a", ["m1"])], s2: [read("b", ["m1", "m2"])] });
    const tracker = new ReceiptTracker({ source: () => src, readReport: async () => ["s1", "s2", "s1", ""] });
    const receipt = await tracker.collect("studio-1", [], true);
    expect(receipt.keepSessionIds).toEqual(["s1", "s2"]);
    expect(receipt.recalledCount).toBe(2);
  });

  it("pages until has_more is false, bounded by MAX_PAGES", async () => {
    const many = Array.from({ length: 450 }, (_, i) => read(`e${i}`, [`m${i}`]));
    const src = source({ s1: many });
    const tracker = new ReceiptTracker({ source: () => src, readReport: async () => ["s1"] });
    const receipt = await tracker.collect("studio-1", [], true);
    expect(src.calls.map(([, offset]) => offset)).toEqual([0, 200, 400]);
    expect(receipt.cursorsAfter).toEqual({ s1: 450 });
  });

  it("commit() advances cursors without a fetch (promoted provisional receipt)", async () => {
    const src = source({ s1: [read("a", ["m1"]), read("b", ["m2"])] });
    const tracker = new ReceiptTracker({ source: () => src, readReport: async () => ["s1"] });
    tracker.commit("studio-1", { s1: 1 });
    expect(tracker.cursors("studio-1")).toEqual({ s1: 1 });
    expect((await tracker.collect("studio-1", [], true)).recalledCount).toBe(1);
  });

  it("restores cursors from a previous final receipt", async () => {
    const src = source({ s1: [read("old", ["m1"]), read("new", ["m2"])] });
    const tracker = new ReceiptTracker({ source: () => src, readReport: async () => ["s1"] });
    tracker.restore("studio-1", { s1: 1 });
    expect((await tracker.collect("studio-1", [], true)).recalledCount).toBe(1);
  });

  it("an error from the Keep is unavailable, and does not move the cursor", async () => {
    const tracker = new ReceiptTracker({ source: () => ({ timeline: async () => ({ kind: "error", message: "403" }) }), readReport: async () => ["s1"] });
    tracker.restore("studio-1", { s1: 7 });
    const receipt = await tracker.collect("studio-1", [], true);
    expect(receipt.status).toBe("unavailable");
    expect(receipt.cursorsAfter).toEqual({ s1: 7 });
  });

  it("a final collect that resolves after a newer commit never moves cursors backwards", async () => {
    const events: Record<string, ReplayEvent[]> = { s1: [read("e1", ["m1"]), read("e2", ["m2"]), read("e3", ["m3"])] };
    let release: (() => void) | undefined;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const tracker = new ReceiptTracker({
      source: () => ({ timeline: async (id, offset) => {
        await gate;
        const all = events[id] ?? [];
        return { kind: "page", events: all.slice(offset), total: all.length, hasMore: false };
      } }),
      readReport: async () => ["s1"],
    });
    const late = tracker.collect("studio-1", [], true);   // a finalize the service gave up on
    events.s1!.push(read("e4", ["m4"]), read("e5", ["m5"]));
    tracker.commit("studio-1", { s1: 5 });               // a later turn committed further
    events.s1!.splice(3);                                 // the late reply only sees 3 events
    release?.();
    await late;
    expect(tracker.cursors("studio-1")).toEqual({ s1: 5 });
  });

  it("commit() merges cursors forward (never backwards)", async () => {
    const src = source({ s1: [read("a", ["m1"])] });
    const tracker = new ReceiptTracker({ source: () => src, readReport: async () => ["s1"] });
    tracker.commit("studio-1", { s1: 5 });
    expect(tracker.cursors("studio-1")).toEqual({ s1: 5 });
    tracker.commit("studio-1", {});  // empty cursors don't reset
    expect(tracker.cursors("studio-1")).toEqual({ s1: 5 });
    tracker.commit("studio-1", { s1: 3 });  // backwards move ignored
    expect(tracker.cursors("studio-1")).toEqual({ s1: 5 });
    tracker.commit("studio-1", { s1: 7 });  // forward move accepted
    expect(tracker.cursors("studio-1")).toEqual({ s1: 7 });
  });

  it("cursor validation accepts only non-negative integers", async () => {
    const src = source({ s1: [read("a", ["m1"])] });
    const tracker = new ReceiptTracker({ source: () => src, readReport: async () => ["s1"] });
    // restore with mixed valid/invalid values: 1.5 (float), -1 (negative), 4 (valid)
    tracker.restore("studio-1", { s1: 1.5, s2: -1, s3: 4 } as Record<string, number>);
    expect(tracker.cursors("studio-1")).toEqual({ s3: 4 });  // only s3 (integer) kept
  });

  it("one session unsupported in final collect → partial, cursors unchanged", async () => {
    const tracker = new ReceiptTracker({
      source: () => ({
        timeline: async (id) => {
          if (id === "s2") return { kind: "unsupported" };
          const all = { s1: [read("a", ["m1"])] }[id] ?? [];
          return { kind: "page", events: all, total: all.length, hasMore: false };
        },
      }),
      readReport: async () => ["s1", "s2"],
    });
    tracker.restore("studio-1", { s1: 10, s2: 20 });
    const receipt = await tracker.collect("studio-1", [], true);
    expect(receipt.status).toBe("partial");
    expect(tracker.cursors("studio-1")).toEqual({ s1: 10, s2: 20 });  // unchanged
  });

  it("2100 events → exactly 10 pages with MAX_PAGES boundary detail", async () => {
    const many = Array.from({ length: 2100 }, (_, i) => read(`e${i}`, [`m${i}`]));
    const src = source({ s1: many });
    const tracker = new ReceiptTracker({ source: () => src, readReport: async () => ["s1"] });
    const receipt = await tracker.collect("studio-1", [], true);
    expect(src.calls.map(([, offset]) => offset)).toEqual([0, 200, 400, 600, 800, 1000, 1200, 1400, 1600, 1800]);
    expect(receipt.cursorsAfter).toEqual({ s1: 2000 });
    expect(receipt.detail).toBe("more Keep activity than one receipt shows");
  });

  it("page with empty events and hasMore=true → terminates", async () => {
    let callCount = 0;
    const tracker = new ReceiptTracker({
      source: () => ({
        timeline: async (_id, offset) => {
          callCount++;
          if (offset === 0) return { kind: "page", events: [read("a", ["m1"])], total: 1, hasMore: true };
          return { kind: "page", events: [], total: 1, hasMore: true };  // empty page but still hasMore
        },
      }),
      readReport: async () => ["s1"],
    });
    const receipt = await tracker.collect("studio-1", [], true);
    expect(callCount).toBe(2);  // two calls: first with data, second empty
    expect(receipt.recalledCount).toBe(1);
    expect(receipt.cursorsAfter).toEqual({ s1: 1 });
  });

  it("report with tombstone #ended lines → filtered out", async () => {
    const src = source({ s1: [read("a", ["m1"])], s2: [read("b", ["m2"])] });
    const tracker = new ReceiptTracker({
      source: () => src,
      readReport: async () => ["#ended s1", "s2"],  // s1 marked as ended
    });
    const receipt = await tracker.collect("studio-1", [], true);
    expect(receipt.keepSessionIds).toEqual(["s2"]);
    expect(receipt.recalledCount).toBe(1);
  });

  describe("against today's server and kit (release-day compatibility, Review Focus #2)", () => {
    const toolEvents = [{ kind: "tool.started" as const, toolCallId: "1", name: "mcp__firekeep__memory_recall", input: { task: "vps" } }];

    it("old kit: no report file → partial from the tool stream, never 'nothing recalled'", async () => {
      const tracker = new ReceiptTracker({ source: () => source({}), readReport: async () => [] });
      const receipt = await tracker.collect("studio-1", toolEvents, true);
      expect(receipt.status).toBe("partial");
      expect(receipt.recalledCount).toBe(1);
    });

    it("old server: Markdown replay_timeline → partial", async () => {
      const tracker = new ReceiptTracker({ source: () => ({ timeline: async () => ({ kind: "unsupported" }) }), readReport: async () => ["s1"] });
      expect((await tracker.collect("studio-1", toolEvents, true)).status).toBe("partial");
    });

    it("no gateway at all → partial", async () => {
      const tracker = new ReceiptTracker({ source: () => null, readReport: async () => ["s1"] });
      expect((await tracker.collect("studio-1", [], true)).status).toBe("partial");
    });
  });
});
