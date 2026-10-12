import { describe, expect, it } from "vitest";
import { ClaudeRuntime } from "../src/main/runtime/claude-runtime.js";
import { CodexRuntime } from "../src/main/runtime/codex-runtime.js";
import { KiroRuntime } from "../src/main/runtime/kiro-runtime.js";
import { FirekeepClient, defaultFirekeepCommand, isMissingExecutable } from "../src/main/firekeep-client.js";
import { KeepGateway } from "../src/main/keep/keep-gateway.js";
import { McpStdioClient } from "../src/main/keep/mcp-stdio-client.js";
import type { AgentRuntime, RuntimeEventPayload } from "../src/core/runtime.js";

const live = process.env.STUDIO_LIVE_RUNTIMES === "1";
const liveKeepTurn = process.env.STUDIO_LIVE_KEEP_TURN === "1";

const runtimeFactories: Record<string, () => AgentRuntime> = {
  codex: () => new CodexRuntime(),
  claude: () => new ClaudeRuntime(),
  kiro: () => new KiroRuntime(),
};

describe.skipIf(!live)("installed runtime smoke", () => {
  it.each([
    ["Codex", () => new CodexRuntime()],
    ["Claude", () => new ClaudeRuntime()],
    ["Kiro", () => new KiroRuntime()],
  ] as const)("discovers %s auth, models, and reasoning controls without an inference turn", async (_name, create) => {
    const runtime = create();
    const connection = await runtime.probe();
    expect(connection, connection.detail).toMatchObject({ state: "ready" });
    const auth = await runtime.authStatus();
    expect(auth, auth.detail).toMatchObject({ state: "connected" });
    const models = await runtime.listModels();
    expect(models.length, `${_name} returned no live models`).toBeGreaterThan(0);
    expect(models.some((model) => model.efforts?.length), `${_name} returned no live reasoning controls`).toBe(true);
  }, 45_000);

  it("executes the installed Firekeep Client Kit status command", async () => {
    const result = await new FirekeepClient().execute("status", []);
    expect(result, result.output).toMatchObject({ ok: true, exitCode: 0 });
  }, 30_000);
});

describe.skipIf(!live)("Keep gateway smoke", () => {
  // STUDIO_LIVE_FIREKEEP overrides the kit executable (for a kit not yet installed); wiring mirrors src/main/index.ts.
  it("connects to the Keep through the studio toolset", async () => {
    const command = process.env.STUDIO_LIVE_FIREKEEP || defaultFirekeepCommand();
    const client = new FirekeepClient({ command });
    let stderrTail = "";
    const gateway: KeepGateway = new KeepGateway({
      whoami: async () => {
        try {
          const r = await client.execute("whoami", []);
          return { ok: r.ok, output: r.output, missing: false };
        } catch (error) {
          return { ok: false, output: "", missing: isMissingExecutable(error) };
        }
      },
      connect: async (env) => {
        const holder: { client: McpStdioClient | null } = { client: null };
        holder.client = await McpStdioClient.connect({
          command, args: ["gateway"], env,
          onExit: (_code, tail) => { stderrTail = tail; if (holder.client) gateway.handleExit(holder.client); },
        });
        return holder.client;
      },
      sleep: async () => undefined,
    });
    try {
      const status = await gateway.start();
      console.log("keep status", JSON.stringify(status), "stderr:", stderrTail);
      expect(status.state, `${JSON.stringify(status)} ${stderrTail}`).toBe("connected");
      const team = await gateway.team();
      console.log("team length", team.length);
      expect(team).toBeTypeOf("string");
      const page = await gateway.timeline("nonexistent-session", 0, 1);
      console.log("timeline kind", page.kind, JSON.stringify(page).slice(0, 300));
      expect(["page", "unsupported"]).toContain(page.kind);   // "unsupported" until server v1.6.2 is deployed
    } finally {
      await gateway.stop();
    }
  }, 60_000);
});

describe.skipIf(!liveKeepTurn)("installed runtime Firekeep integration", () => {
  it("launches one real Studio turn that calls Keep memory", async () => {
    const runtimeId = (process.env.STUDIO_LIVE_KEEP_RUNTIME ?? "codex").toLowerCase();
    const create = runtimeFactories[runtimeId];
    expect(create, `unsupported STUDIO_LIVE_KEEP_RUNTIME=${runtimeId}`).toBeTypeOf("function");
    const runtime = create!();
    expect(runtime.descriptor.capabilities, `${runtime.descriptor.displayName} has no detected Client Kit memory config`).toContain("firekeep-memory");
    const events: RuntimeEventPayload[] = [];
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 120_000);
    try {
      const result = await runtime.run({
        runId: "live-keep-turn",
        studioSessionId: "live-keep-session",
        prompt: "Call memory_recall with task='Firekeep Studio live integration gate'. Then answer in one short sentence. You must call the tool before answering.",
        mode: "primary",
        cwd: process.cwd(),
        permissionMode: "standard",
      }, (event) => events.push(event), controller.signal);
      expect(result.finalText.trim()).not.toBe("");
      expect(events.some((event) => event.kind === "tool.started" && /memory_recall/i.test(event.name))).toBe(true);
    } finally {
      clearTimeout(timeout);
    }
  }, 150_000);
});
