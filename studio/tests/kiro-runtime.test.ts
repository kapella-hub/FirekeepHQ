import * as acp from "@agentclientprotocol/sdk";
import { describe, expect, it, vi } from "vitest";
import type { RunRequest, RuntimeEventPayload } from "../src/core/runtime.js";
import { KiroRuntime, type AcpTargetHandle } from "../src/main/runtime/kiro-runtime.js";
import type { ProcessResult } from "../src/main/runtime/process.js";

function request(overrides: Partial<RunRequest> = {}): RunRequest {
  return {
    runId: "run-1",
    studioSessionId: "studio-1",
    prompt: "Inspect the project",
    mode: "primary",
    cwd: "C:\\work",
    permissionMode: "standard",
    ...overrides,
  };
}

function testAgent(): acp.AgentApp {
  return acp.agent({ name: "test-kiro" })
    .onRequest(acp.methods.agent.initialize, () => ({
      protocolVersion: acp.PROTOCOL_VERSION,
      agentCapabilities: { loadSession: true },
      agentInfo: { name: "Kiro", version: "test" },
    }))
    .onRequest(acp.methods.agent.session.new, () => ({ sessionId: "kiro-session" }))
    .onRequest(acp.methods.agent.session.load, () => ({}))
    .onRequest(acp.methods.agent.session.prompt, async (ctx) => {
      await ctx.client.notify(acp.methods.client.session.update, {
        sessionId: ctx.params.sessionId,
        update: { sessionUpdate: "agent_message_chunk", messageId: "m1", content: { type: "text", text: "Found it." } },
      });
      await ctx.client.notify(acp.methods.client.session.update, {
        sessionId: ctx.params.sessionId,
        update: { sessionUpdate: "tool_call", toolCallId: "t1", title: "Edit config", kind: "edit", status: "pending", rawInput: { path: "a" } },
      });
      const permission = await ctx.client.request(acp.methods.client.session.requestPermission, {
        sessionId: ctx.params.sessionId,
        toolCall: { toolCallId: "t1", title: "Edit config", kind: "edit" },
        options: [
          { optionId: "allow", name: "Allow once", kind: "allow_once" },
          { optionId: "reject", name: "Reject", kind: "reject_once" },
        ],
      });
      await ctx.client.notify(acp.methods.client.session.update, {
        sessionId: ctx.params.sessionId,
        update: {
          sessionUpdate: "tool_call_update",
          toolCallId: "t1",
          status: permission.outcome.outcome === "selected" && permission.outcome.optionId === "allow" ? "completed" : "failed",
          rawOutput: { permission },
          content: [{ type: "diff", path: "a", oldText: "x", newText: "y" }],
        },
      });
      return { stopReason: "end_turn", usage: { totalTokens: 20, inputTokens: 14, outputTokens: 6, thoughtTokens: 2 } };
    });
}

describe("KiroRuntime", () => {
  it("uses stable ACP for sessions, updates, usage, and permissions", async () => {
    const close = vi.fn();
    let args: readonly string[] = [];
    const runtime = new KiroRuntime({
      agentName: null,
      targetFactory: (value): AcpTargetHandle => { args = value; return { target: testAgent(), close, stderr: () => "" }; },
    });
    const approve = vi.fn(async () => "allow");
    const events: RuntimeEventPayload[] = [];

    const result = await runtime.run(request({ model: "kiro-model", effort: "high", requestApproval: approve }), (event) => events.push(event), new AbortController().signal);

    expect(args).toEqual(["acp", "--model", "kiro-model", "--effort", "high"]);
    expect(runtime.descriptor.capabilities).not.toContain("firekeep-hooks");
    expect(runtime.descriptor.capabilities).not.toContain("firekeep-memory");
    expect(result).toMatchObject({ nativeSessionId: "kiro-session", finalText: "Found it.", usage: { totalTokens: 20, inputTokens: 14, outputTokens: 6, reasoningTokens: 2 } });
    expect(approve).toHaveBeenCalledWith(expect.objectContaining({ id: "kiro:t1", options: ["allow", "reject"] }));
    expect(events.map((event) => event.kind)).toEqual(expect.arrayContaining(["message.delta", "message.completed", "tool.started", "tool.completed", "diff.updated", "usage.updated"]));
    expect(close).toHaveBeenCalledOnce();
  });

  it("loads a native session and auto-rejects review permissions", async () => {
    const runtime = new KiroRuntime({ agentName: null, targetFactory: () => ({ target: testAgent(), close: () => undefined, stderr: () => "" }) });
    const approve = vi.fn(async () => "allow");

    const result = await runtime.run(request({ mode: "review", nativeSessionId: "existing", permissionMode: "safe", requestApproval: approve }), () => undefined, new AbortController().signal);

    expect(result.nativeSessionId).toBe("kiro-session");
    expect(approve).not.toHaveBeenCalled();
  });

  it("reads provider-owned Kiro authentication", async () => {
    const result: ProcessResult = { exitCode: 0, signal: null, stdout: '{"accountType":"Social","provider":"Google","email":"dev@example.com"}', stderr: "", timedOut: false, truncated: false, durationMs: 1 };
    const runtime = new KiroRuntime({ runCommand: async () => result });

    await expect(runtime.authStatus()).resolves.toMatchObject({ state: "connected", label: "dev@example.com · Google" });
  });

  it("discovers account-available models and reasoning efforts from Kiro CLI", async () => {
    const runCommand = vi.fn(async (args: readonly string[]): Promise<ProcessResult> => {
      if (args[0] === "chat") {
        return { exitCode: 0, signal: null, stdout: JSON.stringify({ models: [
          { model_name: "auto", model_id: "auto", description: "Choose automatically" },
          { model_name: "new-model", model_id: "new-model", description: "Current account model" },
        ], default_model: "auto" }), stderr: "", timedOut: false, truncated: false, durationMs: 1 };
      }
      return { exitCode: 0, signal: null, stdout: "--effort <EFFORT> Initial effort level (e.g. low, medium, high, xhigh)", stderr: "", timedOut: false, truncated: false, durationMs: 1 };
    });
    const runtime = new KiroRuntime({ runCommand, agentName: null });

    const models = await runtime.listModels();

    expect(runCommand).toHaveBeenCalledWith(["chat", "--list-models", "--format", "json"]);
    expect(runCommand).toHaveBeenCalledWith(["acp", "--help"]);
    expect(models).toEqual([
      expect.objectContaining({ id: "auto", displayName: "auto", isDefault: true, efforts: ["low", "medium", "high", "xhigh"] }),
      expect.objectContaining({ id: "new-model", displayName: "new-model", efforts: ["low", "medium", "high", "xhigh"] }),
    ]);
  });

  it("passes Kiro's native trust-all flag only for explicit unrestricted runs", async () => {
    let args: readonly string[] = [];
    const runtime = new KiroRuntime({
      agentName: null,
      targetFactory: (value) => { args = value; return { target: testAgent(), close: () => undefined, stderr: () => "" }; },
    });

    await runtime.run(request({ permissionMode: "unrestricted" }), () => undefined, new AbortController().signal);

    expect(args).toContain("--trust-all-tools");
  });

  it("selects the Client Kit's named Firekeep agent when it is installed", async () => {
    let args: readonly string[] = [];
    const runtime = new KiroRuntime({
      agentName: "firekeep",
      targetFactory: (value) => { args = value; return { target: testAgent(), close: () => undefined, stderr: () => "" }; },
    });

    await runtime.run(request(), () => undefined, new AbortController().signal);

    expect(args.slice(0, 3)).toEqual(["acp", "--agent", "firekeep"]);
    expect(runtime.descriptor.capabilities).toContain("firekeep-hooks");
    expect(runtime.descriptor.capabilities).toContain("firekeep-memory");
  });
});
