import { spawn, type ChildProcessWithoutNullStreams, type SpawnOptionsWithoutStdio } from "node:child_process";

export interface ProcessResult {
  readonly exitCode: number | null;
  readonly signal: NodeJS.Signals | null;
  readonly stdout: string;
  readonly stderr: string;
  readonly timedOut: boolean;
  readonly truncated: boolean;
  readonly durationMs: number;
}

export interface RunProcessOptions {
  readonly cwd?: string;
  readonly env?: NodeJS.ProcessEnv;
  readonly timeoutMs?: number;
  readonly outputLimit?: number;
  readonly input?: string;
  readonly signal?: AbortSignal;
  readonly killTree?: boolean;
  readonly windowsVerbatimArguments?: boolean;
}

export async function runProcess(command: string, args: readonly string[], options: RunProcessOptions = {}): Promise<ProcessResult> {
  const started = performance.now();
  const outputLimit = options.outputLimit ?? 64 * 1024;
  const child = spawnRuntime(command, args, {
    ...(options.cwd ? { cwd: options.cwd } : {}),
    env: options.env ?? process.env,
    ...(options.killTree && process.platform !== "win32" ? { detached: true } : {}),
    ...(options.windowsVerbatimArguments ? { windowsVerbatimArguments: true } : {}),
  });
  let stdout = "";
  let stderr = "";
  let truncated = false;
  let timedOut = false;
  const append = (current: string, chunk: Buffer): string => {
    const next = current + chunk.toString("utf8");
    if (Buffer.byteLength(next, "utf8") <= outputLimit) return next;
    truncated = true;
    return Buffer.from(next, "utf8").subarray(0, outputLimit).toString("utf8");
  };
  child.stdout.on("data", (chunk: Buffer) => { stdout = append(stdout, chunk); });
  child.stderr.on("data", (chunk: Buffer) => { stderr = append(stderr, chunk); });
  if (options.input !== undefined) child.stdin.end(options.input);
  else child.stdin.end();

  const timeout = options.timeoutMs === 0 ? undefined : setTimeout(() => {
    timedOut = true;
    terminateProcess(child, options.killTree === true);
  }, options.timeoutMs ?? 15_000);
  timeout?.unref();
  const abort = (): void => { terminateProcess(child, options.killTree === true); };
  options.signal?.addEventListener("abort", abort, { once: true });
  if (options.signal?.aborted) abort();

  try {
    const ended = await new Promise<{ exitCode: number | null; signal: NodeJS.Signals | null }>((resolve, reject) => {
      child.once("error", reject);
      child.once("exit", (exitCode, signal) => resolve({ exitCode, signal }));
    });
    return {
      ...ended,
      stdout,
      stderr,
      timedOut,
      truncated,
      durationMs: Math.round(performance.now() - started),
    };
  } finally {
    if (timeout) clearTimeout(timeout);
    options.signal?.removeEventListener("abort", abort);
  }
}

function terminateProcess(child: ChildProcessWithoutNullStreams, killTree: boolean): void {
  if (child.exitCode !== null || child.signalCode !== null) return;
  if (!killTree || !child.pid) {
    child.kill();
    return;
  }
  if (process.platform === "win32") {
    const killer = spawn("taskkill.exe", ["/pid", String(child.pid), "/t", "/f"], {
      stdio: "ignore",
      windowsHide: true,
    });
    killer.once("error", () => child.kill());
    killer.once("exit", (code) => { if (code !== 0) child.kill(); });
    return;
  }
  try { process.kill(-child.pid, "SIGTERM"); }
  catch { child.kill(); }
}

/** Terminate a Studio-owned runtime and every subprocess it launched. */
export async function terminateProcessTree(child: ChildProcessWithoutNullStreams): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null) return;
  const closed = waitForProcessClose(child, 7_500);
  if (process.platform === "win32" && child.pid) {
    await killWindowsProcessTree(child);
  } else {
    terminateProcess(child, true);
  }
  await closed;
}

async function killWindowsProcessTree(child: ChildProcessWithoutNullStreams): Promise<void> {
  const killer = spawn("taskkill.exe", ["/pid", String(child.pid), "/t", "/f"], {
    stdio: "ignore",
    windowsHide: true,
  });
  await new Promise<void>((resolve) => {
    let settled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const finish = (fallback: boolean): void => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      if (fallback && child.exitCode === null && child.signalCode === null) child.kill();
      resolve();
    };
    killer.once("error", () => finish(true));
    killer.once("exit", (code) => finish(code !== 0));
    timer = setTimeout(() => {
      killer.kill();
      finish(true);
    }, 5_000);
    timer.unref();
  });
}

async function waitForProcessClose(child: ChildProcessWithoutNullStreams, timeoutMs: number): Promise<void> {
  await new Promise<void>((resolve) => {
    let settled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const finish = (): void => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      child.off("close", finish);
      child.off("error", finish);
      resolve();
    };
    child.once("close", finish);
    child.once("error", finish);
    timer = setTimeout(finish, timeoutMs);
    timer.unref();
  });
}

export function spawnRuntime(
  command: string,
  args: readonly string[],
  options: SpawnOptionsWithoutStdio = {},
): ChildProcessWithoutNullStreams {
  const usesWindowsShim = process.platform === "win32" && /\.(cmd|bat)$/i.test(command);
  if (usesWindowsShim) throw new Error(`Refusing to launch shell shim directly: ${command}; resolve it to its executable first`);
  return spawn(command, [...args], {
    ...options,
    env: {
      ...process.env,
      ...options.env,
      FIREKEEP_DECISION_SURFACE: "studio",
    },
    stdio: ["pipe", "pipe", "pipe"],
    windowsHide: true,
  });
}

export async function probeVersion(command: string, args: readonly string[] = ["--version"]): Promise<{
  readonly found: boolean;
  readonly version?: string;
  readonly detail: string;
}> {
  try {
    const result = await runProcess(command, args, { timeoutMs: 8_000, outputLimit: 8_192 });
    const text = (result.stdout || result.stderr).trim();
    if (result.exitCode !== 0) return { found: true, detail: text || `Exited with code ${result.exitCode}` };
    return { found: true, ...(text ? { version: text.split(/\r?\n/, 1)[0] } : {}), detail: text || "Installed" };
  } catch (error) {
    const code = error && typeof error === "object" && "code" in error ? error.code : undefined;
    if (code === "ENOENT") return { found: false, detail: `${command} was not found on PATH` };
    return { found: false, detail: error instanceof Error ? error.message : String(error) };
  }
}
