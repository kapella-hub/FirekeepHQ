import { describe, expect, it, vi } from "vitest";
import { BACKOFF_MS, KeepGateway, parsePausedSessions, splitNumberedItems, STUDIO_TOOLS, type McpLike } from "../src/main/keep/keep-gateway.js";

function fakeMcp(reply: (name: string, args: Record<string, unknown>) => string = () => "ok"): McpLike & { calls: Array<[string, Record<string, unknown>]> } {
  const calls: Array<[string, Record<string, unknown>]> = [];
  return { calls, closed: false, async callTool(name, args) { calls.push([name, { ...args }]); return { text: reply(name, { ...args }), isError: false }; }, async close() {} };
}
const whoamiOk = async () => ({ ok: true, missing: false, output: JSON.stringify({ agent_id: "agent-a", kit_version: "1.8.0", personal_mode: false, server_configured: true }) });

describe("KeepGateway", () => {
  it("reports missing when the kit is not installed", async () => {
    const gateway = new KeepGateway({ command: "firekeep", whoami: async () => ({ ok: false, missing: true, output: "" }), connect: vi.fn() });
    expect(await gateway.start()).toEqual({ state: "missing" });
  });

  it("spawns the studio toolset under the panel label and tags every call", async () => {
    const mcp = fakeMcp();
    const connect = vi.fn(async () => mcp);
    const gateway = new KeepGateway({ command: "firekeep", whoami: whoamiOk, connect });
    expect(await gateway.start()).toMatchObject({ state: "connected", agentId: "agent-a" });
    expect(connect).toHaveBeenCalledWith({ FIREKEEP_TOOLSET: "studio", FIREKEEP_AGENT_ID: "agent-a-studio-panel" });
    await gateway.search("vps", "memory");
    expect(mcp.calls[0]).toEqual(["memory_recall", { task: "vps", format: "raw", top_k: 8, agent_id: "agent-a-studio-panel" }]);
  });

  it("falls back to FIREKEEP_TOOLS_ALLOW on a kit without the studio preset", async () => {
    const mcp = fakeMcp();
    const connect = vi.fn()
      .mockRejectedValueOnce(new Error("firekeep gateway exited (1): unknown FIREKEEP_TOOLSET 'studio'"))
      .mockResolvedValueOnce(mcp);
    const gateway = new KeepGateway({ command: "firekeep", whoami: whoamiOk, connect });
    await gateway.start();
    expect(connect).toHaveBeenLastCalledWith({ FIREKEEP_TOOLS_ALLOW: STUDIO_TOOLS.join(","), FIREKEEP_AGENT_ID: "agent-a-studio-panel" });
  });

  it("backs off 1s, 5s, 30s and then waits for a manual retry", async () => {
    const sleeps: number[] = [];
    const connect = vi.fn().mockRejectedValue(new Error("firekeep gateway exited (1): cannot reach cortex"));
    const gateway = new KeepGateway({ command: "firekeep", whoami: whoamiOk, connect, sleep: async (ms) => { sleeps.push(ms); } });
    const status = await gateway.start();
    expect(sleeps).toEqual([...BACKOFF_MS]);
    expect(status).toMatchObject({ state: "unreachable", retryInMs: null });
    expect(status.state === "unreachable" && status.detail).toContain("cannot reach cortex");
  });

  it("asks replay_timeline for JSON and parses it", async () => {
    const mcp = fakeMcp(() => JSON.stringify({ events: [], total: 0, has_more: false }));
    const gateway = new KeepGateway({ command: "firekeep", whoami: whoamiOk, connect: async () => mcp });
    await gateway.start();
    expect(await gateway.timeline("s1", 4, 200)).toEqual({ kind: "page", events: [], total: 0, hasMore: false });
    expect(mcp.calls[0]).toEqual(["replay_timeline", { session_id: "s1", offset: 4, limit: 200, format: "json" }]);
  });

  it("timeline() returns an error page instead of throwing when not connected", async () => {
    const gateway = new KeepGateway({ command: "firekeep", whoami: async () => ({ ok: false, missing: true, output: "" }), connect: vi.fn() });
    await gateway.start();
    expect(await gateway.timeline("s1", 0, 200)).toEqual({ kind: "error", message: "Keep not connected" });
  });

  it("only ever calls preset tools", async () => {
    const mcp = fakeMcp(() => "");
    const gateway = new KeepGateway({ command: "firekeep", whoami: whoamiOk, connect: async () => mcp });
    await gateway.start();
    await gateway.search("x", "skill"); await gateway.skills(); await gateway.team(); await gateway.highlights();
    await gateway.feedback(["m1"], true); await gateway.timeline("s", 0, 1);
    for (const [name] of mcp.calls) expect(STUDIO_TOOLS).toContain(name);
  });

  it("labels only memory_recall and memory_feedback (ctx_list_sessions treats agent_id as a filter)", async () => {
    const mcp = fakeMcp(() => "");
    const gateway = new KeepGateway({ command: "firekeep", whoami: whoamiOk, connect: async () => mcp });
    await gateway.start();
    await gateway.highlights(); await gateway.skills(); await gateway.feedback(["m1"], false);
    for (const [name, args] of mcp.calls) expect("agent_id" in args).toBe(name === "memory_recall" || name === "memory_feedback");
  });

  it("does not restart after stop()", async () => {
    const connect = vi.fn(async () => fakeMcp());
    const gateway = new KeepGateway({ command: "firekeep", whoami: whoamiOk, connect });
    await gateway.start();
    await gateway.stop();
    gateway.handleExit();
    await Promise.resolve();
    expect(connect).toHaveBeenCalledTimes(1);
  });
});

it("splitNumberedItems splits a raw recall list, else returns one block", () => {
  expect(splitNumberedItems("1. Alpha\nmore\n2. Beta")).toEqual([{ title: "Alpha", body: "Alpha\nmore" }, { title: "Beta", body: "Beta" }]);
  expect(splitNumberedItems("just prose")).toEqual([{ title: "just prose", body: "just prose" }]);
  expect(splitNumberedItems("")).toEqual([]);
});

it("parsePausedSessions reads the bridge's real {sessions:[...]} reply", () => {
  const real = JSON.stringify({ sessions: [
    { session_id: "a1b2c3d4", goal: "ship studio", status: "paused", created_at: "2026-10-10T10:00:00Z", updated_at: "2026-10-10T11:00:00Z", agent_id: "agent-a", briefing_id: "" },
    { session_id: "e5f6a7b8", goal: "", status: "paused", created_at: "", updated_at: "", agent_id: "agent-a", briefing_id: "" },
  ] });
  expect(parsePausedSessions(real)).toEqual([{ sessionId: "a1b2c3d4", goal: "ship studio" }, { sessionId: "e5f6a7b8", goal: "" }]);
  expect(parsePausedSessions(JSON.stringify({ sessions: [], error: "x" }))).toEqual([]);
  expect(parsePausedSessions("not json")).toEqual([]);
});

it("highlights surfaces paused sessions from the real reply", async () => {
  const mcp = fakeMcp((name) => name === "ctx_list_sessions" ? JSON.stringify({ sessions: [{ session_id: "a1b2c3d4", goal: "g" }] }) : "x");
  const gateway = new KeepGateway({ command: "firekeep", whoami: whoamiOk, connect: async () => mcp });
  await gateway.start();
  expect((await gateway.highlights()).paused).toEqual([{ sessionId: "a1b2c3d4", goal: "g" }]);
});

it("an old kit without whoami gets the update message", async () => {
  const gateway = new KeepGateway({ command: "firekeep", whoami: async () => ({ ok: false, missing: false, output: "error: unknown command whoami" }), connect: vi.fn() });
  const status = await gateway.start();
  expect(status.state === "unconfigured" && status.detail).toContain("firekeep update");
});
