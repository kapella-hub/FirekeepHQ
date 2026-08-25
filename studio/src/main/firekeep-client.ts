import { existsSync } from "node:fs";
import { delimiter, join } from "node:path";
import { runProcess, type ProcessResult } from "./runtime/process.js";

export type FirekeepClientAction = "status" | "doctor" | "version" | "personal" | "connect" | "update" | "night-shift";

export interface FirekeepClientResult {
  readonly ok: boolean;
  readonly action: FirekeepClientAction;
  readonly output: string;
  readonly exitCode: number | null;
  readonly durationMs: number;
}

interface FirekeepClientOptions {
  readonly command?: string;
  readonly runner?: (args: readonly string[], options: { timeoutMs: number }) => Promise<ProcessResult>;
}

export class FirekeepClient {
  readonly #command: string;
  readonly #runner: (args: readonly string[], options: { timeoutMs: number }) => Promise<ProcessResult>;

  constructor(options: FirekeepClientOptions = {}) {
    this.#command = options.command ?? defaultFirekeepCommand();
    this.#runner = options.runner ?? ((args, settings) => runProcess(this.#command, args, { timeoutMs: settings.timeoutMs, outputLimit: 256 * 1024 }));
  }

  async execute(action: string, args: readonly string[]): Promise<FirekeepClientResult> {
    if (!isAction(action)) throw new Error(`Unsupported Firekeep Client Kit operation: ${action}`);
    const argv = validateArgs(action, args);
    const timeoutMs = action === "connect" || action === "update" || action === "night-shift" ? 10 * 60_000 : 60_000;
    const result = await this.#runner([action, ...argv], { timeoutMs });
    const output = stripAnsi([result.stdout.trim(), result.stderr.trim()].filter(Boolean).join("\n"));
    return { ok: result.exitCode === 0 && !result.timedOut, action, output, exitCode: result.exitCode, durationMs: result.durationMs };
  }
}

function defaultFirekeepCommand(): string {
  if (process.platform !== "win32") return "firekeep";
  const profile = process.env.USERPROFILE;
  const candidates = [
    ...(profile ? [join(profile, ".firekeep", "current", "Scripts", "firekeep.exe")] : []),
    ...(process.env.PATH ?? "").split(delimiter).filter(Boolean).map((directory) => join(directory, "firekeep.exe")),
  ];
  return candidates.find((candidate) => existsSync(candidate)) ?? "firekeep.exe";
}

function validateArgs(action: FirekeepClientAction, args: readonly string[]): string[] {
  if (["status", "version", "update", "night-shift"].includes(action)) {
    if (args.length) throw new Error(`/firekeep ${action} takes no arguments`);
    return [];
  }
  if (action === "doctor") {
    if (args.length === 0) return [];
    if (args.length === 1 && args[0] === "--report") return ["--report"];
    throw new Error("doctor accepts only --report");
  }
  if (action === "personal") {
    const value = args[0] ?? "status";
    if (args.length > 1 || !["on", "off", "status", "toggle"].includes(value)) throw new Error(`Invalid personal action: ${args.join(" ")}`);
    return [value];
  }
  const target = args[0];
  if (!target || !/^[a-z0-9._-]+@[a-z0-9.:[\]-]+$/i.test(target)) throw new Error("Invalid SSH target; expected user@host");
  const validated = [target];
  for (let index = 1; index < args.length; index += 2) {
    const flag = args[index];
    const value = args[index + 1];
    if ((flag !== "--agent-id" && flag !== "--remote-dir") || !value || !/^[a-z0-9_./:\\-]+$/i.test(value)) throw new Error(`Invalid connect option: ${flag ?? "missing"}`);
    validated.push(flag, value);
  }
  return validated;
}

function isAction(value: string): value is FirekeepClientAction {
  return ["status", "doctor", "version", "personal", "connect", "update", "night-shift"].includes(value);
}

function stripAnsi(value: string): string {
  return value.replace(/\x1B\[[0-?]*[ -/]*[@-~]/g, "");
}
