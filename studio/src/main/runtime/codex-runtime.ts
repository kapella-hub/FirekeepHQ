import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import type {
  AgentRuntime,
  LoginRequest,
  LoginResult,
  RunRequest,
  RunResult,
  RuntimeAuthStatus,
  RuntimeConnection,
  RuntimeEffort,
  RuntimeEventSink,
  RuntimeModel,
  RuntimeUsage,
} from "../../core/runtime.js";
import { ChildProcessJsonlTransport, JsonlRpcPeer, type RpcPeer } from "./jsonl-rpc.js";
import { probeVersion } from "./process.js";

interface VersionProbeResult { readonly found: boolean; readonly version?: string; readonly detail: string }

export interface CodexRuntimeOptions {
  readonly command?: string;
  readonly peerFactory?: () => RpcPeer;
  readonly versionProbe?: () => Promise<VersionProbeResult>;
  readonly appVersion?: string;
  readonly firekeepMemory?: boolean;
  readonly loginTimeoutMs?: number;
}

interface JsonObject { readonly [key: string]: unknown }
interface PendingLogin {
  readonly loginId: string;
  readonly peer: RpcPeer;
  readonly dispose: () => void;
  readonly timeout: ReturnType<typeof setTimeout>;
}

export class CodexRuntime implements AgentRuntime {
  readonly descriptor;
  readonly #command: string;
  readonly #commandPrefix: readonly string[];
  readonly #peerFactory: () => RpcPeer;
  readonly #versionProbe: () => Promise<VersionProbeResult>;
  readonly #appVersion: string;
  readonly #loginTimeoutMs: number;
  #pendingLogin: PendingLogin | null = null;

  constructor(options: CodexRuntimeOptions = {}) {
    const invocation = options.command ? { command: options.command, prefix: [] as string[] } : defaultCodexInvocation();
    this.#command = invocation.command;
    this.#commandPrefix = invocation.prefix;
    this.#peerFactory = options.peerFactory ?? (() => new JsonlRpcPeer(
      new ChildProcessJsonlTransport(this.#command, [...this.#commandPrefix, "app-server"], { env: process.env }),
      { requestTimeoutMs: 60_000 },
    ));
    this.#versionProbe = options.versionProbe ?? (() => probeVersion(this.#command, [...this.#commandPrefix, "--version"]));
    this.#appVersion = options.appVersion ?? "0.3.5";
    this.#loginTimeoutMs = options.loginTimeoutMs ?? 10 * 60_000;
    const firekeepMemory = options.firekeepMemory ?? installedFirekeepMemory();
    this.descriptor = {
      id: "codex",
      displayName: "Codex",
      description: "OpenAI Codex through the local App Server protocol",
      transport: "app-server",
      capabilities: ["chat", "review", "streaming", "tools", "approvals", "resume", "models", "images", "usage", "reasoning", ...(firekeepMemory ? ["firekeep-memory" as const] : [])],
      loginMethods: ["browser", "device", "api-key"],
      accent: "#8be3c7",
    } as const;
  }

  async probe(): Promise<RuntimeConnection> {
    const result = await this.#versionProbe();
    return result.found
      ? { state: "ready", ...(result.version ? { version: result.version } : {}), detail: result.detail, executable: this.#command }
      : { state: "missing", detail: result.detail };
  }

  async authStatus(): Promise<RuntimeAuthStatus> {
    try {
      return await this.#withPeer<RuntimeAuthStatus>(async (peer) => {
        const result = object(await peer.request("account/read", { refreshToken: false }));
        const account = result.account;
        if (!account || typeof account !== "object") {
          return { state: "disconnected", detail: result.requiresOpenaiAuth === false ? "No account configured" : "Sign in to Codex" };
        }
        const value = object(account);
        const type = string(value.type) ?? "account";
        const email = string(value.email);
        const plan = string(value.planType);
        const label = [email ?? (type === "apiKey" ? "API key" : type), plan].filter(Boolean).join(" · ");
        return { state: "connected", label, methods: ["browser", "device", "api-key"] };
      });
    } catch (error) {
      return { state: "error", detail: errorMessage(error) };
    }
  }

  async login(request: LoginRequest): Promise<LoginResult> {
    await this.#cancelPendingLogin();
    const method = request.method ?? "browser";
    if (method === "api-key") {
      return this.#withPeer(async (peer) => {
        await peer.request("account/login/start", { type: "apiKey", apiKey: requireSecret(request) });
        return { state: "complete", message: "Codex account connected." };
      });
    }

    const peer = this.#peerFactory();
    let retained = false;
    let completedBeforeStart: string | null = null;
    let dispose = (): void => undefined;
    try {
      await this.#initialize(peer);
      dispose = peer.onNotification((notification, rawParams) => {
        if (notification !== "account/login/completed") return;
        const completedId = string(object(rawParams).loginId);
        if (!completedId) return;
        completedBeforeStart = completedId;
        if (this.#pendingLogin?.peer === peer && this.#pendingLogin.loginId === completedId) this.#finishPendingLogin(peer);
      });
      const params = method === "device"
        ? { type: "chatgptDeviceCode" }
        : { type: "chatgpt", codexStreamlinedLogin: true, useHostedLoginSuccessPage: true, appBrand: "codex" };
      const response = object(await peer.request("account/login/start", params));
      const type = string(response.type);
      const loginId = string(response.loginId);
      if (!loginId) throw new Error("Codex App Server returned no login id");
      if (completedBeforeStart === loginId) return { state: "complete", message: "Codex account connected." };
      let result: LoginResult;
      if (type === "chatgpt" && string(response.authUrl)) {
        result = { state: "browser", url: string(response.authUrl) as string, message: "Finish signing in to Codex in your browser." };
      } else if (type === "chatgptDeviceCode" && string(response.verificationUrl) && string(response.userCode)) {
        result = { state: "device", url: string(response.verificationUrl) as string, code: string(response.userCode) as string, message: "Enter the Codex device code in your browser." };
      } else {
        throw new Error(`Codex App Server returned an unsupported login response: ${type ?? "unknown"}`);
      }
      const timeout = setTimeout(() => { void this.#cancelPendingLogin(loginId); }, this.#loginTimeoutMs);
      timeout.unref();
      this.#pendingLogin = { loginId, peer, dispose, timeout };
      retained = true;
      return result;
    } finally {
      if (!retained) {
        dispose();
        peer.close();
      }
    }
  }

  async logout(): Promise<void> {
    await this.#cancelPendingLogin();
    await this.#withPeer(async (peer) => { await peer.request("account/logout"); });
  }

  async listModels(): Promise<RuntimeModel[]> {
    return this.#withPeer(async (peer) => {
      const models: RuntimeModel[] = [];
      let cursor: string | null = null;
      for (let page = 0; page < 10; page += 1) {
        const response = object(await peer.request("model/list", { cursor, limit: 100, includeHidden: false }));
        for (const raw of array(response.data)) {
          const model = object(raw);
          const id = string(model.id) ?? string(model.model);
          if (!id || model.hidden === true) continue;
          const efforts = array(model.supportedReasoningEfforts)
            .map((item) => string(object(item).reasoningEffort))
            .filter(isRuntimeEffort);
          const modalities = array(model.inputModalities).filter(isInputModality);
          models.push({
            id,
            displayName: string(model.displayName) ?? id,
            ...(string(model.description) ? { description: string(model.description) as string } : {}),
            ...(model.isDefault === true ? { isDefault: true } : {}),
            ...(efforts.length ? { efforts } : {}),
            ...(modalities.length ? { inputModalities: modalities } : {}),
          });
        }
        cursor = string(response.nextCursor);
        if (!cursor) break;
      }
      return models;
    });
  }

  async run(request: RunRequest, sink: RuntimeEventSink, signal: AbortSignal): Promise<RunResult> {
    const peer = this.#peerFactory();
    let threadId: string | null = null;
    let turnId: string | null = null;
    let finalText = "";
    let streamedText = "";
    let usage: RuntimeUsage | undefined;
    let resolveTurn!: () => void;
    let rejectTurn!: (error: Error) => void;
    const completed = new Promise<void>((resolve, reject) => { resolveTurn = resolve; rejectTurn = reject; });
    const timeout = setTimeout(() => rejectTurn(new Error("Codex turn timed out after 30 minutes")), 30 * 60_000);
    timeout.unref();

    const disposeNotification = peer.onNotification((method, rawParams, raw) => {
      const params = object(rawParams);
      const eventThread = string(params.threadId);
      if (threadId && eventThread && eventThread !== threadId) return;
      const eventTurn = string(params.turnId) ?? string(object(params.turn).id);
      if (turnId && eventTurn && eventTurn !== turnId) return;
      if (method === "item/agentMessage/delta") {
        const delta = string(params.delta) ?? "";
        streamedText += delta;
        sink({ kind: "message.delta", messageId: string(params.itemId) ?? "codex-message", role: role(request), text: delta }, raw);
      } else if (method === "item/reasoning/summaryTextDelta" || method === "item/reasoning/textDelta" || method === "item/plan/delta") {
        sink({ kind: "reasoning.delta", itemId: string(params.itemId) ?? "codex-reasoning", text: string(params.delta) ?? "" }, raw);
      } else if (method === "turn/diff/updated") {
        sink({ kind: "diff.updated", itemId: eventTurn ?? "codex-diff", diff: string(params.diff) ?? "" }, raw);
      } else if (method === "thread/tokenUsage/updated") {
        usage = codexUsage(object(object(params.tokenUsage).last));
        sink({ kind: "usage.updated", usage }, raw);
      } else if (method === "item/started") {
        emitItemStarted(object(params.item), sink, raw);
      } else if (method === "item/completed") {
        const item = object(params.item);
        if (item.type === "agentMessage") {
          finalText = string(item.text) ?? streamedText;
          sink({ kind: "message.completed", messageId: string(item.id) ?? "codex-message", role: role(request), text: finalText }, raw);
        } else emitItemCompleted(item, sink, raw);
      } else if (method === "error") {
        const message = string(object(params.error).message) ?? string(params.message) ?? "Codex App Server error";
        const detail = string(params.additionalDetails);
        sink({ kind: "notice", level: "error", message, ...(detail ? { detail } : {}) }, raw);
      } else if (method === "warning" || method === "deprecationNotice" || method === "configWarning") {
        sink({ kind: "notice", level: "warning", message: string(params.message) ?? method }, raw);
      } else if (method === "turn/completed") {
        const turn = object(params.turn);
        const status = string(turn.status);
        const error = object(turn.error);
        if (status === "failed") rejectTurn(new Error(string(error.message) ?? "Codex turn failed"));
        else resolveTurn();
      }
    });
    const disposals = [
      disposeNotification,
      registerApproval(peer, "item/commandExecution/requestApproval", "Run command", request, sink),
      registerApproval(peer, "item/fileChange/requestApproval", "Apply file changes", request, sink),
    ];
    const abort = (): void => {
      if (threadId && turnId) void peer.request("turn/interrupt", { threadId, turnId }, { timeoutMs: 5_000 }).catch(() => undefined);
      rejectTurn(new Error("Codex turn was aborted"));
    };
    signal.addEventListener("abort", abort, { once: true });

    try {
      await this.#initialize(peer);
      const common = {
        cwd: request.cwd ?? process.cwd(),
        ...(request.model ? { model: request.model } : {}),
        approvalPolicy: approvalPolicy(request.permissionMode),
        sandbox: sandboxMode(request.permissionMode),
      };
      const threadResponse = object(request.nativeSessionId && (request.mode === "primary" || request.mode === "handoff")
        ? await peer.request("thread/resume", { threadId: request.nativeSessionId, ...common }, { signal })
        : await peer.request("thread/start", { ...common, ephemeral: request.mode === "review" || request.mode === "compare", serviceName: "Firekeep Studio" }, { signal }));
      threadId = string(object(threadResponse.thread).id);
      if (!threadId) throw new Error("Codex App Server returned no thread id");
      const turnResponse = object(await peer.request("turn/start", {
        threadId,
        input: [{ type: "text", text: request.prompt, text_elements: [] }],
        ...(request.cwd ? { cwd: request.cwd } : {}),
        ...(request.model ? { model: request.model } : {}),
        ...(request.effort ? { effort: request.effort } : {}),
      }, { signal, timeoutMs: 60_000 }));
      turnId = string(object(turnResponse.turn).id);
      if (!turnId) throw new Error("Codex App Server returned no turn id");
      await completed;
      if (!finalText) {
        finalText = streamedText;
        sink({ kind: "message.completed", messageId: "codex-message", role: role(request), text: finalText });
      }
      return { nativeSessionId: threadId, finalText, ...(usage ? { usage } : {}) };
    } finally {
      clearTimeout(timeout);
      signal.removeEventListener("abort", abort);
      for (const dispose of disposals) dispose();
      peer.close();
    }
  }

  async #withPeer<T>(operation: (peer: RpcPeer) => Promise<T>): Promise<T> {
    const peer = this.#peerFactory();
    try {
      await this.#initialize(peer);
      return await operation(peer);
    } finally {
      peer.close();
    }
  }

  #finishPendingLogin(peer: RpcPeer): void {
    const pending = this.#pendingLogin;
    if (!pending || pending.peer !== peer) return;
    this.#pendingLogin = null;
    clearTimeout(pending.timeout);
    pending.dispose();
    pending.peer.close();
  }

  async #cancelPendingLogin(loginId?: string): Promise<void> {
    const pending = this.#pendingLogin;
    if (!pending || (loginId && pending.loginId !== loginId)) return;
    this.#pendingLogin = null;
    clearTimeout(pending.timeout);
    pending.dispose();
    try {
      await pending.peer.request("account/login/cancel", { loginId: pending.loginId }, { timeoutMs: 5_000 });
    } catch {
      // Closing the owning app-server process is the final cancellation fence.
    } finally {
      pending.peer.close();
    }
  }

  async #initialize(peer: RpcPeer): Promise<void> {
    await peer.request("initialize", {
      clientInfo: { name: "firekeep_studio", title: "Firekeep Studio", version: this.#appVersion },
      capabilities: { experimentalApi: false, requestAttestation: false },
    });
    peer.notify("initialized");
  }
}

function registerApproval(
  peer: RpcPeer,
  method: string,
  title: string,
  request: RunRequest,
  sink: RuntimeEventSink,
): () => void {
  return peer.onRequest(method, async (rawParams, raw) => {
    const params = object(rawParams);
    const itemId = string(params.approvalId) ?? string(params.itemId) ?? crypto.randomUUID();
    const options = ["accept", "acceptForSession", "decline", "cancel"] as const;
    const detail = [string(params.command), string(params.cwd), string(params.reason)].filter(Boolean).join("\n") || title;
    const decision = request.requestApproval
      ? await request.requestApproval({ id: `codex:${itemId}`, title, detail, options })
      : "decline";
    sink({ kind: "notice", level: "info", message: `${title}: ${decision}` }, raw);
    return { decision: options.includes(decision as typeof options[number]) ? decision : "decline" };
  });
}

function emitItemStarted(item: JsonObject, sink: RuntimeEventSink, raw: unknown): void {
  const type = string(item.type);
  const id = string(item.id) ?? crypto.randomUUID();
  if (type === "commandExecution") sink({ kind: "tool.started", toolCallId: id, name: "Shell", input: { command: item.command, cwd: item.cwd } }, raw);
  else if (type === "fileChange") sink({ kind: "tool.started", toolCallId: id, name: "File changes", input: item.changes }, raw);
  else if (type === "mcpToolCall") sink({ kind: "tool.started", toolCallId: id, name: `${string(item.server) ?? "MCP"}/${string(item.tool) ?? "tool"}`, input: item.arguments }, raw);
}

function emitItemCompleted(item: JsonObject, sink: RuntimeEventSink, raw: unknown): void {
  const type = string(item.type);
  const id = string(item.id) ?? crypto.randomUUID();
  if (type === "commandExecution") sink({ kind: "tool.completed", toolCallId: id, name: "Shell", output: item.aggregatedOutput, failed: typeof item.exitCode === "number" && item.exitCode !== 0 }, raw);
  else if (type === "fileChange") sink({ kind: "tool.completed", toolCallId: id, name: "File changes", output: item.changes, failed: item.status === "failed" }, raw);
  else if (type === "mcpToolCall") sink({ kind: "tool.completed", toolCallId: id, name: `${string(item.server) ?? "MCP"}/${string(item.tool) ?? "tool"}`, output: item.result ?? item.error, failed: item.error != null }, raw);
}

function codexUsage(value: JsonObject): RuntimeUsage {
  const inputTokens = number(value.inputTokens);
  const cachedInputTokens = number(value.cachedInputTokens);
  const outputTokens = number(value.outputTokens);
  const reasoningTokens = number(value.reasoningOutputTokens);
  const totalTokens = number(value.totalTokens);
  return {
    ...(inputTokens !== undefined ? { inputTokens } : {}),
    ...(cachedInputTokens !== undefined ? { cachedInputTokens } : {}),
    ...(outputTokens !== undefined ? { outputTokens } : {}),
    ...(reasoningTokens !== undefined ? { reasoningTokens } : {}),
    ...(totalTokens !== undefined ? { totalTokens } : {}),
  };
}

function approvalPolicy(mode: RunRequest["permissionMode"]): "never" | "on-request" { return mode === "unrestricted" || mode === "safe" ? "never" : "on-request"; }
function sandboxMode(mode: RunRequest["permissionMode"]): "read-only" | "workspace-write" | "danger-full-access" { return mode === "safe" ? "read-only" : mode === "unrestricted" ? "danger-full-access" : "workspace-write"; }
function role(request: RunRequest): "assistant" | "reviewer" { return request.mode === "review" ? "reviewer" : "assistant"; }
function object(value: unknown): JsonObject { return value && typeof value === "object" && !Array.isArray(value) ? value as JsonObject : {}; }
function array(value: unknown): unknown[] { return Array.isArray(value) ? value : []; }
function string(value: unknown): string | null { return typeof value === "string" ? value : null; }
function number(value: unknown): number | undefined { return typeof value === "number" && Number.isFinite(value) ? value : undefined; }
function isRuntimeEffort(value: string | null): value is RuntimeEffort { return value !== null && ["low", "medium", "high", "xhigh", "max"].includes(value); }
function isInputModality(value: unknown): value is "text" | "image" | "audio" { return value === "text" || value === "image" || value === "audio"; }
function errorMessage(error: unknown): string { return error instanceof Error ? error.message : String(error); }
function requireSecret(request: LoginRequest): string { if (!request.secret) throw new Error("Codex API-key login requires a key"); return request.secret; }

function installedFirekeepMemory(): boolean {
  try {
    const config = readFileSync(join(homedir(), ".codex", "config.toml"), "utf8");
    return config.includes("# >>> firekeep-client")
      && (config.includes("[mcp_servers.firekeep]") || config.includes("[mcp_servers.firekeep-"));
  } catch {
    return false;
  }
}

function defaultCodexInvocation(): { readonly command: string; readonly prefix: readonly string[] } {
  if (process.platform !== "win32") return { command: "codex", prefix: [] };
  const appData = process.env.APPDATA;
  if (appData) {
    const root = join(appData, "npm", "node_modules", "@openai", "codex");
    const architecture = process.arch === "arm64" ? "aarch64-pc-windows-msvc" : "x86_64-pc-windows-msvc";
    const packageName = process.arch === "arm64" ? "codex-win32-arm64" : "codex-win32-x64";
    const nativeCandidates = [
      join(root, "node_modules", "@openai", packageName, "vendor", architecture, "bin", "codex.exe"),
      join(appData, "npm", "node_modules", "@openai", packageName, "vendor", architecture, "bin", "codex.exe"),
    ];
    const native = nativeCandidates.find((candidate) => existsSync(candidate));
    if (native) return { command: native, prefix: [] };
    const script = join(root, "bin", "codex.js");
    if (existsSync(script)) return { command: "node.exe", prefix: [script] };
  }
  return { command: "codex.exe", prefix: [] };
}
