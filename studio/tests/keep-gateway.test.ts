import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";
import type { KeepStatus } from "../src/shared/keep.js";
import { BACKOFF_MS, KeepGateway, parsePausedSessions, splitNumberedItems, STUDIO_TOOLS, type McpLike } from "../src/main/keep/keep-gateway.js";
import { McpStdioClient } from "../src/main/keep/mcp-stdio-client.js";

const fakeServer = fileURLToPath(new URL("./helpers/fake-mcp-server.mjs", import.meta.url));

function fakeMcp(reply: (name: string, args: Record<string, unknown>) => string = () => "ok"): McpLike & { calls: Array<[string, Record<string, unknown>]> } {
  const calls: Array<[string, Record<string, unknown>]> = [];
  return { calls, closed: false, async callTool(name, args) { calls.push([name, { ...args }]); return { text: reply(name, { ...args }), isError: false }; }, async close() {} };
}
const whoamiOk = async () => ({ ok: true, missing: false, output: JSON.stringify({ agent_id: "agent-a", kit_version: "1.8.0", personal_mode: false, server_configured: true }) });

describe("KeepGateway", () => {
  it("refreshIdentity re-reads whoami and emits the changed status without restarting the child", async () => {
    const mcp = fakeMcp();
    const connect = vi.fn(async () => mcp);
    let personal = false;
    const whoami = async () => ({ ok: true, missing: false, output: JSON.stringify({ agent_id: "agent-a", kit_version: personal ? "1.8.1" : "1.8.0", personal_mode: personal }) });
    const statuses: KeepStatus[] = [];
    const gateway = new KeepGateway({ whoami, connect, onStatus: (status) => statuses.push(status) });
    await gateway.start();
    personal = true;
    const next = await gateway.refreshIdentity();
    expect(next).toEqual({ state: "connected", agentId: "agent-a", personalMode: true, kitVersion: "1.8.1" });
    expect(statuses.at(-1)).toEqual(next);
    expect(gateway.status()).toEqual(next);
    expect(connect).toHaveBeenCalledTimes(1);
  });

  it("refreshIdentity leaves a non-connected status alone and survives a failing probe", async () => {
    const gateway = new KeepGateway({ whoami: async () => ({ ok: false, missing: true, output: "" }), connect: vi.fn() });
    await gateway.start();
    expect(await gateway.refreshIdentity()).toEqual({ state: "missing" });
    const connected = new KeepGateway({ whoami: whoamiOk, connect: async () => fakeMcp() });
    await connected.start();
    const before = connected.status();
    const flaky = new KeepGateway({ whoami: async () => { throw new Error("boom"); }, connect: async () => fakeMcp() });
    expect(await flaky.refreshIdentity()).toEqual({ state: "starting" });
    expect(connected.status()).toBe(before);
  });

  it("reports missing when the kit is not installed", async () => {
    const gateway = new KeepGateway({ whoami: async () => ({ ok: false, missing: true, output: "" }), connect: vi.fn() });
    expect(await gateway.start()).toEqual({ state: "missing" });
  });

  it("spawns the studio toolset under the panel label and tags every call", async () => {
    const mcp = fakeMcp();
    const connect = vi.fn(async () => mcp);
    const gateway = new KeepGateway({ whoami: whoamiOk, connect });
    expect(await gateway.start()).toMatchObject({ state: "connected", agentId: "agent-a" });
    expect(connect).toHaveBeenCalledWith({ FIREKEEP_TOOLSET: "studio", FIREKEEP_AGENT_ID: "agent-a-studio-panel" });
    await gateway.search("vps", "memory");
    expect(mcp.calls.at(-1)).toEqual(["memory_recall", { task: "vps", format: "raw", top_k: 8, agent_id: "agent-a-studio-panel" }]);
  });

  it("falls back to FIREKEEP_TOOLS_ALLOW on a kit without the studio preset", async () => {
    const mcp = fakeMcp();
    const connect = vi.fn()
      .mockRejectedValueOnce(new Error("firekeep gateway exited (1): unknown FIREKEEP_TOOLSET 'studio'"))
      .mockResolvedValueOnce(mcp);
    const gateway = new KeepGateway({ whoami: whoamiOk, connect });
    await gateway.start();
    expect(connect).toHaveBeenLastCalledWith({ FIREKEEP_TOOLS_ALLOW: STUDIO_TOOLS.join(","), FIREKEEP_AGENT_ID: "agent-a-studio-panel" });
  });

  it("backs off 1s, 5s, 30s and then waits for a manual retry", async () => {
    const sleeps: number[] = [];
    const connect = vi.fn().mockRejectedValue(new Error("firekeep gateway exited (1): cannot reach cortex"));
    const gateway = new KeepGateway({ whoami: whoamiOk, connect, sleep: async (ms) => { sleeps.push(ms); } });
    const status = await gateway.start();
    expect(sleeps).toEqual([...BACKOFF_MS]);
    expect(status).toMatchObject({ state: "unreachable", retryInMs: null });
    expect(status.state === "unreachable" && status.detail).toContain("cannot reach cortex");
  });

  it("asks replay_timeline for JSON and parses it", async () => {
    const mcp = fakeMcp(() => JSON.stringify({ events: [], total: 0, has_more: false }));
    const gateway = new KeepGateway({ whoami: whoamiOk, connect: async () => mcp });
    await gateway.start();
    expect(await gateway.timeline("s1", 4, 200)).toEqual({ kind: "page", events: [], total: 0, hasMore: false });
    expect(mcp.calls.at(-1)).toEqual(["replay_timeline", { session_id: "s1", offset: 4, limit: 200, format: "json" }]);
  });

  it("timeline() maps a pre-v1.6.2 argument validation error to unsupported, other errors stay errors", async () => {
    // Only the timeline call fails; the connect probe (relay_who_is_online) must answer.
    const failing = (text: string): McpLike => ({ closed: false, async callTool(name) { return name === "replay_timeline" ? { text, isError: true } : { text: "ok", isError: false }; }, async close() {} });
    for (const [text, expected] of [
      ["2 validation errors for call[replay_timeline]\noffset\n  Unexpected keyword argument", { kind: "unsupported" }],
      ["Keep timeout", { kind: "error", message: "Keep timeout" }],
    ] as const) {
      const gateway = new KeepGateway({ whoami: whoamiOk, connect: async () => failing(text) });
      await gateway.start();
      expect(await gateway.timeline("s1", 0, 200)).toEqual(expected);
    }
  });

  it("timeline() returns an error page instead of throwing when not connected", async () => {
    const gateway = new KeepGateway({ whoami: async () => ({ ok: false, missing: true, output: "" }), connect: vi.fn() });
    await gateway.start();
    expect(await gateway.timeline("s1", 0, 200)).toEqual({ kind: "error", message: "Keep not connected" });
  });

  it("only ever calls preset tools", async () => {
    const mcp = fakeMcp(() => "");
    const gateway = new KeepGateway({ whoami: whoamiOk, connect: async () => mcp });
    await gateway.start();
    await gateway.search("x", "skill"); await gateway.skills(); await gateway.team(); await gateway.highlights();
    await gateway.feedback(["m1"], true); await gateway.timeline("s", 0, 1);
    for (const [name] of mcp.calls) expect(STUDIO_TOOLS).toContain(name);
  });

  it("labels only memory_recall and memory_feedback (ctx_list_sessions treats agent_id as a filter)", async () => {
    const mcp = fakeMcp(() => "");
    const gateway = new KeepGateway({ whoami: whoamiOk, connect: async () => mcp });
    await gateway.start();
    await gateway.highlights(); await gateway.skills(); await gateway.feedback(["m1"], false);
    for (const [name, args] of mcp.calls) expect("agent_id" in args).toBe(name === "memory_recall" || name === "memory_feedback");
  });

  it("stop() during a backoff sleep prevents any further connect", async () => {
    let release!: () => void;
    const sleeping = new Promise<void>((r) => { release = r; });
    let sleepStarted = false;
    const connect = vi.fn().mockRejectedValue(new Error("cannot reach cortex"));
    const gateway = new KeepGateway({ whoami: whoamiOk, connect, sleep: () => { sleepStarted = true; return sleeping; } });
    const started = gateway.start();
    await vi.waitFor(() => expect(sleepStarted).toBe(true));
    expect(connect).toHaveBeenCalledTimes(1);
    await gateway.stop();
    release();
    await started;
    expect(connect).toHaveBeenCalledTimes(1);
    expect(gateway.status().state).not.toBe("connected");
  });

  it("a client that is already closed on arrival goes through backoff instead of reporting connected", async () => {
    const dead = { ...fakeMcp(), closed: true };
    const alive = fakeMcp();
    const sleeps: number[] = [];
    const connect = vi.fn().mockResolvedValueOnce(dead).mockResolvedValueOnce(alive);
    const gateway = new KeepGateway({ whoami: whoamiOk, connect, sleep: async (ms) => { sleeps.push(ms); } });
    expect(await gateway.start()).toMatchObject({ state: "connected" });
    expect(sleeps).toEqual([1000]);
    expect(connect).toHaveBeenCalledTimes(2);
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
  const gateway = new KeepGateway({ whoami: whoamiOk, connect: async () => mcp });
  await gateway.start();
  expect((await gateway.highlights()).paused).toEqual([{ sessionId: "a1b2c3d4", goal: "g" }]);
});

it("an old kit without whoami gets the update message", async () => {
  const gateway = new KeepGateway({ whoami: async () => ({ ok: false, missing: false, output: "error: argument action: invalid choice: 'whoami'" }), connect: vi.fn() });
  const status = await gateway.start();
  expect(status.state === "unconfigured" && status.detail).toContain("firekeep update");
});

describe("KeepGateway lifecycle", () => {
  const deferred = <T,>() => { let resolve!: (v: T) => void; const promise = new Promise<T>((r) => { resolve = r; }); return { promise, resolve }; };

  it("start/retry/handleExit share one in-flight promise", async () => {
    const gate = deferred<McpLike>();
    const connect = vi.fn(() => gate.promise);
    const gateway = new KeepGateway({ whoami: whoamiOk, connect });
    const a = gateway.start();
    const b = gateway.retry();
    expect(b).toBe(a);
    await vi.waitFor(() => expect(connect).toHaveBeenCalledTimes(1));
    gate.resolve(fakeMcp());
    await a;
    expect(connect).toHaveBeenCalledTimes(1);
  });

  it("stop() during a pending connect closes the new client and never reports connected", async () => {
    const gate = deferred<McpLike>();
    const mcp = fakeMcp();
    const close = vi.spyOn(mcp, "close");
    const gateway = new KeepGateway({ whoami: whoamiOk, connect: () => gate.promise });
    const started = gateway.start();
    await vi.waitFor(() => expect(gateway.status().state).toBe("starting"));
    await new Promise((r) => setTimeout(r, 0));
    await gateway.stop();
    gate.resolve(mcp);
    await started;
    expect(close).toHaveBeenCalled();
    expect(gateway.status().state).not.toBe("connected");
  });

  it("ignores an exit from a superseded client", async () => {
    const old = fakeMcp(); const next = fakeMcp();
    const connect = vi.fn().mockResolvedValueOnce(old).mockResolvedValueOnce(next);
    const gateway = new KeepGateway({ whoami: whoamiOk, connect, sleep: async () => {} });
    await gateway.start();
    await gateway.retry();
    expect(connect).toHaveBeenCalledTimes(2);
    const before = gateway.status();
    gateway.handleExit(old);
    for (let i = 0; i < 20; i += 1) await Promise.resolve();
    expect(gateway.status()).toBe(before);
    expect(connect).toHaveBeenCalledTimes(2);
  });

  it("a gateway that dies after every handshake backs off 1s, 5s, 30s then waits for manual retry", async () => {
    const sleeps: number[] = [];
    const clients: McpLike[] = [];
    const connect = vi.fn(async () => { const m = fakeMcp(); clients.push(m); return m; });
    let t = 0;
    const gateway = new KeepGateway({ whoami: whoamiOk, connect, now: () => t, sleep: async (ms) => { sleeps.push(ms); t += ms; } });
    await gateway.start();
    for (let i = 0; i < 4; i += 1) {
      gateway.handleExit(clients[clients.length - 1]!);
      await vi.waitFor(() => expect(gateway.status().state === "connected" || (gateway.status() as { retryInMs?: number | null }).retryInMs === null).toBe(true));
    }
    expect(sleeps).toEqual([1000, 5000, 30_000]);
    expect(gateway.status()).toMatchObject({ state: "unreachable", retryInMs: null, detail: "Keep gateway exited" });
    expect(connect).toHaveBeenCalledTimes(4);
  });

  it("resets the failure counter after 60s of stable uptime", async () => {
    const sleeps: number[] = [];
    const clients: McpLike[] = [];
    let t = 0;
    const gateway = new KeepGateway({ whoami: whoamiOk, now: () => t, sleep: async (ms) => { sleeps.push(ms); }, connect: async () => { const m = fakeMcp(); clients.push(m); return m; } });
    await gateway.start();
    for (let i = 0; i < 3; i += 1) {
      t += 61_000;
      gateway.handleExit(clients[clients.length - 1]!);
      await vi.waitFor(() => expect(clients.length).toBe(i + 2));
      await vi.waitFor(() => expect(gateway.status().state).toBe("connected"));
    }
    expect(sleeps).toEqual([1000, 1000, 1000]);
  });
});

describe("KeepGateway whoami outcomes", () => {
  it("exit 3 with no stdout asks for doctor, not a kit update", async () => {
    const gateway = new KeepGateway({ whoami: async () => ({ ok: false, missing: false, output: "" }), connect: vi.fn() });
    const status = await gateway.start();
    expect(status).toMatchObject({ state: "unconfigured" });
    expect(status.state === "unconfigured" && status.detail).toBe("The Firekeep kit config needs attention. Run firekeep doctor.");
  });

  it("agent_id null (CHANGEME) is unconfigured with the identity message", async () => {
    const output = JSON.stringify({ agent_id: null, kit_version: "1.8.0", personal_mode: false, server_configured: true });
    const gateway = new KeepGateway({ whoami: async () => ({ ok: false, missing: false, output }), connect: vi.fn() });
    const status = await gateway.start();
    expect(status.state === "unconfigured" && status.detail).toContain("no Firekeep agent identity");
  });

  it("picks the last JSON line carrying agent_id, ignoring stray JSON", async () => {
    const mcp = fakeMcp();
    const output = `{"warn": 1}
${JSON.stringify({ agent_id: "agent-b", kit_version: "1.8.0", personal_mode: true, server_configured: true })}
noise`;
    const connect = vi.fn(async () => mcp);
    const gateway = new KeepGateway({ whoami: async () => ({ ok: true, missing: false, output }), connect });
    expect(await gateway.start()).toMatchObject({ state: "connected", agentId: "agent-b", personalMode: true });
  });
});

describe("KeepGateway connection truthfulness", () => {
  /** A client whose tool calls fail while `failing` is true: a reachable kit whose server is down. */
  function switchable(): McpLike & { failing: boolean; calls: string[] } {
    const mcp = {
      failing: false,
      calls: [] as string[],
      closed: false,
      async callTool(name: string) {
        mcp.calls.push(name);
        if (mcp.failing) throw new Error("firekeep unavailable: cannot reach cortex\ntraceback...");
        return { text: "ok", isError: false };
      },
      async close() { mcp.closed = true; },
    };
    return mcp;
  }

  it("probes the server after initialize and reports connected only when the probe answers", async () => {
    const mcp = switchable();
    const gateway = new KeepGateway({ whoami: whoamiOk, connect: async () => mcp });
    expect(await gateway.start()).toMatchObject({ state: "connected" });
    expect(mcp.calls).toEqual(["relay_who_is_online"]);
  });

  it("a kit that answers initialize but whose tool calls fail reads unreachable, never connected (stdio fake)", async () => {
    const statuses: KeepStatus[] = [];
    const sleeps: number[] = [];
    const gateway: KeepGateway = new KeepGateway({
      whoami: whoamiOk,
      connect: async (env) => {
        const holder: { client: McpStdioClient | null } = { client: null };
        holder.client = await McpStdioClient.connect({ command: process.execPath, args: [fakeServer], env: { ...env, FAKE_MCP_MODE: "tool-error" }, timeoutMs: 2000, onExit: () => { if (holder.client) gateway.handleExit(holder.client); } });
        return holder.client;
      },
      sleep: async (ms) => { sleeps.push(ms); },
      onStatus: (status) => statuses.push(status),
    });
    const status = await gateway.start();
    expect(statuses.some((s) => s.state === "connected")).toBe(false);
    expect(statuses[0]).toMatchObject({ state: "unreachable", retryInMs: 1000 });
    expect(sleeps).toEqual([...BACKOFF_MS]);
    expect(status).toMatchObject({ state: "unreachable", retryInMs: null });
    expect(status.state === "unreachable" && status.detail).toBe("firekeep unavailable: cannot reach cortex (401 Unauthorized)");
    await gateway.stop();
  });

  it("a probe answered with isError also keeps the status off connected", async () => {
    const failing: McpLike = { closed: false, async callTool() { return { text: "401 Unauthorized: invalid API key\nmore", isError: true }; }, async close() {} };
    const gateway = new KeepGateway({ whoami: whoamiOk, connect: async () => failing, sleep: async () => {} });
    const status = await gateway.start();
    expect(status).toMatchObject({ state: "unreachable", retryInMs: null, detail: "401 Unauthorized: invalid API key" });
  });

  it("three consecutive failed tool calls after connect demote the status to unreachable", async () => {
    const mcp = switchable();
    let release!: () => void;
    const held = new Promise<void>((r) => { release = r; });
    const gateway = new KeepGateway({ whoami: whoamiOk, connect: async () => mcp, sleep: () => held });
    await gateway.start();
    mcp.failing = true;
    await expect(gateway.team()).rejects.toThrow(/cannot reach cortex/);
    await expect(gateway.skills()).rejects.toThrow();
    expect(gateway.status().state).toBe("connected");
    expect(await gateway.timeline("s1", 0, 10)).toMatchObject({ kind: "error" });
    await vi.waitFor(() => expect(gateway.status()).toMatchObject({ state: "unreachable", detail: "firekeep unavailable: cannot reach cortex", retryInMs: 1000 }));
    await gateway.stop();
    release();
  });

  it("a successful call between failures resets the counter", async () => {
    const mcp = switchable();
    const gateway = new KeepGateway({ whoami: whoamiOk, connect: async () => mcp, sleep: async () => {} });
    await gateway.start();
    mcp.failing = true;
    await gateway.team().catch(() => undefined);
    await gateway.team().catch(() => undefined);
    mcp.failing = false;
    await gateway.team();
    mcp.failing = true;
    await gateway.team().catch(() => undefined);
    await gateway.team().catch(() => undefined);
    expect(gateway.status().state).toBe("connected");
  });

  it("tool errors the server answered with never demote the status", async () => {
    const refusing: McpLike = { closed: false, async callTool(name) { return name === "memory_feedback" ? { text: "feedback refused: not your memory", isError: true } : { text: "ok", isError: false }; }, async close() {} };
    const gateway = new KeepGateway({ whoami: whoamiOk, connect: async () => refusing, sleep: async () => {} });
    await gateway.start();
    for (let i = 0; i < 5; i += 1) await expect(gateway.feedback(["m1"], true)).rejects.toThrow(/feedback refused/);
    expect(gateway.status().state).toBe("connected");
  });

  it("a pre-v1.6.2 timeline validation error counts as an answer, not a failure", async () => {
    const old: McpLike = { closed: false, async callTool(name) { return name === "replay_timeline" ? { text: "2 validation errors for call[replay_timeline]", isError: true } : { text: "ok", isError: false }; }, async close() {} };
    const gateway = new KeepGateway({ whoami: whoamiOk, connect: async () => old });
    await gateway.start();
    for (let i = 0; i < 4; i += 1) expect(await gateway.timeline("s1", 0, 10)).toEqual({ kind: "unsupported" });
    expect(gateway.status().state).toBe("connected");
  });
});

describe("KeepGateway whoami failure detail", () => {
  it("a non-zero whoami shows the kit's own first error line", async () => {
    const gateway = new KeepGateway({ whoami: async () => ({ ok: false, missing: false, output: "\n  No pyvenv.cfg file\nTraceback..." }), connect: vi.fn() });
    expect(await gateway.start()).toEqual({ state: "unconfigured", detail: "No pyvenv.cfg file" });
  });
});

describe("KeepGateway restart", () => {
  it("restart() re-reads whoami and respawns the gateway under the new agent label", async () => {
    let agent = "agent-a";
    const closes = vi.fn();
    const connect = vi.fn(async () => ({ ...fakeMcp(), close: async () => { closes(); } }));
    const gateway = new KeepGateway({ whoami: async () => ({ ok: true, missing: false, output: JSON.stringify({ agent_id: agent, kit_version: "1.8.0" }) }), connect });
    await gateway.start();
    agent = "agent-b";
    expect(await gateway.restart()).toMatchObject({ state: "connected", agentId: "agent-b" });
    expect(connect).toHaveBeenLastCalledWith({ FIREKEEP_TOOLSET: "studio", FIREKEEP_AGENT_ID: "agent-b-studio-panel" });
    expect(closes).toHaveBeenCalledTimes(1);
  });

  it("restart() after stop() (app quit) never respawns", async () => {
    const connect = vi.fn(async () => fakeMcp());
    const gateway = new KeepGateway({ whoami: whoamiOk, connect });
    await gateway.start();
    await gateway.stop();
    await gateway.restart();
    expect(connect).toHaveBeenCalledTimes(1);
  });
});
