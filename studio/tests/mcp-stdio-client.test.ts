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
});
