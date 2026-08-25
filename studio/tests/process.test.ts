import { describe, expect, it } from "vitest";
import { runProcess, spawnRuntime } from "../src/main/runtime/process.js";

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

  it.runIf(process.platform === "win32")("refuses command shims instead of implicitly invoking a shell", () => {
    expect(() => spawnRuntime("provider.cmd", [])).toThrow(/shell shim/i);
    expect(() => spawnRuntime("provider.bat", [])).toThrow(/shell shim/i);
  });
});
