import { describe, expect, it, vi } from "vitest";
import type { RunRequest, RuntimeEventPayload } from "../src/core/runtime.js";
import { CodexRuntime } from "../src/main/runtime/codex-runtime.js";
import type { RpcPeer } from "../src/main/runtime/jsonl-rpc.js";

class FakePeer implements RpcPeer {
  readonly calls: Array<{ method: string; params: unknown }> = [];
  readonly notifications: Array<{ method: string; params: unknown }> = [];
  readonly handlers = new Set<(method: string, params: unknown, raw: unknown) => void>();
  readonly requestHandlers = new Map<string, (params: unknown, raw: unknown) => unknown | Promise<unknown>>();
  readonly responses = new Map<string, unknown | ((params: unknown) => unknown | Promise<unknown>)>();
  closed = false;

  async request<T>(method: string, params?: unknown): Promise<T> {
    this.calls.push({ method, params });
    const response = this.responses.get(method);
    return (typeof response === "function" ? await response(params) : response) as T;
  }
  notify(method: string, params?: unknown): void { this.notifications.push({ method, params }); }
  onNotification(handler: (method: string, params: unknown, raw: unknown) => void): () => void { this.handlers.add(handler); return () => this.handlers.delete(handler); }
  onRequest(method: string, handler: (params: unknown, raw: unknown) => unknown | Promise<unknown>): () => void { this.requestHandlers.set(method, handler); return () => this.requestHandlers.delete(method); }
  stderr(): string { return ""; }
  close(): void { this.closed = true; }
  emit(method: string, params: unknown): void { for (const handler of this.handlers) handler(method, params, { method, params }); }
}

function request(overrides: Partial<RunRequest> = {}): RunRequest {
  return {
    runId: "run-1",
    studioSessionId: "studio-1",
    prompt: "Explain the change",
    mode: "primary",
    cwd: "C:\\work",
    permissionMode: "standard",
    ...overrides,
  };
}

function configuredPeer(): FakePeer {
  const peer = new FakePeer();
  peer.responses.set("initialize", { userAgent: "codex_cli/1", codexHome: "/tmp/codex", platformFamily: "windows", platformOs: "windows" });
  peer.responses.set("thread/start", { thread: { id: "thread-1" }, model: "gpt-5.6" });
  peer.responses.set("thread/resume", { thread: { id: "thread-old" }, model: "gpt-5.6" });
  peer.responses.set("turn/start", () => {
    queueMicrotask(() => {
      peer.emit("item/agentMessage/delta", { threadId: "thread-1", turnId: "turn-1", itemId: "message-1", delta: "Hello" });
      peer.emit("item/reasoning/summaryTextDelta", { threadId: "thread-1", turnId: "turn-1", itemId: "reason-1", delta: "Checked" });
      peer.emit("turn/diff/updated", { threadId: "thread-1", turnId: "turn-1", diff: "diff --git a/a b/a" });
      peer.emit("thread/tokenUsage/updated", { threadId: "thread-1", turnId: "turn-1", tokenUsage: { last: { inputTokens: 10, cachedInputTokens: 2, outputTokens: 4, reasoningOutputTokens: 1, totalTokens: 14 } } });
      peer.emit("item/completed", { threadId: "thread-1", turnId: "turn-1", item: { type: "agentMessage", id: "message-1", text: "Hello world" } });
      peer.emit("turn/completed", { threadId: "thread-1", turn: { id: "turn-1", status: "completed", error: null, durationMs: 25 } });
    });
    return { turn: { id: "turn-1" } };
  });
  return peer;
}

describe("CodexRuntime", () => {
  it("reports Keep memory only when native Codex config was detected", () => {
    expect(new CodexRuntime({ firekeepMemory: false }).descriptor.capabilities).not.toContain("firekeep-memory");
    expect(new CodexRuntime({ firekeepMemory: true }).descriptor.capabilities).toContain("firekeep-memory");
  });

  it("initializes App Server, starts a thread, and normalizes its stream", async () => {
    const peer = configuredPeer();
    const runtime = new CodexRuntime({ peerFactory: () => peer, versionProbe: async () => ({ found: true, version: "codex-cli 1", detail: "ready" }) });
    const events: RuntimeEventPayload[] = [];

    const result = await runtime.run(request({ model: "gpt-5.6", effort: "high" }), (event) => events.push(event), new AbortController().signal);

    expect(peer.calls.map((call) => call.method)).toEqual(["initialize", "thread/start", "turn/start"]);
    expect(peer.notifications).toContainEqual({ method: "initialized", params: undefined });
    expect(peer.calls.find((call) => call.method === "thread/start")?.params).toMatchObject({ cwd: "C:\\work", model: "gpt-5.6", sandbox: "workspace-write" });
    expect(result).toMatchObject({ nativeSessionId: "thread-1", finalText: "Hello world", usage: { inputTokens: 10, outputTokens: 4 } });
    expect(events.map((event) => event.kind)).toContain("reasoning.delta");
    expect(events.map((event) => event.kind)).toContain("diff.updated");
    expect(peer.closed).toBe(true);
  });

  it("resumes only when a Codex native session id is supplied", async () => {
    const peer = configuredPeer();
    peer.responses.set("turn/start", () => {
      queueMicrotask(() => {
        peer.emit("item/completed", { threadId: "thread-old", turnId: "turn-2", item: { type: "agentMessage", id: "m", text: "resumed" } });
        peer.emit("turn/completed", { threadId: "thread-old", turn: { id: "turn-2", status: "completed", error: null } });
      });
      return { turn: { id: "turn-2" } };
    });
    const runtime = new CodexRuntime({ peerFactory: () => peer });

    await runtime.run(request({ nativeSessionId: "thread-old" }), () => undefined, new AbortController().signal);

    expect(peer.calls.some((call) => call.method === "thread/resume")).toBe(true);
    expect(peer.calls.some((call) => call.method === "thread/start")).toBe(false);
  });

  it("uses the Studio approval broker for App Server requests", async () => {
    const peer = configuredPeer();
    peer.responses.set("turn/start", async () => {
      const handler = peer.requestHandlers.get("item/commandExecution/requestApproval");
      expect(handler).toBeDefined();
      const response = await handler?.({ itemId: "tool-1", command: "npm test", cwd: "C:\\work" }, {});
      expect(response).toEqual({ decision: "accept" });
      queueMicrotask(() => {
        peer.emit("item/completed", { threadId: "thread-1", turnId: "turn-1", item: { type: "agentMessage", id: "m", text: "done" } });
        peer.emit("turn/completed", { threadId: "thread-1", turn: { id: "turn-1", status: "completed", error: null } });
      });
      return { turn: { id: "turn-1" } };
    });
    const approve = vi.fn(async () => "accept");
    const runtime = new CodexRuntime({ peerFactory: () => peer });

    await runtime.run(request({ requestApproval: approve }), () => undefined, new AbortController().signal);

    expect(approve).toHaveBeenCalledWith(expect.objectContaining({ title: "Run command", options: ["accept", "acceptForSession", "decline", "cancel"] }));
  });

  it("maps explicit unrestricted mode to Codex danger-full-access without approvals", async () => {
    const peer = configuredPeer();
    const runtime = new CodexRuntime({ peerFactory: () => peer });

    await runtime.run(request({ permissionMode: "unrestricted" }), () => undefined, new AbortController().signal);

    expect(peer.calls.find((call) => call.method === "thread/start")?.params).toMatchObject({
      sandbox: "danger-full-access",
      approvalPolicy: "never",
    });
  });

  it("reads account state and paginated model metadata", async () => {
    const authPeer = configuredPeer();
    authPeer.responses.set("account/read", { account: { type: "chatgpt", email: "dev@example.com", planType: "pro" }, requiresOpenaiAuth: true });
    const modelPeer = configuredPeer();
    modelPeer.responses.set("model/list", { data: [{ id: "gpt", displayName: "GPT", description: "Agentic", isDefault: true, hidden: false, supportedReasoningEfforts: [{ reasoningEffort: "high", description: "" }], inputModalities: ["text", "image"] }], nextCursor: null });
    const peers = [authPeer, modelPeer];
    const runtime = new CodexRuntime({ peerFactory: () => peers.shift() as FakePeer });

    await expect(runtime.authStatus()).resolves.toMatchObject({ state: "connected", label: "dev@example.com · pro" });
    await expect(runtime.listModels()).resolves.toEqual([expect.objectContaining({ id: "gpt", efforts: ["high"], inputModalities: ["text", "image"] })]);
  });

  it("keeps browser login alive until the matching completion notification", async () => {
    const peer = configuredPeer();
    peer.responses.set("account/login/start", { type: "chatgpt", loginId: "login-1", authUrl: "https://auth.example.test/login" });
    const runtime = new CodexRuntime({ peerFactory: () => peer, loginTimeoutMs: 60_000 });

    await expect(runtime.login({ method: "browser" })).resolves.toMatchObject({ state: "browser", url: "https://auth.example.test/login" });
    expect(peer.closed).toBe(false);

    peer.emit("account/login/completed", { loginId: "someone-else", success: true });
    expect(peer.closed).toBe(false);

    peer.emit("account/login/completed", { loginId: "login-1", success: true });
    expect(peer.closed).toBe(true);
  });

  it("closes synchronous API-key login immediately", async () => {
    const peer = configuredPeer();
    peer.responses.set("account/login/start", { type: "apiKey" });
    const runtime = new CodexRuntime({ peerFactory: () => peer });

    await expect(runtime.login({ method: "api-key", secret: "test-key" })).resolves.toMatchObject({ state: "complete" });
    expect(peer.closed).toBe(true);
  });

  it("cancels an earlier pending login before starting another", async () => {
    const first = configuredPeer();
    first.responses.set("account/login/start", { type: "chatgpt", loginId: "login-1", authUrl: "https://auth.example.test/one" });
    const second = configuredPeer();
    second.responses.set("account/login/start", { type: "chatgpt", loginId: "login-2", authUrl: "https://auth.example.test/two" });
    const peers = [first, second];
    const runtime = new CodexRuntime({ peerFactory: () => peers.shift() as FakePeer });

    await runtime.login({ method: "browser" });
    await runtime.login({ method: "browser" });

    expect(first.calls).toContainEqual({ method: "account/login/cancel", params: { loginId: "login-1" } });
    expect(first.closed).toBe(true);
    expect(second.closed).toBe(false);
    second.emit("account/login/completed", { loginId: "login-2", success: true });
    expect(second.closed).toBe(true);
  });
});
