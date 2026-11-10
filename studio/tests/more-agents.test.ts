import * as acp from "@agentclientprotocol/sdk";
import { EventEmitter } from "node:events";
import { describe, expect, it, vi } from "vitest";
import type { RunRequest, RuntimeEventPayload } from "../src/core/runtime.js";
import type { JsonlTransport } from "../src/main/runtime/jsonl-rpc.js";
import type { AcpTargetHandle } from "../src/main/runtime/kiro-runtime.js";
import { createRuntimeRegistry } from "../src/main/runtime/index.js";
import { OpenCodeRuntime } from "../src/main/runtime/opencode-runtime.js";
import { PiRuntime, parsePiModels, piArgs } from "../src/main/runtime/pi-runtime.js";
import type { ProcessResult } from "../src/main/runtime/process.js";

const processResult = (stdout: string, exitCode = 0, stderr = ""): ProcessResult => ({ exitCode, signal: null, stdout, stderr, timedOut: false, truncated: false, durationMs: 1 });

function request(overrides: Partial<RunRequest> = {}): RunRequest {
  return { runId: "run-1", studioSessionId: "studio-1", prompt: "Inspect the project", mode: "primary", cwd: "C:\\work", permissionMode: "standard", ...overrides };
}

describe("runtime registry", () => {
  it("offers OpenCode and Pi beside the original three agents", () => {
    expect(createRuntimeRegistry("test").list().map((runtime) => runtime.descriptor.id)).toEqual(["claude", "codex", "kiro", "opencode", "pi"]);
  });
});

describe("OpenCodeRuntime", () => {
  function agent(configured: Array<{ configId: string; value: unknown }>): acp.AgentApp {
    return acp.agent({ name: "test-opencode" })
      .onRequest(acp.methods.agent.initialize, () => ({ protocolVersion: acp.PROTOCOL_VERSION, agentCapabilities: { loadSession: true }, agentInfo: { name: "OpenCode", version: "test" } }))
      .onRequest(acp.methods.agent.session.new, () => ({ sessionId: "ses_new" }))
      .onRequest(acp.methods.agent.session.load, () => ({}))
      .onRequest(acp.methods.agent.session.setConfigOption, (ctx) => { configured.push({ configId: ctx.params.configId, value: ctx.params.value }); return { configOptions: [] }; })
      .onRequest(acp.methods.agent.session.prompt, async (ctx) => {
        await ctx.client.notify(acp.methods.client.session.update, { sessionId: ctx.params.sessionId, update: { sessionUpdate: "agent_message_chunk", content: { type: "text", text: "Done." } } });
        const permission = await ctx.client.request(acp.methods.client.session.requestPermission, {
          sessionId: ctx.params.sessionId,
          toolCall: { toolCallId: "t1", title: "Edit config", kind: "edit" },
          options: [{ optionId: "once", name: "Allow once", kind: "allow_once" }, { optionId: "reject", name: "Reject", kind: "reject_once" }],
        });
        await ctx.client.notify(acp.methods.client.session.update, { sessionId: ctx.params.sessionId, update: { sessionUpdate: "agent_message_chunk", content: { type: "text", text: permission.outcome.outcome === "selected" ? ` ${permission.outcome.optionId}` : " cancelled" } } });
        return { stopReason: "end_turn", usage: { totalTokens: 12, inputTokens: 9, outputTokens: 3 } };
      });
  }

  function runtime(configured: Array<{ configId: string; value: unknown }>, seen: { args?: readonly string[]; cwd?: string | undefined } = {}): OpenCodeRuntime {
    return new OpenCodeRuntime({
      firekeepMemory: false,
      firekeepHooks: false,
      targetFactory: (args, cwd): AcpTargetHandle => { seen.args = args; seen.cwd = cwd; return { target: agent(configured), close: () => undefined, stderr: () => "" }; },
    });
  }

  it("runs build mode with the chosen model and forwards permission requests to the human", async () => {
    const configured: Array<{ configId: string; value: unknown }> = [];
    const seen: { args?: readonly string[]; cwd?: string | undefined } = {};
    const approve = vi.fn(async () => "once");
    const events: RuntimeEventPayload[] = [];
    const result = await runtime(configured, seen).run(request({ model: "anthropic/claude-x", requestApproval: approve }), (event) => events.push(event), new AbortController().signal);
    expect(seen).toEqual({ args: ["acp"], cwd: "C:\\work" });
    expect(configured).toEqual([{ configId: "mode", value: "build" }, { configId: "model", value: "anthropic/claude-x" }]);
    expect(approve).toHaveBeenCalledWith(expect.objectContaining({ id: "opencode:t1", title: "Edit config", options: ["once", "reject"] }));
    expect(result).toMatchObject({ nativeSessionId: "ses_new", finalText: "Done. once", usage: { totalTokens: 12 } });
    expect(events.find((event) => event.kind === "message.completed")).toMatchObject({ messageId: "opencode-message" });
  });

  it("keeps Safe mode and reviews in plan mode and refuses every permission request", async () => {
    for (const overrides of [{ permissionMode: "safe" as const }, { mode: "review" as const, permissionMode: "unrestricted" as const }]) {
      const configured: Array<{ configId: string; value: unknown }> = [];
      const approve = vi.fn(async () => "once");
      const result = await runtime(configured).run(request({ ...overrides, model: "default", requestApproval: approve }), () => undefined, new AbortController().signal);
      expect(configured).toEqual([{ configId: "mode", value: "plan" }]);
      expect(approve).not.toHaveBeenCalled();
      expect(result.finalText).toBe("Done. reject");
    }
  });

  it("allows every request when unrestricted, and resumes a saved session", async () => {
    const configured: Array<{ configId: string; value: unknown }> = [];
    const result = await runtime(configured).run(request({ permissionMode: "unrestricted", nativeSessionId: "ses_old" }), () => undefined, new AbortController().signal);
    expect(result).toMatchObject({ nativeSessionId: "ses_old", finalText: "Done. once" });
  });

  it("lists models and treats zero credentials as the free models, not as signed out", async () => {
    const runCommand = vi.fn(async (args: readonly string[]): Promise<ProcessResult> => args[0] === "models"
      ? processResult("opencode/big-pickle\nanthropic/claude-x\nnot a model line\nopencode/big-pickle\n")
      : processResult("\u001b[0m\n\u001b[90m┌\u001b[39m  Credentials \u001b[90m~/.local/share/opencode/auth.json\n\u001b[90m└\u001b[39m  0 credentials\n"));
    const subject = new OpenCodeRuntime({ runCommand, firekeepMemory: true, firekeepHooks: true });
    expect((await subject.listModels()).map((model) => model.id)).toEqual(["default", "opencode/big-pickle", "anthropic/claude-x"]);
    await expect(subject.authStatus()).resolves.toMatchObject({ state: "connected", label: "OpenCode free models only" });
    expect(subject.descriptor.capabilities).toEqual(expect.arrayContaining(["approvals", "firekeep-memory", "firekeep-hooks"]));
    const two = new OpenCodeRuntime({ runCommand: async () => processResult("2 credentials"), firekeepMemory: false, firekeepHooks: false });
    await expect(two.authStatus()).resolves.toMatchObject({ label: "2 provider credentials" });
    expect(two.descriptor.capabilities).not.toContain("firekeep-memory");
  });
});

class FakeTransport implements JsonlTransport {
  readonly events = new EventEmitter();
  readonly writes: string[] = [];
  killed = false;
  write(line: string): void { this.writes.push(line); }
  end(line?: string): void { if (line !== undefined) this.writes.push(line); }
  kill(): void { this.killed = true; }
  onStdout(listener: (chunk: Uint8Array | string) => void): () => void { this.events.on("stdout", listener); return () => this.events.off("stdout", listener); }
  onStderr(listener: (chunk: Uint8Array | string) => void): () => void { this.events.on("stderr", listener); return () => this.events.off("stderr", listener); }
  onExit(listener: (code: number | null, signal: NodeJS.Signals | null) => void): () => void { this.events.on("exit", listener); return () => this.events.off("exit", listener); }
  onError(listener: (error: Error) => void): () => void { this.events.on("error", listener); return () => this.events.off("error", listener); }
  emit(...lines: unknown[]): void { this.events.emit("stdout", lines.map((line) => `${JSON.stringify(line)}\n`).join("")); }
}

const usage = (input: number, output: number, cost: number) => ({ input, output, cacheRead: 10, cacheWrite: 0, totalTokens: input + output + 10, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: cost } });
const assistant = (text: string, extra: Record<string, unknown> = {}) => ({ role: "assistant", content: [{ type: "thinking", thinking: "hm" }, { type: "text", text }], usage: usage(100, 20, 0.01), stopReason: "stop", ...extra });

describe("PiRuntime", () => {
  it("streams text, thinking, tools and summed usage from Pi's JSON events, with the prompt on stdin", async () => {
    const transport = new FakeTransport();
    let args: readonly string[] = [];
    const runtime = new PiRuntime({ firekeepHooks: false, processFactory: (value) => { args = value; return transport; } });
    const events: RuntimeEventPayload[] = [];
    const running = runtime.run(request({ prompt: "@not-a-file --flag", permissionMode: "unrestricted", model: "anthropic/claude-x", effort: "max" }), (event) => events.push(event), new AbortController().signal);
    expect(args).toEqual(["--mode", "json", "--print", "--model", "anthropic/claude-x", "--thinking", "max"]);
    expect(transport.writes).toEqual(["@not-a-file --flag"]);
    transport.emit(
      { type: "session", version: 3, id: "pi-session", cwd: "C:\\work" },
      { type: "agent_start" },
      { type: "message_update", assistantMessageEvent: { type: "thinking_delta", delta: "hm" } },
      { type: "message_update", assistantMessageEvent: { type: "text_delta", delta: "Reading. " } },
      { type: "message_end", message: assistant("Reading. ", { stopReason: "toolUse" }) },
      { type: "tool_execution_start", toolCallId: "c1", toolName: "read", args: { path: "a.txt" } },
      { type: "tool_execution_end", toolCallId: "c1", toolName: "read", result: { content: "x" }, isError: false },
      { type: "message_end", message: { role: "toolResult", content: [] } },
      { type: "message_update", assistantMessageEvent: { type: "text_delta", delta: "All done." } },
      { type: "message_end", message: assistant("All done.") },
      { type: "agent_end", messages: [], willRetry: false },
      { type: "agent_settled", aborted: false },
    );
    transport.events.emit("exit", 0, null);
    const result = await running;
    expect(result).toEqual({ nativeSessionId: "pi-session", finalText: "All done.", usage: { totalTokens: 260, inputTokens: 220, outputTokens: 40, cachedInputTokens: 20, costUsd: 0.02 } });
    expect(events.map((event) => event.kind)).toEqual(["reasoning.delta", "message.delta", "usage.updated", "tool.started", "tool.completed", "message.delta", "usage.updated", "message.completed"]);
    expect(events.find((event) => event.kind === "tool.started")).toMatchObject({ toolCallId: "c1", name: "read", input: { path: "a.txt" } });
    expect(transport.killed).toBe(false);
  });

  it("says plainly that Pi does not ask before using tools, and keeps Safe mode and reviews read-only", async () => {
    const runtime = new PiRuntime({ firekeepHooks: true, processFactory: () => new FakeTransport() });
    expect(runtime.descriptor.capabilities).not.toContain("approvals");
    expect(runtime.descriptor.capabilities).toContain("firekeep-hooks");
    expect(runtime.descriptor.description).toContain("no permission prompts");

    const transport = new FakeTransport();
    const events: RuntimeEventPayload[] = [];
    const running = new PiRuntime({ firekeepHooks: false, processFactory: () => transport }).run(request(), (event) => events.push(event), new AbortController().signal);
    transport.events.emit("exit", 0, null);
    await running;
    expect(events[0]).toMatchObject({ kind: "notice", level: "warning", message: "Pi does not ask before using tools" });

    expect(piArgs(request({ permissionMode: "safe", nativeSessionId: "abc" }), true)).toEqual(["--mode", "json", "--print", "--tools", "read,grep,find,ls", "--session", "abc"]);
    expect(piArgs(request({ mode: "review", nativeSessionId: "abc" }), true)).toEqual(["--mode", "json", "--print", "--tools", "read,grep,find,ls", "--no-session"]);
  });

  it("reports the retry's answer when Pi retries a failed response on its own", async () => {
    const transport = new FakeTransport();
    const running = new PiRuntime({ firekeepHooks: false, processFactory: () => transport }).run(request({ permissionMode: "safe" }), () => undefined, new AbortController().signal);
    transport.emit(
      { type: "message_update", usage: usage(1, 0, 0), assistantMessageEvent: { type: "text_delta", contentIndex: 0, delta: "Recovered." } },
      { type: "message_end", message: assistant("", { stopReason: "error", errorMessage: "overloaded" }) },
      { type: "agent_end", messages: [], willRetry: true },
      { type: "message_end", message: assistant("Recovered.") },
      { type: "agent_end", messages: [], willRetry: false },
      { type: "agent_settled", aborted: false },
    );
    transport.events.emit("exit", 0, null);
    await expect(running).resolves.toMatchObject({ finalText: "Recovered." });
  });

  it("fails the run on a provider error or a bad exit, and stops the process when cancelled", async () => {
    const failed = new FakeTransport();
    const first = new PiRuntime({ firekeepHooks: false, processFactory: () => failed }).run(request({ permissionMode: "safe" }), () => undefined, new AbortController().signal);
    failed.emit({ type: "message_end", message: assistant("", { stopReason: "error", errorMessage: "No API key found for anthropic." }) }, { type: "agent_end", messages: [] });
    failed.events.emit("exit", 0, null);
    await expect(first).rejects.toThrow("No API key found");

    const crashed = new FakeTransport();
    const second = new PiRuntime({ firekeepHooks: false, processFactory: () => crashed }).run(request({ permissionMode: "safe" }), () => undefined, new AbortController().signal);
    crashed.events.emit("stderr", "boom");
    crashed.events.emit("exit", 1, null);
    await expect(second).rejects.toThrow("boom");

    const cancelled = new FakeTransport();
    const controller = new AbortController();
    const third = new PiRuntime({ firekeepHooks: false, processFactory: () => cancelled }).run(request({ permissionMode: "safe" }), () => undefined, controller.signal);
    controller.abort();
    await expect(third).rejects.toThrow("aborted");
    expect(cancelled.killed).toBe(true);
  });

  it("reads the model table, and reports no models as not signed in", async () => {
    const table = [
      "provider   model                 context  max-out  thinking  images",
      "anthropic  claude-x-5            200K     64K      yes       yes",
      "ollama     qwen3:latest          32K      8K       no        no",
      "Warning: something else entirely",
    ].join("\n");
    expect(parsePiModels(table)).toEqual(["anthropic/claude-x-5", "ollama/qwen3:latest"]);
    expect(parsePiModels("No models available. Use /login to log into a provider via OAuth or API key. See:\n  C:\\docs\\providers.md\n  C:\\docs\\models.md")).toEqual([]);

    const signedIn = new PiRuntime({ firekeepHooks: false, runCommand: async () => processResult(table) });
    await expect(signedIn.authStatus()).resolves.toMatchObject({ state: "connected", label: "2 models available" });
    expect((await signedIn.listModels()).map((model) => model.id)).toEqual(["default", "anthropic/claude-x-5", "ollama/qwen3:latest"]);
    const signedOut = new PiRuntime({ firekeepHooks: false, runCommand: async () => processResult("No models available. Use /login to log into a provider via OAuth or API key.") });
    await expect(signedOut.authStatus()).resolves.toMatchObject({ state: "disconnected" });
    await expect(signedOut.login({})).resolves.toMatchObject({ state: "external" });
  });
});
