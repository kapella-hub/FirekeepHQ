import type { MissionCheck, MissionCheckExecution, MissionCheckRunner } from "../core/mission.js";
import { runProcess } from "./runtime/process.js";

interface ProcessMissionCheckRunnerOptions {
  readonly outputLimit?: number;
}

export class ProcessMissionCheckRunner implements MissionCheckRunner {
  readonly #outputLimit: number;

  constructor(options: ProcessMissionCheckRunnerOptions = {}) {
    this.#outputLimit = options.outputLimit ?? 16 * 1024;
  }

  async run(check: MissionCheck, cwd: string, signal: AbortSignal): Promise<MissionCheckExecution> {
    const invocation = shellInvocation(check.command);
    const result = await runProcess(invocation.command, invocation.args, {
      cwd,
      signal,
      timeoutMs: check.timeoutMs,
      outputLimit: this.#outputLimit,
      killTree: true,
      windowsVerbatimArguments: process.platform === "win32",
    });
    return {
      exitCode: result.exitCode,
      signal: result.signal,
      stdout: result.stdout,
      stderr: result.stderr,
      timedOut: result.timedOut,
      truncated: result.truncated,
      durationMs: result.durationMs,
    };
  }
}

function shellInvocation(command: string): { readonly command: string; readonly args: readonly string[] } {
  if (process.platform === "win32") {
    return {
      command: process.env.ComSpec || "C:\\Windows\\System32\\cmd.exe",
      // windowsVerbatimArguments keeps this standard cmd.exe outer quote pair
      // intact: /S strips only that pair and preserves the quoted executable.
      args: ["/d", "/v:off", "/s", "/c", `"${command}"`],
    };
  }
  return { command: "/bin/sh", args: ["-lc", command] };
}
