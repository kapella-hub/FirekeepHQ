import { EventEmitter } from "node:events";
import { describe, expect, it, vi } from "vitest";
import type { RunRequest, RuntimeEventPayload } from "../src/core/runtime.js";
import { ClaudeRuntime } from "../src/main/runtime/claude-runtime.js";
import type { JsonlTransport } from "../src/main/runtime/jsonl-rpc.js";
import type { ProcessResult } from "../src/main/runtime/process.js";

class FakeTransport implements JsonlTransport {
  readonly events = new EventEmitter();
  readonly writes: string[] = [];
  ended = false;
  killed = false;
  write(line: string): void { this.writes.push(line); }
  end(line?: string): void { if (line !== undefined) this.writes.push(line); this.ended = true; }
  kill(): void { this.killed = true; }
  onStdout(listener: (chunk: Uint8Array | string) => void): () => void { this.events.on("stdout", listener); return () => this.events.off("stdout", listener); }
  onStderr(listener: (chunk: Uint8Array | string) => void): () => void { this.events.on("stderr", listener); return () => this.events.off("stderr", listener); }
  onExit(listener: (code: number | null, signal: NodeJS.Signals | null) => void): () => void { this.events.on("exit", listener); return () => this.events.off("exit", listener); }
  onError(listener: (error: Error) => void): () => void { this.events.on("error", listener); return () => this.events.off("error", listener); }
}

const processResult = (stdout: string, exitCode = 0): ProcessResult => ({
  exitCode,
  signal: null,
  stdout,
  stderr: "",
  timedOut: false,
  truncated: false,
  durationMs: 1,
});

function request(overrides: Partial<RunRequest> = {}): RunRequest {
  return {
    runId: "run-1",
    studioSessionId: "studio-1",
    prompt: "Review this",
    mode: "primary",
    cwd: "C:\\work",
    permissionMode: "standard",
    ...overrides,
  };
}

describe("ClaudeRuntime", () => {
  it("advertises Firekeep capabilities only when the matching native config exists", () => {
    const disconnected = new ClaudeRuntime({ firekeepMemory: false, firekeepHooks: false });
    const configured = new ClaudeRuntime({ firekeepMemory: true, firekeepHooks: true });

    expect(disconnected.descriptor.capabilities).not.toContain("firekeep-memory");
    expect(disconnected.descriptor.capabilities).not.toContain("firekeep-hooks");
    expect(configured.descriptor.capabilities).toEqual(expect.arrayContaining(["firekeep-memory", "firekeep-hooks"]));
  });

  it("normalizes stream-json output and resumes a provider session", async () => {
    const transport = new FakeTransport();
    let capturedArgs: readonly string[] = [];
    const runtime = new ClaudeRuntime({
      processFactory: (args) => { capturedArgs = args; return transport; },
    });
    const events: RuntimeEventPayload[] = [];
    const running = runtime.run(request({ nativeSessionId: "claude-session", model: "sonnet", effort: "high" }), (event) => events.push(event), new AbortController().signal);

    transport.events.emit("stdout", [
      { type: "system", subtype: "init", session_id: "claude-session", model: "sonnet" },
      { type: "stream_event", event: { type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "Looks " } } },
      { type: "stream_event", event: { type: "content_block_delta", index: 1, delta: { type: "thinking_delta", thinking: "Inspecting" } } },
      { type: "assistant", message: { content: [{ type: "tool_use", id: "tool-1", name: "Read", input: { file_path: "a.ts" } }] } },
      { type: "user", message: { content: [{ type: "tool_result", tool_use_id: "tool-1", content: "file", is_error: false }] } },
      { type: "result", subtype: "success", is_error: false, result: "Looks good", session_id: "claude-session", duration_ms: 55, total_cost_usd: 0.01, usage: { input_tokens: 12, cache_read_input_tokens: 2, output_tokens: 5 } },
    ].map((line) => JSON.stringify(line)).join("\n") + "\n");
    transport.events.emit("exit", 0, null);

    await expect(running).resolves.toMatchObject({ nativeSessionId: "claude-session", finalText: "Looks good", usage: { inputTokens: 12, cachedInputTokens: 2, outputTokens: 5, totalTokens: 19, costUsd: 0.01 } });
    expect(capturedArgs).toContain("--resume");
    expect(capturedArgs).toContain("claude-session");
    expect(transport.writes).toEqual(["Review this"]);
    expect(transport.ended).toBe(true);
    expect(events.map((event) => event.kind)).toEqual(expect.arrayContaining(["message.delta", "reasoning.delta", "tool.started", "tool.completed", "message.completed", "usage.updated"]));
  });

  it("forces fresh read-only reviewer sessions", async () => {
    const transport = new FakeTransport();
    let args: readonly string[] = [];
    const runtime = new ClaudeRuntime({ processFactory: (value) => { args = value; return transport; } });
    const running = runtime.run(request({ mode: "review", permissionMode: "safe" }), () => undefined, new AbortController().signal);
    transport.events.emit("stdout", `${JSON.stringify({ type: "result", subtype: "success", result: "finding", session_id: "review-session", usage: {} })}\n`);
    transport.events.emit("exit", 0, null);
    await running;

    expect(args).toEqual(expect.arrayContaining(["--permission-mode", "plan", "--no-session-persistence", "--tools", "Read,Grep,Glob"]));
    expect(args).not.toContain("--resume");
  });

  it("maps an explicit unrestricted selection to Claude's native bypass contract", async () => {
    const transport = new FakeTransport();
    let args: readonly string[] = [];
    const runtime = new ClaudeRuntime({ processFactory: (value) => { args = value; return transport; } });
    const running = runtime.run(request({ permissionMode: "unrestricted" }), () => undefined, new AbortController().signal);
    transport.events.emit("stdout", `${JSON.stringify({ type: "result", subtype: "success", result: "done", usage: {} })}\n`);
    transport.events.emit("exit", 0, null);

    await running;

    expect(args).toEqual(expect.arrayContaining(["--permission-mode", "bypassPermissions", "--dangerously-skip-permissions"]));
  });

  it("discovers model aliases and reasoning efforts from the installed Claude CLI", async () => {
    const help = [
      "  --effort <level>  Effort level (low, medium, high, xhigh)",
      "  --model <model>   Provide an alias for the latest model (e.g. 'nebula', 'sonnet') or a full name",
    ].join("\n");
    const runCommand = vi.fn(async (args: readonly string[]) => processResult(args[0] === "--help" ? help : ""));
    const runtime = new ClaudeRuntime({ runCommand });

    const models = await runtime.listModels();

    expect(runCommand).toHaveBeenCalledWith(["--help"]);
    expect(models.map((model) => model.id)).toEqual(["default", "nebula", "sonnet"]);
    expect(models.every((model) => model.efforts?.join(",") === "low,medium,high,xhigh")).toBe(true);
  });

  it("streams deduplicated usage before a Claude run reaches its final result", async () => {
    const transport = new FakeTransport();
    const runtime = new ClaudeRuntime({ processFactory: () => transport });
    const events: RuntimeEventPayload[] = [];
    const running = runtime.run(request(), (event) => events.push(event), new AbortController().signal);
    const first = { type: "assistant", message: { id: "message-1", content: [], usage: { input_tokens: 2, cache_creation_input_tokens: 10, cache_read_input_tokens: 20, output_tokens: 3 } } };
    transport.events.emit("stdout", `${JSON.stringify(first)}\n${JSON.stringify(first)}\n${JSON.stringify({ type: "assistant", message: { id: "message-2", content: [], usage: { input_tokens: 1, cache_read_input_tokens: 30, output_tokens: 4 } } })}\n`);
    transport.events.emit("exit", 0, null);

    const result = await running;
    const updates = events.filter((event) => event.kind === "usage.updated");

    expect(updates).toHaveLength(2);
    expect(updates.at(-1)).toMatchObject({ usage: { inputTokens: 3, cacheCreationInputTokens: 10, cachedInputTokens: 50, outputTokens: 7, totalTokens: 70 } });
    expect(result.usage?.totalTokens).toBe(70);
  });

  it("reads provider-owned auth state and launches provider login", async () => {
    const runCommand = vi.fn(async (_args: readonly string[]) => processResult(JSON.stringify({ loggedIn: true, email: "dev@example.com", subscriptionType: "max" })));
    const launchLogin = vi.fn();
    const runtime = new ClaudeRuntime({ runCommand, launchLogin });

    await expect(runtime.authStatus()).resolves.toMatchObject({ state: "connected", label: "dev@example.com · max" });
    await expect(runtime.login({ method: "browser" })).resolves.toMatchObject({ state: "external" });
    expect(launchLogin).toHaveBeenCalledOnce();
  });
});
