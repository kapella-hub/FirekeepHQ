import * as acp from "@agentclientprotocol/sdk";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { Readable, Writable } from "node:stream";
import type {
  AgentRuntime,
  LoginRequest,
  LoginResult,
  RunRequest,
  RunResult,
  RuntimeAuthStatus,
  RuntimeDescriptor,
  RuntimeConnection,
  RuntimeEffort,
  RuntimeEventSink,
  RuntimeModel,
  RuntimeUsage,
} from "../../core/runtime.js";
import { RUNTIME_EFFORTS } from "../../core/runtime.js";
import { probeVersion, runProcess, spawnRuntime, terminateProcessTree, type ProcessResult } from "./process.js";

export interface AcpTargetHandle {
  readonly target: acp.Stream | acp.AgentApp;
  readonly close: () => void;
  readonly stderr: () => string;
}

interface KiroRuntimeOptions {
  readonly command?: string;
  readonly targetFactory?: (args: readonly string[], cwd?: string) => AcpTargetHandle;
  readonly runCommand?: (args: readonly string[]) => Promise<ProcessResult>;
  readonly launchLogin?: (device: boolean) => void;
  readonly versionProbe?: () => Promise<{ found: boolean; version?: string; detail: string }>;
  readonly agentName?: string | null;
}

interface JsonObject { readonly [key: string]: unknown }

export class KiroRuntime implements AgentRuntime {
  readonly descriptor: RuntimeDescriptor;
  readonly #command: string;
  readonly #targetFactory: (args: readonly string[], cwd?: string) => AcpTargetHandle;
  readonly #runCommand: (args: readonly string[]) => Promise<ProcessResult>;
  readonly #launchLogin: (device: boolean) => void;
  readonly #versionProbe: () => Promise<{ found: boolean; version?: string; detail: string }>;
  readonly #agentName: string | null;

  constructor(options: KiroRuntimeOptions = {}) {
    this.#command = options.command ?? (process.platform === "win32" ? "kiro-cli.exe" : "kiro-cli");
    this.#targetFactory = options.targetFactory ?? ((args, cwd) => createProcessTarget(this.#command, args, cwd));
    this.#runCommand = options.runCommand ?? ((args) => runProcess(this.#command, args, { timeoutMs: 20_000, outputLimit: 64 * 1024 }));
    this.#launchLogin = options.launchLogin ?? ((device) => {
      const child = spawn(this.#command, ["login", ...(device ? ["--use-device-flow"] : [])], { detached: true, stdio: "ignore", windowsHide: true });
      child.unref();
    });
    this.#versionProbe = options.versionProbe ?? (() => probeVersion(this.#command));
    this.#agentName = options.agentName === undefined ? installedFirekeepAgent() : options.agentName;
    this.descriptor = {
      id: "kiro",
      displayName: "Kiro",
      description: "Kiro CLI through the stable Agent Client Protocol",
      transport: "acp-v1",
      capabilities: ["chat", "review", "streaming", "tools", "approvals", "resume", "models", "usage", "reasoning", ...(this.#agentName ? ["firekeep-memory" as const, "firekeep-hooks" as const] : [])],
      loginMethods: ["browser", "device"],
      accent: "#8f83ff",
    };
  }

  async probe(): Promise<RuntimeConnection> {
    const result = await this.#versionProbe();
    return result.found
      ? {
          state: "ready",
          ...(result.version ? { version: result.version } : {}),
          detail: `${result.detail} · ${this.#agentName ? "Firekeep agent selected" : "provider default agent (Client Kit agent not found)"}`,
          executable: this.#command,
        }
      : { state: "missing", detail: result.detail };
  }

  async authStatus(): Promise<RuntimeAuthStatus> {
    try {
      const result = await this.#runCommand(["whoami", "--format", "json"]);
      if (result.exitCode !== 0) return { state: "disconnected", detail: result.stderr || "Sign in to Kiro", methods: ["browser", "device"] };
      const account = object(JSON.parse(result.stdout));
      const label = [string(account.email) ?? string(account.accountType) ?? "Kiro account", string(account.provider)].filter(Boolean).join(" · ");
      return { state: "connected", label, methods: ["browser", "device"] };
    } catch (error) {
      return { state: "error", detail: errorMessage(error) };
    }
  }

  async login(request: LoginRequest): Promise<LoginResult> {
    const device = request.method === "device";
    this.#launchLogin(device);
    return { state: "external", message: device ? "Kiro device sign-in was launched." : "Kiro browser sign-in was launched." };
  }

  async logout(): Promise<void> {
    const result = await this.#runCommand(["logout"]);
    if (result.exitCode !== 0) throw new Error(result.stderr || "Kiro logout failed");
  }

  async listModels(): Promise<RuntimeModel[]> {
    const [modelsResult, helpResult] = await Promise.all([
      this.#runCommand(["chat", "--list-models", "--format", "json"]),
      this.#runCommand(["acp", "--help"]),
    ]);
    if (modelsResult.exitCode !== 0) throw new Error(modelsResult.stderr || "Kiro model discovery failed");
    let payload: JsonObject;
    try { payload = object(JSON.parse(modelsResult.stdout)); }
    catch { throw new Error("Kiro model discovery returned invalid JSON"); }
    const defaultModel = string(payload.default_model);
    const efforts = helpResult.exitCode === 0 ? advertisedEfforts(`${helpResult.stdout}\n${helpResult.stderr}`) : [];
    return (Array.isArray(payload.models) ? payload.models : []).flatMap((raw): RuntimeModel[] => {
      const model = object(raw);
      const id = string(model.model_id) ?? string(model.model_name);
      if (!id) return [];
      return [{
        id,
        displayName: string(model.model_name) ?? id,
        ...(string(model.description) ? { description: string(model.description) as string } : {}),
        ...(id === defaultModel ? { isDefault: true } : {}),
        ...(efforts.length ? { efforts } : {}),
        inputModalities: ["text"],
      }];
    });
  }

  async run(request: RunRequest, sink: RuntimeEventSink, signal: AbortSignal): Promise<RunResult> {
    const args = ["acp", ...(this.#agentName ? ["--agent", this.#agentName] : []), ...(request.permissionMode === "unrestricted" ? ["--trust-all-tools"] : [])];
    if (request.model && request.model !== "auto") args.push("--model", request.model);
    if (request.effort) args.push("--effort", request.effort);
    const handle = this.#targetFactory(args, request.cwd);
    try {
      return await runAcpConversation(handle.target, request, sink, signal);
    } catch (error) {
      const stderr = handle.stderr().trim();
      throw stderr ? new Error(`${errorMessage(error)}\n${stderr}`) : error;
    } finally {
      handle.close();
    }
  }
}

function installedFirekeepAgent(): string | null {
  return existsSync(join(homedir(), ".kiro", "agents", "firekeep.json")) ? "firekeep" : null;
}

export async function runAcpConversation(
  target: acp.Stream | acp.AgentApp,
  request: RunRequest,
  sink: RuntimeEventSink,
  signal: AbortSignal,
): Promise<RunResult> {
  let finalText = "";
  let usage: RuntimeUsage | undefined;
  const resumableSessionId = request.mode === "primary" || request.mode === "handoff" ? request.nativeSessionId : undefined;
  let activeSessionId = resumableSessionId;
  const toolNames = new Map<string, string>();
  const client = acp.client({ name: "firekeep-studio" })
    .onRequest(acp.methods.client.session.requestPermission, async (ctx) => {
      const options = ctx.params.options;
      let optionId: string | undefined;
      if (request.permissionMode === "safe" || request.mode === "review" || request.mode === "compare") {
        optionId = options.find((option) => option.kind.startsWith("reject"))?.optionId;
      } else if (request.permissionMode === "unrestricted") {
        optionId = options.find((option) => option.kind === "allow_once")?.optionId;
      } else if (request.requestApproval) {
        optionId = await request.requestApproval({
          id: `kiro:${ctx.params.toolCall.toolCallId}`,
          title: ctx.params.toolCall.title ?? "Kiro tool permission",
          detail: options.map((option) => `${option.optionId}: ${option.name} (${option.kind})`).join("\n"),
          options: options.map((option) => option.optionId),
        });
      }
      if (!optionId || !options.some((option) => option.optionId === optionId)) return { outcome: { outcome: "cancelled" } };
      return { outcome: { outcome: "selected", optionId } };
    })
    .onNotification(acp.methods.client.session.update, (ctx) => {
      if (activeSessionId && ctx.params.sessionId !== activeSessionId) return;
      normalizeAcpUpdate(ctx.params.update, request, sink, toolNames, (text) => { finalText += text; }, (next) => { usage = next; });
    });

  const operation = async (ctx: acp.ClientContext): Promise<RunResult> => {
    const initialized = await ctx.request(acp.methods.agent.initialize, {
      protocolVersion: acp.PROTOCOL_VERSION,
      clientCapabilities: {},
      clientInfo: { name: "firekeep-studio", title: "Firekeep Studio", version: "0.3.5" },
    }, { cancellationSignal: signal });
    const cwd = request.cwd ?? process.cwd();
    if (resumableSessionId && initialized.agentCapabilities?.loadSession) {
      await ctx.request(acp.methods.agent.session.load, { sessionId: resumableSessionId, cwd, mcpServers: [] }, { cancellationSignal: signal });
      activeSessionId = resumableSessionId;
    } else {
      if (resumableSessionId) sink({ kind: "notice", level: "warning", message: "Kiro cannot load this saved session; starting a fresh provider session" });
      const created = await ctx.request(acp.methods.agent.session.new, { cwd, mcpServers: [] }, { cancellationSignal: signal });
      activeSessionId = created.sessionId;
    }
    const abort = (): void => { if (activeSessionId) void ctx.notify(acp.methods.agent.session.cancel, { sessionId: activeSessionId }); };
    signal.addEventListener("abort", abort, { once: true });
    try {
      const response = await ctx.request(acp.methods.agent.session.prompt, {
        sessionId: activeSessionId,
        prompt: [{ type: "text", text: request.prompt }],
      }, { cancellationSignal: signal });
      if (response.usage) usage = acpUsage(response.usage);
      if (response.stopReason === "cancelled") throw new Error("Kiro turn was cancelled");
      sink({ kind: "message.completed", messageId: "kiro-message", role: role(request), text: finalText });
      if (usage) sink({ kind: "usage.updated", usage });
      return { nativeSessionId: activeSessionId, finalText, ...(usage ? { usage } : {}) };
    } finally {
      signal.removeEventListener("abort", abort);
    }
  };

  return target instanceof acp.AgentApp
    ? client.connectWith(target, operation)
    : client.connectWith(target, operation);
}

function normalizeAcpUpdate(
  update: acp.SessionUpdate,
  request: RunRequest,
  sink: RuntimeEventSink,
  toolNames: Map<string, string>,
  appendText: (text: string) => void,
  setUsage: (usage: RuntimeUsage) => void,
): void {
  if (update.sessionUpdate === "agent_message_chunk" && update.content.type === "text") {
    appendText(update.content.text);
    sink({ kind: "message.delta", messageId: update.messageId ?? "kiro-message", role: role(request), text: update.content.text }, update);
  } else if (update.sessionUpdate === "agent_thought_chunk" && update.content.type === "text") {
    sink({ kind: "reasoning.delta", itemId: update.messageId ?? "kiro-thought", text: update.content.text }, update);
  } else if (update.sessionUpdate === "tool_call") {
    const name = update.name ?? update.title;
    toolNames.set(update.toolCallId, name);
    sink({ kind: "tool.started", toolCallId: update.toolCallId, name, input: update.rawInput }, update);
    emitAcpDiffs(update.toolCallId, update.content, sink);
  } else if (update.sessionUpdate === "tool_call_update") {
    const name = update.name ?? toolNames.get(update.toolCallId) ?? update.title ?? "Kiro tool";
    if (update.title || update.status === "pending" || update.status === "in_progress") {
      sink({ kind: "tool.updated", toolCallId: update.toolCallId, update: update.title ?? update.status ?? "updated" }, update);
    }
    emitAcpDiffs(update.toolCallId, update.content ?? undefined, sink);
    if (update.status === "completed" || update.status === "failed") {
      sink({ kind: "tool.completed", toolCallId: update.toolCallId, name, output: update.rawOutput ?? update.content, failed: update.status === "failed" }, update);
    }
  } else if (update.sessionUpdate === "plan") {
    sink({ kind: "reasoning.delta", itemId: "kiro-plan", text: JSON.stringify(update.entries) }, update);
  } else if (update.sessionUpdate === "usage_update") {
    const cost = update.cost?.currency === "USD" ? update.cost.amount : undefined;
    setUsage({ totalTokens: update.used, ...(cost !== undefined ? { costUsd: cost } : {}) });
  }
}

function emitAcpDiffs(toolCallId: string, contents: readonly acp.ToolCallContent[] | undefined, sink: RuntimeEventSink): void {
  for (const content of contents ?? []) {
    if (content.type === "diff") sink({ kind: "diff.updated", itemId: toolCallId, diff: `--- ${content.path}\n+++ ${content.path}\n-${content.oldText ?? ""}\n+${content.newText}` }, content);
  }
}

function acpUsage(value: acp.Usage): RuntimeUsage {
  return {
    totalTokens: value.totalTokens,
    inputTokens: value.inputTokens,
    outputTokens: value.outputTokens,
    ...(value.thoughtTokens != null ? { reasoningTokens: value.thoughtTokens } : {}),
    ...(value.cachedReadTokens != null ? { cachedInputTokens: value.cachedReadTokens } : {}),
  };
}

function createProcessTarget(command: string, args: readonly string[], cwd?: string): AcpTargetHandle {
  const child = spawnRuntime(command, args, {
    ...(cwd ? { cwd } : {}),
    env: process.env,
    ...(process.platform === "win32" ? {} : { detached: true }),
  });
  let stderr = "";
  child.stderr.on("data", (chunk: Buffer) => { stderr = (stderr + chunk.toString("utf8")).slice(-64 * 1024); });
  const output = Writable.toWeb(child.stdin) as WritableStream<Uint8Array>;
  const input = Readable.toWeb(child.stdout) as ReadableStream<Uint8Array>;
  return {
    target: acp.ndJsonStream(output, input),
    close: () => { terminateProcessTree(child); },
    stderr: () => stderr,
  };
}

function role(request: RunRequest): "assistant" | "reviewer" { return request.mode === "review" ? "reviewer" : "assistant"; }
function advertisedEfforts(help: string): RuntimeEffort[] {
  const start = help.indexOf("--effort");
  if (start < 0) return [];
  const remainder = help.slice(start);
  const next = remainder.slice("--effort".length).search(/\n\s{2,}--[a-z]/i);
  const block = next < 0 ? remainder : remainder.slice(0, "--effort".length + next);
  return RUNTIME_EFFORTS.filter((effort) => new RegExp(`\\b${effort}\\b`, "i").test(block));
}
function object(value: unknown): JsonObject { return value && typeof value === "object" && !Array.isArray(value) ? value as JsonObject : {}; }
function string(value: unknown): string | undefined { return typeof value === "string" ? value : undefined; }
function errorMessage(error: unknown): string { return error instanceof Error ? error.message : String(error); }
