import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it } from "vitest";
import { McpStdioClient } from "../src/main/keep/mcp-stdio-client.js";

const server = fileURLToPath(new URL("./helpers/fake-mcp-server.mjs", import.meta.url));
const opened: McpStdioClient[] = [];
const connect = async (mode: string, extra: Partial<Parameters<typeof McpStdioClient.connect>[0]> = {}) => {
  const client = await McpStdioClient.connect({ command: process.execPath, args: [server], env: { FAKE_MCP_MODE: mode, FIREKEEP_TOOLSET: "studio" }, timeoutMs: 1500, ...extra });
  opened.push(client);
  return client;
};
afterEach(async () => { await Promise.all(opened.splice(0).map((client) => client.close())); });

describe("McpStdioClient", () => {
  it("initializes and calls a tool with JSON-RPC 2.0", async () => {
    const client = await connect("ok");
    const result = await client.callTool("relay_who_is_online", { include_idle: true });
    expect(JSON.parse(result.text)).toMatchObject({ name: "relay_who_is_online", args: { include_idle: true }, env: { toolset: "studio" } });
    await client.close();
    expect(client.closed).toBe(true);
  });

  it("times out a hung call without hanging the caller", async () => {
    const client = await connect("hang");
    await expect(client.callTool("memory_recall", { task: "x" })).rejects.toThrow(/timed out/);
    await client.close();
  });

  it("rejects pending calls and reports exit when the server dies", async () => {
    let exited: number | null | undefined;
    const client = await connect("exit-on-call", { onExit: (code) => { exited = code; } });
    await expect(client.callTool("memory_recall", { task: "x" })).rejects.toThrow(/exited/);
    expect(exited).toBe(3);
  });

  it("surfaces a startup refusal with its stderr and does not call onExit", async () => {
    let exits = 0;
    await expect(connect("unknown-preset", { onExit: () => { exits += 1; } })).rejects.toThrow(/unknown FIREKEEP_TOOLSET/);
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(exits).toBe(0);
  });

  it("rejects connect and kills the child when initialize hangs", async () => {
    const dir = mkdtempSync(join(tmpdir(), "fake-mcp-"));
    const pidFile = join(dir, "pid");
    try {
      await expect(connect("hang-init", { timeoutMs: 500, env: { FAKE_MCP_MODE: "hang-init", FAKE_MCP_PIDFILE: pidFile } })).rejects.toThrow(/timed out/);
      const pid = Number(readFileSync(pidFile, "utf8"));
      expect(pid).toBeGreaterThan(0);
      const alive = (): boolean => { try { process.kill(pid, 0); return true; } catch { return false; } };
      for (let i = 0; i < 30 && alive(); i += 1) await new Promise((resolve) => setTimeout(resolve, 100));
      expect(alive()).toBe(false);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("rejects an in-flight call on close() without calling onExit", async () => {
    let exits = 0;
    const client = await connect("hang", { timeoutMs: 5000, onExit: () => { exits += 1; } });
    const call = client.callTool("memory_recall", { task: "x" });
    const settled = expect(call).rejects.toThrow(/exited/);
    await client.close();
    await settled;
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(exits).toBe(0);
    expect(client.closed).toBe(true);
  });

  it("reassembles a response split across two writes", async () => {
    const client = await connect("split");
    const result = await client.callTool("a", {});
    expect(JSON.parse(result.text).name).toBe("a");
  });

  it("handles two responses in one write", async () => {
    const client = await connect("double");
    const [a, b] = await Promise.all([client.callTool("a", {}), client.callTool("b", {})]);
    expect([JSON.parse(a.text).name, JSON.parse(b.text).name]).toEqual(["a", "b"]);
  });

  it("ignores a server request that reuses our pending id", async () => {
    const client = await connect("ping-first");
    const result = await client.callTool("real", {});
    expect(JSON.parse(result.text).name).toBe("real");
  });
});
