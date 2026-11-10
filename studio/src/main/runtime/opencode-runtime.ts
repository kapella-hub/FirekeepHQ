import { spawn } from "node:child_process";
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
  RuntimeEventSink,
  RuntimeModel,
} from "../../core/runtime.js";
import { createProcessTarget, runAcpConversation, type AcpTargetHandle } from "./kiro-runtime.js";
import { probeVersion, runProcess, type ProcessResult } from "./process.js";

interface OpenCodeRuntimeOptions {
  readonly command?: string;
  readonly targetFactory?: (args: readonly string[], cwd?: string, env?: Readonly<Record<string, string>>) => AcpTargetHandle;
  readonly runCommand?: (args: readonly string[]) => Promise<ProcessResult>;
  readonly launchLogin?: () => void;
  readonly versionProbe?: () => Promise<{ found: boolean; version?: string; detail: string }>;
  readonly firekeepMemory?: boolean;
  readonly firekeepHooks?: boolean;
  readonly appVersion?: string;
}

const DEFAULT_MODEL = "default";

/**
 * OpenCode through `opencode acp`, the same Agent Client Protocol Kiro speaks.
 *
 * Permission modes: Safe and reviews run in OpenCode's `plan` mode, which disallows edit
 * tools, and every permission request is refused. Standard runs `build` mode and forwards
 * each permission request OpenCode raises to the human; which tools raise one is decided by
 * OpenCode's own `permission` configuration, not by Studio. Unrestricted allows every request.
 */
export class OpenCodeRuntime implements AgentRuntime {
  readonly descriptor: RuntimeDescriptor;
  readonly #command: string;
  readonly #targetFactory: (args: readonly string[], cwd?: string, env?: Readonly<Record<string, string>>) => AcpTargetHandle;
  readonly #runCommand: (args: readonly string[]) => Promise<ProcessResult>;
  readonly #launchLogin: () => void;
  readonly #versionProbe: () => Promise<{ found: boolean; version?: string; detail: string }>;
  readonly #appVersion: string;

  constructor(options: OpenCodeRuntimeOptions = {}) {
    this.#command = options.command ?? defaultOpenCodeCommand();
    this.#targetFactory = options.targetFactory ?? ((args, cwd, env) => createProcessTarget(this.#command, args, cwd, env));
    this.#runCommand = options.runCommand ?? ((args) => runProcess(this.#command, args, { timeoutMs: 20_000, outputLimit: 256 * 1024, env: { ...process.env, NO_COLOR: "1" } }));
    this.#launchLogin = options.launchLogin ?? (() => {
      // `opencode auth login` is an interactive terminal prompt, so it gets a console of its own.
      const child = spawn(this.#command, ["auth", "login"], { detached: true, stdio: "ignore", windowsHide: false });
      child.unref();
    });
    this.#versionProbe = options.versionProbe ?? (() => probeVersion(this.#command));
    this.#appVersion = options.appVersion ?? process.env.npm_package_version ?? "development";
    const firekeepMemory = options.firekeepMemory ?? installedFirekeepMemory();
    const firekeepHooks = options.firekeepHooks ?? existsSync(join(openCodeConfigDir(), "plugins", "firekeep-hooks.js"));
    this.descriptor = {
      id: "opencode",
      displayName: "OpenCode",
      description: "OpenCode through the Agent Client Protocol",
      transport: "acp-v1",
      capabilities: ["chat", "review", "streaming", "tools", "approvals", "resume", "models", "usage", "reasoning", ...(firekeepMemory ? ["firekeep-memory" as const] : []), ...(firekeepHooks ? ["firekeep-hooks" as const] : [])],
      loginMethods: ["console"],
      accent: "#f2b84b",
    };
  }

  async probe(): Promise<RuntimeConnection> {
    const result = await this.#versionProbe();
    return result.found
      ? { state: "ready", ...(result.version ? { version: result.version } : {}), detail: result.detail, executable: this.#command }
      : { state: "missing", detail: `${result.detail}. Install it with: npm install -g opencode-ai` };
  }

  async authStatus(): Promise<RuntimeAuthStatus> {
    try {
      const result = await this.#runCommand(["auth", "list"]);
      if (result.exitCode !== 0) return { state: "error", detail: plain(result.stderr || result.stdout) || "OpenCode could not list its credentials" };
      const count = Number(/(\d+)\s+credentials?/i.exec(plain(result.stdout))?.[1] ?? "0");
      // OpenCode also serves its own free models with no credential at all, so zero is not "signed out".
      return { state: "connected", label: count > 0 ? `${count} provider credential${count === 1 ? "" : "s"}` : "OpenCode free models only", methods: ["console"] };
    } catch (error) {
      return { state: "error", detail: errorMessage(error) };
    }
  }

  async login(_request: LoginRequest): Promise<LoginResult> {
    this.#launchLogin();
    return { state: "external", message: "OpenCode's own provider sign-in was opened in a terminal. Studio never reads its credential store." };
  }

  async logout(): Promise<void> {
    throw new Error("OpenCode signs out one provider at a time: run `opencode auth logout` in a terminal");
  }

  async listModels(): Promise<RuntimeModel[]> {
    const result = await this.#runCommand(["models"]);
    if (result.exitCode !== 0) throw new Error(plain(result.stderr || result.stdout) || "OpenCode model discovery failed");
    const ids = plain(result.stdout).split(/\r?\n/).map((line) => line.trim()).filter((line) => /^[\w.@-]+\/\S+$/.test(line));
    return [
      { id: DEFAULT_MODEL, displayName: "OpenCode default", isDefault: true, inputModalities: ["text"] },
      ...[...new Set(ids)].map((id): RuntimeModel => ({ id, displayName: id, inputModalities: ["text"] })),
    ];
  }

  async run(request: RunRequest, sink: RuntimeEventSink, signal: AbortSignal): Promise<RunResult> {
    const readOnly = request.mode === "review" || request.permissionMode === "safe";
    const handle = this.#targetFactory(["acp"], request.cwd, request.env);
    try {
      return await runAcpConversation(handle.target, request, sink, signal, this.#appVersion, {
        name: "OpenCode",
        prefix: "opencode",
        configOptions: {
          mode: readOnly ? "plan" : "build",
          ...(request.model && request.model !== DEFAULT_MODEL ? { model: request.model } : {}),
        },
      });
    } catch (error) {
      const stderr = handle.stderr().trim();
      throw stderr ? new Error(`${errorMessage(error)}\n${stderr.slice(-2_000)}`) : error;
    } finally {
      await handle.close();
    }
  }
}

function openCodeConfigDir(): string {
  return join(process.env.XDG_CONFIG_HOME?.trim() || join(homedir(), ".config"), "opencode");
}

function installedFirekeepMemory(): boolean {
  try {
    const config = JSON.parse(readFileSync(join(openCodeConfigDir(), "opencode.json"), "utf8")) as { mcp?: Record<string, unknown> };
    return Object.keys(config.mcp ?? {}).some((name) => name === "firekeep" || name.startsWith("firekeep-"));
  } catch {
    return false;
  }
}

/** npm installs a `.cmd` shim on Windows, which Studio never launches; the package's own executable sits behind it. */
function defaultOpenCodeCommand(): string {
  if (process.platform !== "win32") return "opencode";
  const appData = process.env.APPDATA;
  if (appData) {
    const packaged = join(appData, "npm", "node_modules", "opencode-ai", "bin", "opencode.exe");
    if (existsSync(packaged)) return packaged;
  }
  return "opencode.exe";
}

/** Strips terminal colour and cursor sequences from CLI output. */
function plain(text: string): string {
  // eslint-disable-next-line no-control-regex
  return text.replace(/\u001b\[[0-9;?]*[A-Za-z]/g, "").trim();
}

function errorMessage(error: unknown): string { return error instanceof Error ? error.message : String(error); }
