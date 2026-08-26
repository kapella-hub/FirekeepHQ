import { describe, expect, it } from "vitest";
import { runProcess, spawnRuntime, terminateProcessTree } from "../src/main/runtime/process.js";

describe("runProcess", () => {
  it("captures bounded output and preserves the parent environment", async () => {
    const result = await runProcess(process.execPath, ["-e", "process.stdout.write('hello'); process.stderr.write('warn')"], {
      outputLimit: 32,
    });
    expect(result).toMatchObject({ exitCode: 0, stdout: "hello", stderr: "warn", timedOut: false });
  });

  it("times out and truncates oversized output", async () => {
    const output = await runProcess(process.execPath, ["-e", "process.stdout.write('x'.repeat(100))"], { outputLimit: 10 });
    expect(output.stdout).toBe("xxxxxxxxxx");
    expect(output.truncated).toBe(true);

    const timeout = await runProcess(process.execPath, ["-e", "setTimeout(() => {}, 5000)"], { timeoutMs: 10 });
    expect(timeout.timedOut).toBe(true);
  });

  it("marks runtime children for Studio-native Decision Boards", async () => {
    const child = spawnRuntime(process.execPath, ["-e", "process.stdout.write(process.env.FIREKEEP_DECISION_SURFACE || '')"]);
    let output = "";
    child.stdout.on("data", (chunk: Buffer) => { output += chunk.toString("utf8"); });
    await new Promise<void>((resolve, reject) => {
      child.once("error", reject);
      child.once("exit", (code) => code === 0 ? resolve() : reject(new Error(`child exited ${code}`)));
    });
    expect(output).toBe("studio");
  });

  it("terminates a runtime process together with its descendant", async () => {
    const script = "const {spawn}=require('node:child_process');const child=spawn(process.execPath,['-e','setInterval(()=>{},1000)'],{stdio:'ignore'});process.stdout.write(String(child.pid)+'\\n');setInterval(()=>{},1000)";
    const parent = spawnRuntime(process.execPath, ["-e", script], {
      ...(process.platform === "win32" ? {} : { detached: true }),
    });
    let stdout = "";
    parent.stdout.on("data", (chunk: Buffer) => { stdout += chunk.toString("utf8"); });
    await waitFor(() => /^\d+\s*$/.test(stdout));
    const descendantPid = Number(stdout.trim());

    try {
      await terminateProcessTree(parent);
      if (process.platform !== "win32") await waitFor(() => !isAlive(descendantPid), 5_000);
      expect(isAlive(descendantPid)).toBe(false);
    } finally {
      if (isAlive(descendantPid)) {
        try { process.kill(descendantPid, "SIGKILL"); } catch { /* already gone */ }
      }
      if (parent.pid && isAlive(parent.pid)) parent.kill("SIGKILL");
    }
  }, 10_000);

  it.runIf(process.platform === "win32")("refuses command shims instead of implicitly invoking a shell", () => {
    expect(() => spawnRuntime("provider.cmd", [])).toThrow(/shell shim/i);
    expect(() => spawnRuntime("provider.bat", [])).toThrow(/shell shim/i);
  });
});

async function waitFor(predicate: () => boolean, timeoutMs = 2_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!predicate() && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 20));
}

function isAlive(pid: number): boolean {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try { process.kill(pid, 0); return true; }
  catch { return false; }
}
