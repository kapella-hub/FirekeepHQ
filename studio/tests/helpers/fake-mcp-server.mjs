import { createInterface } from "node:readline";

const mode = process.env.FAKE_MCP_MODE ?? "ok";
if (mode === "unknown-preset") {
  process.stderr.write("firekeep gateway: unknown FIREKEEP_TOOLSET 'studio' (valid: chat); refusing to start\n");
  process.exit(1);
}
const send = (message) => process.stdout.write(`${JSON.stringify(message)}\n`);
createInterface({ input: process.stdin }).on("line", (line) => {
  const message = JSON.parse(line);
  if (message.method === "initialize") send({ jsonrpc: "2.0", id: message.id, result: { protocolVersion: "2025-06-18", capabilities: {}, serverInfo: { name: "fake", version: "0" } } });
  else if (message.method === "tools/call") {
    if (mode === "hang") return;
    if (mode === "exit-on-call") process.exit(3);
    const { name, arguments: args } = message.params;
    send({ jsonrpc: "2.0", id: message.id, result: { content: [{ type: "text", text: JSON.stringify({ name, args, env: { toolset: process.env.FIREKEEP_TOOLSET ?? null, allow: process.env.FIREKEEP_TOOLS_ALLOW ?? null, agent: process.env.FIREKEEP_AGENT_ID ?? null } }) }], isError: false } });
  }
});
