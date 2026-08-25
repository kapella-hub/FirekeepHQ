import { spawn } from "node:child_process";
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
import { RUNTIME_EFFORTS } from "../../core/runtime.js";
import { claudeUsageSample, parseClaudeUsage, sumRuntimeUsage } from "../../core/usage.js";
import { ChildProcessJsonlTransport, JsonLineDecoder, type JsonlTransport } from "./jsonl-rpc.js";
import { probeVersion, runProcess, type ProcessResult } from "./process.js";

interface ClaudeRuntimeOptions {
  readonly command?: string;
  readonly processFactory?: (args: readonly string[], cwd?: string) => JsonlTransport;
  readonly runCommand?: (args: readonly string[]) => Promise<ProcessResult>;
  readonly launchLogin?: () => void;
  readonly versionProbe?: () => Promise<{ found: boolean; version?: string; detail: string }>;
}

interface JsonObject { readonly [key: string]: unknown }

export class ClaudeRuntime implements AgentRuntime {
  readonly descriptor = {
    id: "claude",
    displayName: "Claude",
    description: "Anthropic Claude Code through its native streaming CLI",
    transport: "stream-json",
    capabilities: ["chat", "review", "streaming", "tools", "resume", "models", "images", "usage", "reasoning", "firekeep-hooks"],
    loginMethods: ["browser", "console"],
    accent: "#d99b6c",
  } as const;
  readonly #command: string;
  readonly #processFactory: (args: readonly string[], cwd?: string) => JsonlTransport;
  readonly #runCommand: (args: readonly string[]) => Promise<ProcessResult>;
  readonly #launchLogin: () => void;
  readonly #versionProbe: () => Promise<{ found: boolean; version?: string; detail: string }>;

  constructor(options: ClaudeRuntimeOptions = {}) {
    this.#command = options.command ?? (process.platform === "win32" ? "claude.exe" : "claude");
    this.#processFactory = options.processFactory ?? ((args, cwd) => new ChildProcessJsonlTransport(this.#command, args, { ...(cwd ? { cwd } : {}), env: process.env }));
    this.#runCommand = options.runCommand ?? ((args) => runProcess(this.#command, args, { timeoutMs: 15_000, outputLimit: 64 * 1024 }));
    this.#launchLogin = options.launchLogin ?? (() => {
      const child = spawn(this.#command, ["auth", "login"], { detached: true, stdio: "ignore", windowsHide: true });
      child.unref();
    });
    this.#versionProbe = options.versionProbe ?? (() => probeVersion(this.#command));
  }

  async probe(): Promise<RuntimeConnection> {
    const result = await this.#versionProbe();
    return result.found
      ? { state: "ready", ...(result.version ? { version: result.version } : {}), detail: result.detail, executable: this.#command }
      : { state: "missing", detail: result.detail };
  }

  async authStatus(): Promise<RuntimeAuthStatus> {
    try {
      const result = await this.#runCommand(["auth", "status", "--json"]);
      if (result.exitCode !== 0) return { state: "disconnected", detail: result.stderr || "Claude is not signed in", methods: ["browser", "console"] };
      const value = object(JSON.parse(result.stdout));
      if (value.loggedIn !== true) return { state: "disconnected", detail: "Sign in to Claude Code", methods: ["browser", "console"] };
      const label = [string(value.email) ?? string(value.authMethod) ?? "Claude account", string(value.subscriptionType)].filter(Boolean).join(" · ");
      return { state: "connected", label, methods: ["browser", "console"] };
    } catch (error) {
      return { state: "error", detail: errorMessage(error) };
    }
  }

  async login(_request: LoginRequest): Promise<LoginResult> {
    this.#launchLogin();
    return { state: "external", message: "Claude's provider-owned browser sign-in was launched. Studio never reads its credential store." };
  }

  async logout(): Promise<void> {
    const result = await this.#runCommand(["auth", "logout"]);
    if (result.exitCode !== 0) throw new Error(result.stderr || "Claude logout failed");
  }

  async listModels(): Promise<RuntimeModel[]> {
    const result = await this.#runCommand(["--help"]);
    if (result.exitCode !== 0) throw new Error(result.stderr || "Claude model discovery failed");
    const help = `${result.stdout}\n${result.stderr}`;
    const efforts = advertisedEfforts(help);
    return [
      { id: "default", displayName: "Provider default", isDefault: true, ...(efforts.length ? { efforts } : {}), inputModalities: ["text", "image"] },
      ...claudeModelAliases(help).map((id) => ({
        id,
        displayName: `${id[0]?.toUpperCase() ?? ""}${id.slice(1)} alias`,
        ...(efforts.length ? { efforts } : {}),
        inputModalities: ["text", "image"] as const,
      })),
    ];
  }

  async run(request: RunRequest, sink: RuntimeEventSink, signal: AbortSignal): Promise<RunResult> {
    const args = claudeArgs(request);
    const transport = this.#processFactory(args, request.cwd);
    let sessionId = request.nativeSessionId;
    let finalText = "";
    let streamedText = "";
    let usage: RuntimeUsage | undefined;
    let stderr = "";
    let resultSeen = false;
    const toolNames = new Map<string, string>();
    const messageUsage = new Map<string, RuntimeUsage>();
    let lastUsageSignature = "";
    const decoder = new JsonLineDecoder({
      onMalformed: (line) => sink({ kind: "notice", level: "warning", message: "Claude emitted malformed stream JSON", detail: line.slice(0, 500) }),
    });
    let resolveRun!: (result: RunResult) => void;
    let rejectRun!: (error: Error) => void;
    const completed = new Promise<RunResult>((resolve, reject) => { resolveRun = resolve; rejectRun = reject; });

    const cleanups = [
      transport.onStdout((chunk) => {
        for (const raw of decoder.push(chunk)) {
          const value = object(raw);
          const type = string(value.type);
          if (type === "system" && value.subtype === "init") {
            sessionId = string(value.session_id) ?? sessionId;
          } else if (type === "stream_event") {
            const event = object(value.event);
            const delta = object(event.delta);
            if (event.type === "content_block_delta" && delta.type === "text_delta") {
              const text = string(delta.text) ?? "";
              streamedText += text;
              sink({ kind: "message.delta", messageId: "claude-message", role: role(request), text }, raw);
            } else if (event.type === "content_block_delta" && delta.type === "thinking_delta") {
              sink({ kind: "reasoning.delta", itemId: "claude-thinking", text: string(delta.thinking) ?? "" }, raw);
            }
          } else if (type === "assistant") {
            const sample = claudeUsageSample(raw);
            if (sample) {
              const prior = messageUsage.get(sample.messageId);
              const nextSignature = JSON.stringify(sample.usage);
              if (!prior || JSON.stringify(prior) !== nextSignature) {
                messageUsage.set(sample.messageId, sample.usage);
                const aggregate = sumRuntimeUsage(messageUsage.values());
                const aggregateSignature = JSON.stringify(aggregate);
                if (aggregateSignature !== lastUsageSignature) {
                  lastUsageSignature = aggregateSignature;
                  usage = aggregate;
                  sink({ kind: "usage.updated", usage }, raw);
                }
              }
            }
            for (const block of array(object(value.message).content).map(object)) {
              if (block.type === "tool_use") {
                const id = string(block.id) ?? crypto.randomUUID();
                const name = string(block.name) ?? "Tool";
                toolNames.set(id, name);
                sink({ kind: "tool.started", toolCallId: id, name, input: block.input }, raw);
              }
            }
          } else if (type === "user") {
            for (const block of array(object(value.message).content).map(object)) {
              if (block.type === "tool_result") {
                const id = string(block.tool_use_id) ?? crypto.randomUUID();
                sink({ kind: "tool.completed", toolCallId: id, name: toolNames.get(id) ?? "Tool", output: block.content, failed: block.is_error === true }, raw);
              }
            }
          } else if (type === "result") {
            resultSeen = true;
            if (value.is_error === true || value.subtype === "error") {
              rejectRun(new Error(string(value.result) ?? "Claude run failed"));
              continue;
            }
            sessionId = string(value.session_id) ?? sessionId;
            finalText = string(value.result) ?? streamedText;
            usage = parseClaudeUsage(value.usage, value);
            sink({ kind: "message.completed", messageId: "claude-message", role: role(request), text: finalText }, raw);
            sink({ kind: "usage.updated", usage }, raw);
          }
        }
      }),
      transport.onStderr((chunk) => { stderr = (stderr + decode(chunk)).slice(-64 * 1024); }),
      transport.onError((error) => rejectRun(error)),
      transport.onExit((code) => {
        if (code !== 0) rejectRun(new Error(stderr || `Claude exited with code ${code}`));
        else {
          if (!resultSeen) {
            finalText = streamedText;
            sink({ kind: "message.completed", messageId: "claude-message", role: role(request), text: finalText });
          }
          resolveRun({ ...(sessionId ? { nativeSessionId: sessionId } : {}), finalText, ...(usage ? { usage } : {}) });
        }
      }),
    ];
    const abort = (): void => { transport.kill(); rejectRun(new Error("Claude run was aborted")); };
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) abort();
    else transport.end(request.prompt);

    try {
      return await completed;
    } finally {
      signal.removeEventListener("abort", abort);
      for (const cleanup of cleanups) cleanup();
      if (!resultSeen) transport.kill();
    }
  }
}

function claudeArgs(request: RunRequest): string[] {
  const ephemeral = request.mode === "review" || request.mode === "compare";
  const safe = ephemeral || request.permissionMode === "safe";
  const permissionMode = safe ? "plan" : request.permissionMode === "unrestricted" ? "bypassPermissions" : "auto";
  const args = ["-p", "--input-format", "text", "--output-format", "stream-json", "--verbose", "--include-partial-messages", "--permission-mode", permissionMode];
  if (safe) args.push("--tools", "Read,Grep,Glob");
  if (!safe && request.permissionMode === "unrestricted") args.push("--dangerously-skip-permissions");
  if (ephemeral) args.push("--no-session-persistence");
  if (request.nativeSessionId && (request.mode === "primary" || request.mode === "handoff")) args.push("--resume", request.nativeSessionId);
  if (request.model && request.model !== "default") args.push("--model", request.model);
  if (request.effort) args.push("--effort", request.effort);
  return args;
}

function advertisedEfforts(help: string): RuntimeEffort[] {
  const block = optionBlock(help, "--effort");
  return RUNTIME_EFFORTS.filter((effort) => new RegExp(`\\b${effort}\\b`, "i").test(block));
}

function claudeModelAliases(help: string): string[] {
  const block = optionBlock(help, "--model");
  const examples = block.match(/alias[\s\S]*?\(e\.g\.\s*([\s\S]*?)\)\s*or/i)?.[1] ?? "";
  return [...examples.matchAll(/['"]([a-z0-9][a-z0-9._-]*)['"]/gi)].map((match) => match[1] as string);
}

function optionBlock(help: string, option: string): string {
  const start = help.indexOf(option);
  if (start < 0) return "";
  const remainder = help.slice(start);
  const next = remainder.slice(option.length).search(/\n\s{2,}--[a-z]/i);
  return next < 0 ? remainder : remainder.slice(0, option.length + next);
}

function role(request: RunRequest): "assistant" | "reviewer" { return request.mode === "review" ? "reviewer" : "assistant"; }
function object(value: unknown): JsonObject { return value && typeof value === "object" && !Array.isArray(value) ? value as JsonObject : {}; }
function array(value: unknown): unknown[] { return Array.isArray(value) ? value : []; }
function string(value: unknown): string | undefined { return typeof value === "string" ? value : undefined; }
function decode(value: Uint8Array | string): string { return typeof value === "string" ? value : Buffer.from(value).toString("utf8"); }
function errorMessage(error: unknown): string { return error instanceof Error ? error.message : String(error); }
