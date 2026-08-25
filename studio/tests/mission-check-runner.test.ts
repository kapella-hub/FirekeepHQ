import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import type { MissionCheck } from "../src/core/mission.js";
import { ProcessMissionCheckRunner } from "../src/main/mission-check-runner.js";

async function script(source: string): Promise<{ readonly cwd: string; readonly command: string }> {
  const cwd = await mkdtemp(join(tmpdir(), "firekeep-studio-check-"));
  const path = join(cwd, "check.cjs");
  await writeFile(path, source, "utf8");
  return { cwd, command: `${quote(process.execPath)} ${quote(path)}` };
}

function check(command: string, timeoutMs = 5_000): MissionCheck {
  return { id: "check-1", name: "fixture", command, timeoutMs };
}

describe("ProcessMissionCheckRunner", () => {
  it("executes an explicitly approved command in the bound workspace", async () => {
    const fixture = await script("process.stdout.write(process.cwd())");
    const result = await new ProcessMissionCheckRunner().run(check(fixture.command), fixture.cwd, new AbortController().signal);

    expect(result, result.stderr).toMatchObject({ exitCode: 0, timedOut: false, truncated: false });
    expect(result.stdout).toBe(fixture.cwd);
  });

  it("preserves failure evidence while bounding output", async () => {
    const fixture = await script("process.stdout.write('x'.repeat(10000)); process.stderr.write('broken'); process.exitCode=3");
    const result = await new ProcessMissionCheckRunner({ outputLimit: 1_024 }).run(check(fixture.command), fixture.cwd, new AbortController().signal);

    expect(result, result.stderr).toMatchObject({ exitCode: 3, timedOut: false, truncated: true });
    expect(Buffer.byteLength(result.stdout) + Buffer.byteLength(result.stderr)).toBeLessThanOrEqual(2_048);
    expect(result.stderr).toContain("broken");
  });

  it("times out and cancels a long-running check", async () => {
    const fixture = await script("setInterval(() => {}, 1000)");
    const timedOut = await new ProcessMissionCheckRunner().run(check(fixture.command, 25), fixture.cwd, new AbortController().signal);
    expect(timedOut.timedOut).toBe(true);

    const controller = new AbortController();
    const running = new ProcessMissionCheckRunner().run(check(fixture.command, 5_000), fixture.cwd, controller.signal);
    setTimeout(() => controller.abort(), 25);
    const cancelled = await running;
    expect(cancelled.signal ?? cancelled.exitCode).not.toBe(0);
  });
});

function quote(value: string): string {
  return `"${value.replace(/"/g, '\\"')}"`;
}
