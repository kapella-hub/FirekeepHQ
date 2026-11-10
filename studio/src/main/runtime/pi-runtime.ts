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
  RuntimeDescriptor,
  RuntimeEffort,
  RuntimeEventSink,
  RuntimeModel,
  RuntimeUsage,
} from "../../core/runtime.js";
import { ChildProcessJsonlTransport, JsonLineDecoder, type JsonlTransport } from "./jsonl-rpc.js";
import { probeVersion, runProcess, type ProcessResult } from "./process.js";

interface PiInvocation {
  readonly command: string;
  /** Arguments that come before Pi's own (the entry script, when Pi is launched through node). */
  readonly prefix: readonly string[];
}

interface PiRuntimeOptions {
  readonly invocation?: PiInvocation;
  readonly processFactory?: (args: readonly string[], cwd?: string, env?: Readonly<Record<string, string>>) => JsonlTransport;
  readonly runCommand?: (args: readonly string[]) => Promise<ProcessResult>;
  readonly versionProbe?: () => Promise<{ found: boolean; version?: string; detail: string }>;
  readonly firekeepHooks?: boolean;
}

interface JsonObject { readonly [key: string]: unknown }

const DEFAULT_MODEL = "default";
const READ_ONLY_TOOLS = "read,grep,find,ls";
const PI_EFFORTS: readonly RuntimeEffort[] = ["low", "medium", "high", "xhigh", "max"];
const SIGN_IN_HELP = "Run `pi` in a terminal and use /login, or set a provider API key in your environment.";

/**
 * Pi through its JSON event stream (`pi --mode json -p`), one process per run, prompt on stdin.
 *
 * Pi has no permission prompts by design, so Studio cannot ask before a tool runs. Safe mode
 * and reviews restrict Pi to its read-only tools (read, grep, find, ls). Standard and
 * Unrestricted are the same thing here: every tool, including bash and file edits, runs
 * without asking. A notice says so at the start of each such run.
 */
export class PiRuntime implements AgentRuntime {
  readonly descriptor: RuntimeDescriptor;
  readonly #invocation: PiInvocation;
  readonly #processFactory: (args: readonly string[], cwd?: string, env?: Readonly<Record<string, string>>) => JsonlTransport;
  readonly #runCommand: (args: readonly string[]) => Promise<ProcessResult>;
  readonly #versionProbe: () => Promise<{ found: boolean; version?: string; detail: string }>;

  constructor(options: PiRuntimeOptions = {}) {
    this.#invocation = options.invocation ?? defaultPiInvocation();
    const { command, prefix } = this.#invocation;
    this.#processFactory = options.processFactory ?? ((args, cwd, env) => new ChildProcessJsonlTransport(command, [...prefix, ...args], { ...(cwd ? { cwd } : {}), env: { ...process.env, ...env } }));
    this.#runCommand = options.runCommand ?? ((args) => runProcess(command, [...prefix, ...args], { timeoutMs: 20_000, outputLimit: 512 * 1024, env: { ...process.env, NO_COLOR: "1" } }));
    this.#versionProbe = options.versionProbe ?? (() => probeVersion(command, [...prefix, "--version"]));
    const firekeepHooks = options.firekeepHooks ?? installedFirekeepExtension();
    this.descriptor = {
      id: "pi",
      displayName: "Pi",
      description: "Pi coding agent through its JSON event stream. Pi has no permission prompts: outside Safe mode its tools run without asking.",
      transport: "pi-json",
      capabilities: ["chat", "review", "streaming", "tools", "resume", "models", "usage", "reasoning", ...(firekeepHooks ? ["firekeep-hooks" as const] : [])],
      loginMethods: ["console"],
      accent: "#5fb3a1",
    };
  }

  async probe(): Promise<RuntimeConnection> {
    const result = await this.#versionProbe();
    return result.found
      ? { state: "ready", ...(result.version ? { version: result.version } : {}), detail: result.detail, executable: this.#invocation.command }
      : { state: "missing", detail: `${result.detail}. Install it with: npm install -g @earendil-works/pi-coding-agent` };
  }

  async authStatus(): Promise<RuntimeAuthStatus> {
    try {
      const models = await this.#models();
      return models.length > 0
        ? { state: "connected", label: `${models.length} model${models.length === 1 ? "" : "s"} available`, methods: ["console"] }
        : { state: "disconnected", detail: SIGN_IN_HELP, methods: ["console"] };
    } catch (error) {
      return { state: "error", detail: errorMessage(error) };
    }
  }

  async login(_request: LoginRequest): Promise<LoginResult> {
    return { state: "external", message: `Pi signs in from its own terminal UI. ${SIGN_IN_HELP}` };
  }

  async logout(): Promise<void> {
    throw new Error("Pi manages its own sign-in: run `pi` in a terminal and use /logout");
  }

  async listModels(): Promise<RuntimeModel[]> {
    const models = await this.#models();
    return [
      { id: DEFAULT_MODEL, displayName: "Pi default", isDefault: true, efforts: [...PI_EFFORTS], inputModalities: ["text"] },
      ...models.map((id): RuntimeModel => ({ id, displayName: id, efforts: [...PI_EFFORTS], inputModalities: ["text"] })),
    ];
  }

  async #models(): Promise<string[]> {
    const result = await this.#runCommand(["--list-models"]);
    if (result.exitCode !== 0) throw new Error(result.stderr.trim() || "Pi model discovery failed");
    return parsePiModels(`${result.stdout}\n${result.stderr}`);
  }

  async run(request: RunRequest, sink: RuntimeEventSink, signal: AbortSignal): Promise<RunResult> {
    const readOnly = request.mode === "review" || request.permissionMode === "safe";
    if (!readOnly && request.permissionMode === "standard") {
      sink({ kind: "notice", level: "warning", message: "Pi does not ask before using tools", detail: "Pi has no permission prompts, so this run can edit files and run commands without approval. Choose Safe to keep it to read-only tools." });
    }
    const transport = this.#processFactory(piArgs(request, readOnly), request.cwd, request.env);
    const resumable = request.mode !== "review";
    let sessionId = resumable ? request.nativeSessionId : undefined;
    let streamedText = "";
    let lastText = "";
    let failure: string | null = null;
    let ended = false;
    let stderr = "";
    const perMessage: RuntimeUsage[] = [];
    let usage: RuntimeUsage | undefined;
    const decoder = new JsonLineDecoder({
      onMalformed: (line) => sink({ kind: "notice", level: "warning", message: "Pi emitted malformed JSON", detail: line.slice(0, 500) }),
    });
    let resolveRun!: (result: RunResult) => void;
    let rejectRun!: (error: Error) => void;
    const completed = new Promise<RunResult>((resolve, reject) => { resolveRun = resolve; rejectRun = reject; });

    const cleanups = [
      transport.onStdout((chunk) => {
        for (const raw of decoder.push(chunk)) {
          const value = object(raw);
          const type = string(value.type);
          if (type === "session") {
            if (resumable) sessionId = string(value.id) ?? sessionId;
          } else if (type === "message_update") {
            const event = object(value.assistantMessageEvent);
            if (event.type === "text_delta") {
              const text = string(event.delta) ?? "";
              streamedText += text;
              sink({ kind: "message.delta", messageId: "pi-message", role: role(request), text }, raw);
            } else if (event.type === "thinking_delta") {
              sink({ kind: "reasoning.delta", itemId: "pi-thinking", text: string(event.delta) ?? "" }, raw);
            }
          } else if (type === "tool_execution_start") {
            sink({ kind: "tool.started", toolCallId: string(value.toolCallId) ?? crypto.randomUUID(), name: string(value.toolName) ?? "Tool", input: value.args }, raw);
          } else if (type === "tool_execution_end") {
            sink({ kind: "tool.completed", toolCallId: string(value.toolCallId) ?? crypto.randomUUID(), name: string(value.toolName) ?? "Tool", output: value.result, failed: value.isError === true }, raw);
          } else if (type === "message_end") {
            const message = object(value.message);
            if (message.role !== "assistant") continue;
            const text = array(message.content).map(object).filter((block) => block.type === "text").map((block) => string(block.text) ?? "").join("");
            if (text) lastText = text;
            if (message.stopReason === "error") failure = string(message.errorMessage) ?? "Pi reported a provider error";
            else if (message.stopReason !== "aborted") failure = null;
            const sample = piUsage(message.usage);
            if (sample) {
              perMessage.push(sample);
              usage = sumUsage(perMessage);
              sink({ kind: "usage.updated", usage }, raw);
            }
          } else if (type === "agent_settled" || (type === "agent_end" && value.willRetry !== true)) {
            // `agent_end` can be followed by an automatic retry; `agent_settled` is Pi's own "nothing more will happen".
            ended = true;
          }
        }
      }),
      transport.onStderr((chunk) => { stderr = (stderr + decode(chunk)).slice(-64 * 1024); }),
      transport.onError((error) => rejectRun(error)),
      transport.onExit((code) => {
        if (code !== 0) { rejectRun(new Error(stderr.trim() || failure || `Pi exited with code ${code}`)); return; }
        if (failure) { rejectRun(new Error(failure)); return; }
        const finalText = lastText || streamedText;
        sink({ kind: "message.completed", messageId: "pi-message", role: role(request), text: finalText });
        resolveRun({ ...(sessionId ? { nativeSessionId: sessionId } : {}), finalText, ...(usage ? { usage } : {}) });
      }),
    ];
    const abort = (): void => { void transport.kill(); rejectRun(new Error("Pi run was aborted")); };
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) abort();
    else transport.end(request.prompt);

    try {
      return await completed;
    } finally {
      signal.removeEventListener("abort", abort);
      for (const cleanup of cleanups) cleanup();
      if (!ended) void transport.kill();
    }
  }
}

/** Pi reads the prompt from piped stdin in print mode, so it never has to survive argument quoting. */
export function piArgs(request: RunRequest, readOnly: boolean): string[] {
  const args = ["--mode", "json", "--print"];
  if (readOnly) args.push("--tools", READ_ONLY_TOOLS);
  if (request.mode === "review") args.push("--no-session");
  else if (request.nativeSessionId) args.push("--session", request.nativeSessionId);
  if (request.model && request.model !== DEFAULT_MODEL) args.push("--model", request.model);
  if (request.effort) args.push("--thinking", request.effort);
  return args;
}

/**
 * `pi --list-models` prints a six-column table: provider, model, context, max-out, thinking,
 * images, the last two "yes" or "no". Only lines of that shape are models; the header, hints
 * and the "No models available" message are not.
 */
export function parsePiModels(output: string): string[] {
  const models: string[] = [];
  for (const line of output.split(/\r?\n/)) {
    const columns = line.trim().split(/\s+/);
    const [provider, model, , , thinking, images] = columns;
    if (columns.length !== 6 || !provider || !model) continue;
    if (!/^(yes|no)$/.test(thinking ?? "") || !/^(yes|no)$/.test(images ?? "")) continue;
    models.push(`${provider}/${model}`);
  }
  return [...new Set(models)];
}

function piUsage(value: unknown): RuntimeUsage | null {
  const usage = object(value);
  const input = number(usage.input), output = number(usage.output), cacheRead = number(usage.cacheRead), cacheWrite = number(usage.cacheWrite);
  const total = number(usage.totalTokens) ?? (input ?? 0) + (output ?? 0) + (cacheRead ?? 0) + (cacheWrite ?? 0);
  if (!total) return null;
  const cost = number(object(usage.cost).total);
  return {
    totalTokens: total,
    ...(input !== undefined ? { inputTokens: input + (cacheRead ?? 0) + (cacheWrite ?? 0) } : {}),
    ...(output !== undefined ? { outputTokens: output } : {}),
    ...(cacheRead !== undefined ? { cachedInputTokens: cacheRead } : {}),
    ...(cost !== undefined ? { costUsd: cost } : {}),
  };
}

function sumUsage(samples: readonly RuntimeUsage[]): RuntimeUsage {
  const add = (pick: (usage: RuntimeUsage) => number | undefined): number | undefined => {
    const values = samples.map(pick).filter((value): value is number => value !== undefined);
    return values.length ? values.reduce((sum, value) => sum + value, 0) : undefined;
  };
  const inputTokens = add((usage) => usage.inputTokens), outputTokens = add((usage) => usage.outputTokens), cachedInputTokens = add((usage) => usage.cachedInputTokens), costUsd = add((usage) => usage.costUsd);
  return {
    totalTokens: add((usage) => usage.totalTokens) ?? 0,
    ...(inputTokens !== undefined ? { inputTokens } : {}),
    ...(outputTokens !== undefined ? { outputTokens } : {}),
    ...(cachedInputTokens !== undefined ? { cachedInputTokens } : {}),
    ...(costUsd !== undefined ? { costUsd } : {}),
  };
}

function installedFirekeepExtension(): boolean {
  try {
    return readFileSync(join(homedir(), ".pi", "agent", "settings.json"), "utf8").includes("firekeep-pi");
  } catch {
    return false;
  }
}

/** Pi is a Node script. On Windows npm puts a `.cmd` shim on PATH, which Studio never launches, so the script is run through node directly. */
function defaultPiInvocation(): PiInvocation {
  if (process.platform !== "win32") return { command: "pi", prefix: [] };
  const appData = process.env.APPDATA;
  if (appData) {
    const modules = join(appData, "npm", "node_modules");
    // The current package first, then the name it was published under before it moved.
    const script = [
      join(modules, "@earendil-works", "pi-coding-agent", "dist", "bundle", "cli.js"),
      join(modules, "@mariozechner", "pi-coding-agent", "dist", "cli.js"),
    ].find((candidate) => existsSync(candidate));
    if (script) return { command: "node.exe", prefix: [script] };
  }
  return { command: "pi.exe", prefix: [] };
}

function role(request: RunRequest): "assistant" | "reviewer" { return request.mode === "review" ? "reviewer" : "assistant"; }
function decode(chunk: Uint8Array | string): string { return typeof chunk === "string" ? chunk : Buffer.from(chunk).toString("utf8"); }
function object(value: unknown): JsonObject { return value && typeof value === "object" && !Array.isArray(value) ? value as JsonObject : {}; }
function array(value: unknown): unknown[] { return Array.isArray(value) ? value : []; }
function string(value: unknown): string | undefined { return typeof value === "string" ? value : undefined; }
function number(value: unknown): number | undefined { return typeof value === "number" && Number.isFinite(value) ? value : undefined; }
function errorMessage(error: unknown): string { return error instanceof Error ? error.message : String(error); }
