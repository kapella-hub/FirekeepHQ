import { createInterface } from "node:readline";

const mode = process.env.FAKE_MCP_MODE ?? "ok";
if (mode === "unknown-preset") {
  process.stderr.write("firekeep gateway: unknown FIREKEEP_TOOLSET 'studio' (valid: chat); refusing to start\n");
  process.exit(1);
}
import { writeFileSync } from "node:fs";
if (process.env.FAKE_MCP_PIDFILE) writeFileSync(process.env.FAKE_MCP_PIDFILE, String(process.pid));
const held = [];
const send = (message) => process.stdout.write(`${JSON.stringify(message)}\n`);
createInterface({ input: process.stdin }).on("line", (line) => {
  const message = JSON.parse(line);
  if (message.method === "initialize") {
    if (mode === "hang-init") return;
    send({ jsonrpc: "2.0", id: message.id, result: { protocolVersion: "2025-06-18", capabilities: {}, serverInfo: { name: "fake", version: "0" } } });
  }
  else if (message.method === "tools/call") {
    if (mode === "hang") return;
    if (mode === "exit-on-call") process.exit(3);
    // The kit gateway answers initialize locally; a dead or unauthorised server surfaces only on tools/call.
    if (mode === "tool-error") { send({ jsonrpc: "2.0", id: message.id, error: { code: -32000, message: "firekeep unavailable: cannot reach cortex (401 Unauthorized)" } }); return; }
    const { name, arguments: args } = message.params;
    const reply = (id) => ({ jsonrpc: "2.0", id, result: { content: [{ type: "text", text: JSON.stringify({ name, args, env: { toolset: process.env.FIREKEEP_TOOLSET ?? null, allow: process.env.FIREKEEP_TOOLS_ALLOW ?? null, agent: process.env.FIREKEEP_AGENT_ID ?? null } }) }], isError: false } });
    if (mode === "ping-first") send({ jsonrpc: "2.0", id: message.id, method: "ping" });
    if (mode === "split") {
      const text = `${JSON.stringify(reply(message.id))}
`;
      process.stdout.write(text.slice(0, 20));
      setTimeout(() => process.stdout.write(text.slice(20)), 50);
      return;
    }
    if (mode === "double") {
      held.push(reply(message.id));
      if (held.length === 2) { process.stdout.write(held.map((m) => `${JSON.stringify(m)}
`).join("")); held.length = 0; }
      return;
    }
    send(reply(message.id));
  }
});
